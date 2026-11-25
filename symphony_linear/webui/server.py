"""HTTP server for the built-in tracker's web UI.

The server is a ``ThreadingHTTPServer`` with the same lifecycle as
``webhook.py``: :meth:`WebUIServer.start` spawns a daemon thread and returns,
:meth:`WebUIServer.stop` shuts it down and joins the thread.

Security is enforced in one place, :meth:`_WebUIHandler._dispatch`, which every
request funnels through after :meth:`_WebUIHandler.handle_one_request` has
parsed it -- including verbs with no ``do_*`` method and malformed/legacy
request lines:

* HTTP Basic auth on every request, including static assets (constant-time
  ``hmac.compare_digest`` comparison);
* the ``/api/v1`` agent API's bearer token instead of Basic auth, from
  ``webui.api_token``.  Basic credentials are not accepted there, and the
  bearer token is not accepted anywhere else.  With no token configured the
  namespace answers 404 rather than advertising itself with a 401.  Routing,
  body and view errors in the namespace all use the API's JSON error shape;
* same-origin enforcement on every non-GET/HEAD request (browsers attach
  Basic credentials to cross-site requests, so auth alone is not CSRF
  protection).  A bearer-authenticated ``/api/v1`` request is exempt because
  browsers never attach a bearer token by themselves, so there is no
  ambient credential for a cross-site request to ride;
* the request-body cap, which is a per-route value (1 MiB by default), and
  the socket read timeout, in the style of ``webhook.py``;
* the security headers, emitted by :meth:`_WebUIHandler.end_headers` so even
  error responses get them.

Routes are looked up in :mod:`symphony_linear.webui.routes`.  A new page only
adds an entry there plus a view and a template; it must not bypass this file.
GET and HEAD views are read-only: they are not same-origin checked (and HEAD
runs the GET view), so they must never write the store or any persisted state.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import re
import socket
import threading
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Callable, cast
from urllib.parse import parse_qs, urlsplit

from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import LocalTicketStore
from symphony_linear.presence import TicketPresence
from symphony_linear.state import StateManager
from symphony_linear.turn_activity import TurnActivity
from symphony_linear.webui.agent_api import error_response, is_agent_api_path
from symphony_linear.webui.http_types import Request, Response, Route, WebUIApp
from symphony_linear.webui.routes import routes_for

logger = logging.getLogger(__name__)

#: Methods whose bodies this server reads.  GET/HEAD/DELETE never have one
#: that a view needs today.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

#: A body-bearing route must send exactly one ASCII-decimal Content-Length.
_CONTENT_LENGTH_RE = re.compile(r"[0-9]+")

#: Simple non-blocking admission limit.  One thread per concurrent request is
#: fine for a local UI, but unauthenticated connections must not be able to
#: park unbounded handler threads.
_MAX_CONCURRENT_REQUESTS = 32

_REQUEST_TIMEOUT = 10  # seconds — read timeout for request bodies
_STOP_JOIN_TIMEOUT = 5  # seconds
_SERVE_POLL_INTERVAL = 0.1  # seconds — keeps stop() responsive in tests

#: Only same-origin scripts and styles; the time formatter is an external
#: file because inline script is deliberately not allowed.
#: ``connect-src`` gains the Soniox WebSocket origin only when dictation is
#: configured, because the browser then streams audio to it directly.  Every
#: other page keeps the original policy, byte for byte.
_SONIOX_WEBSOCKET = "wss://stt-rt.soniox.com"


def _build_csp(*, dictation: bool) -> str:
    connect_src = "connect-src 'self'"
    if dictation:
        connect_src = f"{connect_src} {_SONIOX_WEBSOCKET}"
    return (
        "default-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self'; "
        # default-src 'none' would otherwise block the web app manifest.
        "manifest-src 'self'; "
        "font-src 'self'; "
        f"{connect_src}; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


_CSP = _build_csp(dictation=False)

_STATIC_ROOT = Path(__file__).resolve().parent / "static"

#: A response no route can produce without authentication.
_UNAUTHORIZED = Response.text(
    "Unauthorized\n",
    status=401,
    headers=(("WWW-Authenticate", 'Basic realm="Symphony", charset="UTF-8"'),),
)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class _WebUIHandler(BaseHTTPRequestHandler):
    """Single entry point for every request; see the module docstring."""

    server_version = "symphony"
    sys_version = ""

    def version_string(self) -> str:
        """Do not advertise the Python version in the Server header."""
        return self.server_version

    # ------------------------------------------------------------------
    # Socket and logging
    # ------------------------------------------------------------------

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(_REQUEST_TIMEOUT)

    def log_message(self, fmt: str, *args: object) -> None:
        logger.debug("webui http: %s", fmt % args)

    # ------------------------------------------------------------------
    # Every verb funnels through _dispatch.
    # ------------------------------------------------------------------

    def handle_one_request(self) -> None:
        """Parse one request and dispatch it regardless of the verb.

        The base class answers an unknown verb with 501 before any of our
        checks run.  We must authenticate and same-origin-check first, so the
        verb is looked up only inside :meth:`_dispatch`.

        An HTTP/0.9 request would otherwise suppress the status line and every
        header (HTTP/0.9 responses are headerless).  Reject it with a framed
        HTTP/1.0 error instead, so the CSP and the security headers are still
        emitted.
        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                # parse_request() already sent a framed error.
                return
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
                self._send(Response.text("Bad Request\n", status=400))
                self.wfile.flush()
                return
            self._dispatch()
            try:
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                logger.debug("webui: client disconnected before flush")
        except TimeoutError as exc:
            self.log_error("Request timed out: %r", exc)
            self.close_connection = True

    # ------------------------------------------------------------------
    # The choke point
    # ------------------------------------------------------------------

    def _dispatch(self) -> None:
        path, query = self._split_target()
        agent_api = path is not None and is_agent_api_path(path)

        if agent_api:
            # The bearer exception to both guards; the module docstring and
            # agent_api.py explain why it is safe.  The 404 when no token is
            # configured is deliberate: an unconfigured daemon must not
            # advertise the namespace with a 401.
            refusal = self._agent_api_refusal()
            if refusal is not None:
                self._send(refusal)
                return
        else:
            if not self._authenticated():
                self._send(_UNAUTHORIZED)
                return

            if self.command not in ("GET", "HEAD") and not self._same_origin():
                logger.warning(
                    "webui: rejected cross-origin %s %s", self.command, self.path
                )
                self._send(Response.text("Forbidden\n", status=403))
                return

        if path is None:
            self._send(Response.text("Bad Request\n", status=400))
            return

        # Resolve the route before touching the body: an unknown POST must get
        # its 404/405 immediately, not after waiting for a body that may never
        # arrive.
        route, match, allowed = self._resolve(path)
        if route is None:
            if agent_api:
                # The API's callers are programs, so every error in this
                # namespace is the JSON shape, including the ones the server
                # produces before a view runs.
                if allowed:
                    self._send(
                        error_response(
                            "Method not allowed.",
                            f"{self.command} is not supported on {path}. "
                            f"Try {', '.join(allowed)}. "
                            "GET /api/v1 returns the guide, which lists every "
                            "endpoint.",
                            status=405,
                            headers=(("Allow", ", ".join(allowed)),),
                        )
                    )
                else:
                    self._send(
                        error_response(
                            "Unknown endpoint.",
                            f"{path} is not part of the agent API. "
                            "GET /api/v1 returns the guide, which lists every "
                            "endpoint.",
                            status=404,
                        )
                    )
            elif allowed:
                self._send(
                    Response.text(
                        "Method Not Allowed\n",
                        status=405,
                        headers=(("Allow", ", ".join(allowed)),),
                    )
                )
            else:
                self._send(Response.text("Not Found\n", status=404))
            return

        body = b""
        body_stream: BinaryIO | None = None
        content_length: int | None = None
        if self.command in _BODY_METHODS:
            prepared = self._prepare_body(route, agent_api=agent_api)
            if prepared is None:
                return
            content_length, body, body_stream = prepared

        request = Request(
            app=self._app,
            method=self.command,
            path=path,
            query=query,
            body=body,
            body_stream=body_stream,
            content_length=content_length,
            match=match,
        )
        try:
            response = route.view(request)
        except Exception:
            logger.exception("webui: view failed for %s %s", self.command, path)
            if agent_api:
                if path.startswith("/api/v1/repos"):
                    read_back = "GET /api/v1/repos"
                elif path.startswith("/api/v1/tickets"):
                    read_back = (
                        "GET /api/v1/tickets, or GET /api/v1/tickets/{ticket_id}"
                    )
                else:
                    read_back = "GET /api/v1"
                response = error_response(
                    "Internal server error.",
                    "The write may have succeeded before the failure. Read it "
                    f"back with {read_back} before repeating a write, and read "
                    "the daemon log for the traceback.",
                    status=500,
                )
            else:
                response = Response.text("Internal Server Error\n", status=500)
        self._send(response)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @property
    def _app(self) -> WebUIApp:
        server = self.server
        assert isinstance(server, _WebUIServer)
        return server.app

    @property
    def _credentials(self) -> tuple[str, str]:
        server = self.server
        assert isinstance(server, _WebUIServer)
        return server.credentials

    @property
    def _routes(self) -> Sequence[Route]:
        server = self.server
        assert isinstance(server, _WebUIServer)
        return server.routes

    @property
    def _csp(self) -> str:
        server = self.server
        assert isinstance(server, _WebUIServer)
        return server.csp

    def _authenticated(self) -> bool:
        username, password = self._credentials
        expected = f"{username}:{password}".encode("utf-8")

        header = self.headers.get("Authorization")
        if not header:
            return False
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic" or not value.strip():
            return False
        try:
            supplied = base64.b64decode(value.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        # Compare the whole "user:password" pair in constant time.  A
        # per-field comparison would leak which half was wrong.
        return hmac.compare_digest(supplied, expected)

    def _agent_api_refusal(self) -> Response | None:
        """Return the refusal for an ``/api/v1`` request, or None to proceed.

        Basic auth is not consulted here at all, and the bearer token is not
        consulted for any other path: the two credentials own disjoint parts
        of the URL space.
        """
        token = self._api_token
        if token is None:
            # Same answer as an unknown route, so a daemon without the API
            # configured never confirms that /api/v1 exists.
            return Response.text("Not Found\n", status=404)

        supplied = self._bearer_token()
        if supplied is None or not hmac.compare_digest(
            supplied.encode("utf-8"), token.encode("utf-8")
        ):
            return error_response(
                "Unauthorized",
                "Send 'Authorization: Bearer <token>' using the token in "
                "webui.api_token of config.yaml.",
                status=401,
            )
        return None

    def _bearer_token(self) -> str | None:
        header = self.headers.get("Authorization")
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        stripped = value.strip()
        return stripped or None

    @property
    def _api_token(self) -> str | None:
        server = self.server
        assert isinstance(server, _WebUIServer)
        return server.api_token

    # ------------------------------------------------------------------
    # Same-origin (CSRF) guard
    # ------------------------------------------------------------------

    def _same_origin(self) -> bool:
        """Return True only when Origin names the host the request went to.

        An absent Origin is a rejection: browsers send it on every
        cross-origin state-changing request, and scripts outside a browser can
        omit it.  The netloc is compared case-insensitively.

        Behind a TLS-terminating proxy the browser's Origin is https while our
        socket is plain http.  Reject an http Origin when
        ``X-Forwarded-Proto: https`` says the browser is actually on TLS:
        otherwise a page served from the plain-http origin of the same
        hostname could post.  A browser cannot add X-Forwarded-Proto to a
        cross-site request, and the only local parties that can forge it
        (sandboxed agents) do not have the password.  Do not trust any other
        forwarded header.
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return False
        try:
            parts = urlsplit(origin)
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            return False
        if parts.netloc.lower() != host.strip().lower():
            return False

        forwarded_proto = self.headers.get_all("X-Forwarded-Proto") or []
        if parts.scheme == "http" and any(
            value.strip().lower() == "https" for value in forwarded_proto
        ):
            return False
        return True

    # ------------------------------------------------------------------
    # Target parsing and routing
    # ------------------------------------------------------------------

    def _split_target(self) -> tuple[str | None, dict[str, list[str]]]:
        try:
            parts = urlsplit(self.path)
        except ValueError:
            return None, {}
        # Keep the path percent-encoded for routing.  Views that take a path
        # parameter decode it themselves (the static view is the only one
        # today), avoiding a double-decode that could change what a `..`
        # segment means between routing and the filesystem check.
        return parts.path, parse_qs(parts.query, keep_blank_values=True)

    def _resolve(
        self, path: str
    ) -> tuple[Route | None, re.Match[str] | None, list[str]]:
        method = "GET" if self.command == "HEAD" else self.command
        allowed: list[str] = []
        path_matched = False

        for route in self._routes:
            match = route.pattern.fullmatch(path)
            if match is None:
                continue
            path_matched = True
            if route.method == method:
                return route, match, []
            if route.method not in allowed:
                allowed.append(route.method)
                if route.method == "GET" and "HEAD" not in allowed:
                    allowed.append("HEAD")
        return None, None, allowed if path_matched else []

    # ------------------------------------------------------------------
    # Body handling
    # ------------------------------------------------------------------

    def _prepare_body(
        self, route: Route, *, agent_api: bool
    ) -> tuple[int, bytes, BinaryIO | None] | None:
        """Validate and (unless streaming) read the body for *route*.

        Returns ``(content_length, body, stream)`` or sends an error and
        returns None.  ``stream`` is ``self.rfile`` only for routes that opted
        into ``stream_body``; the view must read exactly ``content_length``
        bytes from it.

        *agent_api* selects the JSON error shape for the API namespace, whose
        callers are programs; the browser routes keep their plain-text
        refusals.
        """

        def refuse(
            status: int,
            text: str,
            error: str,
            hint: str,
        ) -> None:
            if agent_api:
                self._send(error_response(error, hint, status=status))
            else:
                self._send(Response.text(text, status=status))

        if self.headers.get("Transfer-Encoding") is not None:
            # We only speak Content-Length framing; refusing chunked bodies
            # avoids request-smuggling ambiguity.
            refuse(
                400,
                "Bad Request\n",
                "Chunked request bodies are not supported.",
                "Send the body with one Content-Length header.",
            )
            return None

        values = self.headers.get_all("Content-Length") or []
        if not values:
            logger.warning("webui: missing Content-Length on %s", self.path)
            refuse(
                411,
                "Length Required\n",
                "A Content-Length header is required.",
                "Send one Content-Length header with an ASCII decimal length "
                "(it may be 0).",
            )
            return None
        if len(values) != 1:
            # Duplicate Content-Length values are a request-smuggling vector
            # even when their values agree.
            refuse(
                400,
                "Bad Request\n",
                "Duplicate Content-Length headers.",
                "Send exactly one Content-Length header.",
            )
            return None
        raw_length = values[0].strip()
        if _CONTENT_LENGTH_RE.fullmatch(raw_length) is None:
            refuse(
                400,
                "Bad Request\n",
                "Content-Length is not a plain decimal number.",
                "Send Content-Length as an ASCII decimal integer.",
            )
            return None
        try:
            content_length = int(raw_length)
        except ValueError:
            # Python refuses absurdly long digit strings; reject them rather
            # than returning 500.
            refuse(
                400,
                "Bad Request\n",
                "Content-Length is not a plain decimal number.",
                "Send Content-Length as an ASCII decimal integer.",
            )
            return None
        if content_length > route.max_body_bytes:
            logger.warning(
                "webui: body too large (%d bytes > %d cap)",
                content_length,
                route.max_body_bytes,
            )
            refuse(
                413,
                "Payload Too Large\n",
                f"The body is larger than {route.max_body_bytes} bytes.",
                "Send a smaller body; the agent API has no upload endpoint.",
            )
            return None

        if route.stream_body:
            return content_length, b"", cast(BinaryIO, self.rfile)

        try:
            body = self.rfile.read(content_length)
        except (ConnectionError, OSError):
            logger.warning("webui: failed to read request body")
            refuse(
                400,
                "Bad Request\n",
                "The request body could not be read.",
                "Send the request again.",
            )
            return None
        if len(body) != content_length:
            refuse(
                400,
                "Bad Request\n",
                "The request body ended before Content-Length bytes arrived.",
                "Send the request again with the whole body.",
            )
            return None
        return content_length, body, None

    # ------------------------------------------------------------------
    # Response writing
    # ------------------------------------------------------------------

    def end_headers(self) -> None:
        """Add the headers every response must carry, success or error."""
        self.send_header("Content-Security-Policy", self._csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command == "HEAD" or not response.body:
            return
        try:
            self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionError, OSError):
            logger.debug("webui: client disconnected before the response was sent")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


#: A TCP connection or a Unix-domain ``(address, socket)`` pair.  The web UI
#: only ever binds AF_INET, so handlers below always see a plain socket.
_RequestSocket = socket.socket | tuple[bytes, socket.socket]


def _write_overloaded_response(request: socket.socket, csp: str) -> None:
    """Answer a connection we refuse to admit with a framed 503 and close it."""
    body = b"Service Unavailable\n"
    head = (
        "HTTP/1.0 503 Service Unavailable\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Content-Security-Policy: {csp}\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "X-Frame-Options: DENY\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        request.sendall(head + body)
    except OSError:
        logger.debug("webui: client disconnected while being refused")


class _WebUIServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the shared context for its handlers."""

    daemon_threads = True
    allow_reuse_address = True

    app: WebUIApp
    credentials: tuple[str, str]
    api_token: str | None
    routes: Sequence[Route]
    csp: str

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        max_concurrent: int,
    ) -> None:
        self._slots = threading.BoundedSemaphore(max_concurrent)
        super().__init__(server_address, handler_class)

    def process_request(
        self, request: _RequestSocket, client_address: tuple[str, int]
    ) -> None:
        """Admit a connection only when a handler slot is free.

        No queue: over the limit, answer 503 immediately and close.  This is
        purely so unauthenticated local connections cannot pile up threads.
        """
        if not isinstance(request, socket.socket):
            # Unreachable for our AF_INET listener; let the base class handle
            # the Unix-domain form without an admission slot.
            super().process_request(request, client_address)
            return
        if not self._slots.acquire(blocking=False):
            try:
                _write_overloaded_response(request, self.csp)
            finally:
                try:
                    request.close()
                except OSError:
                    pass
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # Thread creation failed; the thread that would release the slot
            # never started.
            self._slots.release()
            raise

    def process_request_thread(
        self, request: _RequestSocket, client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class WebUIServer:
    """Threaded HTTP server for the built-in tracker's pages and assets.

    ``start()`` and ``stop()`` must be called from the same thread, in
    sequence: the lifecycle is driven from ``cli.py`` (start, run, stop), and
    the only race handled is a ``stop()`` that runs before the freshly started
    serve thread has entered its loop.  Concurrent calls from different
    threads, or a restart racing a stop, are deliberately not supported.

    Parameters
    ----------
    store:
        The local ticket store shared with the orchestrator.
    state:
        The daemon's StateManager.  The web layer only reads from it.
    config:
        The full application config; the ``webui`` block supplies the bind
        address and Basic-auth credentials.
    on_wake:
        The orchestrator's ``wake`` callback, stored for later routes that
        need to start a poll tick.
    static_root:
        Override for the package's ``static/`` directory (tests only).
    port:
        Override for the configured port, e.g. ``0`` for an ephemeral port
        (tests only).
    routes:
        Override for the route table (tests only).
    max_concurrent:
        Admission limit override (tests only).  Production uses 32.
    presence:
        The in-memory viewer presence shared with the local tracker, which
        uses it to suppress the Needs Input push while a ticket's page is
        being watched.  ``None`` disables presence tracking (tests).
    turn_activity:
        The in-memory turn-activity record shared with the orchestrator, which
        records each running turn's last output so the ticket page can show
        its age.  ``None`` disables the age (tests).
    """

    def __init__(
        self,
        *,
        store: LocalTicketStore,
        state: StateManager,
        config: AppConfig,
        on_wake: Callable[[], None],
        static_root: Path | None = None,
        port: int | None = None,
        routes: Sequence[Route] | None = None,
        max_concurrent: int = _MAX_CONCURRENT_REQUESTS,
        presence: TicketPresence | None = None,
        turn_activity: TurnActivity | None = None,
    ) -> None:
        webui = config.webui
        if webui is None:
            raise ValueError("WebUIServer requires a webui config block")

        self._host = webui.host
        # ``port`` is a test convenience (pass 0 for an ephemeral port); the
        # daemon always uses the configured port.
        self._port = webui.port if port is None else port
        self._credentials = (webui.username, webui.password)
        self._api_token = webui.api_token
        self._csp = _build_csp(dictation=webui.soniox_api_key is not None)
        self._routes = tuple(routes if routes is not None else routes_for(config))
        self._max_concurrent = max_concurrent
        self._app = WebUIApp(
            store=store,
            state=state,
            config=config,
            on_wake=on_wake,
            static_root=static_root or _STATIC_ROOT,
            presence=presence,
            turn_activity=turn_activity,
        )
        self._thread: threading.Thread | None = None
        self._server: _WebUIServer | None = None
        self._started = False
        # Guards the hand-off between a starting thread and stop(): the thread
        # marks itself serving under the lock, or stop() marks the server
        # closed and the thread exits at the gate without calling
        # serve_forever().  Without this, stop() can call shutdown() before the
        # loop is entered and block forever.
        self._lifecycle = threading.Lock()
        self._closed = False
        self._serving = False

    @property
    def bound_port(self) -> int | None:
        """The port actually bound, which is useful when constructed with 0."""
        if self._server is None:
            return None
        return int(self._server.server_address[1])

    def start(self) -> None:
        """Spawn the server in a daemon thread and return immediately."""
        server = _WebUIServer(
            (self._host, self._port),
            _WebUIHandler,
            max_concurrent=self._max_concurrent,
        )
        with self._lifecycle:
            self._closed = False
            self._serving = False
        self._server = server
        try:
            server.app = self._app
            server.credentials = self._credentials
            server.api_token = self._api_token
            server.routes = self._routes
            server.csp = self._csp
            # The server object is handed to the thread directly: stop() clears
            # self._server, and a thread reading that field would see None and
            # never enter the loop.
            self._thread = threading.Thread(
                target=self._serve,
                args=(server,),
                name="webui-server",
                daemon=True,
            )
            self._thread.start()
            self._started = True
        except BaseException:
            # The listener is already bound.  Do not leak it when the thread
            # cannot start, and leave the object safe for stop()/a retry.
            server.server_close()
            self._server = None
            self._thread = None
            self._started = False
            raise
        logger.info(
            "webui server listening on %s:%d",
            *server.server_address[:2],
        )

    def _serve(self, server: _WebUIServer) -> None:
        with self._lifecycle:
            if self._closed:
                # stop() got here first; there is no loop to shut down.
                return
            self._serving = True
        server.serve_forever(poll_interval=_SERVE_POLL_INTERVAL)

    def stop(self) -> None:
        """Shut down the server and join its thread (with a timeout).

        Safe before ``start()``, after a failed start, when called twice and
        when the thread has been started but has not entered its serve loop
        yet.  ``shutdown()`` is only called once the thread has committed to
        ``serve_forever()``; it would otherwise wait forever for a loop that
        never runs.
        """
        server = self._server
        thread = self._thread
        started = self._started
        self._server = None
        self._thread = None
        self._started = False

        if server is None:
            return
        with self._lifecycle:
            serving = self._serving
            self._closed = True
        try:
            if started and serving:
                server.shutdown()
        finally:
            server.server_close()
        if started and thread is not None:
            thread.join(timeout=_STOP_JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning(
                    "webui server thread did not exit within %ds",
                    _STOP_JOIN_TIMEOUT,
                )
