"""The web UI route table.

This is the only place a new page needs to be registered.  Every route is
resolved by the single request pipeline in :mod:`symphony_linear.webui.server`,
which authenticates the request (except GET/HEAD on a route marked ``public``),
enforces same-origin on non-GET/HEAD methods and adds the security headers to
the response.  Adding an entry here cannot skip any of that.

A view is ``Callable[[Request], Response]``.  Use a named group in the pattern
to read a path parameter, e.g. ``r"^/tickets/(?P<ticket_id>[^/]+)$"`` and
``request.match.group("ticket_id")``.

GET and HEAD views are read-only.  They bypass the same-origin check because
they are safe methods, and HEAD is dispatched to the GET view, so a GET view
must never mutate the ticket store, ``state.json`` or anything else that
persists.  Any save, state action or persisted "seen" marker belongs on a
POST/PUT/PATCH/DELETE route, which is same-origin guarded.

For a route that accepts a large body, set ``max_body_bytes`` and
``stream_body=True``.  The view then receives the unread ``body_stream`` plus
``content_length`` and must consume exactly that many bytes; the two file
upload routes are the only ones that do this today.
"""

from __future__ import annotations

import re

from symphony_linear.local_tracker import PUBLIC_ICON_PATH
from symphony_linear.webui.api import (
    MAX_UPLOAD_BYTES,
    api_archive,
    api_create_draft,
    api_delete_file,
    api_discard_draft,
    api_discard_new,
    api_draft_text,
    api_home,
    api_reply,
    api_save_draft,
    api_seen,
    api_start_draft,
    api_start_new,
    api_state,
    api_ticket,
    api_upload_file,
    api_upload_new_file,
)
from symphony_linear.webui.editor import (
    edit_page,
    new_page,
    post_create_draft,
    post_discard_draft,
    post_discard_new,
    post_save_draft,
    post_start_draft,
    post_start_new,
)
from symphony_linear.webui.files import serve_file
from symphony_linear.webui.http_types import Route
from symphony_linear.webui.repos import (
    post_add_repo,
    post_remove_repo,
    repos_page,
)
from symphony_linear.webui.static import serve_public, serve_static
from symphony_linear.webui.ticket import post_reply, post_state, ticket_page
from symphony_linear.webui.views import archive, home

_TICKET_EDIT = re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/edit$")
_TICKET_START = re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/start$")
_TICKET_DISCARD = re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/discard$")

_API_TICKET = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)$")
_API_TICKET_EDIT = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/edit$")
_API_TICKET_START = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/start$")
_API_TICKET_DISCARD = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/discard$")
_API_TICKET_DRAFT = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/draft$")
_API_TICKET_SEEN = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/seen$")
_API_TICKET_REPLY = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/reply$")
_API_TICKET_STATE = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/state$")
_API_TICKET_FILES = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/files$")
_API_TICKET_FILE = re.compile(
    r"^/api/tickets/(?P<ticket_id>[^/]+)/files/(?P<name>[^/]+)$"
)

_FILES = re.compile(r"^/files/(?P<ticket_id>[^/]+)/(?P<name>[^/]+)$")

# Built from the one URL constant rather than written out here, so the route
# matches exactly the icon the favicon and the ntfy payload point at.  An
# unknown /public/... path is not a route at all, and therefore still needs
# credentials like everything else.
_PUBLIC_ICON = re.compile(r"^" + re.escape(PUBLIC_ICON_PATH) + r"$")

ROUTES: tuple[Route, ...] = (
    Route("GET", re.compile(r"^/$"), home),
    Route("GET", re.compile(r"^/archive$"), archive),
    Route("GET", re.compile(r"^/repos$"), repos_page),
    Route("POST", re.compile(r"^/repos$"), post_add_repo),
    Route("POST", re.compile(r"^/repos/remove$"), post_remove_repo),
    Route("GET", re.compile(r"^/new$"), new_page),
    Route("GET", _TICKET_EDIT, edit_page),
    Route("GET", re.compile(r"^/tickets/(?P<ticket_id>[^/]+)$"), ticket_page),
    Route("GET", _FILES, serve_file),
    Route("POST", re.compile(r"^/new$"), post_create_draft),
    Route("POST", _TICKET_EDIT, post_save_draft),
    Route("POST", re.compile(r"^/new/start$"), post_start_new),
    Route("POST", _TICKET_START, post_start_draft),
    Route("POST", re.compile(r"^/new/discard$"), post_discard_new),
    Route("POST", _TICKET_DISCARD, post_discard_draft),
    Route("POST", re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/reply$"), post_reply),
    Route("POST", re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/state$"), post_state),
    # JSON endpoints for the live pages.  They go through the same dispatch
    # pipeline as every page, so the Basic auth, same-origin check, body cap
    # and security headers apply without a second implementation.
    Route("GET", re.compile(r"^/api/home$"), api_home),
    Route("GET", re.compile(r"^/api/archive$"), api_archive),
    Route("GET", _API_TICKET, api_ticket),
    Route("POST", re.compile(r"^/api/new$"), api_create_draft),
    Route("POST", re.compile(r"^/api/new/start$"), api_start_new),
    Route("POST", re.compile(r"^/api/new/discard$"), api_discard_new),
    # Uploads are the only routes that stream.  The cap is checked from
    # Content-Length before the view runs, so an oversized upload is refused
    # with 413 without a byte of its body being read.
    Route(
        "POST",
        re.compile(r"^/api/new/files$"),
        api_upload_new_file,
        max_body_bytes=MAX_UPLOAD_BYTES,
        stream_body=True,
    ),
    Route(
        "POST",
        _API_TICKET_FILES,
        api_upload_file,
        max_body_bytes=MAX_UPLOAD_BYTES,
        stream_body=True,
    ),
    Route("DELETE", _API_TICKET_FILE, api_delete_file),
    Route("POST", _API_TICKET_EDIT, api_save_draft),
    Route("POST", _API_TICKET_START, api_start_draft),
    Route("POST", _API_TICKET_DISCARD, api_discard_draft),
    Route("POST", _API_TICKET_DRAFT, api_draft_text),
    Route("POST", _API_TICKET_SEEN, api_seen),
    Route("POST", _API_TICKET_REPLY, api_reply),
    Route("POST", _API_TICKET_STATE, api_state),
    Route("GET", _PUBLIC_ICON, serve_public, public=True),
    Route("GET", re.compile(r"^/static/(?P<path>.+)$"), serve_static),
)
