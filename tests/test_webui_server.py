"""Tests for the web UI HTTP server, pages and static assets.

The server runs for real on an ephemeral port, in the style of
``tests/test_webhook.py``; the tracker store and StateManager point at a
temporary workspace.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import socket
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import Mock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import (
    STATE_DONE,
    STATE_DRAFT,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    LocalTracker,
)
from symphony_linear.state import StateManager, TicketState, TicketStatus, load_state
from symphony_linear.webpush import WebPushStore
from symphony_linear.webui import templates
from symphony_linear.webui.agent_api import _GUIDE_PATH
from symphony_linear.webui.http_types import Request, Response, Route
from symphony_linear.webui.push import PUBLIC_ICON_URL
from symphony_linear.webui.routes import ROUTES
from symphony_linear.webui.server import _CSP, WebUIServer
from symphony_linear.webui.views import display_title, short_repo

_USERNAME = "admin"
_PASSWORD = "secret"
_AGENT_TOKEN = "agent-token-123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    api_token: str | None = None,
    soniox_api_key: str | None = None,
    soniox_region: str | None = None,
    anthropic_api_key: str | None = None,
    models: dict[str, str] | None = None,
) -> AppConfig:
    block: dict[str, object] = {
        "port": 8080,
        "password": _PASSWORD,
    }
    if api_token is not None:
        block["api_token"] = api_token
    if soniox_api_key is not None:
        block["soniox_api_key"] = soniox_api_key
    if soniox_region is not None:
        block["soniox_region"] = soniox_region
    if anthropic_api_key is not None:
        block["anthropic_api_key"] = anthropic_api_key
    data: dict[str, object] = {"webui": block}
    if models is not None:
        data["models"] = models
    return AppConfig.model_validate(data)


def _basic(username: str = _USERNAME, password: str = _PASSWORD) -> str:
    return _raw_basic(f"{username}:{password}".encode())


def _raw_basic(decoded: bytes) -> str:
    return "Basic " + base64.b64encode(decoded).decode("ascii")


def _bearer(token: str = _AGENT_TOKEN) -> str:
    return f"Bearer {token}"


def _write_ticket(
    workspace: Path,
    ticket_id: str,
    *,
    state: str,
    title: str = "",
    description: str = "",
    repo: str = "",
    updated_at: str = "2026-11-20T10:00:00+00:00",
    state_changed_at: str | None = None,
) -> None:
    directory = workspace / "tickets"
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "id": ticket_id,
        "title": title,
        "description": description,
        "repo": repo,
        "model": None,
        "state": state,
        "comments": [],
        "created_at": updated_at,
        "updated_at": updated_at,
        "state_changed_at": state_changed_at or updated_at,
    }
    (directory / f"{ticket_id}.json").write_text(json.dumps(payload))


def _page_body(text: str) -> str:
    """The page's own markup, without the shortcuts dialog base.html adds.

    Assertions that a page does *not* offer something have to ignore that
    dialog: its static list names every workflow state and every state action
    on every page.
    """
    return text.split('<dialog class="confirm help-dialog"', 1)[0]


def _push_subscription(
    endpoint: str = "https://push.example.com/abc",
) -> dict[str, object]:
    """A browser's ``PushSubscription.toJSON()`` payload, with real key material."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    p256dh = (
        base64.urlsafe_b64encode(
            private_key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    auth = base64.urlsafe_b64encode(bytes(range(16))).rstrip(b"=").decode("ascii")
    return {
        "endpoint": endpoint,
        "expirationTime": None,
        "keys": {"p256dh": p256dh, "auth": auth},
    }


def _state_entry(ticket_id: str, **overrides: object) -> TicketState:
    values: dict[str, object] = {
        "ticket_id": ticket_id,
        "ticket_identifier": ticket_id,
        "repo_url": "git@github.com:acme/widgets.git",
        "workspace_path": f"/tmp/{ticket_id}",
        "branch": f"symphony/{ticket_id}",
    }
    values.update(overrides)
    return TicketState.model_validate(values)


def _echo_view(request: Request) -> Response:
    """Test-only POST view: echo the buffered body and its length."""
    return Response.text(
        f"body={request.body!r} length={request.content_length!r} "
        f"stream={request.body_stream is not None}"
    )


def _stream_view(request: Request) -> Response:
    """Test-only streaming POST view: consume exactly content_length bytes."""
    assert request.body_stream is not None
    assert request.content_length is not None
    data = request.body_stream.read(request.content_length)
    return Response.text(
        f"body={request.body!r} length={request.content_length!r} data={data!r}"
    )


def _drain_view(request: Request) -> Response:
    """Test-only large-body view: drain the stream without buffering it."""
    assert request.body_stream is not None
    assert request.content_length is not None
    remaining = request.content_length
    while remaining:
        chunk = request.body_stream.read(min(remaining, 65536))
        if not chunk:
            break
        remaining -= len(chunk)
    return Response.text(f"length={request.content_length!r} remaining={remaining!r}")


_TEST_ROUTES: tuple[Route, ...] = (
    Route("POST", re.compile(r"^/probe$"), _echo_view),
    Route(
        "POST",
        re.compile(r"^/stream$"),
        _stream_view,
        max_body_bytes=2 * 1024 * 1024,
        stream_body=True,
    ),
    Route(
        "POST",
        re.compile(r"^/small$"),
        _echo_view,
        max_body_bytes=8,
    ),
    Route(
        "POST",
        re.compile(r"^/big$"),
        _drain_view,
        max_body_bytes=2 * 1024 * 1024,
        stream_body=True,
    ),
)

#: A POST inside the agent namespace for the authentication tests.  The real
#: namespace has only the guide route so far, and the point here is that a
#: bearer-authenticated POST needs no Origin.
_AGENT_PROBE: Route = Route("POST", re.compile(r"^/api/v1/probe$"), _echo_view)


@contextmanager
def _running_server(
    tmp_path: Path,
    *,
    static_root: Path | None = None,
    routes: Sequence[Route] | None = None,
    max_concurrent: int = 32,
    api_token: str | None = None,
    soniox_api_key: str | None = None,
    soniox_region: str | None = None,
    anthropic_api_key: str | None = None,
    webpush: WebPushStore | None = None,
) -> Iterator[tuple[int, LocalTracker, StateManager]]:
    tracker = LocalTracker(tmp_path, webpush=webpush)
    state = load_state(tmp_path)
    server = WebUIServer(
        store=tracker.store,
        state=state,
        config=_config(
            api_token=api_token,
            soniox_api_key=soniox_api_key,
            soniox_region=soniox_region,
            anthropic_api_key=anthropic_api_key,
        ),
        on_wake=Mock(),
        port=0,
        static_root=static_root,
        routes=routes,
        max_concurrent=max_concurrent,
        webpush=tracker.webpush,
    )
    server.start()
    try:
        port = server.bound_port
        assert port is not None
        yield port, tracker, state
    finally:
        server.stop()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    auth: bool = True,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request_headers: dict[str, str] = {}
    if auth:
        request_headers["Authorization"] = _basic()
    if headers:
        request_headers.update(headers)

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _raw_request(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest(method, path)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        if body:
            connection.send(body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _origin(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _raw_socket_request(port: int, payload: bytes, *, timeout: float = 5) -> bytes:
    """Send raw bytes and return everything the server writes back."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _status_line(response: bytes) -> str:
    return response.split(b"\r\n", 1)[0].decode("latin-1")


def _headers_of(response: bytes) -> dict[str, str]:
    head = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")[1:]
    headers: dict[str, str] = {}
    for line in lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


# ---------------------------------------------------------------------------
# Pure formatting helpers
# ---------------------------------------------------------------------------


class TestFormattingHelpers:
    @pytest.mark.parametrize(
        ("repo", "expected"),
        [
            ("owner/name", "owner/name"),
            ("owner/name.git", "owner/name"),
            ("https://github.com/owner/name.git", "owner/name"),
            ("ssh://git@github.com/owner/name.git", "owner/name"),
            ("git@github.com:owner/name.git", "owner/name"),
            ("https://user:token@github.com/owner/name", "owner/name"),
            ("https://github.com/owner/name/", "owner/name"),
            ("https://github.com/owner/name.git/", "owner/name"),
            ("", "No repository"),
            ("   ", "No repository"),
            ("https://github.com/owner", "owner"),
            ("http://[::1", "http://[::1"),
        ],
    )
    def test_short_repo(self, repo: str, expected: str) -> None:
        assert short_repo(repo) == expected

    def test_display_title_falls_back_to_the_description_or_untitled(
        self, tmp_path: Path
    ) -> None:
        tracker = LocalTracker(tmp_path)
        with_line = tracker.store.create_draft(description="\n\n  First line\nSecond")
        empty = tracker.store.create_draft(description="   \n")

        assert display_title(with_line) == "First line"
        assert display_title(empty) == "Untitled draft"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class TestHomePage:
    def test_groups_are_in_workflow_order_with_counts(self, tmp_path: Path) -> None:
        _write_ticket(tmp_path, "SYM-1", state=STATE_NEEDS_INPUT, title="One")
        _write_ticket(tmp_path, "SYM-2", state=STATE_IN_PROGRESS, title="Two")
        _write_ticket(
            tmp_path,
            "SYM-3",
            state=STATE_QA,
            title="Three",
            updated_at="2026-11-21T10:00:00+00:00",
        )
        _write_ticket(tmp_path, "SYM-4", state=STATE_DRAFT, title="Four")
        _write_ticket(tmp_path, "SYM-5", state=STATE_DONE, title="Done")

        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, body = _request(port, "GET", "/")

        text = _page_body(body.decode())
        assert status == 200
        positions = [
            text.index(label) for label in ("Needs Input", "In Progress", "QA", "Draft")
        ]
        assert positions == sorted(positions)
        assert "Done" not in text
        for identifier in ("SYM-1", "SYM-2", "SYM-3", "SYM-4"):
            assert identifier in text
        assert "SYM-5" not in text
        assert 'class="group-n">1</span>' in text

    def test_saved_repository_name_replaces_the_derived_one(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, tracker, _state):
            tracker.store.add_repo("https://github.com/acme/widgets.git", "Widgets")
            saved = tracker.store.create_started_draft(
                title="Saved name", repo="https://github.com/acme/widgets.git"
            )
            tracker.store.create_started_draft(
                title="Unlisted repo", repo="https://github.com/acme/gadgets.git"
            )
            _status, _headers, body = _request(port, "GET", "/")
            _status, _headers, ticket_body = _request(
                port, "GET", f"/tickets/{saved.id}"
            )

        text = body.decode()
        saved_row = (
            '<span class="row-repo" title="https://github.com/acme/widgets.git">'
            "Widgets</span>"
        )
        derived_row = (
            '<span class="row-repo" title="https://github.com/acme/gadgets.git">'
            "acme/gadgets</span>"
        )
        assert saved_row in text
        assert derived_row in text
        saved_header = (
            '<p class="trepo" title="https://github.com/acme/widgets.git">Widgets</p>'
        )
        assert saved_header in ticket_body.decode()

    def test_failed_status_and_setup_error_use_failed_variant(
        self, tmp_path: Path
    ) -> None:
        _write_ticket(tmp_path, "SYM-1", state=STATE_NEEDS_INPUT, title="Failed")
        _write_ticket(tmp_path, "SYM-2", state=STATE_IN_PROGRESS, title="Setup failed")

        with _running_server(tmp_path) as (port, _tracker, state):
            state.upsert(_state_entry("SYM-1", status=TicketStatus.failed))
            state.upsert(_state_entry("SYM-2", setup_error="clone failed"))
            _status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        assert text.count("row--failed") == 2
        assert text.count("Last turn failed") == 2
        assert "mark--failed" in text

    def test_untrusted_values_are_escaped(self, tmp_path: Path) -> None:
        title = '<script>alert("x")</script>'
        repo = '"><img src=x onerror=alert(1)>'
        _write_ticket(
            tmp_path, "SYM-1", state=STATE_NEEDS_INPUT, title=title, repo=repo
        )

        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        assert '<script>alert("x")</script>' not in text
        assert "&lt;script&gt;alert" in text
        assert "<img src=x" not in text

    def test_malformed_repo_does_not_break_the_page(self, tmp_path: Path) -> None:
        _write_ticket(
            tmp_path,
            "SYM-1",
            state=STATE_NEEDS_INPUT,
            title="Valid repo",
            repo="owner/name",
        )
        _write_ticket(
            tmp_path,
            "SYM-2",
            state=STATE_NEEDS_INPUT,
            title="Broken repo",
            repo="http://[::1",
        )

        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        assert status == 200
        assert "owner/name" in text
        assert "http://[::1" in text


class TestArchivePage:
    def test_only_done_tickets_newest_first(self, tmp_path: Path) -> None:
        _write_ticket(
            tmp_path,
            "SYM-1",
            state=STATE_DONE,
            title="Oldest",
            state_changed_at="2026-11-01T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-2",
            state=STATE_DONE,
            title="Newest",
            state_changed_at="2026-11-03T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-3",
            state=STATE_DONE,
            title="Middle",
            state_changed_at="2026-11-02T10:00:00+00:00",
        )
        _write_ticket(tmp_path, "SYM-4", state=STATE_DRAFT, title="Draft")

        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, body = _request(port, "GET", "/archive")

        text = body.decode()
        assert status == 200
        assert "Archive" in text
        assert "Done tickets, newest first." in text
        assert text.index("SYM-2") < text.index("SYM-3") < text.index("SYM-1")
        assert "SYM-4" not in text
        assert 'class="group-n">3</span>' in text


class TestNextTicket:
    """GET /next is the "next Needs Input ticket" shortcut's redirect."""

    @pytest.fixture
    def waiting(self, tmp_path: Path) -> Path:
        _write_ticket(
            tmp_path,
            "SYM-1",
            state=STATE_NEEDS_INPUT,
            updated_at="2026-11-20T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-2",
            state=STATE_NEEDS_INPUT,
            updated_at="2026-11-20T12:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-3",
            state=STATE_NEEDS_INPUT,
            updated_at="2026-11-20T11:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-4",
            state=STATE_IN_PROGRESS,
            updated_at="2026-11-20T13:00:00+00:00",
        )
        return tmp_path

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            # The home page's Needs Input group is newest first.
            ("/next", "/tickets/SYM-2"),
            ("/next?after=SYM-2", "/tickets/SYM-3"),
            ("/next?after=SYM-3", "/tickets/SYM-1"),
            # The last ticket wraps around to the first.
            ("/next?after=SYM-1", "/tickets/SYM-2"),
            # A ticket outside the group, an unknown id and an empty value all
            # start at the first ticket.
            ("/next?after=SYM-4", "/tickets/SYM-2"),
            ("/next?after=NOPE", "/tickets/SYM-2"),
            ("/next?after=", "/tickets/SYM-2"),
        ],
    )
    def test_follows_the_home_page_order(
        self, waiting: Path, path: str, expected: str
    ) -> None:
        with _running_server(waiting) as (port, _tracker, _state):
            status, headers, body = _request(port, "GET", path)

        assert status == 303
        assert headers["Location"] == expected
        assert body == b""

    def test_empty_group_redirects_to_the_home_page(self, tmp_path: Path) -> None:
        _write_ticket(tmp_path, "SYM-1", state=STATE_IN_PROGRESS)

        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, _body = _request(port, "GET", "/next")

        assert status == 303
        assert headers["Location"] == "/"


class TestTemplates:
    def test_page_uses_external_script_and_stylesheet(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        assert re.search(
            r'<link rel="stylesheet" href="/static/symphony\.css\?v=\d+">', text
        )
        assert re.search(r'<script src="/static/app\.js\?v=\d+" defer></script>', text)
        assert "<script>" not in text
        assert "onclick=" not in text
        assert ' style="' not in text

    def test_page_links_the_manifest_with_credentials(self, tmp_path: Path) -> None:
        # Browsers fetch a manifest without credentials even same-origin, so
        # dropping crossorigin lets Basic auth answer 401 and silently
        # disables installation.
        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        assert re.search(
            r'<link rel="manifest" href="/static/manifest\.webmanifest\?v=\d+"'
            r' crossorigin="use-credentials">',
            text,
        )

    def test_static_urls_are_versioned_by_the_file_mtime(self, tmp_path: Path) -> None:
        static_dir = Path(templates.__file__).resolve().parent / "static"

        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        for name in ("symphony.css", "app.js", "home.js", "icon.png"):
            version = (static_dir / name).stat().st_mtime_ns
            assert f"/static/{name}?v={version}" in text

    def test_static_url_is_not_cached_across_calls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Statting on every call is what lets an in-place edit change the URL
        # without a daemon restart, so caching the version must fail this.
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        asset = static_dir / "app.js"
        asset.write_text("")
        monkeypatch.setattr(templates, "_STATIC_DIR", static_dir)

        os.utime(asset, ns=(1, 1))
        assert templates.static_url("app.js") == "/static/app.js?v=1"
        os.utime(asset, ns=(2, 2))
        assert templates.static_url("app.js") == "/static/app.js?v=2"

    def test_templates_are_not_reloaded_from_disk(self, tmp_path: Path) -> None:
        # The running code must keep rendering the templates of its own
        # revision: a template changed by a checkout move before the restart
        # used to be read back and fail against the old view code with a 500.
        # Both a page rendered before the change and one first rendered after
        # it must keep the startup content.
        rendered = tmp_path / "rendered.html"
        unrendered = tmp_path / "unrendered.html"
        rendered.write_text("old")
        unrendered.write_text("old")
        environment = templates._make_environment(tmp_path)
        assert environment.get_template("rendered.html").render() == "old"

        for path in (rendered, unrendered):
            path.write_text("new")
            os.utime(path, ns=(10**18, 10**18))

        assert environment.get_template("rendered.html").render() == "old"
        assert environment.get_template("unrendered.html").render() == "old"

    def test_list_continuation_loads_only_on_the_text_box_pages(
        self, tmp_path: Path
    ) -> None:
        # lists.js binds to #reply and #description by id, so it must load on
        # exactly the pages that have one.  The home page has neither.
        _write_ticket(tmp_path, "SYM-1", state=STATE_IN_PROGRESS, title="Running")

        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, _headers, home = _request(port, "GET", "/")
            _status, _headers, ticket = _request(port, "GET", "/tickets/SYM-1")
            _status, _headers, editor = _request(port, "GET", "/new")

        script = re.compile(rb'<script src="/static/lists\.js\?v=\d+" defer></script>')
        assert script.search(home) is None
        assert script.search(ticket) is not None
        assert script.search(editor) is not None


class TestDock:
    """The dock lives in one macro; these guard its per-page arguments."""

    def test_the_dock_links_carry_their_navigation_shortcuts(
        self, tmp_path: Path
    ) -> None:
        # app.js clicks these links by name from every page, including the
        # ticket and editor pages whose layout hides the dock, so a missing
        # data-shortcut would silently disable "g" there.
        _write_ticket(tmp_path, "SYM-1", state=STATE_IN_PROGRESS, title="Running")

        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        assert 'data-shortcut="gt" data-shortcut-key="t" title="Tickets (g t)"' in text
        assert 'data-shortcut="ga" data-shortcut-key="a" title="Archive (g a)"' in text
        assert (
            'data-shortcut="gr" data-shortcut-key="r" title="Repositories (g r)"'
            in text
        )


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class TestAuthentication:
    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/archive",
            "/static/symphony.css",
            "/static/app.js",
            "/static/fonts/OFL.txt",
            "/no-such-page",
        ],
    )
    def test_every_path_requires_basic_auth(self, path: str, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, _body = _request(port, "GET", path, auth=False)

        assert status == 401
        assert headers["WWW-Authenticate"].startswith("Basic")

    def test_wrong_credentials_are_rejected(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            for authorization in (
                _basic(password="wrong"),
                "Bearer token",
                "Basic",
                "Basic !!!!",
                # No colon at all: there is no password to compare, so a value
                # that happens to be the password alone must not be accepted.
                _raw_basic(_PASSWORD.encode()),
            ):
                status, _headers, _body = _request(
                    port,
                    "GET",
                    "/",
                    headers={"Authorization": authorization},
                )
                assert status == 401

    @pytest.mark.parametrize(
        ("username", "password", "expected"),
        [
            ("admin", _PASSWORD, 200),
            ("someone-else", _PASSWORD, 200),
            ("", _PASSWORD, 200),
            (" spaced ", _PASSWORD, 200),
            ("admin", "nope", 401),
            ("", "nope", 401),
            ("someone-else", "nope", 401),
        ],
    )
    def test_only_the_password_is_compared(
        self, tmp_path: Path, username: str, password: str, expected: int
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, _body = _request(
                port,
                "GET",
                "/",
                headers={"Authorization": _basic(username=username, password=password)},
            )

        assert status == expected


class TestMetrics:
    def test_metrics_requires_basic_auth_and_serves_exposition(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            unauthenticated, auth_headers, _body = _request(
                port, "GET", "/metrics", auth=False
            )
            status, headers, body = _request(port, "GET", "/metrics")

        assert unauthenticated == 401
        assert auth_headers["WWW-Authenticate"].startswith("Basic")
        assert status == 200
        assert headers["Content-Type"].startswith("text/plain")
        assert b"symphony_turns_total" in body
        assert b"symphony_tickets" in body


class TestRawRequestLines:
    """Raw-socket checks for cases the base handler would answer itself."""

    def test_unknown_verb_is_authenticated_before_dispatch(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            response = _raw_socket_request(
                port,
                b"BREW / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n",
            )

        assert _status_line(response).startswith("HTTP/1.0 401")
        assert "www-authenticate" in _headers_of(response)
        assert "content-security-policy" in _headers_of(response)

    def test_unknown_verb_is_same_origin_checked(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            response = _raw_socket_request(
                port,
                (
                    "BREW / HTTP/1.0\r\n"
                    "Host: 127.0.0.1\r\n"
                    f"Authorization: {_basic()}\r\n"
                    "\r\n"
                ).encode(),
            )

        assert _status_line(response).startswith("HTTP/1.0 403")

    def test_unknown_verb_reaches_routing_when_guards_pass(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            response = _raw_socket_request(
                port,
                (
                    "BREW / HTTP/1.0\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"Authorization: {_basic()}\r\n"
                    f"Origin: http://127.0.0.1:{port}\r\n"
                    "\r\n"
                ).encode(),
            )

        assert _status_line(response).startswith("HTTP/1.0 405")

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"GET /\r\n", id="http-0.9-shorthand"),
            pytest.param(b"GET / HTTP/0.9\r\n\r\n", id="http-0.9-version"),
        ],
    )
    def test_an_http_0_9_request_is_rejected_with_a_framed_error(
        self, tmp_path: Path, payload: bytes
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            response = _raw_socket_request(port, payload)

        assert _status_line(response).startswith("HTTP/1.0 400")
        headers = _headers_of(response)
        assert "content-length" in headers
        assert "content-security-policy" in headers
        assert headers["x-content-type-options"] == "nosniff"

    def test_malformed_request_line_gets_a_framed_error(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            response = _raw_socket_request(port, b"GARBAGE\r\n\r\n")

        assert _status_line(response).startswith("HTTP/1.0 400")
        assert "content-security-policy" in _headers_of(response)

    def test_unauthenticated_head_is_framed_without_a_body(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            response = _raw_socket_request(port, b"HEAD / HTTP/1.0\r\n\r\n")

        assert _status_line(response).startswith("HTTP/1.0 401")
        headers = _headers_of(response)
        assert "www-authenticate" in headers
        assert "content-security-policy" in headers
        assert response.split(b"\r\n\r\n", 1)[1] == b""


class TestSameOrigin:
    def test_post_without_origin_is_forbidden(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, _body = _request(
                port,
                "POST",
                "/",
                headers={"Content-Length": "0"},
                body=b"",
            )

        assert status == 403

    def test_post_with_foreign_origin_is_forbidden(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, _body = _request(
                port,
                "POST",
                "/",
                headers={"Origin": "http://evil.example", "Content-Length": "0"},
                body=b"",
            )

        assert status == 403

    @pytest.mark.parametrize(
        ("scheme", "forwarded_proto"),
        [
            pytest.param("http", None, id="plain-http"),
            pytest.param("http", "http", id="http-behind-http"),
            pytest.param("https", None, id="https-without-proxy"),
            pytest.param("https", "https", id="https-behind-https"),
        ],
    )
    def test_allowed_origin_forms_reach_routing(
        self, tmp_path: Path, scheme: str, forwarded_proto: str | None
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            headers = {"Origin": f"{scheme}://127.0.0.1:{port}"}
            if forwarded_proto is not None:
                headers["X-Forwarded-Proto"] = forwarded_proto
            status, _headers, _body = _request(
                port,
                "POST",
                "/no-such-page",
                headers={**headers, "Content-Length": "0"},
                body=b"",
            )

        assert status == 404

    @pytest.mark.parametrize(
        ("origin", "forwarded_proto"),
        [
            pytest.param("wrong-port", None, id="wrong-port"),
            pytest.param("http", "https", id="http-downgrade"),
        ],
    )
    def test_origin_mismatch_and_downgrade_are_forbidden(
        self, tmp_path: Path, origin: str, forwarded_proto: str | None
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            netloc = (
                f"127.0.0.1:{port + 1}"
                if origin == "wrong-port"
                else f"127.0.0.1:{port}"
            )
            headers = {"Origin": f"http://{netloc}"}
            if forwarded_proto is not None:
                headers["X-Forwarded-Proto"] = forwarded_proto
            status, _headers, _body = _request(
                port,
                "POST",
                "/",
                headers={**headers, "Content-Length": "0"},
                body=b"",
            )

        assert status == 403

    def test_get_is_not_subject_to_the_origin_check(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, _body = _request(
                port, "GET", "/", headers={"Origin": "http://evil.example"}
            )

        assert status == 200

    def test_post_to_a_get_only_route_is_method_not_allowed(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, _body = _request(
                port,
                "POST",
                "/",
                headers={"Origin": _origin(port), "Content-Length": "0"},
                body=b"",
            )

        assert status == 405
        assert "GET" in headers["Allow"]


class TestAgentAPI:
    """The bearer-authenticated ``/api/v1`` namespace.

    These run against the real route table and the real server, so they cover
    the dispatch ordering (namespace check before Basic auth), not a copy of
    it in a helper.
    """

    def test_no_configured_token_hides_the_namespace(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            without_auth, _headers, _body = _request(port, "GET", "/api/v1", auth=False)
            with_bearer, _headers, _body = _request(
                port,
                "GET",
                "/api/v1",
                auth=False,
                headers={"Authorization": _bearer()},
            )

        assert without_auth == 404
        assert with_bearer == 404

    def test_no_configured_token_advertises_no_cors(self, tmp_path: Path) -> None:
        # A preflight is answered like any other unknown route, so a daemon
        # without the API never reveals the namespace through CORS.
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, _body = _request(port, "OPTIONS", "/api/v1", auth=False)

        assert status == 404
        assert "Access-Control-Allow-Origin" not in headers

    def test_a_preflight_is_anonymous_and_the_same_for_every_path(
        self, tmp_path: Path
    ) -> None:
        # A browser never sends Authorization on a preflight, so the namespace
        # cannot require the token there.  One answer covers the whole
        # namespace, including paths that do not exist, because the real
        # request still gets the informative JSON 404.
        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            responses = [
                _request(port, "OPTIONS", path, auth=False)
                for path in ("/api/v1", "/api/v1/tickets", "/api/v1/nope")
            ]

        for status, headers, body in responses:
            assert status == 204
            assert body == b""
            assert headers["Access-Control-Allow-Origin"] == "*"
            assert (
                headers["Access-Control-Allow-Methods"]
                == "GET, POST, PATCH, DELETE, OPTIONS"
            )
            assert headers["Access-Control-Allow-Headers"] == (
                "Authorization, Content-Type"
            )
            assert headers["Access-Control-Max-Age"] == "86400"

    def test_every_namespace_response_carries_the_cors_header(
        self, tmp_path: Path
    ) -> None:
        # The guide, a 401, a 404 and a 405 are the paths that run before a
        # view does, so the header has to come from the response writer rather
        # than from each view.
        bearer = {"Authorization": _bearer()}
        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            responses = [
                _request(port, "GET", "/api/v1", auth=False),
                _request(port, "GET", "/api/v1/tickets", auth=False),
                _request(port, "GET", "/api/v1/nope", auth=False, headers=bearer),
                _request(port, "PUT", "/api/v1", auth=False, headers=bearer),
            ]

        assert [status for status, _headers, _body in responses] == [
            200,
            401,
            404,
            405,
        ]
        for _status, headers, _body in responses:
            assert headers["Access-Control-Allow-Origin"] == "*"

    def test_a_browser_route_carries_no_cors_header(self, tmp_path: Path) -> None:
        # OPTIONS on a browser route still goes through Basic auth and the
        # same-origin check, then gets the plain 405 exactly as before.
        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            status, headers, _body = _request(
                port, "OPTIONS", "/", headers={"Origin": _origin(port)}
            )

        assert status == 405
        assert "Access-Control-Allow-Origin" not in headers

    @pytest.mark.parametrize(
        "authorization",
        [
            pytest.param(None, id="missing"),
            pytest.param(_bearer("wrong"), id="wrong-token"),
            pytest.param("Bearer", id="empty-token"),
            pytest.param(_basic(), id="basic-credentials"),
        ],
    )
    def test_only_the_matching_bearer_is_accepted(
        self, tmp_path: Path, authorization: str | None
    ) -> None:
        headers = {} if authorization is None else {"Authorization": authorization}

        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            status, _headers, body = _request(
                port, "GET", "/api/v1/tickets", auth=False, headers=headers
            )

        assert status == 401
        payload = json.loads(body)
        assert set(payload) == {"error", "hint"}
        assert payload["error"]
        assert "webui.api_token" in payload["hint"]

    def test_the_guide_needs_no_token(self, tmp_path: Path) -> None:
        # An agent reads the guide before it is given the key.  A wrong token
        # is ignored rather than refused: the request needs no credential at
        # all, so there is nothing to compare.
        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            status, headers, body = _request(port, "GET", "/api/v1", auth=False)
            wrong_token = _request(
                port,
                "GET",
                "/api/v1",
                auth=False,
                headers={"Authorization": _bearer("wrong")},
            )[0]
            head_status, head_headers, head_body = _request(
                port, "HEAD", "/api/v1", auth=False
            )

        assert status == 200
        assert headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert headers["Content-Security-Policy"] == _CSP
        assert body == _GUIDE_PATH.read_bytes()
        assert wrong_token == 200
        assert head_status == 200
        assert head_headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert head_body == b""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            pytest.param("POST", "/api/v1", id="post"),
            pytest.param("PUT", "/api/v1", id="put"),
            pytest.param("DELETE", "/api/v1", id="delete"),
            # The exemption is the exact raw path: a trailing slash is a
            # different route, and a percent-encoded alias must not reach the
            # anonymous read by another name.
            pytest.param("GET", "/api/v1/", id="trailing-slash"),
            pytest.param("GET", "/%61pi/v1", id="percent-encoded"),
        ],
    )
    def test_only_a_get_or_head_of_the_exact_path_is_exempt(
        self, tmp_path: Path, method: str, path: str
    ) -> None:
        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            status, _headers, _body = _request(port, method, path, auth=False)

        assert status == 401

    def test_subpaths_are_protected_and_neighbours_are_not(
        self, tmp_path: Path
    ) -> None:
        # The namespace is the prefix plus a separator, not a plain string
        # prefix: /api/v10 must stay on the Basic-auth side.
        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            unauthorized = _request(port, "GET", "/api/v1/nope", auth=False)[0]
            authorized = _request(
                port,
                "GET",
                "/api/v1/nope",
                auth=False,
                headers={"Authorization": _bearer()},
            )[0]
            neighbour = _request(
                port,
                "GET",
                "/api/v10",
                auth=False,
                headers={"Authorization": _bearer()},
            )[0]

        assert unauthorized == 401
        assert authorized == 404
        assert neighbour == 401

    def test_the_guide_is_markdown_for_a_lowercase_scheme(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            status, headers, body = _request(
                port,
                "GET",
                "/api/v1",
                auth=False,
                headers={"Authorization": f"bearer {_AGENT_TOKEN}"},
            )

        assert status == 200
        assert headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert headers["Content-Security-Policy"] == _CSP
        assert body.startswith(b"# ")
        assert b"webui.api_token" in body

    def test_bearer_on_a_browser_route_is_rejected(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, api_token=_AGENT_TOKEN) as (port, _t, _s):
            status, headers, _body = _request(
                port,
                "GET",
                "/",
                auth=False,
                headers={"Authorization": _bearer()},
            )

        assert status == 401
        assert headers["WWW-Authenticate"].startswith("Basic")

    def test_a_bearer_post_without_origin_reaches_the_view(
        self, tmp_path: Path
    ) -> None:
        routes = (*ROUTES, _AGENT_PROBE)

        with _running_server(tmp_path, api_token=_AGENT_TOKEN, routes=routes) as (
            port,
            _t,
            _s,
        ):
            status, _headers, body = _request(
                port,
                "POST",
                "/api/v1/probe",
                auth=False,
                headers={"Authorization": _bearer()},
                body=b"hi",
            )

        assert status == 200
        assert "body=b'hi'" in body.decode()

    def test_the_body_cap_still_applies(self, tmp_path: Path) -> None:
        routes = (*ROUTES, _AGENT_PROBE)
        oversized = b"x" * (1024 * 1024 + 1)

        with _running_server(tmp_path, api_token=_AGENT_TOKEN, routes=routes) as (
            port,
            _t,
            _s,
        ):
            status, _headers, _body = _request(
                port,
                "POST",
                "/api/v1/probe",
                auth=False,
                headers={"Authorization": _bearer()},
                body=oversized,
            )

        assert status == 413


class TestBodyLimits:
    def test_content_length_is_required(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            status, _headers, _body = _raw_request(
                port,
                "POST",
                "/probe",
                {
                    "Authorization": _basic(),
                    "Origin": _origin(port),
                },
            )

        assert status == 411

    @pytest.mark.parametrize(
        "content_length",
        ["not-a-number", "+5", "5.0", "0x10", "", "5, 5"],
    )
    def test_non_decimal_content_length_is_rejected(
        self, content_length: str, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            status, _headers, _body = _raw_request(
                port,
                "POST",
                "/probe",
                {
                    "Authorization": _basic(),
                    "Origin": _origin(port),
                    "Content-Length": content_length,
                },
            )

        assert status == 400

    def test_duplicate_content_length_is_rejected(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            response = _raw_socket_request(
                port,
                (
                    "POST /probe HTTP/1.0\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"Authorization: {_basic()}\r\n"
                    f"Origin: http://127.0.0.1:{port}\r\n"
                    "Content-Length: 2\r\n"
                    "Content-Length: 2\r\n"
                    "\r\n"
                ).encode(),
            )

        assert _status_line(response).startswith("HTTP/1.0 400")

    def test_oversized_body_is_rejected_before_reading(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            status, _headers, _body = _raw_request(
                port,
                "POST",
                "/probe",
                {
                    "Authorization": _basic(),
                    "Origin": _origin(port),
                    "Content-Length": str(2 * 1024 * 1024),
                },
            )

        assert status == 413

    def test_chunked_body_is_rejected(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            status, _headers, _body = _raw_request(
                port,
                "POST",
                "/probe",
                {
                    "Authorization": _basic(),
                    "Origin": _origin(port),
                    "Transfer-Encoding": "chunked",
                },
            )

        assert status == 400

    def test_unknown_post_route_is_answered_without_waiting_for_a_body(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                sock.sendall(
                    (
                        "POST /no-such-page HTTP/1.0\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        f"Authorization: {_basic()}\r\n"
                        f"Origin: http://127.0.0.1:{port}\r\n"
                        "Content-Length: 100\r\n"
                        "\r\n"
                    ).encode()
                )
                sock.settimeout(2)
                response = sock.recv(65536)
            finally:
                sock.close()

        assert response.startswith(b"HTTP/1.0 404")

    def test_per_route_body_limit(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            too_large, _h, _b = _request(
                port,
                "POST",
                "/small",
                headers={"Origin": _origin(port)},
                body=b"123456789",
            )
            allowed, _h, body = _request(
                port,
                "POST",
                "/small",
                headers={"Origin": _origin(port)},
                body=b"12345678",
            )

        assert too_large == 413
        assert allowed == 200
        assert "body=b'12345678'" in body.decode()
        assert "length=8" in body.decode()
        assert "stream=False" in body.decode()

    def test_streaming_route_receives_the_unread_body_stream(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            status, _headers, body = _request(
                port,
                "POST",
                "/stream",
                headers={"Origin": _origin(port)},
                body=b"hello",
            )

        text = body.decode()
        assert status == 200
        assert "body=b''" in text
        assert "length=5" in text
        assert "data=b'hello'" in text

    def test_streaming_route_can_raise_the_default_limit(self, tmp_path: Path) -> None:
        payload = b"x" * (1024 * 1024 + 1)
        with _running_server(tmp_path, routes=_TEST_ROUTES) as (port, _t, _s):
            status, _headers, body = _request(
                port,
                "POST",
                "/big",
                headers={"Origin": _origin(port)},
                body=payload,
            )

        assert status == 200
        assert "length=1048577" in body.decode()
        assert "remaining=0" in body.decode()


class TestResponseHeaders:
    def test_security_headers_on_success(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, headers, _body = _request(port, "GET", "/")

        assert headers["Content-Security-Policy"] == _CSP
        assert "script-src 'self'" in _CSP
        assert "worker-src 'self'" in _CSP
        assert "style-src 'self'" in _CSP
        assert "'unsafe-inline'" not in _CSP
        assert "frame-ancestors 'none'" in _CSP
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"

    def test_security_headers_on_errors(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            responses = [
                _request(port, "GET", "/", auth=False),
                _request(port, "GET", "/no-such-page"),
                _request(
                    port,
                    "POST",
                    "/",
                    headers={"Content-Length": "0"},
                    body=b"",
                ),
            ]

        for _status, headers, _body in responses:
            assert headers["Content-Security-Policy"] == _CSP
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["X-Frame-Options"] == "DENY"

    def test_head_returns_headers_without_a_body(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, body = _request(port, "HEAD", "/")

        assert status == 200
        assert body == b""
        assert int(headers["Content-Length"]) > 0
        assert headers["Content-Security-Policy"] == _CSP


class TestSonioxDictation:
    """The optional dictation route and its CSP exception.

    These are the security-relevant parts: a daemon without a Soniox key must
    be indistinguishable from one that never had the feature, and the
    temporary-key route must stay behind the normal dispatch pipeline.
    """

    #: The policy without dictation.  Hard-coded on purpose: comparing with
    #: the module's own constant would not notice a change to it.
    _BASE_CSP = (
        "default-src 'none'; "
        "script-src 'self'; "
        "worker-src 'self'; "
        "style-src 'self'; "
        "img-src 'self'; "
        "manifest-src 'self'; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )

    def test_csp_is_unchanged_without_a_key(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, _body = _request(port, "GET", "/")

        assert status == 200
        assert headers["Content-Security-Policy"] == self._BASE_CSP

    def test_csp_allows_the_soniox_websocket_with_a_key(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, soniox_api_key="long-lived-soniox-key") as (
            port,
            _tracker,
            _state,
        ):
            _status, headers, _body = _request(port, "GET", "/")

        policy = headers["Content-Security-Policy"]
        assert "connect-src 'self' wss://stt-rt.soniox.com;" in policy
        # The rest of the policy, and the default one in particular, is
        # untouched.
        assert policy != _CSP
        assert "script-src 'self';" in policy
        assert "frame-ancestors 'none'" in policy

    def test_temporary_key_route_is_absent_without_a_key(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, _body = _request(
                port,
                "POST",
                "/api/soniox-key",
                headers={"Origin": _origin(port)},
                body=b"{}",
            )

        assert status == 404

    def test_editor_offers_the_button_only_with_a_key(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, _headers, without_key = _request(port, "GET", "/new")
        with _running_server(tmp_path, soniox_api_key="long-lived-soniox-key") as (
            port,
            _tracker,
            _state,
        ):
            _status, _headers, with_key = _request(port, "GET", "/new")

        assert b"data-dictation" not in without_key
        assert b"dictation.js" not in without_key
        assert b'data-dictation="description"' in with_key
        assert b"dictation.js" in with_key

    def test_temporary_key_route_is_same_origin_guarded(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, soniox_api_key="long-lived-soniox-key") as (
            port,
            _tracker,
            _state,
        ):
            status, _headers, _body = _request(
                port, "POST", "/api/soniox-key", body=b"{}"
            )

        assert status == 403

    def test_temporary_key_route_mints_a_single_use_key(self, tmp_path: Path) -> None:
        minted = httpx.Response(201, json={"api_key": "snx_temp_test"})
        with patch("symphony_linear.webui.api.httpx.post", return_value=minted) as post:
            with _running_server(tmp_path, soniox_api_key="long-lived-soniox-key") as (
                port,
                _tracker,
                _state,
            ):
                status, _headers, body = _request(
                    port,
                    "POST",
                    "/api/soniox-key",
                    headers={"Origin": _origin(port)},
                    body=b"{}",
                )

        assert status == 200
        assert json.loads(body) == {
            "api_key": "snx_temp_test",
            "websocket_url": "wss://stt-rt.soniox.com/transcribe-websocket",
        }
        # The long-lived key must only ever travel in the outbound header.
        assert b"long-lived-soniox-key" not in body
        assert post.call_args.args[0] == (
            "https://api.soniox.com/v1/auth/temporary-api-key"
        )
        assert post.call_args.kwargs["headers"] == {
            "Authorization": "Bearer long-lived-soniox-key"
        }
        assert post.call_args.kwargs["json"] == {
            "usage_type": "transcribe_websocket",
            "expires_in_seconds": 60,
            "single_use": True,
        }

    def test_soniox_error_message_reaches_the_page(self, tmp_path: Path) -> None:
        refused = httpx.Response(401, json={"message": "Incorrect API key provided."})
        with patch("symphony_linear.webui.api.httpx.post", return_value=refused):
            with _running_server(tmp_path, soniox_api_key="long-lived-soniox-key") as (
                port,
                _tracker,
                _state,
            ):
                status, _headers, body = _request(
                    port,
                    "POST",
                    "/api/soniox-key",
                    headers={"Origin": _origin(port)},
                    body=b"{}",
                )

        # A gateway status, not a mirrored 401: the browser must not read that
        # as "your Basic password is wrong" and prompt again.
        assert status == 502
        assert json.loads(body) == {"error": "Incorrect API key provided."}

    def test_soniox_error_message_redacts_the_long_lived_key(
        self, tmp_path: Path
    ) -> None:
        refused = httpx.Response(
            401,
            json={"message": "Key long-lived-soniox-key is not valid."},
        )
        with patch("symphony_linear.webui.api.httpx.post", return_value=refused):
            with _running_server(tmp_path, soniox_api_key="long-lived-soniox-key") as (
                port,
                _tracker,
                _state,
            ):
                status, _headers, body = _request(
                    port,
                    "POST",
                    "/api/soniox-key",
                    headers={"Origin": _origin(port)},
                    body=b"{}",
                )

        assert status == 502
        assert json.loads(body) == {"error": "Key [redacted] is not valid."}
        assert b"long-lived-soniox-key" not in body


class TestStaticFiles:
    def test_stylesheet_is_local_and_stops_before_mockup_helpers(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, body = _request(port, "GET", "/static/symphony.css")

        text = body.decode()
        assert status == 200
        assert headers["Content-Type"].startswith("text/css")
        assert "/* ---------- 12. Ticket editor and dialogs" in text
        assert "13. Mockup helpers" not in text
        assert "http://" not in text
        assert "https://" not in text
        assert "@font-face" in text
        assert 'url("fonts/atkinson-hyperlegible-mono.woff2")' in text
        assert 'url("fonts/atkinson-hyperlegible-next.woff2")' in text

    @pytest.mark.parametrize(
        "name",
        ["atkinson-hyperlegible-mono.woff2", "atkinson-hyperlegible-next.woff2"],
    )
    def test_fonts_are_served_as_woff2(self, name: str, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, body = _request(port, "GET", f"/static/fonts/{name}")

        assert status == 200
        assert headers["Content-Type"] == "font/woff2"
        assert body.startswith(b"wOF2")

    @pytest.mark.parametrize(
        ("name", "marker"),
        [
            ("app.js", b"querySelectorAll"),
            ("lists.js", b"insertText"),
        ],
    )
    def test_javascript_is_served(
        self, name: str, marker: bytes, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, body = _request(port, "GET", f"/static/{name}")

        assert status == 200
        assert headers["Content-Type"].startswith("text/javascript")
        assert marker in body

    def test_the_manifest_is_served_with_its_icons(self, tmp_path: Path) -> None:
        # A wrong content type or a missing icon file makes the UI
        # uninstallable with no visible error, so check both through the
        # server rather than reading the files off disk.
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, body = _request(
                port, "GET", "/static/manifest.webmanifest"
            )
            icons = [
                _request(port, "GET", icon["src"]) for icon in json.loads(body)["icons"]
            ]

        assert status == 200
        assert headers["Content-Type"] == "application/manifest+json"
        for icon_status, icon_headers, _icon_body in icons:
            assert icon_status == 200
            assert icon_headers["Content-Type"] == "image/png"

    def test_only_versioned_requests_are_cacheable(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            versioned_status, versioned_headers, _body = _request(
                port, "GET", "/static/app.js?v=1"
            )
            plain_status, plain_headers, _body = _request(port, "GET", "/static/app.js")

        assert versioned_status == 200
        assert (
            versioned_headers["Cache-Control"] == "private, max-age=31536000, immutable"
        )
        assert plain_status == 200
        assert "Cache-Control" not in plain_headers

    @pytest.mark.parametrize(
        "path",
        [
            "/static/../symphony_linear/config.py",
            "/static/%2e%2e/%2e%2e/config.yaml",
            "/static/%2e%2e/symphony_linear/config.py",
            "/static/fonts/../../__init__.py",
            "/static/%2fetc%2fpasswd",
            "/static/%00symphony.css",
            "/static/fonts/",
        ],
    )
    def test_static_lookup_cannot_escape_the_static_directory(
        self, path: str, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, _headers, body = _request(port, "GET", path)

        assert status == 404
        assert b"api_key" not in body
        assert b"def main" not in body

    def test_symlink_inside_static_root_cannot_read_outside(
        self, tmp_path: Path
    ) -> None:
        static_root = tmp_path / "static"
        static_root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET")
        (static_root / "leak.txt").symlink_to(secret)

        with _running_server(tmp_path, static_root=static_root) as (port, _t, _s):
            status, _headers, body = _request(port, "GET", "/static/leak.txt")

        assert status == 404
        assert b"TOP-SECRET" not in body


class TestServerLifecycle:
    def test_stop_is_safe_before_start(self, tmp_path: Path) -> None:
        server = WebUIServer(
            store=LocalTracker(tmp_path).store,
            state=load_state(tmp_path),
            config=_config(),
            on_wake=Mock(),
            port=0,
        )

        server.stop()
        assert server.bound_port is None

    def test_failed_thread_start_closes_the_listener_and_does_not_shutdown(
        self, tmp_path: Path
    ) -> None:
        with (
            patch("symphony_linear.webui.server._WebUIServer") as server_class,
            patch(
                "symphony_linear.webui.server.threading.Thread.start",
                side_effect=RuntimeError("cannot start thread"),
            ),
        ):
            fake_server = server_class.return_value
            server = WebUIServer(
                store=LocalTracker(tmp_path).store,
                state=load_state(tmp_path),
                config=_config(),
                on_wake=Mock(),
                port=0,
            )

            with pytest.raises(RuntimeError, match="cannot start thread"):
                server.start()

            assert server.bound_port is None
            fake_server.server_close.assert_called_once()
            server.stop()
            fake_server.shutdown.assert_not_called()

    def test_stop_after_a_failed_real_start_returns(self, tmp_path: Path) -> None:
        with patch(
            "symphony_linear.webui.server.threading.Thread.start",
            side_effect=RuntimeError("cannot start thread"),
        ):
            server = WebUIServer(
                store=LocalTracker(tmp_path).store,
                state=load_state(tmp_path),
                config=_config(),
                on_wake=Mock(),
                port=0,
            )
            with pytest.raises(RuntimeError, match="cannot start thread"):
                server.start()

        # shutdown() would block forever on a loop that never ran.
        server.stop()
        assert server.bound_port is None

    def test_stop_returns_before_the_serve_loop_is_entered(
        self, tmp_path: Path
    ) -> None:
        """A slow-starting thread must not make stop() wait for a loop forever.

        The thread is paused before its serve loop.  stop() must notice that
        the loop was never entered, close the listener and return; calling
        shutdown() there would block until a serve_forever() that never runs.
        """
        entered = threading.Event()
        release = threading.Event()
        original_serve = WebUIServer._serve

        def delayed_serve(self: WebUIServer, bound_server: object) -> None:
            entered.set()
            release.wait(5)
            original_serve(self, bound_server)  # type: ignore[arg-type]

        server = WebUIServer(
            store=LocalTracker(tmp_path).store,
            state=load_state(tmp_path),
            config=_config(),
            on_wake=Mock(),
            port=0,
        )
        with patch.object(WebUIServer, "_serve", delayed_serve):
            server.start()
            assert entered.wait(5)
            stopper = threading.Thread(target=server.stop, daemon=True)
            stopper.start()
            try:
                time.sleep(0.2)
            finally:
                release.set()
            stopper.join(5)

        assert not stopper.is_alive()
        assert server.bound_port is None

    def test_concurrency_over_the_limit_gets_503(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, max_concurrent=1) as (port, _t, _s):
            first = socket.create_connection(("127.0.0.1", port), timeout=5)
            second: socket.socket | None = None
            try:
                # A partial request holds the only handler slot in
                # parse_request() until its headers arrive.
                first.sendall(b"GET / HTTP/1.0\r\n")
                time.sleep(0.2)
                second = socket.create_connection(("127.0.0.1", port), timeout=5)
                second.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                second.settimeout(2)
                response = second.recv(65536)
            finally:
                if second is not None:
                    second.close()
                first.close()

        assert response.startswith(b"HTTP/1.0 503")
        headers = _headers_of(response)
        assert headers["content-security-policy"] == _CSP
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["connection"] == "close"


class TestWebPush:
    """The browser half of Web Push: the worker route, the bell and the endpoint.

    The store and the sender are covered in ``test_webpush.py``; these tests
    are about the HTTP plumbing review would otherwise have to take on trust.
    """

    def test_the_worker_is_served_for_root_scope_behind_auth(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path, webpush=WebPushStore(tmp_path)) as (
            port,
            _tracker,
            _state,
        ):
            status, headers, body = _request(port, "GET", "/sw.js")
            unauthenticated = _request(port, "GET", "/sw.js", auth=False)[0]

        assert status == 200
        assert headers["Content-Type"] == "text/javascript; charset=utf-8"
        # A worker's registration carries the page's credentials, but it must
        # still be behind the one dispatch pipeline like every other route.
        assert unauthenticated == 401
        assert b'addEventListener("push"' in body
        assert b'addEventListener("notificationclick"' in body
        # The notification service fetches the icon without Basic credentials,
        # so it must be the same public GitHub copy the worker's CSP allows.
        assert PUBLIC_ICON_URL.encode() in body

    def test_the_worker_csp_adds_only_the_icon_origin(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, webpush=WebPushStore(tmp_path)) as (
            port,
            _tracker,
            _state,
        ):
            _status, headers, _body = _request(port, "GET", "/sw.js")

        # Built from the page policy here rather than through the route's own
        # helper, so a change to the policy that skipped the replacement fails
        # this test instead of quietly blocking the notification icon.
        expected = _CSP.replace("img-src 'self';", f"img-src 'self' {PUBLIC_ICON_URL};")
        assert expected != _CSP
        assert headers["Content-Security-Policy"] == expected

    def test_the_bell_appears_only_with_a_store(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            _status, _headers, without = _request(port, "GET", "/")
            refused = _request(
                port,
                "POST",
                "/api/push-subscriptions",
                headers={"Origin": _origin(port)},
                body=b"{}",
            )[0]

        store = WebPushStore(tmp_path)
        with _running_server(tmp_path, webpush=store) as (port, _tracker, _state):
            _status, _headers, with_bell = _request(port, "GET", "/")

        assert b'id="push-bell"' not in without
        assert refused == 404
        assert b'id="push-bell"' in with_bell
        assert f'data-push-public-key="{store.public_key()}"'.encode() in with_bell

    def test_the_endpoint_stores_a_browser_subscription(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)
        payload = _push_subscription()
        with _running_server(tmp_path, webpush=store) as (port, _tracker, _state):
            status, _headers, body = _request(
                port,
                "POST",
                "/api/push-subscriptions",
                headers={
                    "Origin": _origin(port),
                    "Content-Type": "application/json",
                },
                body=json.dumps(payload).encode(),
            )

        assert status == 201
        assert json.loads(body) == {"ok": True}
        # The same instance the tracker sends from, so the subscription is live
        # immediately.
        assert [entry.endpoint for entry in store.subscriptions()] == [
            payload["endpoint"]
        ]

    def test_the_endpoint_rejects_a_non_https_endpoint(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)
        with _running_server(tmp_path, webpush=store) as (port, _tracker, _state):
            status, _headers, body = _request(
                port,
                "POST",
                "/api/push-subscriptions",
                headers={
                    "Origin": _origin(port),
                    "Content-Type": "application/json",
                },
                body=json.dumps(
                    _push_subscription("http://push.example.com/x")
                ).encode(),
            )

        assert status == 400
        assert "https" in json.loads(body)["error"]
        assert store.subscriptions() == []
