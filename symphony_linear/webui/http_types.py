"""Shared types for the web UI request pipeline.

``WebUIApp`` is the read-only context every route receives.  ``Route`` is the
only thing a later page needs to add: a method, a compiled path pattern and a
view function.  The authorisation, same-origin and response-header rules live
in :mod:`symphony_linear.webui.server` and are applied to every route.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import LocalTicketStore
from symphony_linear.presence import TicketPresence
from symphony_linear.state import StateManager

#: Default request-body limit.  A route may raise it explicitly; the file
#: upload routes do so and stream the body instead of buffering it.
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB


@dataclass(frozen=True)
class WebUIApp:
    """Configuration and stores shared by every request handler thread."""

    store: LocalTicketStore
    state: StateManager
    config: AppConfig
    on_wake: Callable[[], None]
    static_root: Path
    #: Shared with the tracker, which suppresses the Needs Input push while
    #: this ticket's page is being watched.  ``None`` means no presence
    #: tracking (tests and other construction paths).
    presence: TicketPresence | None = None


@dataclass(frozen=True)
class Request:
    """A routed request handed to a view.

    Views never touch the socket or the authentication headers; the server has
    already checked both by the time one of them runs.

    ``body`` holds a buffered body for ordinary body-bearing routes.
    ``stream_body`` routes instead receive the unread ``body_stream`` and the
    validated ``content_length``; the view is responsible for consuming
    exactly that many bytes.  ``body`` is empty in that case.
    """

    app: WebUIApp
    method: str
    path: str
    query: Mapping[str, list[str]]
    body: bytes = b""
    body_stream: BinaryIO | None = None
    content_length: int | None = None
    match: re.Match[str] | None = None


@dataclass(frozen=True)
class Response:
    """A response body plus its route-specific headers.

    The server always adds its security headers (CSP, nosniff, frame denial)
    and ``Content-Type``/``Content-Length`` when it writes this object.
    """

    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def html(
        cls,
        text: str,
        *,
        status: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> Response:
        return cls(
            status=status,
            body=text.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            headers=headers,
        )

    @classmethod
    def text(
        cls,
        text: str,
        *,
        status: int = 200,
        content_type: str = "text/plain; charset=utf-8",
        headers: tuple[tuple[str, str], ...] = (),
    ) -> Response:
        return cls(
            status=status,
            body=text.encode("utf-8"),
            content_type=content_type,
            headers=headers,
        )


#: A view receives a fully authenticated request.  GET/HEAD views have not
#: been same-origin checked; all other methods have.
View = Callable[[Request], Response]


@dataclass(frozen=True)
class Route:
    """One method/path pair in the route table.

    **GET and HEAD views are read-only.**  The server deliberately does not
    apply the same-origin check to them, so a GET or HEAD view must never
    mutate the ticket store, ``state.json``, or any other persisted state.
    Saves, actions and persisted "seen" markers belong on POST/PUT/PATCH/
    DELETE routes, which the server guards with the Origin check.  HEAD is
    dispatched to the GET view, so this constraint applies to it twice.

    ``max_body_bytes`` applies to body-bearing methods (POST/PUT/PATCH).  The
    default is 1 MiB.  Set ``stream_body=True`` on a route that needs to
    consume a large body itself: the view then receives ``body_stream`` and
    ``content_length`` and must read exactly ``content_length`` bytes; the
    server does not buffer the body first.
    """

    method: str
    pattern: re.Pattern[str]
    view: View
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    stream_body: bool = False
