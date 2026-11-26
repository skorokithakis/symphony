"""JSON API for the user's personal agent, mounted at ``/api/v1``.

This is a second authentication surface, but not a second server: the
``/api/v1`` routes are ordinary entries in :mod:`symphony_linear.webui.routes`
and every request still goes through the single dispatch choke point in
:mod:`symphony_linear.webui.server`.  Only the credential differs.  The agent
runs outside the sandbox and cannot use the browser's Basic auth credentials,
and the daemon must not trust a header a sandboxed agent could forge, so the
API uses a bearer token from ``webui.api_token`` in ``config.yaml`` -- a file
the sandbox masks from the coding agents.

The server deliberately skips the same-origin check for a bearer-authenticated
``/api/v1`` request.  That check exists because browsers attach Basic
credentials to cross-site requests automatically; they never attach a bearer
token by themselves, so a cross-site request has nothing to ride and the
check would only break non-browser clients.  Basic credentials are not
accepted on ``/api/v1``, and the bearer token is not accepted anywhere else.

One request in the namespace needs no credential at all: a GET or HEAD of the
exact path ``/api/v1``, which is the guide (``agent_api.md``).  An agent can
read it before anyone gives it the token, and it carries no ticket data.  The
exemption lives in the server's dispatch choke point, matches the raw path
exactly, and is skipped when ``webui.api_token`` is unset so an unconfigured
daemon still answers ``404`` for the whole namespace.

Everything else still applies to these routes: the per-route body cap, the
socket read timeout and the security headers the server emits for every
response.  Errors are JSON in one shape, ``{"error": ..., "hint": ...}``, and
the hint always says what to do next.

The endpoints deliberately reuse the browser UI's rules rather than restating
them: :func:`symphony_linear.webui.editor.validation_errors` decides whether a
ticket may start, :func:`symphony_linear.webui.ticket.state_action_target`
decides which state action is legal now, and ``LocalTicketStore`` enforces the
Draft-only and reply-state rules inside its own locked transactions.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony_linear.local_tracker import (
    REPLY_STATES,
    STATE_DONE,
    STATE_DRAFT,
    TICKET_STATES,
    DraftValidationError,
    LocalComment,
    LocalTicket,
    RepoEntry,
)
from symphony_linear.state import TicketState
from symphony_linear.tracker import TrackerNotFoundError, is_bot_comment
from symphony_linear.webui import editor
from symphony_linear.webui.http_types import Request, Response
from symphony_linear.webui.ticket import state_action_target
from symphony_linear.webui.title import schedule_generation
from symphony_linear.webui.views import display_title

#: The whole namespace, and nothing else: a percent-encoded alias such as
#: ``/%61pi/v1`` is not this prefix, so it falls through to Basic auth and the
#: ordinary 404 instead of widening the API surface.
API_PREFIX = "/api/v1"

#: The guide is a file so it can be edited without touching Python code, and
#: it is read per request for the same reason the static assets are statted
#: per request: an edit then needs no daemon restart.
_GUIDE_PATH = Path(__file__).resolve().parent / "agent_api.md"

#: Every hint that is not specific to one call says where the full answer is.
_GUIDE_HINT = "GET /api/v1 returns the guide, which documents every endpoint."

#: The state-action vocabulary, in a fixed order only so the JSON is stable.
#: Whether one is legal is answered by ``state_action_target`` against the
#: ticket page's own transition table; this tuple deliberately holds no
#: state/action pairs for that reason.
_STATE_ACTION_NAMES = ("stop", "needs_input", "qa", "done", "reopen")

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"

#: The tracker adapter's visible footer: "\n\n*Symphony · <kind>*".  It is
#: anchored at the end so a quoted footer earlier in the body cannot win, and
#: the kind may itself contain " · model: ..." or be "context: N tokens".
_FOOTER_KIND = re.compile(r"\n\n\*Symphony · ([^*]*)\*[ \t]*\Z")


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Responses and errors
# ---------------------------------------------------------------------------


def is_agent_api_path(path: str) -> bool:
    """Return whether *path* belongs to the bearer-authenticated API."""
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def json_response(
    payload: Mapping[str, Any],
    *,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] = (),
) -> Response:
    """Render a JSON object response.

    Agent API responses are never cacheable: they describe live daemon state
    and some of them carry a ticket's text.
    """
    return Response.text(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        content_type=_JSON_CONTENT_TYPE,
        headers=(("Cache-Control", "no-store"), *headers),
    )


def error_response(
    error: str,
    hint: str,
    *,
    status: int,
    headers: tuple[tuple[str, str], ...] = (),
) -> Response:
    """Render the API's one error shape: ``{"error": ..., "hint": ...}``.

    The hint is always present so a caller that only reads ``error`` still
    sees what to do next, and so a wrong token or a refused state never leaves
    the client guessing.
    """
    return json_response({"error": error, "hint": hint}, status=status, headers=headers)


def _bad_body() -> Response:
    return error_response(
        "A JSON object body is required.",
        f"Send Content-Type: application/json and a JSON object; {_GUIDE_HINT}",
        status=400,
    )


def _bad_field(name: str, expected: str) -> Response:
    return error_response(
        f"'{name}' is not valid.",
        f"Send '{name}' as {expected}. {_GUIDE_HINT}",
        status=400,
    )


def _no_ticket(ticket_id: str) -> Response:
    return error_response(
        f"No ticket {ticket_id}.",
        f"GET /api/v1/tickets lists the ticket ids that exist. {_GUIDE_HINT}",
        status=404,
    )


def _not_a_draft(ticket: LocalTicket, operation: str) -> Response:
    return error_response(
        f"Ticket {ticket.id} is in {ticket.state}, not Draft.",
        f"{operation} applies to Draft tickets only. "
        f"GET /api/v1/tickets/{ticket.id} shows its current state and "
        f"allowed_actions.",
        status=409,
    )


def _comment_refusal(ticket: LocalTicket) -> Response:
    """Explain why a comment was refused, based on the *current* state."""
    if ticket.state == STATE_DRAFT:
        return error_response(
            f"Ticket {ticket.id} is a Draft and the agent has not seen it.",
            f"Start it first: POST /api/v1/tickets/{ticket.id}/start. "
            "Comments are accepted in Needs Input and QA.",
            status=409,
        )
    if ticket.state == STATE_DONE:
        return error_response(
            f"Ticket {ticket.id} is Done.",
            f"Reopen it first: POST /api/v1/tickets/{ticket.id}/actions with "
            '{"action": "reopen"}. Comments are accepted in Needs Input and QA.',
            status=409,
        )
    return error_response(
        f"Ticket {ticket.id} cannot take a comment while it is in {ticket.state}.",
        "Comments are accepted in Needs Input and QA only; the agent is "
        "likely still working. Poll "
        f'GET /api/v1/tickets/{ticket.id} until state is "Needs Input" '
        'or "QA", then post the comment again.',
        status=409,
    )


def _validation_error(errors: Mapping[str, str], retry: str) -> Response:
    return error_response(
        " ".join(errors.values()),
        retry,
        status=400,
    )


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _object_body(request: Request) -> dict[str, Any] | None:
    """Parse a JSON object body, or return ``None``."""
    if not request.body:
        return None
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _text(payload: Mapping[str, Any], name: str) -> str:
    """Return a string field, or ``""`` for a missing or non-string one."""
    value = payload.get(name)
    return value if isinstance(value, str) else ""


def _optional_text(
    payload: Mapping[str, Any], name: str
) -> tuple[str | None, Response | None]:
    """Return an optional string field; ``None`` means "not in the body".

    An explicit JSON ``null`` is an empty string, so a caller can clear a
    field without guessing.  A wrong type is an error rather than a silent
    empty value, because the agent is likely to be writing these by hand.
    """
    if name not in payload:
        return None, None
    value = payload[name]
    if value is None:
        return "", None
    if not isinstance(value, str):
        return None, _bad_field(name, "a string")
    return value, None


def _model_value(
    request: Request, payload: Mapping[str, Any]
) -> tuple[str | None, Response | None]:
    """Resolve the ``model`` field to a configured alias or ``None``.

    This is the editor's rule: the stored value is the alias the ticket was
    created with, only configured aliases are accepted, and an empty value
    means the agent's own default.  The browser select silently falls back to
    Default for an alias the configuration no longer defines; an API caller
    gets a 400 instead, since a model it asked for and did not get is worse
    than an error.
    """
    if "model" not in payload:
        return None, None
    value = payload["model"]
    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        return None, _bad_field("model", "a configured alias, or null for the default")
    if value not in request.app.config.models:
        return None, error_response(
            "Unknown model alias.",
            _model_hint(request),
            status=400,
        )
    return value, None


def _model_hint(request: Request) -> str:
    aliases = [alias for alias in request.app.config.models if alias]
    if not aliases:
        return (
            "No model aliases are configured; omit 'model' to use the agent's "
            f"default. {_GUIDE_HINT}"
        )
    return (
        f"Use one of: {', '.join(aliases)}. Omit 'model' to use the agent's "
        f"default. GET /api/v1/models lists them."
    )


def _timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, treating a naive one as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Ticket views
# ---------------------------------------------------------------------------


def _state_entry(request: Request, ticket_id: str) -> TicketState | None:
    """Read the ticket's StateManager entry without mutating anything."""
    for entry in list(request.app.state.tickets):
        if entry.ticket_id == ticket_id:
            return entry
    return None


def _available_state_actions(request: Request, ticket: LocalTicket) -> list[str]:
    return [
        name
        for name in _STATE_ACTION_NAMES
        if state_action_target(request, ticket, name) is not None
    ]


def _allowed_actions(request: Request, ticket: LocalTicket) -> list[str]:
    """List what the caller may do to *ticket* right now.

    The state actions come from the ticket page's transition table through
    ``state_action_target``, so this can never drift from what the buttons on
    the page offer.  The non-state actions are the Draft-only editor
    operations and the reply rule, both taken from the same constants the
    store enforces.
    """
    actions = _available_state_actions(request, ticket)
    if ticket.state in REPLY_STATES:
        actions.append("reply")
    if ticket.state == STATE_DRAFT:
        actions.append("edit")
        # Start is offered only when the editor's own validation would accept
        # the Draft, so a caller never spends a round trip on a 400.
        if not editor.validation_errors(
            request, ticket.title, ticket.description, ticket.repo
        ):
            actions.append("start")
        actions.append("delete")
    return actions


def _summary(request: Request, ticket: LocalTicket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "title": display_title(ticket),
        "state": ticket.state,
        "repo": ticket.repo.strip(),
        "model": ticket.model,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "state_changed_at": ticket.state_changed_at,
        "allowed_actions": _allowed_actions(request, ticket),
    }


def _comment_json(comment: LocalComment) -> dict[str, Any]:
    # The body stays exactly as stored, footer included: that footer is what
    # is_bot_comment reads, and stripping it here would give the API a second
    # copy of the UI's display logic.  ``kind`` is the footer label, because
    # some kinds (``context: ... tokens · model: ...``) are what tells a
    # polling client whether the turn ended.
    agent = is_bot_comment(comment.body)
    kind: str | None = None
    if agent:
        match = _FOOTER_KIND.search(comment.body)
        kind = match.group(1) if match is not None else None
    return {
        "id": comment.id,
        "author": "agent" if agent else "human",
        "kind": kind,
        "body": comment.body,
        "created_at": comment.created_at,
    }


def _detail(request: Request, ticket: LocalTicket) -> dict[str, Any]:
    entry = _state_entry(request, ticket.id)
    payload = _summary(request, ticket)
    payload["description"] = ticket.description
    payload["agent_status"] = entry.status.value if entry is not None else None
    payload["comments"] = [_comment_json(comment) for comment in ticket.comments]
    return payload


def _ticket_id(request: Request) -> str:
    match = request.match
    assert match is not None
    return match.group("ticket_id")


def _ticket(request: Request) -> LocalTicket | None:
    return request.app.store.get(_ticket_id(request))


def _sorted_by_update(tickets: list[LocalTicket]) -> list[LocalTicket]:
    """Order tickets newest-updated first, like the home page."""
    return sorted(
        tickets,
        key=lambda ticket: _timestamp(ticket.updated_at) or _EPOCH,
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


def api_tickets(request: Request) -> Response:
    """List ticket summaries, filtered by state and update time."""
    state = request.query.get("state", [""])[0].strip()
    if state and state not in TICKET_STATES:
        return error_response(
            "Unknown state.",
            f"'state' must be one of: {', '.join(TICKET_STATES)}. "
            "Use curl's -G with --data-urlencode so a space is encoded.",
            status=400,
        )

    since_raw = request.query.get("updated_since", [""])[0].strip()
    since: datetime | None = None
    if since_raw:
        since = _timestamp(since_raw)
        if since is None:
            return error_response(
                "Invalid updated_since.",
                "Send an ISO-8601 timestamp with a timezone, such as "
                "2026-11-25T12:00:00+00:00 or 2026-11-25T12:00:00Z.",
                status=400,
            )

    tickets = request.app.store.list_tickets()
    if state:
        tickets = [ticket for ticket in tickets if ticket.state == state]
    if since is not None:
        tickets = [
            ticket
            for ticket in tickets
            if (_timestamp(ticket.updated_at) or _EPOCH) > since
        ]
    return json_response(
        {
            "tickets": [
                _summary(request, ticket) for ticket in _sorted_by_update(tickets)
            ]
        }
    )


def api_ticket_detail(request: Request) -> Response:
    """Return one ticket's summary, description, agent status and comments."""
    ticket = _ticket(request)
    if ticket is None:
        return _no_ticket(_ticket_id(request))
    return json_response(_detail(request, ticket))


def api_ticket_create(request: Request) -> Response:
    """Create a Draft, or create and start it when ``start`` is true."""
    payload = _object_body(request)
    if payload is None:
        return _bad_body()

    description, description_error = _optional_text(payload, "description")
    if description_error is not None:
        return description_error
    repo, repo_error = _optional_text(payload, "repo")
    if repo_error is not None:
        return repo_error
    title, title_error = _optional_text(payload, "title")
    if title_error is not None:
        return title_error
    description = description or ""
    repo = (repo or "").strip()
    title = (title or "").strip()

    if not description.strip():
        return error_response(
            "A ticket needs a description.",
            'Send {"description": "what the agent should do", '
            '"repo": "owner/name"}. Add "start": true to start it '
            "immediately. GET /api/v1 returns the guide.",
            status=400,
        )

    model, model_error = _model_value(request, payload)
    if model_error is not None:
        return model_error

    start = payload.get("start", False)
    if not isinstance(start, bool):
        return _bad_field("start", "true or false")

    if start:
        errors = editor.validation_errors(request, title, description, repo)
        if errors:
            return _validation_error(
                errors,
                "Send a corrected POST /api/v1/tickets with the same fields "
                'and "start": true.',
            )
        ticket = request.app.store.create_started_draft(
            title=title,
            description=description,
            repo=repo,
            model=model,
            # Start is the only path that adds to the managed repository
            # list, exactly like the editor's Start; an empty repo was
            # rejected above by validation_errors.
            new_repo=(repo, ""),
        )
        schedule_generation(request.app.store, ticket.id, request.app.config)
        request.app.on_wake()
        return json_response(_detail(request, ticket), status=201)

    ticket = request.app.store.create_draft(
        title=title,
        description=description,
        repo=repo,
        model=model,
    )
    return json_response(_detail(request, ticket), status=201)


def api_ticket_edit(request: Request) -> Response:
    """Replace any of a Draft's fields; omitted fields keep their value."""
    ticket = _ticket(request)
    if ticket is None:
        return _no_ticket(_ticket_id(request))
    if ticket.state != STATE_DRAFT:
        return _not_a_draft(ticket, "Editing")

    payload = _object_body(request)
    if payload is None:
        return _bad_body()

    title, title_error = _optional_text(payload, "title")
    if title_error is not None:
        return title_error
    description, description_error = _optional_text(payload, "description")
    if description_error is not None:
        return description_error
    repo, repo_error = _optional_text(payload, "repo")
    if repo_error is not None:
        return repo_error
    model, model_error = _model_value(request, payload)
    if model_error is not None:
        return model_error

    changes: dict[str, str | None] = {}
    if title is not None:
        changes["title"] = title
    if description is not None:
        if not description.strip():
            return error_response(
                "A ticket needs a description.",
                f"Send a non-empty 'description' to keep ticket {ticket.id} "
                "usable. GET /api/v1 returns the guide.",
                status=400,
            )
        changes["description"] = description
    if repo is not None:
        changes["repo"] = repo
    if "model" in payload:
        changes["model"] = model

    try:
        updated = request.app.store.update_draft_fields(ticket.id, changes)
    except TrackerNotFoundError:
        return _no_ticket(ticket.id)
    except ValueError:
        current = request.app.store.get(ticket.id) or ticket
        return _not_a_draft(current, "Editing")
    return json_response(_detail(request, updated))


def api_ticket_delete(request: Request) -> Response:
    """Delete a Draft and its files.  Started tickets are refused."""
    ticket = _ticket(request)
    if ticket is None:
        return _no_ticket(_ticket_id(request))
    if ticket.state != STATE_DRAFT:
        return _not_a_draft(ticket, "Deleting")
    try:
        request.app.store.delete_draft(ticket.id)
    except TrackerNotFoundError:
        return _no_ticket(ticket.id)
    except ValueError:
        current = request.app.store.get(ticket.id) or ticket
        return _not_a_draft(current, "Deleting")
    return json_response({"deleted": ticket.id})


def api_ticket_start(request: Request) -> Response:
    """Start a Draft, validating and transitioning under the store lock."""
    ticket = _ticket(request)
    if ticket is None:
        return _no_ticket(_ticket_id(request))
    if ticket.state != STATE_DRAFT:
        return _not_a_draft(ticket, "Starting")

    def validate(stored: LocalTicket) -> Mapping[str, str]:
        # Runs under the store lock, on the fields the ticket holds at that
        # moment, so a concurrent PATCH cannot leave an invalid Draft started.
        return editor.validation_errors(
            request, stored.title, stored.description, stored.repo
        )

    try:
        started = request.app.store.start_draft_stored(
            ticket.id,
            add_repo=True,
            validate=validate,
            model_aliases=frozenset(request.app.config.models),
        )
    except DraftValidationError as exc:
        hint = (
            f"PATCH /api/v1/tickets/{ticket.id} to fix the Draft, then send "
            "POST .../start again. A title is required unless automatic "
            "titles are enabled."
        )
        aliases = [alias for alias in request.app.config.models if alias]
        if aliases:
            hint += " Configured model aliases: " + ", ".join(aliases) + "."
        return _validation_error(exc.errors, hint)
    except TrackerNotFoundError:
        return _no_ticket(ticket.id)
    except ValueError:
        current = request.app.store.get(ticket.id) or ticket
        return _not_a_draft(current, "Starting")
    schedule_generation(request.app.store, started.id, request.app.config)
    request.app.on_wake()
    return json_response(_detail(request, started))


def api_ticket_action(request: Request) -> Response:
    """Apply one state action from the ticket's ``allowed_actions``."""
    ticket = _ticket(request)
    if ticket is None:
        return _no_ticket(_ticket_id(request))

    payload = _object_body(request)
    if payload is None:
        return _bad_body()
    action = _text(payload, "action").strip()
    if not action:
        available = _available_state_actions(request, ticket)
        return error_response(
            "An action is required.",
            f'Send {{"action": one of {", ".join(available) or "..."}}}. {_GUIDE_HINT}',
            status=400,
        )

    target = state_action_target(request, ticket, action)
    if target is None:
        available = _available_state_actions(request, ticket)
        return error_response(
            f"'{action}' is not available for ticket {ticket.id} while it is "
            f"in {ticket.state}.",
            f"Allowed state actions now: {', '.join(available) or 'none'}. "
            f"GET /api/v1/tickets/{ticket.id} lists allowed_actions with every "
            "call you can make.",
            status=409,
        )
    try:
        moved = request.app.store.set_state_if(
            ticket.id, frozenset({ticket.state}), target
        )
    except TrackerNotFoundError:
        return _no_ticket(ticket.id)
    if not moved:
        current = request.app.store.get(ticket.id) or ticket
        return error_response(
            f"Ticket {ticket.id} changed state while the action was applied.",
            f"It is now in {current.state}. GET /api/v1/tickets/{ticket.id} "
            f"and retry from the allowed_actions it lists.",
            status=409,
        )
    request.app.on_wake()
    updated = request.app.store.get(ticket.id)
    if updated is None:
        return _no_ticket(ticket.id)
    return json_response(_detail(request, updated))


def api_ticket_comment(request: Request) -> Response:
    """Append a human comment, using the same reply rule as the page's Send.

    The state check lives inside the store's locked append, so a turn that
    starts between this read and the write still refuses the comment.  The
    tracker state is deliberately not changed: the orchestrator's resume
    pipeline moves the ticket when it picks the comment up.
    """
    ticket = _ticket(request)
    if ticket is None:
        return _no_ticket(_ticket_id(request))
    payload = _object_body(request)
    if payload is None:
        return _bad_body()
    body = _text(payload, "body").strip()
    if not body:
        return error_response(
            "A comment needs a body.",
            f'Send {{"body": "your reply"}}. {_GUIDE_HINT}',
            status=400,
        )

    try:
        # The browser's saved reply text and pending attachments belong to the
        # browser, not to this caller: consume_composer stays False so an API
        # reply leaves both exactly as they were.  A server-generated token is
        # enough for the store's signature; the API has no idempotency key.
        comment = request.app.store.add_human_comment_once(
            ticket.id,
            body,
            token=secrets.token_urlsafe(24),
            consume_composer=False,
        )
    except TrackerNotFoundError:
        return _no_ticket(ticket.id)
    except ValueError:
        current = request.app.store.get(ticket.id) or ticket
        return _comment_refusal(current)
    request.app.on_wake()
    if comment is None:
        # Unreachable with a fresh random token, but keep the return type
        # honest instead of inventing a comment that was not stored.
        return error_response(
            "The comment was not stored.",
            f"Read the ticket again: GET /api/v1/tickets/{ticket.id}.",
            status=409,
        )
    return json_response(_comment_json(comment), status=201)


# ---------------------------------------------------------------------------
# Repositories and models
# ---------------------------------------------------------------------------


def _repo_json(entry: RepoEntry) -> dict[str, Any]:
    return {"url": entry.url, "name": entry.name}


def api_repos(request: Request) -> Response:
    """List the managed repository entries."""
    return json_response(
        {"repos": [_repo_json(entry) for entry in request.app.store.list_repos()]}
    )


def api_repo_add(request: Request) -> Response:
    """Add a repository to the managed list.  A duplicate URL is a no-op."""
    payload = _object_body(request)
    if payload is None:
        return _bad_body()
    url = _text(payload, "url").strip()
    if not url:
        return error_response(
            "A repository URL is required.",
            'Send {"url": "owner/name"} or a full clone URL, with an '
            'optional "name". GET /api/v1 returns the guide.',
            status=400,
        )
    entry = request.app.store.add_repo(url, _text(payload, "name"))
    return json_response(_repo_json(entry), status=201)


def api_repo_rename(request: Request) -> Response:
    """Rename a managed repository's label.  Its URL and tickets do not change."""
    payload = _object_body(request)
    if payload is None:
        return _bad_body()
    url = _text(payload, "url").strip()
    name, name_error = _optional_text(payload, "name")
    if name_error is not None:
        return name_error
    if not url:
        return error_response(
            "A repository URL is required.",
            'Send {"url": "the stored URL", "name": "the new label"}. '
            "GET /api/v1/repos lists the stored URLs.",
            status=400,
        )
    if name is None:
        return _bad_field("name", "the new label")
    try:
        entry = request.app.store.rename_repo(url, name.strip())
    except TrackerNotFoundError:
        return error_response(
            f"No managed repository for {url}.",
            "GET /api/v1/repos lists the stored URLs; POST /api/v1/repos "
            "adds a missing one. Tickets are never renamed by this call.",
            status=404,
        )
    return json_response(_repo_json(entry))


def api_repo_delete(request: Request) -> Response:
    """Remove a repository from the managed list, leaving tickets untouched."""
    url = request.query.get("url", [""])[0].strip()
    if not url:
        return error_response(
            "A repository URL is required.",
            "Send DELETE /api/v1/repos?url=<stored URL>. GET /api/v1/repos "
            "lists the stored URLs.",
            status=400,
        )
    request.app.store.remove_repo(url)
    return json_response({"deleted": url})


def api_models(request: Request) -> Response:
    """List the configured model aliases."""
    return json_response(
        {"models": [alias for alias in request.app.config.models if alias]}
    )


# ---------------------------------------------------------------------------
# Guide
# ---------------------------------------------------------------------------


def guide(request: Request) -> Response:
    """Return the integration guide for the agent API as Markdown."""
    return Response.text(
        _GUIDE_PATH.read_text(encoding="utf-8"),
        content_type="text/markdown; charset=utf-8",
    )
