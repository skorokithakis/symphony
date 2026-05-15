"""Tests for the webhook HTTP server and HMAC verification."""

from __future__ import annotations

import hashlib
import hmac
import socket
import threading
import time
from typing import Any
from unittest.mock import Mock

import pytest

from symphony_linear.webhook import WebhookServer, _WEBHOOK_PATH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-secret-123"
_VALID_BODY = b'{"action":"create","type":"Issue"}'
_BIG_BODY = b"x" * (2 * 1024 * 1024)  # 2 MiB (> 1 MiB cap)


def _make_signature(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(
    port: int, body: bytes, *, signature: str | None = None, path: str = _WEBHOOK_PATH
) -> tuple[int, bytes]:
    """Send a POST request to the webhook server and return (status_code, body)."""
    import http.client

    headers: dict[str, str] = {
        "Content-Length": str(len(body)),
    }
    if signature is not None:
        headers["Linear-Signature"] = signature

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read()
        return status, resp_body
    finally:
        conn.close()


def _get(port: int, path: str) -> tuple[int, bytes]:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read()
        return status, resp_body
    finally:
        conn.close()


class _WebhookServerFixture:
    """Context-managed webhook server bound to an ephemeral port."""

    def __init__(self) -> None:
        self.server: WebhookServer | None = None
        self.port: int = 0
        # Always a real Mock so tests can call .assert_*; reset on each start().
        self.wake_mock: Mock = Mock()

    def __enter__(self) -> _WebhookServerFixture:
        return self

    def __exit__(self, *args: Any) -> None:
        if self.server is not None:
            self.server.stop()

    def start(self) -> None:
        self.wake_mock = Mock()
        # Bind to port 0 — the OS picks a free ephemeral port.
        self.server = WebhookServer(
            port=0, linear_secret=_SECRET, on_wake=self.wake_mock
        )
        self.server.start()
        # Give the server a moment to bind.
        time.sleep(0.05)
        # Read the actual assigned port.
        assert self.server._server is not None
        self.port = self.server._server.server_address[1]
        assert self.port > 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebhookValidRequests:
    """Happy-path: valid signature on the right endpoint."""

    def test_valid_signature_calls_wake(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            sig = _make_signature(_VALID_BODY)
            status, resp_body = _post(fxt.port, _VALID_BODY, signature=sig)
            assert status == 200
            assert resp_body == b""
            # Wake callback must be called exactly once.
            fxt.wake_mock.assert_called_once()

    def test_valid_signature_no_wake_on_other_path(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            sig = _make_signature(_VALID_BODY)
            status, resp_body = _post(
                fxt.port, _VALID_BODY, signature=sig, path="/something-else"
            )
            assert status == 404
            fxt.wake_mock.assert_not_called()


class TestWebhookAuth:
    """Authentication / signature verification edge cases."""

    def test_missing_signature_header_returns_401(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            status, resp_body = _post(fxt.port, _VALID_BODY)
            assert status == 401
            assert b"Unauthorized" in resp_body
            fxt.wake_mock.assert_not_called()

    def test_bad_signature_returns_401(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            status, resp_body = _post(fxt.port, _VALID_BODY, signature="bad-sig")
            assert status == 401
            assert b"Unauthorized" in resp_body
            fxt.wake_mock.assert_not_called()

    def test_signature_with_wrong_secret_returns_401(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            sig = _make_signature(_VALID_BODY, secret="wrong-secret")
            status, resp_body = _post(fxt.port, _VALID_BODY, signature=sig)
            assert status == 401
            fxt.wake_mock.assert_not_called()


class TestWebhookRouting:
    """Path and method routing."""

    def test_wrong_path_post_returns_404(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            sig = _make_signature(_VALID_BODY)
            status, _ = _post(fxt.port, _VALID_BODY, signature=sig, path="/foo")
            assert status == 404
            fxt.wake_mock.assert_not_called()

    def test_get_on_webhook_path_returns_405(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            status, resp_body = _get(fxt.port, _WEBHOOK_PATH)
            assert status == 405
            assert b"Method Not Allowed" in resp_body
            fxt.wake_mock.assert_not_called()

    def test_get_on_wrong_path_returns_404(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            status, _ = _get(fxt.port, "/bar")
            assert status == 404
            fxt.wake_mock.assert_not_called()


class TestWebhookBodyLimits:
    """Body size handling."""

    def test_body_exceeding_cap_returns_413(self) -> None:
        """Server must reject oversized payloads based on the Content-Length
        header, before reading the body.  Use a raw socket and send only the
        headers — the server should respond 413 without ever reading the body,
        so we never need to transmit the 2 MiB payload (which would race the
        server's close()).
        """
        import socket

        with _WebhookServerFixture() as fxt:
            fxt.start()
            sig = _make_signature(_BIG_BODY)
            big_len = len(_BIG_BODY)
            request_headers = (
                f"POST {_WEBHOOK_PATH} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{fxt.port}\r\n"
                f"Content-Length: {big_len}\r\n"
                f"Linear-Signature: {sig}\r\n"
                f"\r\n"
            ).encode("ascii")
            sock = socket.create_connection(("127.0.0.1", fxt.port), timeout=5)
            try:
                sock.sendall(request_headers)
                # Read full response.  We never write the body.
                data = b""
                sock.settimeout(5)
                while True:
                    try:
                        chunk = sock.recv(4096)
                    except (ConnectionResetError, socket.timeout):
                        break
                    if not chunk:
                        break
                    data += chunk
            finally:
                sock.close()

            assert b" 413 " in data, f"expected 413 status line, got: {data!r}"
            # The response body is "Payload Too Large" (set by our handler);
            # the status line uses BaseHTTPRequestHandler's default reason
            # phrase ("Request Entity Too Large").  Either is fine — the 413
            # status code is what matters.
            fxt.wake_mock.assert_not_called()


class TestWebhookConcurrency:
    """Smoke-test that concurrent valid requests each fire the wake callback."""

    def test_concurrent_valid_requests_all_wake(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            sig = _make_signature(_VALID_BODY)
            results: list[int] = []
            lock = threading.Lock()

            def worker() -> None:
                try:
                    status, _ = _post(fxt.port, _VALID_BODY, signature=sig)
                    with lock:
                        results.append(status)
                except Exception:
                    with lock:
                        results.append(-1)

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert len(results) == 2
            assert all(s == 200 for s in results)
            # wake callback must be called exactly twice (once per request).
            assert fxt.wake_mock.call_count == 2


class TestWebhookServerLifecycle:
    """Start, stop, clean shutdown."""

    def test_start_and_stop(self) -> None:
        wake_mock = Mock()
        server = WebhookServer(port=0, linear_secret=_SECRET, on_wake=wake_mock)
        server.start()
        # Server should be running and bound to a non-zero port.
        assert server._server is not None
        port = server._server.server_address[1]
        assert port > 0

        server.stop()
        # After stop, the thread should be finished.
        assert server._thread is not None and not server._thread.is_alive()

    def test_double_stop_is_safe(self) -> None:
        wake_mock = Mock()
        server = WebhookServer(port=0, linear_secret=_SECRET, on_wake=wake_mock)
        server.start()
        server.stop()
        server.stop()  # Must not raise.


class TestWebhookNegativeContentLength:
    """Negative Content-Length is rejected with 400."""

    def test_negative_content_length_returns_400(self) -> None:
        with _WebhookServerFixture() as fxt:
            fxt.start()
            import http.client

            sig = _make_signature(_VALID_BODY)
            conn = http.client.HTTPConnection("127.0.0.1", fxt.port, timeout=5)
            try:
                # Send explicit -1 Content-Length
                conn.request(
                    "POST",
                    _WEBHOOK_PATH,
                    body=_VALID_BODY,
                    headers={
                        "Content-Length": "-1",
                        "Linear-Signature": sig,
                    },
                )
                resp = conn.getresponse()
                assert resp.status == 400
                resp_body = resp.read()
                assert b"Bad Request" in resp_body
            finally:
                conn.close()
            fxt.wake_mock.assert_not_called()


class TestWebhookWakeBeforeResponse:
    """on_wake is called before the 200 response is sent."""

    def test_on_wake_called_before_response(self) -> None:
        """on_wake fires before the 200 response is sent to the client."""
        called = threading.Event()

        def on_wake() -> None:
            called.set()

        server = WebhookServer(port=0, linear_secret=_SECRET, on_wake=on_wake)
        server.start()
        try:
            time.sleep(0.05)
            assert server._server is not None
            port = server._server.server_address[1]

            import http.client

            sig = _make_signature(_VALID_BODY)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request(
                    "POST",
                    _WEBHOOK_PATH,
                    body=_VALID_BODY,
                    headers={
                        "Content-Length": str(len(_VALID_BODY)),
                        "Linear-Signature": sig,
                    },
                )
                # Read response — on_wake is called BEFORE _respond_ok(), so by
                # the time the client receives the 200 response, called MUST
                # already be set.
                resp = conn.getresponse()
                status = resp.status
                resp.read()
                assert called.is_set(), (
                    "on_wake was not called before response returned"
                )
                assert status == 200
            finally:
                conn.close()
        finally:
            server.stop()

    def test_on_wake_exception_still_returns_200(self) -> None:
        """If on_wake raises, log it but still return 200."""
        wake_raises = Mock(side_effect=RuntimeError("test error"))

        server = WebhookServer(port=0, linear_secret=_SECRET, on_wake=wake_raises)
        server.start()
        try:
            time.sleep(0.05)
            assert server._server is not None
            port = server._server.server_address[1]

            import http.client

            sig = _make_signature(_VALID_BODY)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request(
                    "POST",
                    _WEBHOOK_PATH,
                    body=_VALID_BODY,
                    headers={
                        "Content-Length": str(len(_VALID_BODY)),
                        "Linear-Signature": sig,
                    },
                )
                resp = conn.getresponse()
                assert resp.status == 200
                resp.read()
            finally:
                conn.close()
            wake_raises.assert_called_once()
        finally:
            server.stop()


class TestWebhookReadTimeout:
    """Read timeout cuts off a slow or incomplete body."""

    def test_read_timeout_slow_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Send a request with Content-Length larger than actual body;
        the server times out waiting for more data and closes the connection."""
        # Lower the timeout to 0.2s for a fast test.
        monkeypatch.setattr("symphony_linear.webhook._REQUEST_TIMEOUT", 0.2)

        wake_mock = Mock()
        server = WebhookServer(port=0, linear_secret=_SECRET, on_wake=wake_mock)
        server.start()
        try:
            time.sleep(0.05)
            assert server._server is not None
            port = server._server.server_address[1]

            # Use a raw socket to send a partial request (Content-Length says
            # 100 bytes but we send only 10, then hold the connection open).
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect(("127.0.0.1", port))

            partial_body = b"x" * 10
            sig = _make_signature(partial_body)
            request = (
                f"POST {_WEBHOOK_PATH} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Length: 100\r\n"
                f"Linear-Signature: {sig}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode() + partial_body

            sock.sendall(request)
            # Don't close — wait for the server to time out.  The server may
            # either close the connection or send an error response; either
            # is fine, we just want to confirm wake is NOT called.
            try:
                sock.recv(4096)
            except (socket.timeout, ConnectionResetError, BrokenPipeError):
                pass
            finally:
                sock.close()

            # on_wake must NOT be called — the body couldn't be fully read.
            wake_mock.assert_not_called()
        finally:
            server.stop()
