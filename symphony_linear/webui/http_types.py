"""Shared types for the web UI request pipeline.

``WebUIApp`` is the read-only context every route receives.  ``Route`` is the
only thing a later page needs to add: a method, a compiled path pattern and a
view function.  The authorisation, same-origin and response-header rules live
in :mod:`symphony_linear.webui.server` and are applied to every route.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import LocalTicketStore
from symphony_linear.presence import TicketPresence
from symphony_linear.state import StateManager
from symphony_linear.turn_activity import TurnActivity
from symphony_linear.webpush import WebPushStore
from symphony_linear.webui.speech import SpeechCache

#: Default request-body limit.  A route may raise it explicitly; the file
#: upload routes do so and stream the body instead of buffering it.
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB


def speech_is_enabled(config: AppConfig) -> bool:
    """Whether agent replies can be read aloud.

    Both keys are needed: the Anthropic key writes the listening script on the
    daemon, and the Soniox key generates its audio.  An empty Anthropic key is
    what an unset ``${VAR}`` leaves behind and cannot make a summary, so it
    reads as absent; the Soniox key is validated as non-empty in the config.
    The route table, the Content-Security-Policy and the view all ask this one
    function so none of them can enable a path the others do not.
    """
    webui = config.webui
    if webui is None:
        return False
    return bool((webui.anthropic_api_key or "").strip()) and bool(
        (webui.soniox_api_key or "").strip()
    )


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
    #: Shared with the orchestrator, which records each running turn's last
    #: output.  ``None`` means the page never shows an output age (tests and
    #: other construction paths).
    turn_activity: TurnActivity | None = None
    #: Shared with the tracker, which sends the Needs Input push and drops
    #: expired subscriptions; this is that same instance, and the subscribe
    #: endpoint is the only thing that adds one.  ``None`` means no Web Push
    #: (tests and other construction paths), so the pages render no bell and
    #: the endpoint answers 404.
    webpush: WebPushStore | None = None
    #: The VAPID public key the bell hands to the browser.  Resolved once when
    #: the server is constructed, because generating the pair writes
    #: ``webpush.json`` and a page GET must never write persisted state.
    #: ``None`` exactly when ``webpush`` is None.
    push_public_key: str | None = None
    #: The server-wide Content-Security-Policy.  A route that needs one extra
    #: source in its own response (the Web Push service worker's notification
    #: icon) derives its override from this rather than restating the policy.
    csp: str = ""
    #: The speak route's audio, held for this server only.  An entry fills
    #: while the GET route streams it, and it is never written anywhere.  A
    #: cache per app instance rather than a module-level one keeps two servers
    #: in one process (tests, or a daemon restarting in-process) from serving
    #: each other's audio.
    speech_cache: SpeechCache = field(default_factory=SpeechCache)

    @property
    def dictation_enabled(self) -> bool:
        """Whether the page may offer microphone dictation.

        The long-lived Soniox key is the on/off switch; without it there is no
        temporary-key route and no WebSocket origin in the CSP, so the page
        must not render a button either.
        """
        webui = self.config.webui
        return webui is not None and webui.soniox_api_key is not None

    @property
    def speech_enabled(self) -> bool:
        """Whether the page may offer to read an agent reply aloud.

        Unlike dictation this needs both keys, because the script is written by
        the daemon.  The route table and the CSP are built from the same rule,
        so a page never renders a button the server would answer 404 for.
        """
        return speech_is_enabled(self.config)


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

    ``body_chunks`` is the streaming form of the body: an iterator of byte
    chunks that the server writes and flushes one at a time.  A response with
    chunks carries no ``Content-Length``, because the total is not known while
    the producer is still filling it; the handler speaks HTTP/1.0, so the body
    ends when the connection closes.  ``body`` stays empty then.  A HEAD
    response never consumes the iterator.

    ``csp`` overrides the server-wide Content-Security-Policy for this one
    response.  ``None``, the only value every route but the Web Push service
    worker uses, keeps the server's policy byte for byte.
    """

    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()
    csp: str | None = None
    body_chunks: Iterator[bytes] | None = None

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
