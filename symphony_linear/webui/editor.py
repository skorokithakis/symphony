"""The ticket editor: create and edit Draft tickets, then start them.

``GET /new`` renders an empty editor and writes nothing, as the server's
safe-method contract requires.  A ticket is created by the first ``POST /new``
that carries some content, so an abandoned empty form leaves nothing behind.
``GET /tickets/<id>/edit`` edits a Draft; a started ticket redirects to its
page.

The page is a plain HTML form, so it works without JavaScript:

* **Save** posts the fields and returns to the editor.
* **Start** posts the fields to a separate route, validates them on the
  server, and moves the Draft to In Progress.  Validation errors are rendered
  back into the page with ``aria-invalid`` and ``.field-err``.
* **Discard** posts without a confirmation; the server re-renders the editor
  with the design's ``<dialog class="confirm">`` already open.  The dialog's
  own form carries ``confirm=1`` and is the request that deletes the Draft.
  With JavaScript, ``app.js`` opens the dialog in place instead of posting.
* The repository control is a ``<select>`` over the managed repository list,
  with an "Add a repository…" option that reveals two text fields.  Without
  JavaScript both fields stay visible; either way a non-empty new URL wins
  over the select.  Starting the ticket adds it to the managed list; saving
  and autosave only store it on the Draft.  Any stored repository that is not
  in the list is shown as an extra option, so it is never lost.
* The model control is a native ``<select>``, so it works without JavaScript.
  Its first option, Default, stores no model and means the agent's default;
  the rest are the configured aliases in configuration order.

The title field is shown only when ``webui.anthropic_api_key`` is unset, in
which case Start requires a title (the design's variant B).  With a key, the
editor hides the field (variant A) and Start stores an empty title, then asks
``webui.title`` for an automatic one in the background; the stored empty title
marks that the automatic title may still be written, and both the page and the
orchestrator fall back to the first line of the text until it arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs

from symphony_linear.local_tracker import (
    STATE_DRAFT,
    LocalTicket,
    RepoEntry,
    repo_short_name,
)
from symphony_linear.webui.files import FileChip, chips_for
from symphony_linear.tracker import TrackerNotFoundError
from symphony_linear.webui.http_types import Request, Response
from symphony_linear.webui.templates import render
from symphony_linear.webui.title import schedule_generation
from symphony_linear.webui.views import needs_input_count

#: The states the editor may save, start or discard.  Anything else redirects
#: to the ticket page; a started ticket's description is fixed from then on.
_EDITABLE_STATES = frozenset({STATE_DRAFT})

#: The two title variants.  The mock's wording is copied verbatim.
_TITLE_NOTE = "You do not write a title. Symphony writes it from your text after Start."

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoOption:
    """One repository select option: the stored URL and its display name."""

    url: str
    name: str


@dataclass(frozen=True)
class EditorPage:
    """Everything the editor template needs, already formatted."""

    ticket_id: str | None
    heading: str
    title: str
    description: str
    repo: str
    #: The stored repository list, in list order.
    repos: tuple[RepoOption, ...]
    #: The current value when it is not in the list, so an old Draft keeps it.
    repo_extra: RepoOption | None
    #: A submitted inline repository, preserved when Start validation failed.
    #: When set, the select shows "Add a repository…" instead of ``repo_extra``.
    repo_new_url: str
    repo_new_name: str
    #: The selected model alias, or "" for Default (no model stored).
    model: str
    #: The selectable aliases, in ``models:`` configuration order.
    models: tuple[str, ...]
    has_title_field: bool
    save_action: str
    start_action: str
    discard_action: str
    keep_action: str
    errors: dict[str, str] = field(default_factory=dict)
    discard_open: bool = False
    title_note: str = _TITLE_NOTE
    #: When the Draft was last written, for the initial "Saved 14:38" label.
    #: ``None`` means nothing has been saved yet.
    saved_at: str | None = None
    #: Files already attached to the Draft's text, for a reload.
    files: tuple[FileChip, ...] = field(default_factory=tuple)
    #: Where this editor uploads once the Draft exists.  Empty on ``/new``;
    #: the first upload there uses the separate create-on-upload endpoint.
    upload_action: str = ""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


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


def default_repo(tickets: list[LocalTicket], repos: list[RepoEntry]) -> str:
    """Pick the repository a new ticket should start with.

    The most recent ticket's repository is preferred, but only while it is
    still in the managed list; otherwise the first list entry is used, and an
    empty list leaves the select on "Add a repository…".
    """
    by_url = {entry.url for entry in repos}
    ordered = sorted(tickets, key=lambda ticket: _sort_key(ticket.updated_at))
    for ticket in reversed(ordered):
        value = ticket.repo.strip()
        if value and value in by_url:
            return value
    return repos[0].url if repos else ""


def _repo_options(repos: list[RepoEntry]) -> tuple[RepoOption, ...]:
    return tuple(RepoOption(url=entry.url, name=entry.name) for entry in repos)


def _title_editable(request: Request) -> bool:
    """True when the editor must ask for a title (no automatic-title key)."""
    webui = request.app.config.webui
    if webui is None:  # WebUIServer cannot be built without the block.
        return True
    return not (webui.anthropic_api_key or "").strip()


def action_paths(ticket_id: str | None) -> tuple[str, str, str, str]:
    """Return (save, start, discard, keep) paths for a new or saved editor."""
    if ticket_id is None:
        return "/new", "/new/start", "/new/discard", "/new"
    return (
        f"/tickets/{ticket_id}/edit",
        f"/tickets/{ticket_id}/start",
        f"/tickets/{ticket_id}/discard",
        f"/tickets/{ticket_id}/edit",
    )


def _page(
    request: Request,
    ticket_id: str | None,
    *,
    title: str,
    description: str,
    repo: str,
    model: str | None,
    errors: dict[str, str] | None = None,
    discard_open: bool = False,
    saved_at: str | None = None,
    files: tuple[FileChip, ...] = (),
    repo_new_url: str = "",
    repo_new_name: str = "",
) -> EditorPage:
    save_action, start_action, discard_action, keep_action = action_paths(ticket_id)
    # An empty alias is not a selectable choice: "" is the Default option.
    aliases = tuple(alias for alias in request.app.config.models if alias)
    repos = _repo_options(request.app.store.list_repos())
    repo_extra = (
        RepoOption(url=repo, name=repo_short_name(repo) or repo)
        if repo and not repo_new_url and all(option.url != repo for option in repos)
        else None
    )
    return EditorPage(
        ticket_id=ticket_id,
        heading="New ticket",
        title=title,
        description=description,
        repo=repo,
        repos=repos,
        repo_extra=repo_extra,
        repo_new_url=repo_new_url,
        repo_new_name=repo_new_name,
        # A stored alias the configuration no longer defines reads as Default.
        model=model if model is not None and model in aliases else "",
        models=aliases,
        has_title_field=_title_editable(request),
        save_action=save_action,
        start_action=start_action,
        discard_action=discard_action,
        keep_action=keep_action,
        errors=dict(errors or {}),
        discard_open=discard_open,
        saved_at=saved_at,
        files=files,
        upload_action=(
            f"/api/tickets/{ticket_id}/files" if ticket_id is not None else ""
        ),
    )


def _page_for_ticket(
    request: Request,
    ticket: LocalTicket,
    *,
    errors: dict[str, str] | None = None,
    discard_open: bool = False,
    values: tuple[str, str, str, str | None] | None = None,
    repo_new_url: str = "",
    repo_new_name: str = "",
) -> EditorPage:
    title, description, repo, model = (
        values if values is not None else _ticket_values(ticket)
    )
    return _page(
        request,
        ticket.id,
        title=title,
        description=description,
        repo=repo,
        model=model,
        errors=errors,
        discard_open=discard_open,
        # An autosaved Draft's last write is its "Saved" time on page open.
        saved_at=ticket.updated_at if ticket.state == STATE_DRAFT else None,
        files=chips_for(ticket.id, ticket.files),
        repo_new_url=repo_new_url,
        repo_new_name=repo_new_name,
    )


def _ticket_values(ticket: LocalTicket) -> tuple[str, str, str, str | None]:
    return ticket.title, ticket.description, ticket.repo, ticket.model


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


def new_repo_from_form(form: dict[str, list[str]]) -> tuple[str, str] | None:
    """Return the inline new repository's ``(url, name)``, if a URL was typed."""
    url = _field(form, "repo_new_url").strip()
    if not url:
        return None
    return url, _field(form, "repo_new_name").strip()


def _selected_repo(form: dict[str, list[str]]) -> str:
    """Resolve the repository from the select and the inline add fields.

    A non-empty ``repo_new_url`` wins over the select.  The select's
    "Add a repository…" option has an empty value, so it resolves to an empty
    string until a new URL is typed.  This stores the value on the ticket
    only; the list write happens inside the store's Start methods.
    """
    new_repo = new_repo_from_form(form)
    if new_repo is not None:
        return new_repo[0]
    return _field(form, "repo").strip()


def editor_values(
    request: Request,
    form: dict[str, list[str]],
    ticket: LocalTicket | None,
) -> tuple[str, str, str, str | None]:
    """Read the editable values from *form*, applying the variant rules.

    Variant A renders no title input, so a posted form cannot carry one.  The
    stored title is preserved rather than wiped: a later automatic title or a
    configuration change must not be undone by pressing Save.
    """
    if _title_editable(request):
        title = _field(form, "title").strip()
    else:
        title = ticket.title if ticket is not None else ""

    description = _field(form, "description")
    repo = _selected_repo(form)

    aliases = request.app.config.models
    # Store the alias, not the provider/model id: LocalTracker turns it into
    # the ``Model: <alias>`` label the orchestrator resolves through the same
    # ``models:`` map.  An empty alias is never a choice: it is how the
    # Default option says "no model".
    model = next(
        (value for value in form.get("model", []) if value and value in aliases),
        None,
    )
    return title, description, repo, model


def validation_errors(
    request: Request, title: str, description: str, repo: str
) -> dict[str, str]:
    errors: dict[str, str] = {}
    if _title_editable(request) and not title.strip():
        errors["title"] = "Write a title."
    if not description.strip():
        errors["description"] = "Describe the task."
    if not repo.strip():
        errors["repo"] = "Name the repository to clone."
    return errors


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _render(request: Request, page: EditorPage, *, status: int = 200) -> Response:
    return Response.html(
        render(
            "editor.html",
            app_class="app app--editor",
            page=page,
            needs_input_count=needs_input_count(request),
            dictation_enabled=request.app.dictation_enabled,
        ),
        status=status,
    )


def _redirect(location: str) -> Response:
    return Response(status=303, headers=(("Location", location),))


def _ticket_redirect(ticket_id: str) -> Response:
    return _redirect(f"/tickets/{ticket_id}")


def _not_found() -> Response:
    return Response.text("Not Found\n", status=404)


def _bad_request() -> Response:
    return Response.text("Bad Request\n", status=400)


def _load(request: Request) -> LocalTicket | None:
    match = request.match
    if match is None:
        return None
    return request.app.store.get(match.group("ticket_id"))


# ---------------------------------------------------------------------------
# GET views (read-only)
# ---------------------------------------------------------------------------


def new_page(request: Request) -> Response:
    """Render an empty editor.  Nothing is created until the first Save."""
    return _render(
        request,
        _page(
            request,
            None,
            title="",
            description="",
            repo=default_repo(
                request.app.store.list_tickets(), request.app.store.list_repos()
            ),
            model=None,
        ),
    )


def edit_page(request: Request) -> Response:
    """Render an existing Draft, or send a started ticket to its page."""
    ticket = _load(request)
    if ticket is None:
        return _not_found()
    if ticket.state not in _EDITABLE_STATES:
        return _ticket_redirect(ticket.id)
    return _render(request, _page_for_ticket(request, ticket))


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def post_create_draft(request: Request) -> Response:
    """Create the Draft on the first save that carries any content."""
    form = _form(request)
    if form is None:
        return _bad_request()
    title, description, repo, model = editor_values(request, form, None)

    if not (title or description.strip() or repo or model):
        # An empty form is not a ticket: go back to the same empty editor.
        return _redirect("/new")

    ticket = request.app.store.create_draft(
        title=title,
        description=description,
        repo=repo,
        model=model,
    )
    return _redirect(f"/tickets/{ticket.id}/edit")


def post_save_draft(request: Request) -> Response:
    """Replace the editable fields of an existing Draft."""
    ticket = _load(request)
    if ticket is None:
        return _not_found()
    if ticket.state not in _EDITABLE_STATES:
        return _ticket_redirect(ticket.id)

    form = _form(request)
    if form is None:
        return _bad_request()
    title, description, repo, model = editor_values(request, form, ticket)
    try:
        request.app.store.update_draft(
            ticket.id,
            title=title,
            description=description,
            repo=repo,
            model=model,
        )
    except ValueError:
        # Another request started the ticket between our read and the write.
        return _ticket_redirect(ticket.id)
    return _redirect(f"/tickets/{ticket.id}/edit")


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def post_start_new(request: Request) -> Response:
    """Validate an unsaved editor, create the Draft and start it."""
    form = _form(request)
    if form is None:
        return _bad_request()
    values = editor_values(request, form, None)
    title, description, repo, _model = values

    errors = validation_errors(request, title, description, repo)
    if errors:
        return _render(
            request,
            _page(
                request,
                None,
                title=title,
                description=description,
                repo=repo,
                model=_model,
                errors=errors,
                repo_new_url=_field(form, "repo_new_url").strip(),
                repo_new_name=_field(form, "repo_new_name").strip(),
            ),
            status=400,
        )

    try:
        ticket = request.app.store.create_started_draft(
            title=title,
            description=description,
            repo=repo,
            model=_model,
            new_repo=new_repo_from_form(form),
        )
    except TrackerNotFoundError:
        # A Start that races the Draft's deletion is a 404, not a 500.
        return _not_found()
    schedule_generation(request.app.store, ticket.id, request.app.config)
    request.app.on_wake()
    return _ticket_redirect(ticket.id)


def post_start_draft(request: Request) -> Response:
    """Validate an existing Draft, persist the edits and start it."""
    ticket = _load(request)
    if ticket is None:
        return _not_found()
    if ticket.state not in _EDITABLE_STATES:
        return _ticket_redirect(ticket.id)

    form = _form(request)
    if form is None:
        return _bad_request()
    values = editor_values(request, form, ticket)
    title, description, repo, model = values

    errors = validation_errors(request, title, description, repo)
    if errors:
        return _render(
            request,
            _page_for_ticket(
                request,
                ticket,
                errors=errors,
                values=values,
                repo_new_url=_field(form, "repo_new_url").strip(),
                repo_new_name=_field(form, "repo_new_name").strip(),
            ),
            status=400,
        )

    try:
        request.app.store.start_draft(
            ticket.id,
            title=title,
            description=description,
            repo=repo,
            model=model,
            new_repo=new_repo_from_form(form),
        )
    except TrackerNotFoundError:
        # A Start that races the Draft's deletion is a 404, not a 500.
        return _not_found()
    except ValueError:
        # Another tab started the ticket between the read and the write; the
        # ticket page shows the current state.
        return _ticket_redirect(ticket.id)

    schedule_generation(request.app.store, ticket.id, request.app.config)
    request.app.on_wake()
    return _ticket_redirect(ticket.id)


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


def post_discard_new(request: Request) -> Response:
    """Discard an unsaved editor; nothing is stored to delete."""
    form = _form(request)
    if form is None:
        return _bad_request()
    if _field(form, "confirm") == "1":
        return _redirect("/")
    # Without JavaScript the first post asks for confirmation by re-rendering
    # the editor with the dialog open.
    return _render(
        request,
        _page(
            request,
            None,
            title="",
            description="",
            repo="",
            model=None,
            discard_open=True,
        ),
    )


def post_discard_draft(request: Request) -> Response:
    """Delete a Draft after the confirmation dialog's post."""
    ticket = _load(request)
    if ticket is None:
        return _not_found()
    if ticket.state not in _EDITABLE_STATES:
        return _ticket_redirect(ticket.id)

    form = _form(request)
    if form is None:
        return _bad_request()
    if _field(form, "confirm") == "1":
        try:
            request.app.store.delete_draft(ticket.id)
        except ValueError:
            return _ticket_redirect(ticket.id)
        return _redirect("/")

    return _render(
        request,
        _page_for_ticket(request, ticket, discard_open=True),
    )
