"""Webhook HTTP server for Linear with HMAC signature verification.

A tiny ``ThreadingHTTPServer``-based receiver.  Listens on ``0.0.0.0:<port>``
so that Linear (an external service) can reach it.  Only ``POST /webhooks/linear/``
is accepted; all other paths or methods receive 404 / 405.

The server does **not** parse the JSON body — it only verifies the HMAC
signature and calls the wake callback so the orchestrator can run a poll tick
immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WEBHOOK_PATH = "/webhooks/linear/"
_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
_STOP_JOIN_TIMEOUT = 5  # seconds
_REQUEST_TIMEOUT = 10  # seconds — read timeout for webhook request bodies


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class _WebhookHandler(BaseHTTPRequestHandler):
    """Request handler for Linear webhook POSTs.

    - Only ``POST /webhooks/linear/`` is handled.
    - Verifies the ``Linear-Signature`` HMAC header against the shared secret.
    - Calls ``on_wake`` on success; never parses the JSON body.
    """

    # ------------------------------------------------------------------
    # Socket timeout
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Set a read timeout on the request socket before handling."""
        super().setup()
        self.connection.settimeout(_REQUEST_TIMEOUT)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:
        """Suppress the default access-log line by routing to DEBUG."""
        logger.debug("http: %s", fmt % args)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 (method name from base class)
        if self.path != _WEBHOOK_PATH:
            self._respond_error(404, "Not Found")
            return
        self._handle_webhook()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == _WEBHOOK_PATH:
            self._respond_error(405, "Method Not Allowed")
        else:
            self._respond_error(404, "Not Found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # Anything else → 405 on the webhook path, 404 elsewhere.
    def _method_not_allowed(self) -> None:
        if self.path == _WEBHOOK_PATH:
            self._respond_error(405, "Method Not Allowed")
        else:
            self._respond_error(404, "Not Found")

    do_PUT = _method_not_allowed  # noqa: N815
    do_PATCH = _method_not_allowed  # noqa: N815
    do_DELETE = _method_not_allowed  # noqa: N815
    do_OPTIONS = _method_not_allowed  # noqa: N815

    # ------------------------------------------------------------------
    # Webhook handling
    # ------------------------------------------------------------------

    def _handle_webhook(self) -> None:
        # Read body
        content_length_str = self.headers.get("Content-Length")
        if content_length_str is None:
            self._respond_error(411, "Length Required")
            logger.warning("webhook: missing Content-Length header")
            return

        try:
            content_length = int(content_length_str)
        except ValueError:
            self._respond_error(400, "Bad Request")
            logger.warning("webhook: invalid Content-Length: %s", content_length_str)
            return

        if content_length < 0:
            self._respond_error(400, "Bad Request")
            logger.warning("webhook: negative Content-Length: %d", content_length)
            return

        if content_length > _MAX_BODY_BYTES:
            self._respond_error(413, "Payload Too Large")
            logger.warning(
                "webhook: body too large (%d bytes > %d cap)",
                content_length,
                _MAX_BODY_BYTES,
            )
            return

        try:
            body = self.rfile.read(content_length)
        except (ConnectionError, OSError):
            logger.warning("webhook: failed to read body")
            return

        # Read signature header
        signature = self.headers.get("Linear-Signature")
        if not signature:
            self._respond_error(401, "Unauthorized")
            logger.warning("webhook: missing Linear-Signature header")
            return

        # Verify HMAC
        if not self._verify_signature(body, signature):
            self._respond_error(401, "Unauthorized")
            logger.warning(
                "webhook: signature mismatch (path=%s content-length=%d)",
                self.path,
                len(body),
            )
            return

        # Success — call wake callback BEFORE responding so the orchestrator
        # is woken before the HTTP response is sent to Linear.  If on_wake
        # raises, log it but still return 200 — the webhook delivery was
        # valid; failure is on our side and Linear shouldn't retry.
        # NOTE: _on_wake is set as a class attribute on _HandlerWithWake.
        # Accessing it via type(self) avoids Python binding it as a bound
        # method, which would pass self as the first argument.
        # mypy can't see this attribute because it's injected dynamically,
        # but it is always set before any request is handled.
        try:
            type(self)._on_wake()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("webhook: on_wake raised")
        logger.info("webhook: 200 %s", self.path)
        self._respond_ok()

    # ------------------------------------------------------------------
    # HMAC
    # ------------------------------------------------------------------

    def _verify_signature(self, body: bytes, signature_header: str) -> bool:
        server = self.server
        # _webhook_secret is injected on the server instance by WebhookServer.
        secret: str = server._webhook_secret  # type: ignore[attr-defined]
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _respond_ok(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _respond_error(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class WebhookServer:
    """A tiny threaded HTTP server for receiving Linear webhooks.

    Binds to ``0.0.0.0:<port>`` so that Linear's external servers can
    reach the webhook endpoint.  Only ``POST /webhooks/linear/`` is
    accepted; every other request receives a 4xx response.

    Parameters
    ----------
    port:
        TCP port to listen on.
    linear_secret:
        Shared secret for HMAC-SHA256 signature verification.
    on_wake:
        Zero-argument callable invoked after a valid webhook POST
        (called from the server thread, so it must be fast and
        thread-safe).
    """

    def __init__(
        self,
        port: int,
        linear_secret: str,
        on_wake: Callable[[], None],
    ) -> None:
        self.port = port
        self._linear_secret = linear_secret
        self._on_wake = on_wake
        self._thread: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the server in a daemon thread and return immediately."""
        self._server = self._create_server()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="webhook-server",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "webhook server listening on %s:%d",
            *self._server.server_address[:2],
        )

    def stop(self) -> None:
        """Shut down the server and join its thread (with timeout)."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT)
            if self._thread.is_alive():
                logger.warning(
                    "webhook server thread did not exit within %ds",
                    _STOP_JOIN_TIMEOUT,
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _create_server(self) -> ThreadingHTTPServer:
        """Build a ThreadingHTTPServer with the custom handler wired up."""

        # Bind to 0.0.0.0 so that Linear's external webhook delivery
        # infrastructure can reach this endpoint.  Clients outside the
        # host need to hit the server.
        class _HandlerWithWake(_WebhookHandler):
            _on_wake = self._on_wake  # type: ignore[assignment]

        server = ThreadingHTTPServer(
            ("0.0.0.0", self.port),
            _HandlerWithWake,  # type: ignore[arg-type]
        )
        # Inject the secret onto the server instance so the handler
        # can access it (ThreadingHTTPServer uses a single class
        # across all requests; instance attributes are the cleanest
        # way to pass configuration without a global).
        server._webhook_secret = self._linear_secret  # type: ignore[attr-defined]
        # Disable DNS lookups in the access log (we override log_message anyway).
        server.address_family = socket.AF_INET
        # Set daemon threads so request-handler threads don't block process exit.
        server.daemon_threads = True

        return server
