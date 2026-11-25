"""The managed repository list page.

The list lives in the ticket store's ``repos.json``; this page only adds and
removes entries.  Tickets keep the repository string they were created with,
so removing an entry never changes a ticket.

The add form posts to ``/repos`` and each Remove button posts to
``/repos/remove``; both are ordinary POSTs through the server's dispatch, so
they get the Basic auth and same-origin checks like every other write.  The
GET view is read-only: seeding the list happens when the store loads at
startup, never here.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs

from symphony_linear.webui.http_types import Request, Response
from symphony_linear.webui.templates import render
from symphony_linear.webui.views import needs_input_count


@dataclass(frozen=True)
class RepoRow:
    url: str
    name: str


@dataclass(frozen=True)
class ReposPage:
    repos: tuple[RepoRow, ...]
    error: str = ""
    new_url: str = ""
    new_name: str = ""


def _form(request: Request) -> dict[str, list[str]] | None:
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


def _page(
    request: Request,
    *,
    error: str = "",
    new_url: str = "",
    new_name: str = "",
) -> ReposPage:
    return ReposPage(
        repos=tuple(
            RepoRow(url=entry.url, name=entry.name)
            for entry in request.app.store.list_repos()
        ),
        error=error,
        new_url=new_url,
        new_name=new_name,
    )


def _render(request: Request, page: ReposPage, *, status: int = 200) -> Response:
    return Response.html(
        render(
            "repos.html",
            app_class="app app--home",
            page=page,
            needs_input_count=needs_input_count(request),
            push_public_key=request.app.push_public_key,
        ),
        status=status,
    )


def repos_page(request: Request) -> Response:
    """List the managed repositories.  Writes nothing."""
    return _render(request, _page(request))


def post_add_repo(request: Request) -> Response:
    """Add one repository and return to the list."""
    form = _form(request)
    if form is None:
        return Response.text("Bad Request\n", status=400)
    url = _field(form, "url").strip()
    name = _field(form, "name")
    if not url:
        return _render(
            request,
            _page(request, error="Enter a repository URL.", new_url=url, new_name=name),
            status=400,
        )
    request.app.store.add_repo(url, name)
    return _redirect("/repos")


def post_remove_repo(request: Request) -> Response:
    """Remove one repository from the list, leaving tickets untouched."""
    form = _form(request)
    if form is None:
        return Response.text("Bad Request\n", status=400)
    request.app.store.remove_repo(_field(form, "url"))
    return _redirect("/repos")


def _redirect(location: str) -> Response:
    return Response(status=303, headers=(("Location", location),))
