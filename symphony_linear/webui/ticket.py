"""The ticket page: thread, reply composer and state actions.

The page is a plain HTML form so it works without JavaScript.  Every mutation
is a POST: the GET view only reads the ticket store and the StateManager, as
the server's safe-method contract requires.

Comment classification is driven by the ``kind`` the orchestrator stored with
the comment (see ``LocalComment.kind`` and ``LocalTracker.post_comment``):

* no kind         -- a human comment ("You")
* ``final``       -- an agent reply; ``context: N tokens`` does the same
* ``error``       -- an error alert
* ``workspace``   -- a one-line "workspace ready / session restored" notice
* anything else   -- a notice when it is one short line, otherwise a warning

The stored body ends with the bot footer (``*Symphony · kind*``); it is
removed here and never shown.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import parse_qs

from symphony_linear.local_tracker import (
    STATE_DONE,
    STATE_DRAFT,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    REPLY_STATES,
    LocalComment,
    LocalTicket,
)
from symphony_linear.state import TicketState
from symphony_linear.tracker import is_agent_kind
from symphony_linear.webui import editor
from symphony_linear.webui.files import FileChip, chips_for
from symphony_linear.webui.http_types import Request, Response
from symphony_linear.webui.markdown import (
    markdown_to_text,
    render_markdown,
    render_markdown_sections,
)
from symphony_linear.webui.templates import render, render_macro
from symphony_linear.webui.views import (
    display_title,
    needs_input_count,
    repo_names,
    short_repo,
    turn_failed,
)

#: State name -> (CSS class, label).  The labels follow the design mock, which
#: uses sentence case in the tag even though the workflow state is Title Case.
_STATE_TAGS: dict[str, tuple[str, str]] = {
    STATE_IN_PROGRESS: ("state--progress", "In progress"),
    STATE_NEEDS_INPUT: ("state--input", "Needs input"),
    STATE_QA: ("state--qa", "QA"),
    STATE_DONE: ("state--done", "Done"),
}

#: Actions allowed from each state, and the state they move the ticket to.
#: A ticket in any other state gets 409.  "send" is handled by its own route
#: and is allowed from Needs Input and QA; it deliberately does not change the
#: tracker state (the orchestrator moves the ticket itself when it picks the
#: comment up).  "qa" is likewise a pure transition and does not post the
#: composer text: a fresh human comment on a ticket that just entered QA is a
#: pending comment for the orchestrator, which would resume the agent and pull
#: the ticket straight back out of QA instead of starting the serve.
STATE_ACTIONS: dict[str, dict[str, str]] = {
    STATE_IN_PROGRESS: {"stop": STATE_NEEDS_INPUT},
    STATE_NEEDS_INPUT: {"qa": STATE_QA, "done": STATE_DONE},
    STATE_QA: {"needs_input": STATE_NEEDS_INPUT, "done": STATE_DONE},
}

#: Done has no entry in the map: Reopen's target depends on whether the
#: orchestrator still tracks the ticket.  See :func:`state_action_target`.
#: States that may post a reply.  Every other state's Send action is a 409,
#: including In Progress (the page disables the button) and Done (no composer).
#: The store enforces the same set in the append transaction; this alias keeps
#: one definition.
COMPOSER_STATES = REPLY_STATES
_COMPOSER_STATES = COMPOSER_STATES

#: States whose page shows the reply composer.  The text stays editable and is
#: autosaved in all of them, including while the agent works; only Send is
#: disabled outside ``COMPOSER_STATES``.
DRAFT_TEXT_STATES = frozenset({STATE_IN_PROGRESS, STATE_NEEDS_INPUT, STATE_QA})

#: The bot footer appended by the tracker adapter: "\n\n*Symphony · kind*".
_FOOTER_MARKER = "\n\n*Symphony · "

#: "**Symphony error**: " prefix on error bodies; the alert title carries it.
_ERROR_PREFIX_RE = re.compile(r"^\*\*Symphony error\*\*:\s*")

#: A body longer than this (or taller than four lines) is hidden behind a
#: "Show details" toggle inside an error alert.
_LONG_BODY = 400
_LONG_LINES = 4

#: A single line longer than this becomes a warning instead of a notice.
_SHORT_NOTICE = 180

#: Maximum accepted length of the client-generated send token.
MAX_TOKEN_LENGTH = 200
_MAX_TOKEN_LENGTH = MAX_TOKEN_LENGTH

#: The fixed human comment Retry posts.  It is deliberately a comment rather
#: than a state action: every failure path already resumes on a human comment
#: (the orchestrator's comment-gated retry is the only retry route there is).
RETRY_BODY = "Retry."


# ---------------------------------------------------------------------------
# Entry model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One item in the thread, with every display decision already made.

    ``kind`` is one of ``user``, ``agent``, ``error``, ``notice`` or
    ``warning``.  ``html`` is rendered Markdown and is the only value a
    template may mark safe; ``notice`` is escaped plain text.  ``new`` marks an
    entry posted after the ticket's seen marker when the page was opened; the
    page keeps the badge until it is reloaded.
    """

    kind: str
    who: str = ""
    created_at: str = ""
    clock: str = ""
    html: str = ""
    notice: str = ""
    meta: tuple[str, ...] = ()
    long: bool = False
    detail_html: str = ""
    anchor: str = ""
    new: bool = False
    files: tuple[FileChip, ...] = ()


@dataclass(frozen=True)
class TicketPage:
    """Everything the ticket template needs, all of it already formatted."""

    ticket: LocalTicket
    title: str
    repo: str
    repo_url: str
    state_class: str
    state_label: str
    branch: str
    model: str
    workspace: str | None
    session: str | None
    entries: tuple[Entry, ...] = field(default_factory=tuple)
    working_for: str | None = None
    #: Age of the running turn's last output, already formatted (e.g. "40 s").
    #: None until the turn produces its first output, and always None outside
    #: In Progress.
    last_output_ago: str | None = None
    token: str = ""
    #: Retry's own idempotency token.  It is separate from ``token`` so a Retry
    #: whose response was lost can never be mistaken for a duplicate of a Send
    #: (or the other way round), which would clear a reply that never posted.
    retry_token: str = ""
    has_composer: bool = False
    send_reason: str | None = None
    state_actions: tuple[str, ...] = ()
    reply_draft: str = ""
    reply_draft_saved_at: str | None = None
    pending_files: tuple[FileChip, ...] = ()
    last_comment_id: str = ""
    seen_comment_id: str | None = None
    needs_input_count: int = 0
    page_title: str = ""
    #: The composer's Model select: the aliases to offer, in configuration
    #: order, and the stored model when it is still one of them.  Like the
    #: editor, an alias the configuration no longer defines reads as Default.
    #: ``retry`` is set only in Needs Input when the last turn failed.
    composer_models: tuple[str, ...] = ()
    composer_model: str = ""
    retry: bool = False


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clock(value: str) -> str:
    """Return the no-JavaScript fallback time shown in an entry header."""
    parsed = _parse_datetime(value)
    if parsed is None:
        return value
    return parsed.astimezone(timezone.utc).strftime("%H:%M UTC")


def _format_duration(seconds: int) -> str:
    """Format *seconds* the way the design mock does.

    Seconds under a minute are exact because that is the range a stalled turn
    is judged in; above that the minutes (and hours) carry the meaning.
    """
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s"
    if seconds < 86400:
        return f"{seconds // 3600} h {(seconds % 3600) // 60} min"
    return f"{seconds // 86400} d {(seconds % 86400) // 3600} h"


def _elapsed(start: str) -> str:
    """Return a coarse but precise elapsed time, like the design mock."""
    parsed = _parse_datetime(start)
    if parsed is None:
        return "0 s"
    seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    return _format_duration(seconds)


def _strip_footer(body: str) -> str:
    """Remove the bot footer from *body* when it ends with one."""
    index = body.rfind(_FOOTER_MARKER)
    if index != -1 and body.rstrip().endswith("*"):
        return body[:index]
    return body


def _is_long(text: str) -> bool:
    return len(text) > _LONG_BODY or text.count("\n") >= _LONG_LINES


def _kind_meta(kind: str) -> tuple[str, ...]:
    """Extract the display metadata from a final-comment kind string.

    Kinds are ``final`` or ``context: 37,074 tokens`` optionally followed by
    `` · model: <id>``.  The middle dot (U+00B7) is the footer separator.
    """
    segments = [segment.strip() for segment in kind.split(" · ")]
    meta: list[str] = []
    first = segments[0]
    if first.startswith("context: "):
        meta.append(first[len("context: ") :])
    for segment in segments[1:]:
        if segment.startswith("model: "):
            meta.append(segment[len("model: ") :])
    return tuple(meta)


def _notice_text(plain: str) -> str:
    """Turn visible plain text into the label of a system notice."""
    notice = plain.strip()
    if notice.startswith("Symphony: "):
        notice = notice[len("Symphony: ") :]
    return notice or "Symphony notice"


# ---------------------------------------------------------------------------
# Entry assembly
# ---------------------------------------------------------------------------


def _entry_body(comment: LocalComment, ticket_id: str) -> Entry:
    kind = comment.kind
    created_at = comment.created_at
    clock = _clock(created_at)

    if kind is None:
        return Entry(
            kind="user",
            who="You",
            created_at=created_at,
            clock=clock,
            html=render_markdown(comment.body),
            files=chips_for(ticket_id, comment.files),
        )

    body = _strip_footer(comment.body)

    if kind == "workspace":
        restored = body.lstrip().startswith("**Restored**")
        return Entry(
            kind="notice",
            created_at=created_at,
            clock=clock,
            notice="Session restored" if restored else "Workspace ready",
        )

    if is_agent_kind(kind):
        return Entry(
            kind="agent",
            who="Agent",
            created_at=created_at,
            clock=clock,
            html=render_markdown_sections(body),
            meta=_kind_meta(kind),
            anchor=f"reply-{comment.id}",
        )

    if kind == "error":
        text = _ERROR_PREFIX_RE.sub("", body.strip())
        if not _is_long(text):
            return Entry(
                kind="error",
                created_at=created_at,
                clock=clock,
                html=render_markdown(text),
            )
        # Keep the lead line visible and hide the rest behind the toggle, like
        # the design mock.  A body that has no lead line (or starts with a
        # code fence) is hidden whole.
        head, separator, rest = text.partition("\n")
        if separator and head and not head.lstrip().startswith("```"):
            return Entry(
                kind="error",
                created_at=created_at,
                clock=clock,
                html=render_markdown(head),
                long=True,
                detail_html=render_markdown(rest),
            )
        return Entry(
            kind="error",
            created_at=created_at,
            clock=clock,
            long=True,
            detail_html=render_markdown(text),
        )

    text = body.strip()
    plain = markdown_to_text(text)
    if "\n" not in plain and len(plain) <= _SHORT_NOTICE:
        return Entry(
            kind="notice",
            created_at=created_at,
            clock=clock,
            notice=_notice_text(plain),
        )
    return Entry(
        kind="warning",
        created_at=created_at,
        clock=clock,
        html=render_markdown(text),
    )


def build_entries(
    ticket: LocalTicket, *, unread_from: int | None = None
) -> tuple[Entry, ...]:
    """Return the thread entries, oldest first, starting with the ticket text.

    ``unread_from`` is the index in ``ticket.comments`` of the seen marker, or
    ``None`` when nothing has been seen.  Every comment after it carries the
    "New" badge.  This is only used when the page is rendered: a later poll
    appends entries that are new by construction.
    """
    entries = [
        Entry(
            kind="user",
            who="Ticket text",
            created_at=ticket.created_at,
            clock=_clock(ticket.created_at),
            html=render_markdown(ticket.description),
            files=chips_for(ticket.id, ticket.files),
        )
    ]
    for index, comment in enumerate(ticket.comments):
        new = unread_from is None or index > unread_from
        entries.append(entry_for(comment, new=new, ticket_id=ticket.id))
    return tuple(entries)


def entry_for(comment: LocalComment, *, new: bool = False, ticket_id: str) -> Entry:
    """Return the display entry for one stored comment."""
    entry = _entry_body(comment, ticket_id)
    return replace(entry, new=True) if new else entry


def state_action_target(
    request: Request, ticket: LocalTicket, action: str
) -> str | None:
    """Return the state *action* moves *ticket* to, or None when not allowed.

    Reopen is decided here, at the moment of the click, by reading the
    ``StateManager`` (never writing it):

    * a state entry still exists -> Needs Input.  The daemon tracks the ticket,
      so a reply resumes the session through the normal path instead of
      leaving it "working" with nothing running.
    * no state entry -> In Progress.  The orchestrator's new-ticket pipeline
      picks it up and itself ends in Needs Input, with or without a session
      snapshot.  Send stays unavailable until then, which is what protects a
      reply written on the path that has no snapshot.
    """
    if ticket.state == STATE_DONE and action == "reopen":
        return (
            STATE_NEEDS_INPUT
            if _state_entry(request, ticket.id) is not None
            else STATE_IN_PROGRESS
        )
    return STATE_ACTIONS.get(ticket.state, {}).get(action)


def _seen_index(ticket: LocalTicket) -> int | None:
    """Return the comment index the page has seen up to, or None."""
    if ticket.seen_comment_id is None:
        return None
    return next(
        (i for i, c in enumerate(ticket.comments) if c.id == ticket.seen_comment_id),
        None,
    )


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------


def _state_entry(request: Request, ticket_id: str) -> TicketState | None:
    """Read the ticket's StateManager entry without mutating anything."""
    for entry in list(request.app.state.tickets):
        if entry.ticket_id == ticket_id:
            return entry
    return None


def retry_allowed(request: Request, ticket_id: str) -> bool:
    """Whether Retry should be accepted for *ticket_id* right now.

    This is only the failed-turn half; the store checks Needs Input inside its
    locked append transaction, where a state change cannot race it.  The
    read here is deliberately side-effect free.
    """
    return turn_failed(_state_entry(request, ticket_id))


def page_for(
    request: Request, ticket: LocalTicket, *, include_entries: bool = True
) -> TicketPage:
    state_entry = _state_entry(request, ticket.id)
    state_class, state_label = _STATE_TAGS.get(
        ticket.state, ("state--input", ticket.state)
    )
    working_for = (
        _elapsed(ticket.state_changed_at) if ticket.state == STATE_IN_PROGRESS else None
    )
    last_output_ago: str | None = None
    if working_for is not None and request.app.turn_activity is not None:
        output_age = request.app.turn_activity.output_age(ticket.id)
        if output_age is not None:
            last_output_ago = _format_duration(max(0, int(output_age)))
    title = display_title(ticket)
    names = repo_names(request.app.store.list_repos())
    last_comment_id = ticket.comments[-1].id if ticket.comments else ""
    # An empty alias is not a selectable choice: "" is the Default option.
    composer_models = tuple(alias for alias in request.app.config.models if alias)
    page = TicketPage(
        ticket=ticket,
        title=title,
        repo=short_repo(ticket.repo, names),
        repo_url=ticket.repo.strip(),
        state_class=state_class,
        state_label=state_label,
        branch=state_entry.branch if state_entry else "",
        model=ticket.model or "Default",
        workspace=state_entry.workspace_path if state_entry else None,
        session=state_entry.session_id if state_entry else None,
        entries=(
            build_entries(ticket, unread_from=_seen_index(ticket))
            if include_entries
            else ()
        ),
        working_for=working_for,
        last_output_ago=last_output_ago,
        # The token is the Send idempotency token.  It is rendered on every
        # composer page, not only when Send is allowed, so that a state change
        # picked up by the poll re-enables Send without a fresh fragment.
        token=secrets.token_urlsafe(24),
        # Retry gets its own token rather than sharing Send's: the two actions
        # are independent requests, so a lost Retry response must not make the
        # next Send look like a duplicate (and clear the reply text).
        retry_token=secrets.token_urlsafe(24),
        has_composer=ticket.state != STATE_DONE,
        send_reason=(
            "The agent is working. You can send when it stops."
            if ticket.state == STATE_IN_PROGRESS
            else None
        ),
        state_actions=tuple(STATE_ACTIONS.get(ticket.state, {})),
        reply_draft=ticket.reply_draft,
        reply_draft_saved_at=ticket.reply_draft_saved_at,
        pending_files=chips_for(ticket.id, ticket.pending_files),
        last_comment_id=last_comment_id,
        seen_comment_id=ticket.seen_comment_id,
        needs_input_count=needs_input_count(request),
        page_title=f"{ticket.id} · {title} · symphony",
        composer_models=composer_models,
        composer_model=(
            ticket.model
            if ticket.model is not None and ticket.model in composer_models
            else ""
        ),
        # Retry is the composer's recovery action: only a Needs Input ticket
        # whose last turn (or workspace setup) failed has anything to retry.
        retry=ticket.state == STATE_NEEDS_INPUT and turn_failed(state_entry),
    )
    return page


def snapshot(request: Request, ticket: LocalTicket, after: str) -> dict[str, object]:
    """Build the poll response for a ticket page.

    Only comments after *after* are rendered, with the same entry macro as the
    full page.  An unknown anchor yields no entries rather than replaying the
    thread.  The read is side-effect free, as GET must be.
    """
    # Header data only: rendering the whole thread here and then throwing it
    # away to render the delta again is the poll's dominant cost.
    page = page_for(request, ticket, include_entries=False)
    if after:
        ids = [comment.id for comment in ticket.comments]
        newer = ticket.comments[ids.index(after) + 1 :] if after in ids else []
    else:
        # The page was rendered with no comments at all; anything that exists
        # now is new to it.
        newer = list(ticket.comments)
    newer_entries = [entry_for(comment, ticket_id=ticket.id) for comment in newer]
    return {
        "entries_html": "".join(
            render_macro("entry", entry) for entry in newer_entries
        ),
        "last_comment_id": page.last_comment_id,
        "title": page.title,
        "page_title": page.page_title,
        # The Details row shows the stored model, which a Send can change.
        "model": page.model,
        # The composer select's value for the same stored model.  The page
        # updates it only while the user has not made a choice of their own.
        "composer_model": page.composer_model,
        "state_html": render_macro("state_tag", page),
        "actions_html": render_macro("composer_actions", page),
        "state_forms_html": render_macro("state_forms", page),
        "working_html": render_macro("working", page) if page.working_for else "",
        "has_composer": page.has_composer,
        "needs_input_count": page.needs_input_count,
    }


def ticket_for(request: Request) -> LocalTicket | None:
    """Load the routed ticket id, or None when the route has no match."""
    match = request.match
    if match is None:
        return None
    return request.app.store.get(match.group("ticket_id"))


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------


def _form(request: Request) -> dict[str, list[str]] | None:
    """Parse an ``application/x-www-form-urlencoded`` body, or None."""
    try:
        text = request.body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        return parse_qs(text, keep_blank_values=True)
    except ValueError:
        return None


def _field(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name)
    return values[0] if values else ""


def _redirect(ticket_id: str) -> Response:
    return Response(status=303, headers=(("Location", f"/tickets/{ticket_id}"),))


def _not_found() -> Response:
    return Response.text("Not Found\n", status=404)


def _conflict() -> Response:
    return Response.text("Conflict\n", status=409)


def _bad_request() -> Response:
    return Response.text("Bad Request\n", status=400)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def ticket_page(request: Request) -> Response:
    """Render a started ticket; a Draft goes to the editor."""
    match = request.match
    if match is None:
        return _not_found()
    ticket = request.app.store.get(match.group("ticket_id"))
    if ticket is None:
        return _not_found()
    if ticket.state == STATE_DRAFT:
        return Response(
            status=303,
            headers=(("Location", f"/tickets/{ticket.id}/edit"),),
        )
    return Response.html(
        render(
            "ticket.html",
            app_class="app app--ticket",
            page=page_for(request, ticket),
            needs_input_count=needs_input_count(request),
            dictation_enabled=request.app.dictation_enabled,
        )
    )


def post_reply(request: Request) -> Response:
    """Append a human reply.  The state is deliberately not changed."""
    match = request.match
    if match is None:
        return _not_found()
    ticket = request.app.store.get(match.group("ticket_id"))
    if ticket is None:
        return _not_found()

    form = _form(request)
    if form is None:
        return _bad_request()
    body = _field(form, "body").strip()
    token = _field(form, "token").strip()
    if not body or not token or len(token) > _MAX_TOKEN_LENGTH:
        return _bad_request()

    # An absent model field means the page had no Model select, so a stored
    # model stays as it is; a submitted field (including the empty Default
    # value) is validated exactly like the editor's and stored with the reply.
    set_model = "model" in form
    model = editor.model_from_form(request, form) if set_model else None

    # The allowed-state check lives inside the store call, in the same locked
    # transaction as the append; a state check here would be racy.
    try:
        request.app.store.add_human_comment_once(
            ticket.id,
            body,
            token=token,
            model=model,
            set_model=set_model,
        )
    except ValueError:
        return _conflict()
    request.app.on_wake()
    return _redirect(ticket.id)


def post_retry(request: Request) -> Response:
    """Post the fixed "Retry." comment for a failed turn.

    The no-JavaScript path only: the Retry button submits the composer form
    here with ``formaction``, so its own token and the Model choice arrive in
    the same shape Send reads.  Retry has a narrower window than a normal
    reply: the ticket must be in Needs Input (checked inside the store's
    locked transaction) and its last turn must have failed.  A duplicate token
    is still accepted and reports success, because its first attempt did the
    work.

    The submitted composer text is not part of the Retry comment, and pending
    files stay where they are.  A browser without JavaScript has not
    autosaved what the human typed, so a body that differs from the stored
    draft is saved as the draft before the redirect re-renders it.
    """
    match = request.match
    if match is None:
        return _not_found()
    ticket = request.app.store.get(match.group("ticket_id"))
    if ticket is None:
        return _not_found()

    form = _form(request)
    if form is None:
        return _bad_request()
    token = _field(form, "retry_token").strip()
    if not token or len(token) > _MAX_TOKEN_LENGTH:
        return _bad_request()

    set_model = "model" in form
    model = editor.model_from_form(request, form) if set_model else None
    # Eligibility first, the token only when ineligible: a replay whose first
    # attempt committed after this check began must still report success.
    if not retry_allowed(request, ticket.id) and not request.app.store.token_accepted(
        ticket.id, token
    ):
        return _conflict()
    try:
        comment = request.app.store.add_human_comment_once(
            ticket.id,
            RETRY_BODY,
            token=token,
            consume_composer=False,
            model=model,
            set_model=set_model,
            retry_only=True,
        )
    except ValueError:
        return _conflict()
    # A replayed token (comment is None) must not save its stale body over a
    # draft edited or sent since the first attempt.
    if comment is not None and "body" in form:
        body = _field(form, "body")
        if body != ticket.reply_draft:
            request.app.store.set_reply_draft(ticket.id, body)
    request.app.on_wake()
    return _redirect(ticket.id)


def post_state(request: Request) -> Response:
    """Apply one state action allowed from the ticket's current state."""
    match = request.match
    if match is None:
        return _not_found()
    ticket = request.app.store.get(match.group("ticket_id"))
    if ticket is None:
        return _not_found()

    form = _form(request)
    if form is None:
        return _bad_request()
    action = _field(form, "action").strip()
    if not action:
        return _bad_request()

    target = state_action_target(request, ticket, action)
    if target is None:
        # Known action from the wrong state, or no such action.  Either way
        # the request is refused rather than silently ignored.
        return _conflict()

    if not request.app.store.set_state_if(ticket.id, frozenset({ticket.state}), target):
        # Another request changed the state between the read and the write.
        return _conflict()
    request.app.on_wake()
    return _redirect(ticket.id)
