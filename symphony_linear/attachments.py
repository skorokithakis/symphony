"""Attachment processing: extraction, download, validation, rewriting.

Two kinds of attachment are handled:

* **Images** (``.png`` / ``.jpg`` / ``.jpeg`` / ``.gif`` / ``.webp``) are
  passed to the coding agent as ``--file`` / ``@file`` arguments.
* **Non-image files** (archives, PDFs, …) are downloaded into the per-ticket
  attachments directory and their URL in the prompt is rewritten to the
  sandbox path, so the agent can open them with its own tools.  They are
  *not* added to :attr:`AttachmentResult.file_paths` — binaries in the model
  context are useless at best and break the turn at worst.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from symphony_linear.tracker import (
    AttachmentDownloadError,
    AttachmentTooLargeError,
)

if TYPE_CHECKING:
    from symphony_linear.tracker import Tracker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------------

_MARKDOWN_IMAGE_RE = re.compile(
    r"!\s*\[(.*?)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)",
)

# A plain Markdown link, i.e. ``[text](url)`` that is not image syntax.  The
# lookbehind excludes ``![...]`` (including ``! [...]`` with whitespace, which
# the image regex tolerates); the image pass runs first, so any URL already
# claimed as an image is skipped by dedup anyway.
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[(.*?)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)",
)

# Match a whole line whose sole content is a URL.
_BARE_URL_RE = re.compile(
    r"^\s*(https?://\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Check whether a URL's path ends with a recognised image extension.
_IMAGE_EXT_RE = re.compile(
    r"\.(?:png|jpg|jpeg|gif|webp)(?:\?|#|$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API — extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentRef:
    """A candidate attachment found in a prompt body.

    Attributes:
        url: The attachment URL.
        text: The link's visible text (an image's alt text for image syntax,
            the link text for a plain Markdown link, ``""`` for a bare URL).
            Used as the filename hint for non-image files.
    """

    url: str
    text: str


def extract_image_refs(body: str) -> list[tuple[str, str]]:
    """Return a de-duplicated list of ``(url, alt_text)`` image references.

    Detection is performed in two passes:

    1. **Markdown images** – ``![alt](url)`` syntax.  Optional whitespace
       around the URL and an optional trailing title in double quotes are
       accepted.
    2. **Bare image URLs** – a line whose sole content is a URL ending in
       ``.png``, ``.jpg``, ``.jpeg``, ``.gif``, or ``.webp``
       (case-insensitive).  Bare URLs have an empty alt-text.

    Entries are returned in source order.  Duplicate URLs are dropped;
    the *first* occurrence wins — Markdown matches therefore take
    precedence over any subsequent bare-URL match of the same URL.

    This is a **pure** function — no I/O, no network.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []

    # Pass 1: Markdown image syntax .......................................
    for m in _MARKDOWN_IMAGE_RE.finditer(body):
        alt = m.group(1).strip()
        url = m.group(2)
        if url not in seen:
            seen.add(url)
            result.append((url, alt))

    # Pass 2: Bare image URLs (only those not already found) ..............
    for m in _BARE_URL_RE.finditer(body):
        url = m.group(1)
        if _IMAGE_EXT_RE.search(url) and url not in seen:
            seen.add(url)
            result.append((url, ""))

    return result


def extract_attachment_refs(body: str, tracker: Tracker) -> list[AttachmentRef]:
    """Return candidate attachments in *body*, images first.

    Image references are extracted exactly as :func:`extract_image_refs`
    does (unchanged).  On top of that, plain Markdown links and bare URLs
    are picked up when the backend reports them as upload URLs via
    :meth:`~Tracker.is_upload_url`, so non-image uploads that Linear renders
    as ordinary links are downloaded too.

    Precedence follows the historical image extraction: all Markdown images
    come first, then bare image URLs; plain links and bare non-image upload
    URLs follow in source order.  Duplicate URLs are dropped, first
    occurrence wins.
    """
    seen: set[str] = set()
    result: list[AttachmentRef] = []

    for url, alt in extract_image_refs(body):
        if url not in seen:
            seen.add(url)
            result.append(AttachmentRef(url=url, text=alt))

    # Plain Markdown links (not image syntax) to an uploaded file.
    for m in _MARKDOWN_LINK_RE.finditer(body):
        url = m.group(2)
        if url in seen:
            continue
        if tracker.is_upload_url(url):
            seen.add(url)
            result.append(AttachmentRef(url=url, text=m.group(1).strip()))

    # Bare URLs that are upload URLs but not image-extension URLs (the
    # latter were already picked up by extract_image_refs).
    for m in _BARE_URL_RE.finditer(body):
        url = m.group(1)
        if url in seen:
            continue
        if tracker.is_upload_url(url):
            seen.add(url)
            result.append(AttachmentRef(url=url, text=""))

    return result


# ---------------------------------------------------------------------------
# Attachment result
# ---------------------------------------------------------------------------


@dataclass
class AttachmentResult:
    """The result of processing attachments for a turn.

    Attributes:
        rewritten_body: The body text with successfully-downloaded
            attachment URLs replaced by sandbox paths.
        file_paths: Paths as seen inside the sandbox (e.g.
            ``/tmp/symphony-attachments/img-0001.png``) for **images only**.
            Non-image files appear in *rewritten_body* but are deliberately
            excluded here.
        skipped: ``(url, reason)`` tuples for every URL that was not
            successfully downloaded and persisted.
        next_index: The lowest un-consumed attachment index.  Equal to
            ``existing_count + len(refs)`` regardless of how many files
            were successfully written, so that skipped indices are never
            reused and filename collisions across turns are avoided.
    """

    rewritten_body: str
    file_paths: list[str]
    skipped: list[tuple[str, str]]
    next_index: int = 0


# ---------------------------------------------------------------------------
# Whitelist & extension helpers
# ---------------------------------------------------------------------------

_WHITELIST_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

_CONTENT_TYPE_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# Content types that carry no useful type information, so the URL path is a
# better clue than the header.  Any *other* declared, non-image type (e.g.
# text/html, application/pdf) wins over a misleading URL extension.
_GENERIC_CONTENT_TYPES: frozenset[str] = frozenset(
    {"application/octet-stream", "binary/octet-stream"}
)

# Fallback filename hint length for non-image files.  Long enough to keep a
# real filename recognisable, short enough to stay well under filesystem
# limits once ``file-NNNN-`` is prepended.
_MAX_NAME_LEN = 80

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _ext_from_url(url: str) -> str | None:
    """Return a whitelisted image extension (with leading dot) from the URL
    path, or ``None`` if the path has no recognised image extension."""
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    if ext and ext[1:] in _WHITELIST_EXTENSIONS:
        return ext
    return None


def _ext_from_content_type(content_type: str | None) -> str | None:
    """Map a normalised image Content-Type to a file extension, or ``None``."""
    if content_type is None:
        return None
    return _CONTENT_TYPE_TO_EXT.get(content_type)


def _generic_ext_from_content_type(content_type: str | None) -> str | None:
    """Best-effort extension (with leading dot) for a non-image type.

    Used only as a filename hint when a non-image file has no link text.
    Returns ``None`` when the type is missing or unknown.
    """
    if not content_type:
        return None
    ext = mimetypes.guess_extension(content_type)
    if ext is None:
        return None
    # Guard against oddities from an unusually-shaped system mime database.
    return ext if re.fullmatch(r"\.[A-Za-z0-9]+", ext) else None


def _sanitize_name(text: str) -> str:
    """Return *text* reduced to a safe single path component.

    Every character outside ``[A-Za-z0-9._-]`` (including path separators)
    becomes ``_``; the result is truncated to ``_MAX_NAME_LEN`` characters.
    """
    return _UNSAFE_NAME_RE.sub("_", text)[:_MAX_NAME_LEN]


# ---------------------------------------------------------------------------
# Body rewriting
# ---------------------------------------------------------------------------


def _build_url_regex(url: str) -> re.Pattern[str]:
    """Build a regex that matches ``![<any alt>](url)`` with flexible whitespace.

    Capture group 1 captures the alt text so callers can preserve it during
    replacement.
    """
    escaped_url = re.escape(url)
    return re.compile(r"!\s*\[(.*?)\]\(\s*" + escaped_url + r"(?:\s+\"[^\"]*\")?\s*\)")


def _build_link_regex(url: str) -> re.Pattern[str]:
    """Build a regex that matches a plain ``[<any text>](url)`` Markdown link.

    The lookbehind keeps it from matching image syntax.  Capture group 1
    captures the link text so callers can preserve it during replacement.
    """
    escaped_url = re.escape(url)
    return re.compile(
        r"(?<!!)\[(.*?)\]\(\s*" + escaped_url + r"(?:\s+\"[^\"]*\")?\s*\)"
    )


def _build_bare_url_regex(url: str) -> re.Pattern[str]:
    """Build a regex that matches *url* as a standalone token.

    The URL is only matched when bounded by whitespace (or the start/end of
    the body), i.e. exactly how :data:`_BARE_URL_RE` extracts whole-line bare
    URLs.  A plain global ``str.replace`` would also rewrite the prefix of a
    longer URL that merely starts with *url* (``.../a`` inside
    ``.../a/b.zip``), corrupting it.
    """
    return re.compile(r"(?<!\S)" + re.escape(url) + r"(?!\S)")


def _rewrite_body(body: str, url: str, sandbox_path: str, is_image: bool) -> str:
    """Replace every occurrence of *url* in *body* with *sandbox_path*.

    All syntactic forms always run, so a URL that appears in multiple forms
    (e.g. an image and a bare URL) is rewritten everywhere:

    * Markdown images (``![alt](url)``) become ``![alt](sandbox_path)`` for
      images and a plain ``[alt](sandbox_path)`` link for generic files — an
      image-syntax ref that turned out not to be an image should not keep
      pretending to be one.
    * Plain Markdown links (``[text](url)``) become ``[text](sandbox_path)``,
      keeping their link text.
    * Bare URLs become ``![](sandbox_path)`` for images (preserving the
      historical form) and the plain ``sandbox_path`` for non-image files.
      Only standalone (whitespace/string-bounded) occurrences are replaced.

    The Markdown passes run first; their output no longer contains the
    literal *url*, so the bare-URL pass is safe.
    """

    # Pass 1: replace all Markdown-image occurrences of this URL (any alt).
    def _replace_image(match: re.Match[str]) -> str:
        marker = "!" if is_image else ""
        return f"{marker}[{match.group(1)}]({sandbox_path})"

    body = _build_url_regex(url).sub(_replace_image, body)

    # Pass 2: replace all plain Markdown-link occurrences (any text).
    def _replace_link(match: re.Match[str]) -> str:
        return f"[{match.group(1)}]({sandbox_path})"

    body = _build_link_regex(url).sub(_replace_link, body)

    # Pass 3: replace standalone bare-URL occurrences.  Bound the match so a
    # longer URL sharing this URL as a prefix is left untouched.
    def _replace_bare(match: re.Match[str]) -> str:
        return f"![]({sandbox_path})" if is_image else sandbox_path

    return _build_bare_url_regex(url).sub(_replace_bare, body)


# ---------------------------------------------------------------------------
# Public API — processing
# ---------------------------------------------------------------------------


def process_attachments(
    body: str,
    tracker: Tracker,
    host_attachments_dir: str,
    sandbox_mount: str = "/tmp/symphony-attachments",
    existing_count: int = 0,
    per_turn_byte_cap: int = 50 * 1024 * 1024,
) -> AttachmentResult:
    """Download, validate, and persist attachments from *body*.

    Images and non-image uploads are both downloaded.  Images are numbered
    ``img-NNNN.<ext>`` and returned in *file_paths*; non-image files are
    numbered ``file-NNNN-<name>`` (or ``file-NNNN`` / ``file-NNNN.<ext>``
    when the link has no usable text) and are referenced only in the
    rewritten body.  Both kinds share the same ``attachment_count`` index.

    Parameters
    ----------
    body:
        The Markdown text to scan for attachment references.
    tracker:
        Any :class:`Tracker` implementation providing
        :meth:`~Tracker.is_upload_url` and
        :meth:`~Tracker.download_attachment`.
    host_attachments_dir:
        Host-side directory to write downloaded files into.  Created if
        it does not exist.
    sandbox_mount:
        Prefix used for sandbox-side paths (default
            ``"/tmp/symphony-attachments"``).
    existing_count:
        Number of files already in *host_attachments_dir* so that
        numbering does not collide (e.g. ``5`` → first new file is
        ``img-0006.png``).
    per_turn_byte_cap:
        Maximum total bytes to write in this call.  Once this cap would
        be exceeded the current attachment is skipped (reason ``"turn byte
        cap exceeded"``) and all remaining attachments are also checked
        against the cap.

    Returns
    -------
    AttachmentResult
        The rewritten body, the list of sandbox-side image paths, and any
        ``(url, reason)`` skip entries.
    """
    refs = extract_attachment_refs(body, tracker)
    if not refs:
        return AttachmentResult(
            rewritten_body=body,
            file_paths=[],
            skipped=[],
            next_index=existing_count,
        )

    os.makedirs(host_attachments_dir, exist_ok=True)

    rewritten_body = body
    file_paths: list[str] = []
    skipped: list[tuple[str, str]] = []
    total_bytes = 0

    for idx, ref in enumerate(refs, start=existing_count + 1):
        url = ref.url

        # 1. Download .......................................................
        try:
            data, content_type = tracker.download_attachment(url)
        except AttachmentTooLargeError:
            skipped.append((url, "attachment too large"))
            continue
        except AttachmentDownloadError:
            skipped.append((url, "download failed"))
            continue
        except Exception:
            logger.debug("Unexpected download error for %s", url, exc_info=True)
            skipped.append((url, "download failed"))
            continue

        # 2. Classify and pick a filename ...................................
        # A whitelisted image Content-Type is authoritative.  The URL path
        # is only consulted when the Content-Type is missing or an opaque
        # generic type (octet-stream): a server-declared text/html or
        # application/pdf must not enter file_paths just because the URL
        # ends in .png.  Anything that is not a whitelisted image is
        # persisted as a generic file — including image-syntax refs whose
        # target is not an image.
        image_ext = _ext_from_content_type(content_type)
        if image_ext is None and (
            content_type is None or content_type in _GENERIC_CONTENT_TYPES
        ):
            image_ext = _ext_from_url(url)

        if image_ext is not None:
            is_image = True
            filename = f"img-{idx:04d}{image_ext}"
        else:
            is_image = False
            name = _sanitize_name(ref.text)
            if name:
                filename = f"file-{idx:04d}-{name}"
            else:
                ext = _generic_ext_from_content_type(content_type)
                filename = f"file-{idx:04d}{ext}" if ext else f"file-{idx:04d}"

        # 3. Check per-turn byte cap ........................................
        if total_bytes + len(data) > per_turn_byte_cap:
            skipped.append((url, "turn byte cap exceeded"))
            continue

        total_bytes += len(data)

        # 4. Persist ........................................................
        host_path = os.path.join(host_attachments_dir, filename)
        try:
            with open(host_path, "wb") as f:
                f.write(data)
        except OSError:
            logger.debug("Failed to write attachment %s", host_path, exc_info=True)
            skipped.append((url, "write failed"))
            total_bytes -= len(data)  # roll back the byte tally
            continue

        sandbox_path = f"{sandbox_mount}/{filename}"
        if is_image:
            file_paths.append(sandbox_path)

        # 5. Rewrite body ..................................................
        rewritten_body = _rewrite_body(rewritten_body, url, sandbox_path, is_image)

    return AttachmentResult(
        rewritten_body=rewritten_body,
        file_paths=file_paths,
        skipped=skipped,
        next_index=existing_count + len(refs),
    )
