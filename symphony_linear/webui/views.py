"""Views for the home and archive pages.

The pages are rendered on the server with Jinja2.  The view models are plain
data in dataclasses here, which keeps the templates free of formatting rules.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from symphony_linear.local_tracker import (
    STATE_DONE,
    STATE_DRAFT,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    LocalComment,
    LocalTicket,
    RepoEntry,
    display_title as _display_title,
    repo_short_name,
)
from symphony_linear.state import TicketState, TicketStatus
from symphony_linear.tracker import is_agent_kind
from symphony_linear.webui.http_types import Request, Response
from symphony_linear.webui.templates import render

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

#: Home groups, in the order they are displayed: (state, CSS class, empty text).
_HOME_GROUPS: tuple[tuple[str, str, str], ...] = (
    (STATE_NEEDS_INPUT, "state--input", "Nothing needs your input."),
    (STATE_IN_PROGRESS, "state--progress", "Nothing is in progress."),
    (STATE_QA, "state--qa", "Nothing is in QA."),
    (STATE_DRAFT, "state--draft", "No drafts."),
)


# ---------------------------------------------------------------------------
# Unread
# ---------------------------------------------------------------------------


def is_unread_comment(kind: str | None) -> bool:
    """True for the comment kinds that make a ticket unread: replies and errors."""
    return is_agent_kind(kind) or kind == "error"


def unread_comments(ticket: LocalTicket) -> list[LocalComment]:
    """Return the agent replies and errors posted after the seen marker.

    The marker is a comment id, so comment order is the list order.  A marker
    that no longer exists (a deleted comment) is treated as "nothing is
    unread" rather than making the ticket permanently unread.
    """
    if ticket.seen_comment_id is None:
        return [c for c in ticket.comments if is_unread_comment(c.kind)]
    index = next(
        (i for i, c in enumerate(ticket.comments) if c.id == ticket.seen_comment_id),
        None,
    )
    if index is None:
        return []
    return [c for c in ticket.comments[index + 1 :] if is_unread_comment(c.kind)]


def is_unread(ticket: LocalTicket) -> bool:
    """True when the ticket has an agent reply or error the user has not seen."""
    return bool(unread_comments(ticket))


# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One ticket row, with all display decisions already made."""

    id: str
    title: str
    summary: str | None
    repo: str
    repo_url: str
    updated_at: str
    updated_text: str
    working: bool
    working_for: str | None
    failed: bool
    unread: bool


@dataclass(frozen=True)
class Group:
    """A home-page section and its rows."""

    title: str
    state_class: str
    count: int
    rows: tuple[Row, ...]
    empty_text: str


@dataclass(frozen=True)
class HomeList:
    """The parts of a list page the poll re-renders and its fingerprint."""

    groups: tuple[Group, ...]
    open_count: int
    needs_input_count: int
    version: str


@dataclass(frozen=True)
class ArchiveList:
    """The Done list, plus the count shown in the tab title."""

    rows: tuple[Row, ...]
    count: int
    needs_input_count: int
    version: str


# ---------------------------------------------------------------------------
# Pure formatting helpers
# ---------------------------------------------------------------------------


def repo_names(repos: Iterable[RepoEntry]) -> dict[str, str]:
    """Map each managed repository's URL to the name the user saved for it.

    Keys are stripped because the store compares repository URLs after
    stripping, while a ticket keeps whatever string it was created with.
    """
    return {entry.url.strip(): entry.name for entry in repos}


def short_repo(repo: str, names: Mapping[str, str] | None = None) -> str:
    """The repository label: its saved name, else ``owner/name``, else "No repository".

    *names* is :func:`repo_names` for the current request, so a ticket's
    repository shows the same label the repos page lists it under.  The
    ``owner/name`` derivation lives in the store module because the managed
    repository list seeds its names from it; this is only the page-facing
    label.
    """
    stripped = repo.strip()
    saved = names.get(stripped) if names and stripped else None
    return saved or repo_short_name(stripped) or "No repository"


def display_title(ticket: LocalTicket) -> str:
    """Return the shared display fallback (see ``local_tracker.display_title``).

    Kept as a module-level name because the web modules and their tests import
    it from here; the rule itself lives in one place, next to the store.
    """
    return _display_title(ticket)


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sort_key(value: str) -> datetime:
    return _parse_datetime(value) or _EPOCH


def _utc_text(value: str) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return value
    return parsed.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _elapsed_text(start: str, now: datetime) -> str:
    parsed = _parse_datetime(start)
    if parsed is None:
        return "0 s"
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------


def turn_failed(state_entry: TicketState | None) -> bool:
    """True when the ticket's last turn failed.

    Both a failed status and a sticky setup error mean "the agent stopped
    before replying", so the ticket page's Retry button and the home row's
    failed flag share this one rule.
    """
    return state_entry is not None and (
        state_entry.status == TicketStatus.failed or bool(state_entry.setup_error)
    )


def latest_agent_summary(ticket: LocalTicket) -> str | None:
    """Return the summary of the ticket's latest agent reply, or ``None``.

    The *latest* reply decides: an older summary must not outlive a newer reply
    that has none.  Summaries need ``webui.anthropic_api_key`` and arrive
    asynchronously, so the newest turn may legitimately have no summary yet.
    """
    for comment in reversed(ticket.comments):
        if is_agent_kind(comment.kind):
            summary = (comment.summary or "").strip()
            return summary or None
    return None


def _row(
    ticket: LocalTicket,
    state_entry: TicketState | None,
    now: datetime,
    names: Mapping[str, str],
) -> Row:
    failed = turn_failed(state_entry)
    working = ticket.state == STATE_IN_PROGRESS and not failed
    # Only a waiting-on-you row shows the summary: In Progress has a live turn,
    # QA is about the serve, and a failed row's flag already says what happened
    # while its last "reply" may be the failure notice rather than an answer.
    summary = (
        latest_agent_summary(ticket)
        if ticket.state == STATE_NEEDS_INPUT and not failed
        else None
    )
    return Row(
        id=ticket.id,
        title=display_title(ticket),
        summary=summary,
        repo=short_repo(ticket.repo, names),
        repo_url=ticket.repo.strip(),
        updated_at=ticket.updated_at,
        updated_text=_utc_text(ticket.updated_at),
        working=working,
        working_for=_elapsed_text(ticket.state_changed_at, now) if working else None,
        failed=failed,
        unread=is_unread(ticket),
    )


def _state_entries(request: Request) -> dict[str, TicketState]:
    """Snapshot the StateManager's ticket list for read-only lookups.

    The web layer must never write ``state.json``.  Copying the list once per
    request avoids iterating a list the orchestrator may append to.
    """
    return {entry.ticket_id: entry for entry in list(request.app.state.tickets)}


def needs_input_count(request: Request) -> int:
    """Return the number of tickets waiting on the user, for the tab title."""
    return sum(
        1
        for ticket in request.app.store.list_tickets()
        if ticket.state == STATE_NEEDS_INPUT
    )


def _fingerprint(value: object) -> str:
    """Return a stable digest of what a list fragment renders."""
    payload = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def home_list(request: Request) -> HomeList:
    """Build the home page's groups and the fingerprint of their rendering."""
    now = datetime.now(timezone.utc)
    tickets = request.app.store.list_tickets()
    state_entries = _state_entries(request)
    names = repo_names(request.app.store.list_repos())

    groups: list[Group] = []
    for state, state_class, empty_text in _HOME_GROUPS:
        matching = [ticket for ticket in tickets if ticket.state == state]
        matching.sort(key=lambda ticket: _sort_key(ticket.updated_at), reverse=True)
        rows = tuple(
            _row(ticket, state_entries.get(ticket.id), now, names)
            for ticket in matching
        )
        groups.append(
            Group(
                title=state,
                state_class=state_class,
                count=len(rows),
                rows=rows,
                empty_text=empty_text,
            )
        )

    return HomeList(
        groups=tuple(groups),
        open_count=sum(group.count for group in groups),
        needs_input_count=needs_input_count(request),
        version=_fingerprint([asdict(group) for group in groups]),
    )


def archive_list(request: Request) -> ArchiveList:
    """Build the archive page's Done rows and their fingerprint."""
    now = datetime.now(timezone.utc)
    done = [
        ticket
        for ticket in request.app.store.list_tickets()
        if ticket.state == STATE_DONE
    ]
    done.sort(key=lambda ticket: _sort_key(ticket.state_changed_at), reverse=True)
    names = repo_names(request.app.store.list_repos())
    # The working/failed row variants belong to the home page; a finished
    # ticket is shown plainly even if a stale StateManager entry lingers.
    rows = tuple(_row(ticket, None, now, names) for ticket in done)
    return ArchiveList(
        rows=rows,
        count=len(rows),
        needs_input_count=needs_input_count(request),
        version=_fingerprint([asdict(row) for row in rows]),
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def home(request: Request) -> Response:
    """Render the ticket list, grouped by workflow state."""
    page = home_list(request)
    return Response.html(
        render(
            "home.html",
            app_class="app app--home",
            page=page,
            needs_input_count=page.needs_input_count,
            push_public_key=request.app.push_public_key,
        )
    )


def archive(request: Request) -> Response:
    """Render Done tickets, newest first."""
    page = archive_list(request)
    return Response.html(
        render(
            "archive.html",
            app_class="app app--home",
            page=page,
            needs_input_count=page.needs_input_count,
            push_public_key=request.app.push_public_key,
        )
    )


def next_ticket(request: Request) -> Response:
    """Redirect to the next ticket in Needs Input, wrapping at the end.

    The order is the home page's Needs Input group, taken from
    :func:`home_list` rather than re-sorted here, so the shortcut and the list
    can never disagree.  An absent or unknown ``after`` starts at the first
    ticket; an empty group goes to the list, whose empty state explains that
    nothing is waiting.
    """
    page = home_list(request)
    group = next(group for group in page.groups if group.title == STATE_NEEDS_INPUT)
    ids = [row.id for row in group.rows]
    values = request.query.get("after") or []
    after = values[0] if values else ""
    if not ids:
        target = "/"
    elif after in ids:
        target = f"/tickets/{ids[(ids.index(after) + 1) % len(ids)]}"
    else:
        target = f"/tickets/{ids[0]}"
    return Response(status=303, headers=(("Location", target),))
