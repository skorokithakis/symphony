"""Built-in, on-disk tracker backend for the web UI.

Tickets live as one JSON file per ticket under ``<workspace>/tickets/`` and
are loaded into memory at startup.  ``LocalTracker`` implements the generic
``Tracker`` protocol, so the orchestrator works against it unchanged; the
``LocalTicketStore`` it wraps is the API the web layer uses for drafts,
comments and workflow states.

The store directory deliberately lives in the workspace root: the
orchestrator masks every workspace-root entry except the current ticket's
directory inside the sandbox, so agents cannot read other tickets.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from symphony_linear.config import _NtfyConfig
from symphony_linear.linear import Comment, Issue
from symphony_linear.presence import TicketPresence
from symphony_linear.state import StateManager
from symphony_linear.tracker import (
    AttachmentDownloadError,
    TrackerError,
    TrackerNotFoundError,
    TransitionTarget,
    is_agent_kind,
    model_label_name,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixed workflow states
# ---------------------------------------------------------------------------

STATE_DRAFT = "Draft"
STATE_IN_PROGRESS = "In Progress"
STATE_NEEDS_INPUT = "Needs Input"
STATE_QA = "QA"
STATE_DONE = "Done"

#: Every state a ticket may be in, in workflow order.
TICKET_STATES: tuple[str, ...] = (
    STATE_DRAFT,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    STATE_DONE,
)

#: States the daemon polls for.  Draft and Done tickets are never triggered.
TRIGGERED_STATES: frozenset[str] = frozenset(
    {STATE_IN_PROGRESS, STATE_NEEDS_INPUT, STATE_QA}
)

#: States a human reply may be appended in.  Send is refused everywhere else,
#: including while a turn runs.  ``add_human_comment_once`` enforces this in
#: the same locked transaction as the append, so a racing state change cannot
#: leave a reply on a ticket that moved to In Progress or Done.
REPLY_STATES: frozenset[str] = frozenset({STATE_NEEDS_INPUT, STATE_QA})


#: Same footer the Linear and GitHub adapters append, so ``is_bot_comment``
#: keeps working.  The middle dot is U+00B7.
_BOT_FOOTER = "\n\n*Symphony · {kind}*"

#: ``user_id`` reported for human comments, so the resume prompt names an
#: author instead of "unknown".
_HUMAN_USER_ID = "user"

_TICKET_ID_RE = re.compile(r"^SYM-(\d+)$")
#: ``owner/name`` is accepted as GitHub-over-SSH shorthand.  Anything else
#: (https://, git@, ssh://, local paths) is passed through as a clone URL.
_REPO_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_COUNTER_FILE = "counter.json"

#: The managed repository list: ``{"repos": [{"url": ..., "name": ...}]}``.
#: It lives beside the ticket files so the sandbox's workspace-root masking
#: covers it, and it is seeded once from the tickets that exist at startup.
_REPOS_FILE = "repos.json"

#: How many accepted Send idempotency tokens a ticket keeps.  A token is only
#: useful while its request may still be retried, so an old one can go.
_MAX_SEND_TOKENS = 20

#: The stored file name is what the browser, the tracker URL and the Markdown
#: link regexes in ``attachments.py`` all see.  Restricting it to this
#: character set keeps it a single path component that cannot end the
#: ``[text](url)`` match early (no spaces, no ``)``, no separators).
_STORED_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")

#: Everything else in an original file name, collapsed to one underscore.
_UNSAFE_STORED_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Long enough to stay recognisable, short enough to keep the whole stored
#: name well under the usual 255-byte filename limit.
_MAX_STORED_STEM = 80
_MAX_EXTENSION = 16

#: The original name is display metadata and the link text only; cap it so a
#: hostile query string cannot bloat the ticket JSON.
_MAX_ORIGINAL_NAME = 255

#: Only these four types are ever served inline.  The decision is made from
#: the stored name's extension, never from the file's bytes.
_IMAGE_CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: The tracker's own upload URLs, as seen in a prompt/comment body.  Relative
#: only: an absolute URL is never one of ours, whatever its path.  The shape
#: is exactly what this tracker generates, so an unrelated ``/files/`` path is
#: not mistaken for one of ours.
_UPLOAD_PATH_RE = re.compile(
    r"^/files/(?P<ticket_id>SYM-[0-9]+)/(?P<name>[A-Za-z0-9._-]+)$"
)

#: Upload targets: the Draft's description, or the composer's pending reply.
TARGET_DESCRIPTION = "description"
TARGET_REPLY = "reply"
UPLOAD_TARGETS: frozenset[str] = frozenset({TARGET_DESCRIPTION, TARGET_REPLY})

#: States in which each target may be written.  A Draft's description is
#: fixed once it starts; a reply may be prepared in every composer state.
DESCRIPTION_FILE_STATES: frozenset[str] = frozenset({STATE_DRAFT})
REPLY_FILE_STATES: frozenset[str] = frozenset(
    {STATE_IN_PROGRESS, STATE_NEEDS_INPUT, STATE_QA}
)


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


def image_content_type(stored_name: str) -> str | None:
    """Return the inline image type for *stored_name*, or ``None``.

    Only the four allowlisted extensions are recognised, case-insensitively.
    Nothing about the file's contents is consulted.
    """
    return _IMAGE_CONTENT_TYPES.get(os.path.splitext(stored_name)[1].lower())


def clean_original_name(value: str) -> str:
    """Return a safe display name derived from an untrusted file name.

    Only the last path component is kept, whatever separators the client used,
    and NUL bytes are dropped.  The result is capped; control characters are
    left alone here because Jinja escapes them on output and the link text
    builder normalises them.
    """
    name = value.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "")
    name = name.strip()
    return name[:_MAX_ORIGINAL_NAME] or "file"


def stored_file_name(original_name: str) -> str:
    """Return a fresh, safe on-disk name for *original_name*.

    The name keeps the original extension when it has one, so the four inline
    image types remain recognisable, and prefixes it with random hex so two
    uploads of ``screenshot.png`` never collide.  A name that is only an
    extension (``.png``) is treated as a dotfile and does not become an image,
    since it has no extension as far as the browser is concerned.
    """
    base = clean_original_name(original_name)
    dot = base.rfind(".")
    if dot > 0:
        stem, extension = base[:dot], base[dot:]
    else:
        stem, extension = base, ""

    stem = _UNSAFE_STORED_CHARS_RE.sub("_", stem).strip("._") or "file"
    extension = _UNSAFE_STORED_CHARS_RE.sub("", extension)[: _MAX_EXTENSION + 1]
    if extension in ("", "."):
        extension = ""

    prefix = uuid.uuid4().hex[:16]
    return f"{prefix}-{stem[:_MAX_STORED_STEM]}{extension}"


def markdown_link_text(original_name: str) -> str:
    """Escape *original_name* so it cannot break a Markdown link.

    ``attachments.py`` matches ``[text](url)`` with a non-greedy group that
    stops at the first unescaped-looking ``]`` followed by ``(``.  Escaping
    those characters (and backslashes) keeps a name such as ``a](b`` inside
    the link text.  The asterisk is replaced with U+2217 too: ``*Symphony · ``
    is the bot footer marker ``is_bot_comment`` looks for, and a file name must
    not make a human reply look like a bot comment.  Newlines, the Unicode line
    separators and control characters become spaces because the regexes do
    not match them.
    """
    text = "".join(
        " "
        if ch in "\r\n\u2028\u2029\u0085" or ord(ch) < 0x20 or ord(ch) == 0x7F
        else ch
        for ch in original_name
    )
    for char in ("\\", "[", "]", "(", ")"):
        text = text.replace(char, "\\" + char)
    # A literal "*" here would reproduce the bot footer marker
    # "*Symphony · " and make the human reply look like a bot comment.
    # U+2217 ASTERISK OPERATOR reads the same but is not the marker.
    return text.replace("*", "\u2217")


def upload_path(url: str) -> tuple[str, str] | None:
    """Return ``(ticket_id, stored_name)`` for one of our canonical URLs.

    The match is against the raw string, never a parsed or decoded form: that
    makes malformed URLs (``urlsplit`` raises on some of them) simply "not
    ours", and it rejects query strings, fragments and percent-encoded aliases
    of the same path, which would otherwise bypass the de-duplication in
    ``attachments.py``.  This helper is the one definition used by
    ``is_upload_url``, ``download_attachment`` and the HTTP serving view.
    """
    match = _UPLOAD_PATH_RE.fullmatch(url)
    if match is None:
        return None
    return match.group("ticket_id"), match.group("name")


def body_with_file_refs(ticket_id: str, body: str, files: list["LocalFile"]) -> str:
    """Append one Markdown reference per stored file to *body*.

    This is the only place the references exist.  They are built on every
    read rather than stored, so the UI keeps showing text and files separately
    and calling this twice cannot duplicate them.
    """
    if not files:
        return body
    refs = "\n".join(
        f"{'!' if image_content_type(item.name) else ''}"
        f"[{markdown_link_text(item.original_name)}]"
        f"(/files/{ticket_id}/{item.name})"
        for item in files
    )
    if not body.strip():
        return refs
    return f"{body.rstrip()}\n\n{refs}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    """Return the current time as a timezone-aware ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _ticket_number(ticket_id: str) -> int:
    """Return the numeric part of ``SYM-<n>`` for ordering."""
    match = _TICKET_ID_RE.match(ticket_id)
    # LocalTicket validates this, so a mismatch means the file was hand-edited.
    return int(match.group(1)) if match else 0


def _parse_timestamp(value: str) -> datetime:
    """Parse a stored ISO timestamp, treating anything unparseable as oldest."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def repo_short_name(url: str) -> str:
    """Derive the ``owner/name`` display name from a repository value.

    Accepts the store's ``owner/name`` shorthand, HTTPS/SSH URLs and
    ``git@host:owner/name`` scp-style syntax.  Credentials embedded in a URL
    are never shown.  Returns an empty string for an empty value or one with
    no path part; the web layer turns that into its "No repository" label.
    """
    value = url.strip()
    if not value:
        return ""
    if value.endswith(".git"):
        value = value[: -len(".git")]

    if "://" in value:
        try:
            path = urlsplit(value).path
        except ValueError:
            # Malformed URL (for example an unclosed IPv6 bracket).  Render
            # the raw value: Jinja autoescapes it, and a page must never fail
            # because a ticket has a bad repository string.
            return value
    elif value.startswith("git@") and ":" in value:
        path = value.split(":", 1)[1]
    else:
        path = value

    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return ""
    if parts[-1].endswith(".git"):
        parts[-1] = parts[-1][: -len(".git")]
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-2]}/{parts[-1]}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* to *path* atomically, in the style of ``StateManager``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(payload, indent=2, ensure_ascii=False)

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        dir=str(path.parent),
        delete=False,
    )
    try:
        tmp.write(json_text)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()

    os.replace(tmp.name, str(path))


# ---------------------------------------------------------------------------
# Stored models
# ---------------------------------------------------------------------------


class LocalFile(BaseModel):
    """An uploaded file attached to a ticket or one of its comments.

    ``name`` is the server-generated on-disk name and the last segment of the
    ``/files/`` URL.  ``original_name`` is display metadata and the Markdown
    link text, and ``size`` is the number of bytes written.
    """

    name: str
    original_name: str
    size: int = 0

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if _STORED_NAME_RE.fullmatch(v) is None or v in (".", ".."):
            raise ValueError(f"Invalid stored file name {v!r}")
        return v


class RepoEntry(BaseModel):
    """One entry in the managed repository list.

    ``url`` is the clone URL stored verbatim on the ticket; ``name`` is the
    label the editor shows.  URLs are unique and compared after stripping
    whitespace.
    """

    url: str
    name: str = ""


class LocalComment(BaseModel):
    """A comment as stored on disk.

    ``kind`` is the footer label the bot supplied (e.g. ``"final"``), and is
    ``None`` for a human comment.  The web UI uses it to style bot comments;
    the orchestrator only looks at ``body``, which includes the appended file
    references when ``files`` is not empty.

    ``summary`` is the web UI's one-line account of an agent reply, shown on
    the home page's Needs Input rows.  It is ``None`` for human comments, for
    replies summarised without ``webui.anthropic_api_key``, and until the
    background request lands.
    """

    id: str
    body: str
    kind: str | None = None
    summary: str | None = None
    created_at: str = Field(default_factory=_iso_now)
    files: list[LocalFile] = Field(default_factory=list)


class LocalTicket(BaseModel):
    """A ticket stored on disk.

    ``extra="allow"`` keeps unknown fields written by a later version, so
    adding e.g. reply drafts, files or a seen marker does not need a
    migration.  Nothing but the fields listed here is read today.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    title: str = ""
    description: str = ""
    repo: str = ""
    model: str | None = None
    state: str = STATE_DRAFT
    comments: list[LocalComment] = Field(default_factory=list)
    #: Files attached to the ticket's own text (the Draft's description).
    #: They stay on the ticket after Start and move with it to Done.
    files: list[LocalFile] = Field(default_factory=list)
    #: Files attached to the reply composer but not sent yet.  On Send they
    #: move onto the new comment; Discarding the Draft deletes them with it.
    pending_files: list[LocalFile] = Field(default_factory=list)
    #: Client-generated tokens of replies the web UI has already accepted.
    #: A retry that reuses one of these must not post the same reply twice.
    #: Only the most recent ``_MAX_SEND_TOKENS`` are kept.
    accepted_send_tokens: list[str] = Field(default_factory=list)
    #: The reply composer's text, saved by the page while the user types.  It
    #: is cleared when the reply is sent and never touches ``updated_at``: a
    #: note being typed is not activity for the home list's ordering.
    reply_draft: str = ""
    #: When ``reply_draft`` was last written, for the "Saved 14:38" indicator.
    reply_draft_saved_at: str | None = None
    #: The newest comment the user has seen on the ticket page.  ``None`` means
    #: nothing has been seen yet.  Advanced by a POST from the page, never by a
    #: GET, and never moved backwards.
    seen_comment_id: str | None = None
    created_at: str = Field(default_factory=_iso_now)
    updated_at: str = Field(default_factory=_iso_now)
    state_changed_at: str = Field(default_factory=_iso_now)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _TICKET_ID_RE.match(v):
            raise ValueError(f"Ticket id must look like 'SYM-<n>', got {v!r}")
        return v

    @field_validator("state")
    @classmethod
    def _validate_state(cls, v: str) -> str:
        if v not in TICKET_STATES:
            raise ValueError(
                f"Unknown ticket state {v!r}; expected one of "
                f"{', '.join(TICKET_STATES)}"
            )
        return v

    @property
    def identifier(self) -> str:
        """Return the human-readable identifier, which equals the id."""
        return self.id


def display_title(ticket: LocalTicket) -> str:
    """Return the title to show, falling back to the first non-empty line.

    With ``webui.anthropic_api_key`` set, Start stores an empty title and the
    automatic title is written asynchronously; the empty title is the marker
    that nothing has been written yet.  Until it lands (and if it never does),
    this fallback is the ticket's name, so the web UI and the orchestrator's
    prompt show the same heading.  Both call this one helper for that reason.
    """
    title = ticket.title.strip()
    if title:
        return title
    first_line = next(
        (line.strip() for line in ticket.description.splitlines() if line.strip()),
        "",
    )
    return first_line or "Untitled draft"


# ---------------------------------------------------------------------------
# Ticket store
# ---------------------------------------------------------------------------


class FileStoreError(Exception):
    """The uploaded-file store could not perform an operation."""


class DraftValidationError(ValueError):
    """A Draft failed the caller's Start validation under the store lock.

    ``errors`` is the field-to-message mapping the validation callable
    returned.  It subclasses ``ValueError`` so existing callers that treat a
    refused start as a value error keep working; callers that need the
    messages catch this class first.
    """

    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__(" ".join(errors.values()))
        self.errors = dict(errors)


@dataclass(frozen=True)
class PendingUpload:
    """A reserved on-disk name for an upload whose body is still streaming.

    ``path`` is already containment-checked by the store; the caller writes a
    temporary file next to it and renames it into place, then calls
    :meth:`LocalTicketStore.register_file`.
    """

    ticket_id: str
    name: str
    original_name: str
    target: str
    path: Path


class LocalTicketStore:
    """Thread-safe in-memory ticket store backed by one JSON file per ticket.

    One lock guards both the in-memory dictionary and every file write;
    handlers in the web threads and pipelines in the orchestrator threads
    share a single instance.  Writes reuse a temporary file and ``os.replace``
    so a crash never leaves a half-written ticket.

    Public mutators raise ``TrackerNotFoundError`` for an unknown ticket and
    ``ValueError`` when an operation is invalid for the ticket's state.
    Public readers return deep copies, so callers cannot mutate stored state
    behind the lock.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._lock = threading.Lock()
        self._tickets: dict[str, LocalTicket] = {}
        self._repos: list[RepoEntry] = []
        self._next_id = 1
        self._load()

    # ------------------------------------------------------------------
    # Protocol-facing reads (used by LocalTracker)
    # ------------------------------------------------------------------

    def get(self, ticket_id: str) -> LocalTicket | None:
        """Return a copy of ticket *ticket_id*, or ``None``."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            return ticket.model_copy(deep=True) if ticket is not None else None

    def list_tickets(self) -> list[LocalTicket]:
        """Return copies of every ticket, lowest id first."""
        with self._lock:
            return [t.model_copy(deep=True) for t in self._sorted_tickets()]

    # ------------------------------------------------------------------
    # Managed repository list
    # ------------------------------------------------------------------

    def list_repos(self) -> list[RepoEntry]:
        """Return copies of the managed repositories, in list order."""
        with self._lock:
            return [entry.model_copy(deep=True) for entry in self._repos]

    def add_repo(self, url: str, name: str = "") -> RepoEntry:
        """Add *url* to the list unless it is already there.

        A duplicate URL is a no-op, keeping the name it was first added with.
        An empty *name* is derived from the URL as ``owner/name``.
        """
        with self._lock:
            return self._add_repo_locked(url, name)

    def _add_repo_locked(self, url: str, name: str = "") -> RepoEntry:
        """Add one repository; the caller holds the store lock."""
        url = url.strip()
        if not url:
            raise ValueError("A repository URL is required")
        for entry in self._repos:
            if entry.url == url:
                return entry.model_copy(deep=True)
        entry = RepoEntry(url=url, name=name.strip() or repo_short_name(url) or url)
        entries = [*self._repos, entry]
        self._persist_repos(entries)
        self._repos = entries
        return entry.model_copy(deep=True)

    def remove_repo(self, url: str) -> None:
        """Remove *url* from the list; an unknown URL is a no-op.

        Tickets are never touched: a ticket keeps the repository string it was
        created with even after the list entry is gone.
        """
        url = url.strip()
        with self._lock:
            remaining = [entry for entry in self._repos if entry.url != url]
            if len(remaining) == len(self._repos):
                return
            self._persist_repos(remaining)
            self._repos = remaining

    def rename_repo(self, url: str, name: str) -> RepoEntry:
        """Change one managed repository's display name, keeping its URL.

        The URL identifies the entry, so it is never changed and renaming
        cannot orphan a ticket or create a second entry for the same
        repository.  Tickets are untouched: each keeps the repository string
        it was created with.  Raises ``TrackerNotFoundError`` for a URL that
        is not in the list, so the web layer can answer 404 instead of
        silently creating an entry.
        """
        url = url.strip()
        with self._lock:
            for index, entry in enumerate(self._repos):
                if entry.url != url:
                    continue
                renamed = entry.model_copy(update={"name": name.strip()})
                entries = list(self._repos)
                entries[index] = renamed
                self._persist_repos(entries)
                self._repos = entries
                return renamed.model_copy(deep=True)
        raise TrackerNotFoundError(f"Repository not found: {url}")

    # ------------------------------------------------------------------
    # Uploaded files
    # ------------------------------------------------------------------

    @property
    def files_root(self) -> Path:
        """The directory holding one subdirectory per ticket's files."""
        return self._dir / "files"

    def file_path(self, ticket_id: str, stored_name: str) -> Path:
        """Return the contained path of one stored file.

        Both parameters are untrusted when they come from a URL.  The ticket
        id must name a live ticket and both it and the file name must resolve
        to a direct child of the right directory: a resolved path anywhere
        else, including through a symlink, is rejected with ``ValueError``.
        """
        with self._lock:
            self._require(ticket_id)
        return self._contained_file(ticket_id, stored_name)

    def read_file(self, ticket_id: str, stored_name: str) -> bytes:
        """Return the bytes of one registered stored file.

        Only a name that appears in the ticket's description files, its
        pending reply files, or a comment's files is readable.  That keeps
        ``.upload-*`` staging files and orphaned bytes (a failed registration,
        a hand-edited store) out of reach even though they sit in the same
        directory.  A directory or other non-regular file at a registered
        name is treated as missing.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            if stored_name not in self._registered_names(ticket):
                raise FileNotFoundError(f"No such file: {stored_name}")
        path = self._contained_file(ticket_id, stored_name)
        if not path.is_file():
            raise FileNotFoundError(f"No such file: {stored_name}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise FileNotFoundError(f"No such file: {stored_name}") from exc

    @staticmethod
    def _registered_names(ticket: LocalTicket) -> set[str]:
        """Return every stored name the ticket's metadata refers to."""
        names = {record.name for record in ticket.files}
        names.update(record.name for record in ticket.pending_files)
        for comment in ticket.comments:
            names.update(record.name for record in comment.files)
        return names

    def new_upload(
        self, ticket_id: str, original_name: str, *, target: str
    ) -> "PendingUpload":
        """Reserve a unique stored name for an upload that is about to start.

        Nothing is written to the ticket yet: the caller streams the body to
        the returned path and then calls :meth:`register_file`.  Reserving
        first means two concurrent uploads cannot pick the same name.
        """
        original = clean_original_name(original_name)
        with self._lock:
            ticket = self._require(ticket_id)
            self._check_target_state(ticket, target)
            directory = self._contained_ticket_dir(ticket_id, create=True)
            stored_name = self._unique_stored_name(directory, original)
        return PendingUpload(
            ticket_id=ticket_id,
            name=stored_name,
            original_name=original,
            target=target,
            path=directory / stored_name,
        )

    def register_file(self, upload: "PendingUpload", size: int) -> LocalFile:
        """Record a completed upload on the ticket.

        If the ticket disappeared or left the state that allows the target
        while the body was streaming, the bytes are deleted again and
        ``ValueError`` is raised.  The metadata write and the in-memory update
        happen under the same lock as every other ticket mutation.
        """
        with self._lock:
            ticket = self._require(upload.ticket_id)
            try:
                self._check_target_state(ticket, upload.target)
            except ValueError:
                self._unlink_quietly(upload.path)
                raise
            record = LocalFile(
                name=upload.name,
                original_name=upload.original_name,
                size=max(0, size),
            )
            update: dict[str, Any] = {}
            if upload.target == TARGET_DESCRIPTION:
                update["files"] = [*ticket.files, record]
                update["updated_at"] = _iso_now()
            else:
                # Attaching to a reply is typing, not activity: the Home list
                # must not reorder because someone picked a screenshot.
                update["pending_files"] = [*ticket.pending_files, record]
            updated = ticket.model_copy(update=update)
            try:
                self._persist(updated)
            except Exception:
                self._unlink_quietly(upload.path)
                raise
            self._tickets[ticket.id] = updated
            return record.model_copy(deep=True)

    def delete_file(self, ticket_id: str, stored_name: str, *, target: str) -> None:
        """Detach and delete one pending or description file."""
        with self._lock:
            ticket = self._require(ticket_id)
            self._check_target_state(ticket, target)
            records = (
                ticket.files if target == TARGET_DESCRIPTION else ticket.pending_files
            )
            if not any(record.name == stored_name for record in records):
                raise FileNotFoundError(f"No such file: {stored_name}")
            remaining = [record for record in records if record.name != stored_name]
            key = "files" if target == TARGET_DESCRIPTION else "pending_files"
            update: dict[str, Any] = {key: remaining}

            if target == TARGET_DESCRIPTION:
                update["updated_at"] = _iso_now()
            updated = ticket.model_copy(update=update)
            self._persist(updated)
            self._tickets[ticket.id] = updated
            path = self._contained_file(ticket_id, stored_name)
        self._unlink_quietly(path)

    def _check_target_state(self, ticket: LocalTicket, target: str) -> None:
        if target not in UPLOAD_TARGETS:
            raise ValueError(f"Unknown upload target {target!r}")
        allowed = (
            DESCRIPTION_FILE_STATES
            if target == TARGET_DESCRIPTION
            else REPLY_FILE_STATES
        )
        if ticket.state not in allowed:
            raise ValueError(
                f"Ticket {ticket.id} is in state {ticket.state!r} and "
                f"cannot take files for {target}"
            )

    def _contained_ticket_dir(self, ticket_id: str, *, create: bool) -> Path:
        """Resolve ``files/<ticket_id>`` and check it is a direct child.

        ``realpath`` on both sides is what makes symlinks and ``..`` harmless;
        the parent check additionally rejects a ticket id that resolves to the
        files root itself or to a nested directory.

        Trust assumption: a symlinked ``tickets/files`` root is not defended
        against.  Creating or swapping one needs write access to the workspace
        root, which already means access to ``config.yaml`` and the tokens in
        it, so that attacker does not need this path at all.
        """
        if _TICKET_ID_RE.fullmatch(ticket_id) is None:
            raise ValueError(f"Invalid ticket id {ticket_id!r}")
        root = Path(os.path.realpath(self.files_root))
        raw = os.path.join(root, ticket_id)
        if os.path.islink(raw):
            # A symlink could point at another ticket's directory; resolving
            # it would make delete_draft remove the wrong ticket's files.
            raise ValueError(f"Ticket path is a symlink: {ticket_id!r}")
        candidate = Path(os.path.realpath(raw))
        if os.path.dirname(candidate) != str(root):
            raise ValueError(f"Ticket path escapes the file store: {ticket_id!r}")
        if create:
            candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _contained_file(self, ticket_id: str, stored_name: str) -> Path:
        """Resolve one file under a ticket's directory, containment-checked."""
        if _STORED_NAME_RE.fullmatch(stored_name) is None or stored_name in (".", ".."):
            raise ValueError(f"Invalid stored file name {stored_name!r}")
        directory = self._contained_ticket_dir(ticket_id, create=False)
        raw = os.path.join(directory, stored_name)
        if os.path.islink(raw):
            raise ValueError(f"File path is a symlink: {stored_name!r}")
        candidate = Path(os.path.realpath(raw))
        if os.path.dirname(candidate) != str(directory):
            raise ValueError(f"File path escapes the ticket directory: {stored_name!r}")
        return candidate

    def _unique_stored_name(self, directory: Path, original_name: str) -> str:
        for _ in range(8):
            name = stored_file_name(original_name)
            if not (directory / name).exists():
                return name
        raise FileStoreError(f"Could not allocate a file name under {directory}")

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not delete uploaded file %s", path, exc_info=True)

    # ------------------------------------------------------------------
    # Web-facing writes
    # ------------------------------------------------------------------

    def create_draft(
        self,
        *,
        title: str = "",
        description: str = "",
        repo: str = "",
        model: str | None = None,
    ) -> LocalTicket:
        """Create and persist a new Draft ticket, returning a copy."""
        with self._lock:
            return self._create_ticket_locked(
                title=title,
                description=description,
                repo=repo,
                model=model,
                state=STATE_DRAFT,
            )

    def create_started_draft(
        self,
        *,
        title: str = "",
        description: str = "",
        repo: str = "",
        model: str | None = None,
        new_repo: tuple[str, str] | None = None,
    ) -> LocalTicket:
        """Create a ticket already in In Progress, in one locked write.

        Start must commit the validated fields and the Draft -> In Progress
        transition as one operation.  A new ticket has no earlier state to
        interleave with, but going through the same store path as
        :meth:`start_draft` keeps one definition of "started".

        *new_repo* is an optional ``(url, name)`` inline repository to add to
        the managed list under the same lock.  Only Start passes one, so a
        Save can never fill the list with URLs typed along the way.  The
        ticket is persisted first: a failed ticket write leaves the list
        untouched, while a failed list write after a started ticket is
        harmless.
        """
        with self._lock:
            ticket = self._create_ticket_locked(
                title=title,
                description=description,
                repo=repo,
                model=model,
                state=STATE_IN_PROGRESS,
            )
            if new_repo is not None:
                self._add_repo_locked(*new_repo)
            return ticket

    def _create_ticket_locked(
        self,
        *,
        title: str,
        description: str,
        repo: str,
        model: str | None,
        state: str,
    ) -> LocalTicket:
        """Create and persist one ticket (lock held)."""
        now = _iso_now()
        ticket = LocalTicket(
            id=self._allocate_id(),
            title=title,
            description=description,
            repo=repo,
            model=model,
            state=state,
            created_at=now,
            updated_at=now,
            state_changed_at=now,
        )
        self._persist(ticket)
        self._tickets[ticket.id] = ticket
        return ticket.model_copy(deep=True)

    def update_draft(
        self,
        ticket_id: str,
        *,
        title: str,
        description: str,
        repo: str,
        model: str | None,
    ) -> LocalTicket:
        """Replace the editable fields of draft *ticket_id*.

        A started ticket cannot be edited, so this rejects anything not in
        ``Draft`` with ``ValueError``.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            if ticket.state != STATE_DRAFT:
                raise ValueError(
                    f"Ticket {ticket_id} is in state {ticket.state!r} and "
                    f"cannot be edited"
                )
            updated = ticket.model_copy(
                update={
                    "title": title,
                    "description": description,
                    "repo": repo,
                    "model": model,
                    "updated_at": _iso_now(),
                }
            )
            self._persist(updated)
            self._tickets[ticket_id] = updated
            return updated.model_copy(deep=True)

    def update_draft_fields(
        self, ticket_id: str, fields: Mapping[str, str | None]
    ) -> LocalTicket:
        """Apply only the supplied editable fields to draft *ticket_id*.

        The browser editor posts every field at once, but an API caller may
        change one.  Reading the ticket, merging and writing under one lock is
        what makes two concurrent single-field edits keep each other's work;
        a merge against a copy read before the lock loses whichever write
        lands second.  ``fields`` may hold only ``title``, ``description``,
        ``repo`` and ``model``, already validated by the caller.
        """
        allowed = {"title", "description", "repo", "model"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown draft fields: {', '.join(sorted(unknown))}")
        with self._lock:
            ticket = self._require(ticket_id)
            if ticket.state != STATE_DRAFT:
                raise ValueError(
                    f"Ticket {ticket_id} is in state {ticket.state!r} and "
                    f"cannot be edited"
                )
            updated = ticket.model_copy(update={**fields, "updated_at": _iso_now()})
            self._persist(updated)
            self._tickets[ticket_id] = updated
            return updated.model_copy(deep=True)

    def start_draft_stored(
        self,
        ticket_id: str,
        *,
        add_repo: bool = False,
        validate: Callable[[LocalTicket], Mapping[str, str]] | None = None,
        model_aliases: frozenset[str] | None = None,
    ) -> LocalTicket:
        """Validate and move a Draft to In Progress without rewriting its fields.

        The agent API's Start has no fields of its own: it starts whatever the
        Draft holds when the lock is taken.  Passing a snapshot read earlier
        would overwrite an edit that landed in between, so the read,
        validation, transition and repository-list write all happen here,
        under the lock.

        *validate* runs on the stored ticket and returns a field-to-message
        mapping; a non-empty result raises :class:`DraftValidationError` and
        nothing is written.  It must not call back into this store, because
        the lock is held.

        *model_aliases*, when given, clears a stored alias that no longer
        appears in it, matching the editor's Start, which drops an alias the
        configuration no longer defines.

        *add_repo* adds the ticket's repository to the managed list, the same
        side effect Start has in the editor.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            if ticket.state != STATE_DRAFT:
                raise ValueError(
                    f"Ticket {ticket_id} is in state {ticket.state!r} and "
                    f"cannot be started"
                )
            if validate is not None:
                errors = validate(ticket)
                if errors:
                    raise DraftValidationError(errors)
            now = _iso_now()
            update: dict[str, Any] = {
                "state": STATE_IN_PROGRESS,
                "state_changed_at": now,
                "updated_at": now,
            }
            if (
                model_aliases is not None
                and ticket.model is not None
                and ticket.model not in model_aliases
            ):
                update["model"] = None
            started = ticket.model_copy(update=update)
            self._persist(started)
            self._tickets[ticket_id] = started
            if add_repo and ticket.repo.strip():
                self._add_repo_locked(ticket.repo, "")
            return started.model_copy(deep=True)

    def start_draft(
        self,
        ticket_id: str,
        *,
        title: str,
        description: str,
        repo: str,
        model: str | None,
        new_repo: tuple[str, str] | None = None,
    ) -> LocalTicket:
        """Commit a Draft's fields and move it to In Progress, atomically.

        The field write and the transition happen under one lock and one
        persist, so a concurrent autosave from another tab can either run
        before (and be overwritten by these validated fields) or after (and be
        refused, because the ticket is no longer a Draft).  It can never land
        between the two and start a ticket with an emptied repository or text.

        *new_repo* is an optional ``(url, name)`` inline repository, added to
        the managed list under the same lock and only after the Draft-state
        check, so a Start that loses its race must not change the list.  The
        ticket write happens first too: if it fails, the list is untouched;
        if the list write fails afterwards, the started ticket is harmless.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            if ticket.state != STATE_DRAFT:
                raise ValueError(
                    f"Ticket {ticket_id} is in state {ticket.state!r} and "
                    f"cannot be started"
                )
            now = _iso_now()
            started = ticket.model_copy(
                update={
                    "title": title,
                    "description": description,
                    "repo": repo,
                    "model": model,
                    "state": STATE_IN_PROGRESS,
                    "state_changed_at": now,
                    "updated_at": now,
                }
            )
            self._persist(started)
            self._tickets[ticket_id] = started
            if new_repo is not None:
                self._add_repo_locked(*new_repo)
            return started.model_copy(deep=True)

    def delete_draft(self, ticket_id: str) -> None:
        """Delete draft *ticket_id*, its file and every uploaded file.

        Its number is never reused: the id counter is persisted separately
        and only ever moves forward.  Started tickets are refused.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            if ticket.state != STATE_DRAFT:
                raise ValueError(
                    f"Ticket {ticket_id} is in state {ticket.state!r} and "
                    f"cannot be deleted"
                )
            directory = self._remove_ticket_locked(ticket_id)
        self._remove_directory(ticket_id, directory)

    def delete_draft_if_untouched(self, ticket_id: str) -> bool:
        """Delete a Draft only when it is still the empty placeholder.

        The first upload on ``/new`` has to create a Draft before it knows
        whether the upload will succeed.  If the upload fails, this removes
        that placeholder, but only while it is still untouched: a title,
        text, repository, model or file added by another request in the
        meantime means the Draft is real work and is kept.  The check and the
        removal happen under the store lock.
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket.state != STATE_DRAFT:
                return False
            if (
                ticket.title.strip()
                or ticket.description.strip()
                or ticket.repo.strip()
                or ticket.model
                or ticket.files
                or ticket.pending_files
            ):
                return False
            directory = self._remove_ticket_locked(ticket_id)
        self._remove_directory(ticket_id, directory)
        return True

    def _remove_ticket_locked(self, ticket_id: str) -> Path | None:
        """Delete the ticket JSON and drop the in-memory entry (lock held)."""
        try:
            self._path_for(ticket_id).unlink()
        except FileNotFoundError:
            pass
        self._tickets.pop(ticket_id, None)
        try:
            return self._contained_ticket_dir(ticket_id, create=False)
        except ValueError:
            # A hand-edited store; the ticket JSON is gone either way and
            # nothing outside the files root may be touched.
            logger.warning("Refusing to delete files of %s", ticket_id)
            return None

    @staticmethod
    def _remove_directory(ticket_id: str, directory: Path | None) -> None:
        if directory is None or not directory.exists():
            return
        try:
            shutil.rmtree(directory)
        except OSError:
            logger.warning("Could not delete the files of %s", ticket_id, exc_info=True)

    def add_human_comment(self, ticket_id: str, body: str) -> LocalComment:
        """Append a human comment (no bot footer) and return a copy."""
        return self.add_comment(ticket_id, body, kind=None)

    def add_human_comment_once(
        self,
        ticket_id: str,
        body: str,
        *,
        token: str,
        consume_composer: bool = True,
        model: str | None = None,
        set_model: bool = False,
        retry_only: bool = False,
    ) -> LocalComment | None:
        """Append a human comment unless *token* was already accepted.

        The check and the write happen under one lock, so two retries of the
        same form post cannot both append.  Returns ``None`` when the token was
        already used; the caller should treat that as success (the first
        attempt's comment is the one the user sees).

        ``consume_composer`` is true for the browser's Send, which sends what
        is in the composer: the pending files move onto the new comment and
        the saved draft text is cleared.  The agent API and the composer's
        Retry reply from outside the composer text, so they pass ``False`` and
        leave the browser's draft text and pending attachments exactly as they
        were.

        ``retry_only`` is true for Retry, which has a narrower window than a
        reply: it is only meaningful while the ticket waits for input with a
        failed turn.  The Needs Input half is checked here so it cannot race a
        state change between the caller's read and this append; the failed-turn
        half lives in the orchestrator's StateManager and is the caller's
        read-only check.  A duplicate token is still reported as success
        before this check, because the first attempt already did the work.

        ``set_model`` says the caller did supply the composer's Model choice:
        ``model`` is then stored on the ticket in this same locked write, with
        ``None`` meaning Default.  It is separate from ``model`` because an
        absent model field means the ticket has no model control at all (no
        aliases configured, or a caller that does not choose models), and that
        must leave a previously stored model alone rather than clearing it.
        The caller validates the alias, as with :meth:`update_draft`.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            if token in ticket.accepted_send_tokens:
                # A retry of an accepted Send reports success even if the
                # ticket has moved on since: the first attempt's comment is
                # the one the user sees.
                return None
            if retry_only:
                if ticket.state != STATE_NEEDS_INPUT:
                    raise ValueError(
                        f"Ticket {ticket_id} is in state {ticket.state!r} and "
                        f"cannot retry"
                    )
            elif ticket.state not in REPLY_STATES:
                raise ValueError(
                    f"Ticket {ticket_id} is in state {ticket.state!r} and "
                    f"cannot take a reply"
                )
            comment = LocalComment(
                id=f"c-{uuid.uuid4().hex}",
                body=body,
                kind=None,
                created_at=_iso_now(),
                files=(
                    [record.model_copy() for record in ticket.pending_files]
                    if consume_composer
                    else []
                ),
            )
            update: dict[str, Any] = {
                "comments": [*ticket.comments, comment],
                "accepted_send_tokens": [
                    *ticket.accepted_send_tokens,
                    token,
                ][-_MAX_SEND_TOKENS:],
                "updated_at": comment.created_at,
            }
            if consume_composer:
                update.update(
                    {
                        "pending_files": [],
                        # Sending consumes the composer text, which is then
                        # empty for the next visit from any device.
                        "reply_draft": "",
                        "reply_draft_saved_at": None,
                    }
                )
            if set_model:
                update["model"] = model
            updated = ticket.model_copy(update=update)
            self._persist(updated)
            self._tickets[ticket_id] = updated
            return comment.model_copy(deep=True)

    def token_accepted(self, ticket_id: str, token: str) -> bool:
        """Return whether *token* already committed a comment on the ticket.

        Retry's narrow eligibility window makes the duplicate check special:
        a retry whose first response was lost must still report success even
        if the ticket has moved on since.  The caller can therefore skip its
        failed-turn check for a token this reports as accepted; the append
        itself checks the token again under the same lock it writes with, so a
        token that is not accepted yet is still deduplicated there.
        """
        with self._lock:
            return token in self._require(ticket_id).accepted_send_tokens

    def add_comment(
        self, ticket_id: str, body: str, *, kind: str | None = None
    ) -> LocalComment:
        """Append a comment with the given pre-composed *body*.

        *kind* is stored alongside the body for the UI; the caller is
        responsible for appending the bot footer when *kind* is not ``None``.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            comment = LocalComment(
                id=f"c-{uuid.uuid4().hex}",
                body=body,
                kind=kind,
                created_at=_iso_now(),
            )
            updated = ticket.model_copy(
                update={
                    "comments": [*ticket.comments, comment],
                    "updated_at": comment.created_at,
                }
            )
            self._persist(updated)
            self._tickets[ticket_id] = updated
            return comment.model_copy(deep=True)

    def edit_comment(
        self, comment_id: str, body: str, *, kind: str | None
    ) -> LocalComment:
        """Replace the body and kind of comment *comment_id*.

        Comment ids are unique across the store, so the owning ticket is
        found by search.  Raises ``TrackerNotFoundError`` for an unknown id.
        """
        with self._lock:
            for ticket in self._tickets.values():
                for index, comment in enumerate(ticket.comments):
                    if comment.id != comment_id:
                        continue
                    edited = comment.model_copy(update={"body": body, "kind": kind})
                    comments = list(ticket.comments)
                    comments[index] = edited
                    updated = ticket.model_copy(
                        update={"comments": comments, "updated_at": _iso_now()}
                    )
                    self._persist(updated)
                    self._tickets[ticket.id] = updated
                    return edited.model_copy(deep=True)
        raise TrackerNotFoundError(f"Comment not found: {comment_id}")

    def set_reply_draft(self, ticket_id: str, text: str) -> LocalTicket:
        """Store the reply composer's text and return the updated copy.

        This deliberately does not touch ``updated_at``: the Home list orders
        by activity, and typing a note is not activity.  Callers must have
        refused a Done or Draft ticket before calling.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            updated = ticket.model_copy(
                update={
                    "reply_draft": text,
                    "reply_draft_saved_at": _iso_now(),
                }
            )
            self._persist(updated)
            self._tickets[ticket_id] = updated
            return updated.model_copy(deep=True)

    def set_title_if_empty(self, ticket_id: str, title: str) -> bool:
        """Store *title* only while the ticket still has no title of its own.

        Automatic titles are best-effort and arrive after Start, so a title the
        user wrote, or one an earlier call already stored, is never replaced.
        The check and the write happen under the store lock, which is what
        makes that guarantee hold against a concurrent writer.  Returns
        ``False`` when nothing was written.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            if ticket.title.strip():
                return False
            updated = ticket.model_copy(
                update={"title": title, "updated_at": _iso_now()}
            )
            self._persist(updated)
            self._tickets[ticket_id] = updated
            return True

    def set_comment_summary(
        self, ticket_id: str, comment_id: str, summary: str
    ) -> bool:
        """Store *summary* on one comment; ``False`` when either is gone.

        Summaries are generated in the background, so the ticket may have been
        deleted by the time the reply arrives.  That is a no-op rather than an
        error: nothing is retried and nothing else changes.  ``updated_at`` is
        deliberately untouched, exactly like a reply draft: the summary
        describes a reply that already counts as activity, so it must not
        reorder the home list when it lands.
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                return False
            for index, comment in enumerate(ticket.comments):
                if comment.id != comment_id:
                    continue
                comments = list(ticket.comments)
                comments[index] = comment.model_copy(update={"summary": summary})
                updated = ticket.model_copy(update={"comments": comments})
                self._persist(updated)
                self._tickets[ticket_id] = updated
                return True
            return False

    def set_seen_comment(self, ticket_id: str, comment_id: str) -> None:
        """Advance the ticket's seen marker to *comment_id*.

        The marker only moves forward: a stale tab that posts an older comment
        must not clear a newer unread reply.  ``ValueError`` is raised for a
        comment that does not belong to this ticket, which also catches a page
        whose comment was deleted.  Nothing else is written, in particular not
        ``updated_at``.
        """
        with self._lock:
            ticket = self._require(ticket_id)
            ids = [comment.id for comment in ticket.comments]
            try:
                new_index = ids.index(comment_id)
            except ValueError:
                raise ValueError(
                    f"Comment {comment_id} does not belong to ticket {ticket_id}"
                ) from None
            try:
                current_index = ids.index(ticket.seen_comment_id or "")
            except ValueError:
                current_index = -1
            if new_index <= current_index:
                return
            updated = ticket.model_copy(update={"seen_comment_id": comment_id})
            self._persist(updated)
            self._tickets[ticket_id] = updated

    def set_state(self, ticket_id: str, state: str) -> None:
        """Move *ticket_id* to *state*.

        ``state_changed_at`` advances only on an actual change; a no-op
        transition neither writes to disk nor bumps ``updated_at``, so a
        repeated transition does not reorder QA entrants.
        """
        if state not in TICKET_STATES:
            raise ValueError(
                f"Unknown ticket state {state!r}; expected one of "
                f"{', '.join(TICKET_STATES)}"
            )
        with self._lock:
            ticket = self._require(ticket_id)
            if ticket.state == state:
                return
            now = _iso_now()
            updated = ticket.model_copy(
                update={"state": state, "state_changed_at": now, "updated_at": now}
            )
            self._persist(updated)
            self._tickets[ticket_id] = updated

    def set_state_if(
        self, ticket_id: str, expected: frozenset[str], state: str
    ) -> bool:
        """Move *ticket_id* to *state* only if it is currently in *expected*.

        Returns ``False`` without writing when another request has already
        changed the state, so a web action cannot win a race against a
        concurrent one.  ``True`` also covers a ticket that is already in
        *state*: the question this answers is "did my action win?", not "did a
        change happen?".  A caller with a side effect on the transition wants
        :meth:`set_state_if_changed` instead.  The state name is validated even
        when no transition happens, since a typo in a route table is a
        programming error.
        """
        return self._move_state_if(ticket_id, expected, state) is not None

    def set_state_if_changed(
        self, ticket_id: str, expected: frozenset[str], state: str
    ) -> bool:
        """Like :meth:`set_state_if`, but report only a real state change.

        ``set_state_if`` is also true when the ticket is already in *state*.
        A notification must not fire for that no-op, so this variant returns
        ``True`` only when it wrote.  The check and the write share one lock
        acquisition, which is what makes the answer race-free: a separate read
        before the write could see a different state than the write acted on.
        """
        return self._move_state_if(ticket_id, expected, state) is True

    def _move_state_if(
        self, ticket_id: str, expected: frozenset[str], state: str
    ) -> bool | None:
        """Move *ticket_id*; ``None`` when refused, else whether it wrote.

        ``False`` means the ticket was already in *state* and nothing was
        written.  Both public variants share this one locked transaction so
        "refused" and "already there" cannot be confused by a second read.
        """
        if state not in TICKET_STATES:
            raise ValueError(
                f"Unknown ticket state {state!r}; expected one of "
                f"{', '.join(TICKET_STATES)}"
            )
        with self._lock:
            ticket = self._require(ticket_id)
            if ticket.state not in expected:
                return None
            if ticket.state == state:
                return False
            now = _iso_now()
            updated = ticket.model_copy(
                update={"state": state, "state_changed_at": now, "updated_at": now}
            )
            self._persist(updated)
            self._tickets[ticket_id] = updated
            return True

    # ------------------------------------------------------------------
    # Persistence helpers (callers hold the lock)
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Create the store directory and load every ticket file into memory."""
        self._dir.mkdir(parents=True, exist_ok=True)

        counter_next = 1
        counter_path = self._dir / _COUNTER_FILE
        if counter_path.exists():
            raw = json.loads(counter_path.read_text())
            counter_next = int(raw.get("next_id", 1))
            if counter_next < 1:
                raise ValueError(f"Invalid ticket counter in {counter_path}")

        tickets: dict[str, LocalTicket] = {}
        for path in self._dir.glob("SYM-*.json"):
            ticket = LocalTicket.model_validate_json(path.read_text())
            if ticket.id != path.stem:
                raise ValueError(
                    f"Ticket file {path} contains id {ticket.id!r}; "
                    f"delete or fix the file"
                )
            tickets[ticket.id] = ticket

        # Keep ids monotonic even if counter.json was lost: starting below an
        # existing ticket's number would overwrite it.
        highest_existing = max((_ticket_number(tid) for tid in tickets), default=0)
        self._tickets = tickets
        self._next_id = max(counter_next, highest_existing + 1)
        self._load_repos()

    def _load_repos(self) -> None:
        """Load the managed repository list, seeding it on first startup.

        Seeding is a write, so it happens here (store construction) and never
        in a GET handler: once ``repos.json`` exists it is never reseeded, and
        an empty list stays empty.
        """
        path = self._dir / _REPOS_FILE
        if path.exists():
            raw = json.loads(path.read_text())
            entries = raw.get("repos", []) if isinstance(raw, dict) else []
            self._repos = [RepoEntry.model_validate(entry) for entry in entries]
            return

        self._repos = self._seed_repos()
        self._persist_repos(self._repos)

    def _seed_repos(self) -> list[RepoEntry]:
        """Build the initial list from the tickets' repositories, newest first."""
        ordered = sorted(
            self._tickets.values(),
            key=lambda ticket: _parse_timestamp(ticket.updated_at),
            reverse=True,
        )
        seen: set[str] = set()
        entries: list[RepoEntry] = []
        for ticket in ordered:
            value = ticket.repo.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            entries.append(RepoEntry(url=value, name=repo_short_name(value) or value))
        return entries

    def _persist_repos(self, entries: list[RepoEntry]) -> None:
        _write_json(
            self._dir / _REPOS_FILE,
            {"repos": [entry.model_dump(mode="json") for entry in entries]},
        )

    def _require(self, ticket_id: str) -> LocalTicket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise TrackerNotFoundError(f"Ticket not found: {ticket_id}")
        return ticket

    def _sorted_tickets(self) -> list[LocalTicket]:
        return sorted(self._tickets.values(), key=lambda t: _ticket_number(t.id))

    def _allocate_id(self) -> str:
        """Reserve the next id, persisting the counter before the ticket."""
        ticket_id = f"SYM-{self._next_id}"
        self._next_id += 1
        _write_json(self._dir / _COUNTER_FILE, {"next_id": self._next_id})
        return ticket_id

    def _path_for(self, ticket_id: str) -> Path:
        # Ids are generated here and validated on load, so they cannot escape
        # the store directory.
        return self._dir / f"{ticket_id}.json"

    def _persist(self, ticket: LocalTicket) -> None:
        _write_json(self._path_for(ticket.id), ticket.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# LocalTracker
# ---------------------------------------------------------------------------

#: A notification must not hold a turn for long: the human is waiting for the
#: turn's reply, and the message itself is already on the ticket.
_NTFY_TIMEOUT_SECONDS = 10.0

#: The icon's two spellings, both naming ``webui/static/icon.png`` in the
#: package.  The web UI serves it at ICON_PATH to the favicon and the title-bar
#: logo, which are fetched by an authenticated browser.  ntfy cannot send the
#: UI's Basic credentials, so its payload points at the same file in the public
#: GitHub repository instead.  Moving or renaming the file changes both.  The
#: PNG is rendered from the editable source ``misc/icon.svg`` with:
#:
#:     uvx --from cairosvg cairosvg misc/icon.svg \
#:         --output-width 256 --output-height 256 \
#:         -o symphony_linear/webui/static/icon.png
ICON_PATH = "/static/icon.png"
NTFY_ICON_URL = (
    "https://raw.githubusercontent.com/skorokithakis/symphony/master/"
    "symphony_linear/webui/static/icon.png"
)


def publish_needs_input(
    config: _NtfyConfig,
    ticket: LocalTicket,
    *,
    public_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Publish one ntfy notification that *ticket* reached Needs Input.

    The message is the identifier and display title only: no comment or reply
    text exists at transition time, and none may be sent to a third-party
    server anyway.  The JSON publish to the server root is chosen over ntfy's
    ``Title``/``Message`` headers because HTTP headers are latin-1, and a
    ticket title is arbitrary UTF-8 text.

    Losing a notification must never fail the turn that just ended, so the
    whole notification — building the request and delivering it — is one
    best-effort boundary: every failure is a single WARNING and a return, and
    nothing is retried.  *transport* exists for tests, which pass
    ``httpx.MockTransport`` so no request leaves the process.

    *public_url* is the configured ``webui.public_url``.  It lives in
    ``_WebUIConfig`` rather than the ntfy block because it describes the web
    UI, not the topic.  It only drives the tap target; the icon is always sent
    from :data:`NTFY_ICON_URL`, because ntfy fetches it without credentials and
    the web UI requires them.
    """
    try:
        root, topic = config.endpoint()
        payload: dict[str, Any] = {
            "topic": topic,
            "title": "Needs Input",
            "message": f"{ticket.identifier}: {display_title(ticket)}",
            # ntfy renders this on Android only; iOS ignores the field and the
            # notification still works without one.
            "icon": NTFY_ICON_URL,
        }
        if public_url:
            payload["click"] = f"{public_url}/tickets/{ticket.id}"
        headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
        with httpx.Client(timeout=_NTFY_TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(root, json=payload, headers=headers)
        if not response.is_success:
            logger.warning(
                "ntfy notification for %s returned HTTP %s",
                ticket.id,
                response.status_code,
            )
    except Exception as exc:
        # Deliberately broad.  This runs after the state change was already
        # persisted and a turn has just ended, so a lost notification must not
        # fail that turn.  httpx reports transport and TLS problems as several
        # unrelated types (HTTPError, InvalidURL, and plain OSError for an
        # unusable SSL certificate file), so any narrower list could still leak
        # a raise.  The type is all that is logged: an exception message can
        # echo the URL, and the token lives in the headers.
        logger.warning(
            "ntfy notification for %s failed: %s", ticket.id, type(exc).__name__
        )


_STATE_FOR_TARGET: dict[TransitionTarget, str] = {
    TransitionTarget.in_progress: STATE_IN_PROGRESS,
    TransitionTarget.needs_input: STATE_NEEDS_INPUT,
    TransitionTarget.qa: STATE_QA,
}

#: States each orchestrator transition may start from.
#:
#: Done and Draft are only ever left by a user action (Start, Reopen), which
#: uses the store directly, so a worker that already read its input must not
#: pull a Done ticket back to In Progress or QA.  ``needs_input`` is the
#: exception and must accept Done: when the user moves a ticket with a dirty
#: workspace to Done, the cleanup refusal calls ``transition_to(needs_input)``
#: to move it back and records that state in ``cleanup_refused_state``.  If
#: that transition is a no-op the recorded state differs from the real one and
#: the next tick deletes uncommitted work.  Do not widen this back to all
#: targets.
_TRANSITION_EXPECTED: dict[TransitionTarget, frozenset[str]] = {
    TransitionTarget.in_progress: TRIGGERED_STATES,
    TransitionTarget.qa: TRIGGERED_STATES,
    TransitionTarget.needs_input: TRIGGERED_STATES | {STATE_DONE},
}


#: The web UI's optional reply-summary hook.  It receives this tracker's
#: store (the summary is written onto the comment that was just posted), the
#: ticket id, the new comment id, and the agent's reply text without the bot
#: footer.  The CLI supplies one only when ``webui.anthropic_api_key`` is set,
#: and it starts its own thread so posting stays fast.
AgentReplyCallback = Callable[[LocalTicketStore, str, str, str], None]


class LocalTracker:
    """Tracker protocol implementation backed by ``LocalTicketStore``.

    Tickets live under ``<workspace>/tickets/``.  There is no external
    service, no trigger label and no project: every ticket in a triggered
    state is polled, and moving a ticket to ``Done`` un-triggers it.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        ntfy: _NtfyConfig | None = None,
        public_url: str | None = None,
        presence: TicketPresence | None = None,
        on_agent_reply: AgentReplyCallback | None = None,
    ) -> None:
        self.store = LocalTicketStore(workspace / "tickets")
        # Notifications hang off the tracker because transition_to is the one
        # place a ticket reaches Needs Input, and it is only reachable from the
        # orchestrator.  ``None`` means the config block was absent.
        self._ntfy = ntfy
        # The UI's base URL is a webui setting, not an ntfy one, but the only
        # thing that consumes it is the notification payload here.
        self._public_url = public_url
        # Shared with the web UI, which records a human looking at the ticket.
        # ``None`` is a tracker with no web UI attached (tests, or a path that
        # constructs the tracker on its own), and then nothing suppresses.
        self._presence = presence
        # The web UI's optional reply-summary hook.  It lives here, rather than
        # the tracker calling the Anthropic API itself, so this module stays
        # free of that dependency; the CLI installs it only when
        # ``webui.anthropic_api_key`` is set.  It is called with this tracker's
        # store because the store is created here and the hook needs it to
        # write the result.
        self._on_agent_reply = on_agent_reply

    # ------------------------------------------------------------------
    # Tracker protocol methods
    # ------------------------------------------------------------------

    def list_triggered_issues(self) -> list[Issue]:
        return [
            self._to_issue(ticket)
            for ticket in self.store.list_tickets()
            if ticket.state in TRIGGERED_STATES
        ]

    def get_issue(self, id: str) -> Issue:
        ticket = self.store.get(id)
        if ticket is None:
            raise TrackerNotFoundError(f"Ticket not found: {id}")
        return self._to_issue(ticket)

    def list_comments_since(self, id: str, last_seen: str | None) -> list[Comment]:
        ticket = self.store.get(id)
        if ticket is None:
            raise TrackerNotFoundError(f"Ticket not found: {id}")

        comments = [self._to_comment(ticket.id, c) for c in ticket.comments]
        if last_seen is None:
            return comments

        for index, comment in enumerate(comments):
            if comment.id == last_seen:
                return comments[index + 1 :]

        # The anchor was deleted or belongs to another ticket: replaying old
        # comments as new input would be worse than dropping them.
        logger.warning(
            "Reference comment %s not found on ticket %s — returning empty list",
            last_seen,
            id,
        )
        return []

    def post_comment(self, id: str, body: str, kind: str) -> Comment:
        comment = self.store.add_comment(
            id, body + _BOT_FOOTER.format(kind=kind), kind=kind
        )
        # Only agent replies get a summary: an error, a workspace notice or a
        # QA notice has nothing to summarise for the human, and the home page
        # only looks at agent-kind comments anyway.  The hook is passed the
        # reply without the footer, which is presentation the model should not
        # have to see.
        if self._on_agent_reply is not None and is_agent_kind(kind):
            self._on_agent_reply(self.store, id, comment.id, body)
        return self._to_comment(id, comment)

    def edit_comment(self, id: str, body: str, kind: str) -> None:
        self.store.edit_comment(id, body + _BOT_FOOTER.format(kind=kind), kind=kind)

    def transition_to(self, id: str, target: TransitionTarget) -> None:
        """Move *id* to the state for *target*, unless the target may not.

        A turn can take minutes, during which the user may move the ticket to
        Done; pulling it back to In Progress would silently undo that.  The
        allowed source states are per target (see
        ``_TRANSITION_EXPECTED``): ``in_progress`` and ``qa`` refuse Done and
        Draft, while ``needs_input`` deliberately accepts Done because the
        dirty-workspace refusal depends on it.  A refused transition is a
        logged no-op, never an exception: the next tick sees an untriggered
        ticket and runs its normal cleanup, which cancels the turn.

        A successful ``needs_input`` transition publishes one ntfy
        notification unless the web UI recorded the human viewing that ticket
        seconds earlier (see :class:`~symphony_linear.presence.TicketPresence`).
        """
        state = _STATE_FOR_TARGET.get(target)
        if state is None:
            raise ValueError(
                f"No state mapping configured for transition target '{target.value}'"
            )
        expected = _TRANSITION_EXPECTED[target]
        if not self.store.set_state_if_changed(id, expected, state):
            ticket = self.store.get(id)
            logger.info(
                "Not transitioning %s to '%s': it is in %r (refused, or already there)",
                id,
                state,
                ticket.state if ticket is not None else "a deleted ticket",
            )
            return
        logger.info("Transitioned %s to '%s'", id, state)
        if target is TransitionTarget.needs_input and self._ntfy is not None:
            # The reply is already on the reader's screen when the human has
            # this ticket's page open, so a push would only repeat it on their
            # phone.  A stale or absent signal publishes normally.
            if self._presence is not None and self._presence.is_active(id):
                logger.info(
                    "Skipping ntfy notification for %s: its page is being viewed",
                    id,
                )
                return
            ticket = self.store.get(id)
            if ticket is not None:
                publish_needs_input(self._ntfy, ticket, public_url=self._public_url)

    def is_still_triggered(self, issue: Issue) -> bool:
        return issue.state in TRIGGERED_STATES

    def repo_url_for(self, issue: Issue) -> str:
        ticket = self.store.get(issue.id)
        if ticket is None:
            raise TrackerNotFoundError(f"Ticket not found: {issue.id}")
        return _clone_url(ticket.repo)

    def download_attachment(self, url: str) -> tuple[bytes, str | None]:
        """Read one of this tracker's registered uploaded files from disk.

        Only the exact canonical relative ``/files/<ticket>/<stored-name>``
        string is accepted; a query, fragment, percent-encoded alias or
        absolute URL is not ours.  The store's realpath containment and
        registration check are the security boundary for both untrusted
        components.  The content type is decided from the stored name's
        extension, exactly as serving decides it.
        """
        match = upload_path(url)
        if match is None:
            raise AttachmentDownloadError(f"Not an upload URL: {url}")
        ticket_id, stored_name = match
        try:
            data = self.store.read_file(ticket_id, stored_name)
        except (ValueError, FileNotFoundError, OSError, TrackerError) as exc:
            raise AttachmentDownloadError(f"Cannot read attachment {url}") from exc
        return data, image_content_type(stored_name) or "application/octet-stream"

    def is_upload_url(self, url: str) -> bool:
        """Return True only for this tracker's exact canonical upload path."""
        return upload_path(url) is not None

    def is_in_qa(self, issue: Issue) -> bool:
        return issue.state == STATE_QA

    @property
    def qa_enabled(self) -> bool:
        return True

    def transition_name_for(self, target: TransitionTarget) -> str:
        state = _STATE_FOR_TARGET.get(target)
        if state is None:
            raise ValueError(f"Unknown transition target: {target}")
        return state

    def ensure_trigger_setup(
        self, state: StateManager, model_labels: list[str]
    ) -> None:
        # Nothing to provision: every stored ticket is local.
        return None

    def human_trigger_description(self) -> str:
        return f"move the ticket to `{STATE_DONE}`"

    # ------------------------------------------------------------------
    # Conversions to the tracker-neutral models
    # ------------------------------------------------------------------

    def _to_issue(self, ticket: LocalTicket) -> Issue:
        return Issue(
            id=ticket.id,
            identifier=ticket.identifier,
            title=display_title(ticket),
            description=body_with_file_refs(
                ticket.id, ticket.description, ticket.files
            ),
            state=ticket.state,
            labels=[model_label_name(ticket.model)] if ticket.model else [],
            branchName=None,
            project=None,
            comments=[self._to_comment(ticket.id, c) for c in ticket.comments],
            # The orchestrator's only use of ``updatedAt`` is picking the
            # newest QA entrant.  Activity (a comment, an automatic title)
            # must not make an older QA ticket steal the serve, so report the
            # state change instead; the UI keeps using ``updated_at`` for
            # ordering and "last activity".
            updatedAt=ticket.state_changed_at,
        )

    @staticmethod
    def _to_comment(ticket_id: str, comment: LocalComment) -> Comment:
        return Comment(
            id=comment.id,
            body=body_with_file_refs(ticket_id, comment.body, comment.files),
            createdAt=comment.created_at,
            user_id=_HUMAN_USER_ID if comment.kind is None else None,
        )


def _clone_url(repo: str) -> str:
    """Turn the stored repo value into a clone URL.

    A full clone URL is returned unchanged; ``owner/name`` is GitHub-over-SSH
    shorthand.  An empty value is a user-facing configuration error.
    """
    value = repo.strip()
    if not value:
        raise TrackerError(
            "No repository is set on this ticket. Add one in the web UI and try again."
        )
    if _REPO_SHORTHAND_RE.match(value):
        if value.endswith(".git"):
            value = value[: -len(".git")]
        return f"git@github.com:{value}.git"
    return value
