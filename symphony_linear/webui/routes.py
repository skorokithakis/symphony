"""The web UI route table.

This is the only place a new page needs to be registered.  Every route is
resolved by the single request pipeline in :mod:`symphony_linear.webui.server`,
which authenticates the request, enforces same-origin on non-GET/HEAD methods
and adds the security headers to the response.  Adding an entry here cannot
skip any of that.

A feature that has an on/off switch in the config registers its route
conditionally through :func:`routes_for` rather than adding it to ``ROUTES``:
a daemon without the feature must answer 404 for the path rather than expose
an endpoint that cannot work.

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

from symphony_linear.config import AppConfig
from symphony_linear.webui.agent_api import (
    api_models,
    api_repo_add,
    api_repo_delete,
    api_repo_rename,
    api_repos,
    api_ticket_action,
    api_ticket_comment,
    api_ticket_create,
    api_ticket_delete,
    api_ticket_detail,
    api_ticket_edit,
    api_ticket_start,
    api_tickets,
    guide,
)
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
    api_soniox_key,
    api_speech,
    api_speech_audio,
    api_start_draft,
    api_start_new,
    api_state,
    api_ticket,
    api_retry,
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
from symphony_linear.webui.http_types import Route, speech_is_enabled
from symphony_linear.webui.push import api_push_subscription, serve_service_worker
from symphony_linear.webui.repos import (
    post_add_repo,
    post_remove_repo,
    repos_page,
)
from symphony_linear.webui.static import serve_static
from symphony_linear.webui.ticket import (
    post_reply,
    post_retry,
    post_state,
    ticket_page,
)
from symphony_linear.webui.views import archive, home, metrics_page, next_ticket

_TICKET_EDIT = re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/edit$")
_TICKET_START = re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/start$")
_TICKET_DISCARD = re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/discard$")
_TICKET_RETRY = re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/retry$")

_API_TICKET = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)$")
_API_TICKET_EDIT = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/edit$")
_API_TICKET_START = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/start$")
_API_TICKET_DISCARD = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/discard$")
_API_TICKET_DRAFT = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/draft$")
_API_TICKET_SEEN = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/seen$")
_API_TICKET_REPLY = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/reply$")
_API_TICKET_RETRY = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/retry$")
_API_TICKET_STATE = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/state$")
_API_TICKET_FILES = re.compile(r"^/api/tickets/(?P<ticket_id>[^/]+)/files$")
_API_TICKET_FILE = re.compile(
    r"^/api/tickets/(?P<ticket_id>[^/]+)/files/(?P<name>[^/]+)$"
)
_API_TICKET_COMMENT_SPEECH = re.compile(
    r"^/api/tickets/(?P<ticket_id>[^/]+)/comments/(?P<comment_id>[^/]+)/speech$"
)

_FILES = re.compile(r"^/files/(?P<ticket_id>[^/]+)/(?P<name>[^/]+)$")

_API_V1_TICKETS = re.compile(r"^/api/v1/tickets$")
_API_V1_TICKET = re.compile(r"^/api/v1/tickets/(?P<ticket_id>[^/]+)$")
_API_V1_TICKET_START = re.compile(r"^/api/v1/tickets/(?P<ticket_id>[^/]+)/start$")
_API_V1_TICKET_ACTIONS = re.compile(r"^/api/v1/tickets/(?P<ticket_id>[^/]+)/actions$")
_API_V1_TICKET_COMMENTS = re.compile(r"^/api/v1/tickets/(?P<ticket_id>[^/]+)/comments$")
_API_V1_REPOS = re.compile(r"^/api/v1/repos$")
_API_V1_MODELS = re.compile(r"^/api/v1/models$")

ROUTES: tuple[Route, ...] = (
    Route("GET", re.compile(r"^/$"), home),
    Route("GET", re.compile(r"^/archive$"), archive),
    Route("GET", re.compile(r"^/next$"), next_ticket),
    # The Prometheus exposition.  Always on, including for the Linear and
    # GitHub backends (they record the same metrics, nobody scrapes them);
    # an unauthenticated scrape gets the ordinary Basic auth challenge.
    Route("GET", re.compile(r"^/metrics$"), metrics_page),
    Route("GET", re.compile(r"^/repos$"), repos_page),
    Route("POST", re.compile(r"^/repos$"), post_add_repo),
    Route("POST", re.compile(r"^/repos/remove$"), post_remove_repo),
    Route("GET", re.compile(r"^/new$"), new_page),
    Route("GET", _TICKET_EDIT, edit_page),
    Route("GET", re.compile(r"^/tickets/(?P<ticket_id>[^/]+)$"), ticket_page),
    Route("GET", _FILES, serve_file),
    # The Web Push service worker must be at the root for its scope to cover
    # the ticket pages a notification opens; see webui/push.py.
    Route("GET", re.compile(r"^/sw\.js$"), serve_service_worker),
    Route("POST", re.compile(r"^/api/push-subscriptions$"), api_push_subscription),
    Route("POST", re.compile(r"^/new$"), post_create_draft),
    Route("POST", _TICKET_EDIT, post_save_draft),
    Route("POST", re.compile(r"^/new/start$"), post_start_new),
    Route("POST", _TICKET_START, post_start_draft),
    Route("POST", re.compile(r"^/new/discard$"), post_discard_new),
    Route("POST", _TICKET_DISCARD, post_discard_draft),
    Route("POST", re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/reply$"), post_reply),
    Route("POST", _TICKET_RETRY, post_retry),
    Route("POST", re.compile(r"^/tickets/(?P<ticket_id>[^/]+)/state$"), post_state),
    # The agent API.  These are ordinary routes, so the body cap and the
    # security headers still apply; the server authenticates /api/v1 with a
    # bearer token instead of Basic auth, and turns its routing and body
    # errors into the JSON error shape.  The guide lists every one of them.
    # GET/HEAD of the bare /api/v1 is the one route served without the token,
    # so a caller can read the guide before it has the key.
    Route("GET", re.compile(r"^/api/v1$"), guide),
    Route("GET", _API_V1_TICKETS, api_tickets),
    Route("POST", _API_V1_TICKETS, api_ticket_create),
    Route("GET", _API_V1_TICKET, api_ticket_detail),
    Route("PATCH", _API_V1_TICKET, api_ticket_edit),
    Route("DELETE", _API_V1_TICKET, api_ticket_delete),
    Route("POST", _API_V1_TICKET_START, api_ticket_start),
    Route("POST", _API_V1_TICKET_ACTIONS, api_ticket_action),
    Route("POST", _API_V1_TICKET_COMMENTS, api_ticket_comment),
    Route("GET", _API_V1_REPOS, api_repos),
    Route("POST", _API_V1_REPOS, api_repo_add),
    Route("PATCH", _API_V1_REPOS, api_repo_rename),
    Route("DELETE", _API_V1_REPOS, api_repo_delete),
    Route("GET", _API_V1_MODELS, api_models),
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
    Route("POST", _API_TICKET_RETRY, api_retry),
    Route("POST", _API_TICKET_STATE, api_state),
    Route("GET", re.compile(r"^/static/(?P<path>.+)$"), serve_static),
)

#: Optional routes are kept out of ``ROUTES`` so a daemon without the feature
#: answers 404 for their paths instead of exposing an endpoint that cannot
#: work.  ``routes_for`` is the whole table for a given config.
SONIOX_KEY_ROUTE: Route = Route(
    "POST", re.compile(r"^/api/soniox-key$"), api_soniox_key
)

#: Reading an agent reply aloud needs both the Anthropic key (the listening
#: script) and the Soniox key (the audio), so it is absent unless both are set.
#: The POST starts generation and costs money; the GET on the same path streams
#: the entry it made, which is why the two are separate routes.
SPEECH_ROUTE: Route = Route("POST", _API_TICKET_COMMENT_SPEECH, api_speech)
SPEECH_AUDIO_ROUTE: Route = Route("GET", _API_TICKET_COMMENT_SPEECH, api_speech_audio)


def routes_for(config: AppConfig) -> tuple[Route, ...]:
    """Return the routes enabled by *config*.

    Two routes are conditional today.  Without ``webui.soniox_api_key`` the
    temporary-key route is absent, so ``/api/soniox-key`` is an unknown route
    and the page never renders the microphone button.  The speak routes are
    present only when both ``webui.anthropic_api_key`` and
    ``webui.soniox_api_key`` are set, for the same reason: a click must not
    reach an endpoint that cannot produce audio.
    """
    webui = config.webui
    routes = ROUTES
    if webui is not None and webui.soniox_api_key is not None:
        routes = (*routes, SONIOX_KEY_ROUTE)
    if speech_is_enabled(config):
        routes = (*routes, SPEECH_ROUTE, SPEECH_AUDIO_ROUTE)
    return routes
