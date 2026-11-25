"""JSON endpoints behind the live pages and the autosave.

Every endpoint here sits behind the same Basic auth and same-origin checks as
the pages: routes are ordinary entries in ``routes.py`` and go through the
server's one dispatch choke point.  The GET endpoints never write persisted
state, like every GET view; the ticket poll's ``active=1`` records viewer
presence in memory only.  The "seen" marker and both kinds of autosave are
POSTs.

HTML is never assembled in JavaScript.  The fragments are rendered from the
same ``_macros.html`` macros as the full pages, and the JSON adds only the
small scalars the script needs to decide whether to touch the DOM.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from symphony_linear.local_tracker import (
    STATE_DRAFT,
    TARGET_DESCRIPTION,
    UPLOAD_TARGETS,
    FileStoreError,
    LocalFile,
    LocalTicket,
    image_content_type,
)
from symphony_linear.tracker import TrackerError, TrackerNotFoundError
from symphony_linear.webui import editor, views
from symphony_linear.webui.http_types import Request, Response
from symphony_linear.webui.templates import render_macro
from symphony_linear.webui.title import schedule_generation
from symphony_linear.webui.ticket import (
    DRAFT_TEXT_STATES,
    MAX_TOKEN_LENGTH,
    snapshot,
    state_action_target,
    ticket_for,
)

logger = logging.getLogger(__name__)

#: Poll responses must never be replayed from the browser's cache.
_NO_STORE = (("Cache-Control", "no-store"),)

#: A maximum on the reply-draft text in characters, well under the 1 MiB body
#: cap.  A note longer than this is a mistake, not a draft.
_MAX_DRAFT_CHARS = 200_000

#: The upload route's own cap.  The server refuses a declared length above
#: this with 413 before reading a single body byte; the route streams the rest
#: to disk, so the default 1 MiB buffered limit stays untouched.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: How much of a streaming upload is held in memory at a time.
_UPLOAD_CHUNK = 64 * 1024


class _ShortUpload(Exception):
    """The client sent fewer bytes than its Content-Length announced."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json(payload: dict[str, Any], *, status: int = 200) -> Response:
    return Response.text(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        content_type="application/json; charset=utf-8",
        headers=_NO_STORE,
    )


def _error(message: str, *, status: int) -> Response:
    return _json({"error": message}, status=status)


def _body(request: Request) -> dict[str, Any] | None:
    """Parse a JSON object body, or return None."""
    if not request.body:
        return None
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    return value if isinstance(value, str) else ""


def _ticket(request: Request) -> LocalTicket | None:
    return ticket_for(request)


# ---------------------------------------------------------------------------
# List polling
# ---------------------------------------------------------------------------


def api_home(request: Request) -> Response:
    """Return the home list fragment, its version and the Needs Input count."""
    page = views.home_list(request)
    return _json(
        {
            "version": page.version,
            "html": render_macro("home_body", page),
            "needs_input_count": page.needs_input_count,
        }
    )


def api_archive(request: Request) -> Response:
    """Return the archive list fragment and the Needs Input count."""
    page = views.archive_list(request)
    return _json(
        {
            "version": page.version,
            "html": render_macro("archive_body", page),
            "needs_input_count": page.needs_input_count,
        }
    )


# ---------------------------------------------------------------------------
# Ticket polling and mutations
# ---------------------------------------------------------------------------


def api_ticket(request: Request) -> Response:
    """Return the ticket's new entries plus the mutable header fragments."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    # The page adds active=1 while it is visible, focused and recently
    # interacted with.  This is deliberately a GET: it records only an
    # in-memory presence timestamp shared with the tracker, never the ticket
    # store, state.json or the seen marker, so it does not break the read-only
    # GET rule.
    if request.query.get("active", [""])[0] == "1" and request.app.presence is not None:
        request.app.presence.mark_active(ticket.id)
    after = request.query.get("after", [""])[0]
    return _json(snapshot(request, ticket, after))


def api_draft_text(request: Request) -> Response:
    """Save the reply composer's text.  Last save wins; there is no merge."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    if ticket.state not in DRAFT_TEXT_STATES:
        return _error("This ticket has no reply composer.", status=409)
    payload = _body(request)
    if payload is None:
        return _error("Bad request.", status=400)
    text = _text(payload, "text")
    if len(text) > _MAX_DRAFT_CHARS:
        return _error("The draft is too long.", status=413)
    updated = request.app.store.set_reply_draft(ticket.id, text)
    return _json({"saved_at": updated.reply_draft_saved_at})


def api_seen(request: Request) -> Response:
    """Advance the ticket's seen marker to a comment the page has rendered."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    payload = _body(request)
    if payload is None:
        return _error("Bad request.", status=400)
    comment_id = _text(payload, "comment_id").strip()
    if not comment_id:
        return _error("A comment id is required.", status=400)
    try:
        request.app.store.set_seen_comment(ticket.id, comment_id)
    except ValueError:
        return _error("Unknown comment.", status=400)
    return _json({"seen": comment_id})


def api_reply(request: Request) -> Response:
    """Append a reply and return a fresh idempotency token for the next one."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    payload = _body(request)
    if payload is None:
        return _error("Bad request.", status=400)
    body = _text(payload, "body").strip()
    token = _text(payload, "token").strip()
    if not body or not token or len(token) > MAX_TOKEN_LENGTH:
        return _error("Write a reply first.", status=400)
    # The allowed-state check is inside the store call, in the same locked
    # transaction as the append.  A retry of an already accepted token still
    # succeeds even if the ticket has moved on since.
    try:
        comment = request.app.store.add_human_comment_once(ticket.id, body, token=token)
    except ValueError:
        return _error(
            "This ticket cannot take a reply now. The agent may be working, "
            "or the ticket may have moved on; reload the page.",
            status=409,
        )
    request.app.on_wake()
    # "duplicate" means this token had already committed a reply: the first
    # attempt's text is the one on the ticket, which may differ from this
    # request's body (a retry after a lost response, with edits in between).
    # The page uses that to decide whether clearing the textarea is safe.
    # The accepted token is rotated for the same reason.
    return _json(
        {
            "token": secrets.token_urlsafe(24),
            "duplicate": comment is None,
        }
    )


def api_state(request: Request) -> Response:
    """Apply one state action; the page then polls for the new fragments."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    payload = _body(request)
    if payload is None:
        return _error("Bad request.", status=400)
    action = _text(payload, "action").strip()
    target = state_action_target(request, ticket, action)
    if target is None:
        return _error("That action is not available now.", status=409)
    if not request.app.store.set_state_if(ticket.id, frozenset({ticket.state}), target):
        return _error("The ticket changed state; reload the page.", status=409)
    request.app.on_wake()
    return _json({"ok": True})


# ---------------------------------------------------------------------------
# Editor autosave and actions
# ---------------------------------------------------------------------------


def _form(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Turn the JSON fields into the form shape the editor helpers expect."""
    return {
        "title": [_text(payload, "title")],
        "description": [_text(payload, "description")],
        "repo": [_text(payload, "repo")],
        "repo_new_url": [_text(payload, "repo_new_url")],
        "repo_new_name": [_text(payload, "repo_new_name")],
        "model": [_text(payload, "model")],
    }


def _saved(ticket: LocalTicket) -> dict[str, Any]:
    """Describe a saved Draft: its URL, the form actions and the API actions.

    The page updates its form actions from this response, so an editor that
    started at ``/new`` behaves like the saved Draft's editor from then on,
    including if the script later fails.
    """
    save_action, start_action, discard_action, keep_action = editor.action_paths(
        ticket.id
    )
    return {
        "id": ticket.id,
        "url": f"/tickets/{ticket.id}/edit",
        "saved_at": ticket.updated_at,
        "actions": {
            "save": save_action,
            "start": start_action,
            "discard": discard_action,
            "keep": keep_action,
            "api_save": f"/api/tickets/{ticket.id}/edit",
            "api_start": f"/api/tickets/{ticket.id}/start",
            "api_discard": f"/api/tickets/{ticket.id}/discard",
            "api_upload": f"/api/tickets/{ticket.id}/files",
        },
    }


def api_create_draft(request: Request) -> Response:
    """Create the Draft on the first autosave that carries any content."""
    payload = _body(request)
    if payload is None:
        return _error("Bad request.", status=400)
    form = _form(payload)
    title, description, repo, model = editor.editor_values(request, form, None)
    if not (title or description.strip() or repo or model):
        # An empty editor stays just an empty editor; nothing is stored.
        return _json({"id": None, "saved_at": None})
    ticket = request.app.store.create_draft(
        title=title,
        description=description,
        repo=repo,
        model=model,
    )
    return _json(_saved(ticket))


def api_save_draft(request: Request) -> Response:
    """Replace the editable fields of an existing Draft."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    if ticket.state != STATE_DRAFT:
        return _error("This ticket is no longer a draft.", status=409)
    payload = _body(request)
    if payload is None:
        return _error("Bad request.", status=400)
    form = _form(payload)
    title, description, repo, model = editor.editor_values(request, form, ticket)
    try:
        updated = request.app.store.update_draft(
            ticket.id,
            title=title,
            description=description,
            repo=repo,
            model=model,
        )
    except ValueError:
        return _error("This ticket is no longer a draft.", status=409)
    return _json(_saved(updated))


def _start_error(errors: dict[str, str]) -> Response:
    return _error(" ".join(errors.values()), status=400)


def api_start_new(request: Request) -> Response:
    """Validate an unsaved editor, create the Draft and start it."""
    payload = _body(request)
    if payload is None:
        return _error("Bad request.", status=400)
    form = _form(payload)
    values = editor.editor_values(request, form, None)
    title, description, repo, model = values
    errors = editor.validation_errors(request, title, description, repo)
    if errors:
        return _start_error(errors)
    try:
        ticket = request.app.store.create_started_draft(
            title=title,
            description=description,
            repo=repo,
            model=model,
            new_repo=editor.new_repo_from_form(form),
        )
    except TrackerNotFoundError:
        # A Start that races the Draft's deletion is a 404, not a 500.
        return _error("Ticket not found.", status=404)
    request.app.on_wake()
    schedule_generation(request.app.store, ticket.id, request.app.config)
    return _json({"redirect": f"/tickets/{ticket.id}"})


def api_start_draft(request: Request) -> Response:
    """Validate a saved Draft, persist the fields and start it."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    if ticket.state != STATE_DRAFT:
        return _json({"redirect": f"/tickets/{ticket.id}"})
    payload = _body(request)
    if payload is None:
        return _error("Bad request.", status=400)
    form = _form(payload)
    values = editor.editor_values(request, form, ticket)
    title, description, repo, model = values
    errors = editor.validation_errors(request, title, description, repo)
    if errors:
        return _start_error(errors)
    try:
        request.app.store.start_draft(
            ticket.id,
            title=title,
            description=description,
            repo=repo,
            model=model,
            new_repo=editor.new_repo_from_form(form),
        )
    except TrackerNotFoundError:
        # A Start that races the Draft's deletion is a 404, not a 500.
        return _error("Ticket not found.", status=404)
    except ValueError:
        # Another tab started the ticket between the read and the write; the
        # ticket page shows the current state.
        return _json({"redirect": f"/tickets/{ticket.id}"})
    request.app.on_wake()
    schedule_generation(request.app.store, ticket.id, request.app.config)
    return _json({"redirect": f"/tickets/{ticket.id}"})


def api_discard_new(request: Request) -> Response:
    """Discard an unsaved editor; nothing is stored to delete."""
    return _json({"redirect": "/"})


def api_discard_draft(request: Request) -> Response:
    """Delete a Draft that the page has already confirmed."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    try:
        request.app.store.delete_draft(ticket.id)
    except ValueError:
        return _json({"redirect": f"/tickets/{ticket.id}"})
    return _json({"redirect": "/"})


# ---------------------------------------------------------------------------
# Uploaded files
# ---------------------------------------------------------------------------


def _upload_name(request: Request) -> str | None:
    """Return the original file name query parameter, or None."""
    name = request.query.get("name", [""])[0]
    return name if name else None


def _upload_target(request: Request) -> str | None:
    """Return the requested upload target, or None when it is not known."""
    value = request.query.get("target", [TARGET_DESCRIPTION])[0]
    return value if value in UPLOAD_TARGETS else None


def _file_json(ticket_id: str, record: LocalFile, target: str) -> dict[str, Any]:
    """Describe a stored file the way the uploader script expects it."""
    return {
        "name": record.name,
        "original_name": record.original_name,
        "size": record.size,
        "url": f"/files/{ticket_id}/{record.name}",
        "delete_url": (f"/api/tickets/{ticket_id}/files/{record.name}?target={target}"),
        "image": image_content_type(record.name) is not None,
    }


def _receive_upload(
    request: Request, ticket_id: str, name: str, target: str
) -> Response:
    """Stream one request body to a reserved file and attach it.

    The body is copied in fixed-size chunks to a temporary file in the
    destination directory and renamed into place only once every announced
    byte has arrived.  A short read, a broken connection or a failed
    registration removes the temporary file, so no partial bytes are left
    behind.
    """
    stream = request.body_stream
    length = request.content_length
    if stream is None or length is None:
        return _error("Bad request.", status=400)

    try:
        upload = request.app.store.new_upload(ticket_id, name, target=target)
    except (ValueError, TrackerError):
        return _error("This ticket cannot take files now.", status=409)
    except (OSError, FileStoreError):
        logger.warning("webui: could not reserve an upload", exc_info=True)
        return _error("The upload could not be saved.", status=500)

    temp_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".upload-", dir=str(upload.path.parent)
        )
        temp_path = Path(raw_path)
        remaining = length
        with os.fdopen(descriptor, "wb") as handle:
            while remaining > 0:
                chunk = stream.read(min(remaining, _UPLOAD_CHUNK))
                if not chunk:
                    raise _ShortUpload
                handle.write(chunk)
                remaining -= len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, upload.path)
        temp_path = None
    except _ShortUpload:
        return _error("The upload ended early. Try again.", status=400)
    except OSError:
        logger.warning("webui: failed to receive an upload", exc_info=True)
        return _error("The upload could not be saved.", status=500)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "webui: could not remove a partial upload", exc_info=True
                )

    try:
        record = request.app.store.register_file(upload, length)
    except (ValueError, TrackerError):
        return _error("This ticket cannot take files now.", status=409)
    except OSError:
        logger.warning("webui: failed to register an upload", exc_info=True)
        return _error("The upload could not be saved.", status=500)

    ticket = request.app.store.get(ticket_id)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    payload = _saved(ticket)
    payload["file"] = _file_json(ticket_id, record, target)
    return _json(payload)


def api_upload_file(request: Request) -> Response:
    """Attach one streamed upload to an existing ticket.

    ``?name=`` carries the original file name and ``?target=description`` or
    ``?target=reply`` selects the Draft's text or the pending reply.
    """
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    name = _upload_name(request)
    if name is None:
        return _error("A file name is required.", status=400)
    target = _upload_target(request)
    if target is None:
        return _error("Unknown upload target.", status=400)
    return _receive_upload(request, ticket.id, name, target)


def api_upload_new_file(request: Request) -> Response:
    """Create the Draft from the first upload made on ``/new``.

    The first autosave already creates the Draft when the editor has content;
    an upload has no such content to send, so it creates the empty Draft
    itself.  If the upload fails, the new empty Draft is removed again rather
    than left behind.
    """
    name = _upload_name(request)
    if name is None:
        return _error("A file name is required.", status=400)
    ticket = request.app.store.create_draft()
    succeeded = False
    try:
        response = _receive_upload(request, ticket.id, name, TARGET_DESCRIPTION)
        succeeded = response.status == 200
        return response
    finally:
        if not succeeded:
            # The upload may fail after the Draft was created.  Remove the
            # placeholder, but never a Draft that was edited or gained a file
            # in the meantime; that check is under the store's lock.
            try:
                request.app.store.delete_draft_if_untouched(ticket.id)
            except Exception:
                logger.warning(
                    "webui: could not remove the Draft of a failed upload",
                    exc_info=True,
                )


def api_delete_file(request: Request) -> Response:
    """Delete one pending or description file and its bytes."""
    ticket = _ticket(request)
    if ticket is None:
        return _error("Ticket not found.", status=404)
    match = request.match
    if match is None:
        return _error("That file is no longer attached.", status=404)
    try:
        stored_name = unquote(match.group("name"), errors="strict")
    except UnicodeDecodeError:
        return _error("Bad request.", status=400)
    target = _upload_target(request)
    if target is None:
        return _error("Unknown upload target.", status=400)
    try:
        request.app.store.delete_file(ticket.id, stored_name, target=target)
    except FileNotFoundError:
        return _error("That file is no longer attached.", status=404)
    except (ValueError, TrackerError):
        return _error("That file cannot be removed now.", status=409)
    return _json({"ok": True})
