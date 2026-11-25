"""Tests for automatic ticket titles through the Anthropic Messages API.

The HTTP layer is exercised with ``httpx.MockTransport`` so no request leaves
the process; the two Start paths are exercised through the real server on an
ephemeral port, with ``title.generate_title`` stubbed.  The boundaries that
matter here are the request shape, the "never replace a title" rule and the
fact that Start does not wait for the call.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import (
    STATE_IN_PROGRESS,
    LocalTicketStore,
    LocalTracker,
)
from symphony_linear.webui import title
from tests.test_webui_editor import _post, _server
from tests.test_webui_server import _PASSWORD, _USERNAME, _origin, _request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tracker(tmp_path: Path) -> LocalTracker:
    return LocalTracker(tmp_path)


def _config(api_key: str | None, base_url: str | None = None) -> AppConfig:
    webui: dict[str, object] = {
        "port": 8080,
        "username": _USERNAME,
        "password": _PASSWORD,
    }
    if api_key is not None:
        webui["anthropic_api_key"] = api_key
    if base_url is not None:
        webui["anthropic_base_url"] = base_url
    return AppConfig.model_validate({"webui": webui})


def _reply(text: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": title.MODEL,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
    )


def _capture(
    handler: Any,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Wrap *handler* and record every request it is given."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(recording), seen


def _wait_for_title(
    store: LocalTicketStore, ticket_id: str, expected: str, timeout: float = 5.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ticket = store.get(ticket_id)
        if ticket is not None and ticket.title == expected:
            return
        time.sleep(0.01)
    stored = store.get(ticket_id)
    raise AssertionError(
        f"Ticket {ticket_id} title is {stored.title if stored else None!r}, "
        f"expected {expected!r}"
    )


def _started_ticket(tracker: LocalTracker, description: str) -> str:
    ticket = tracker.store.create_draft(description=description)
    tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
    return ticket.id


# ---------------------------------------------------------------------------
# Cleaning the reply
# ---------------------------------------------------------------------------


class TestCleanTitle:
    def test_keeps_only_the_first_non_empty_line(self) -> None:
        assert (
            title.clean_title("\n  Fix the widget\nAn explanation") == "Fix the widget"
        )

    def test_strips_surrounding_quotes_and_collapses_whitespace(self) -> None:
        assert title.clean_title('  "Fix   the   widget"  ') == "Fix the widget"
        assert title.clean_title("\u2018Fix the widget\u2019") == "Fix the widget"

    def test_caps_the_length(self) -> None:
        cleaned = title.clean_title("word " * 100)

        assert len(cleaned) <= title._MAX_TITLE_CHARS
        assert cleaned.startswith("word word")

    def test_nothing_usable_is_empty(self) -> None:
        assert title.clean_title("") == ""
        assert title.clean_title('  ""  \n\n') == ""


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


class TestGenerateTitle:
    def test_success_sends_the_documented_request(self) -> None:
        transport, seen = _capture(lambda request: _reply('"Fix the widget"'))

        result = title.generate_title(
            "The widget breaks on load.",
            api_key="sk-test",
            transport=transport,
        )

        assert result == "Fix the widget"
        assert len(seen) == 1
        request = seen[0]
        assert request.method == "POST"
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "sk-test"
        assert request.headers["anthropic-version"] == "2026-10-21"
        assert request.headers["content-type"] == "application/json"

        body = json.loads(request.content)
        assert body["model"] == title.MODEL == "claude-haiku-5-5"
        assert 0 < body["max_tokens"] <= 256
        assert body["messages"][0]["role"] == "user"
        assert "The widget breaks on load." in body["messages"][0]["content"]

    def test_a_custom_base_url_replaces_the_default(self) -> None:
        transport, seen = _capture(lambda request: _reply("A title"))

        result = title.generate_title(
            "Text",
            api_key="sk-test",
            base_url="https://anthropic-gateway.example.com",
            transport=transport,
        )

        assert result == "A title"
        assert len(seen) == 1
        assert str(seen[0].url) == "https://anthropic-gateway.example.com/v1/messages"

    def test_an_api_error_returns_none_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, _seen = _capture(
            lambda request: httpx.Response(529, json={"error": "overloaded"})
        )

        with caplog.at_level("WARNING"):
            result = title.generate_title(
                "Text", api_key="sk-test", transport=transport
            )

        assert result is None
        assert "HTTP 529" in caplog.text

    def test_a_timeout_returns_none_and_never_logs_the_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        transport = httpx.MockTransport(timeout)

        with caplog.at_level("WARNING"):
            result = title.generate_title(
                "Text", api_key="sk-do-not-log-me", transport=transport
            )

        assert result is None
        assert "ReadTimeout" in caplog.text
        assert "sk-do-not-log-me" not in caplog.text

    def test_an_unexpected_response_shape_returns_none(self) -> None:
        for response in (
            httpx.Response(200, json={"content": []}),
            httpx.Response(200, json={"content": [{"type": "tool_use"}]}),
            httpx.Response(200, json=["not", "an", "object"]),
            httpx.Response(200, text="not json"),
        ):
            assert (
                title.generate_title(
                    "Text",
                    api_key="sk-test",
                    transport=httpx.MockTransport(lambda request: response),
                )
                is None
            )

    def test_the_description_is_capped_before_it_is_sent(self) -> None:
        transport, seen = _capture(lambda request: _reply("A title"))

        title.generate_title(
            "x" * (title._MAX_DESCRIPTION_CHARS + 500),
            api_key="sk-test",
            transport=transport,
        )

        body = json.loads(seen[0].content)
        assert len(body["messages"][0]["content"]) < (
            title._MAX_DESCRIPTION_CHARS + len(title._PROMPT) + 100
        )


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------


class TestScheduleGeneration:
    def test_writes_the_title_of_a_ticket_without_one(
        self, tracker: LocalTracker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticket_id = _started_ticket(tracker, "First line\nrest")
        monkeypatch.setattr(
            title, "generate_title", lambda *args, **kwargs: "Generated title"
        )

        title.schedule_generation(tracker.store, ticket_id, _config("sk-test"))

        _wait_for_title(tracker.store, ticket_id, "Generated title")
        stored = tracker.store.get(ticket_id)
        assert stored is not None
        assert stored.state == STATE_IN_PROGRESS

    def test_the_configured_base_url_reaches_the_request(
        self, tracker: LocalTracker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticket_id = _started_ticket(tracker, "First line")
        seen: list[str | None] = []

        def record(*args: Any, **kwargs: Any) -> str:
            seen.append(kwargs.get("base_url"))
            return "Generated title"

        monkeypatch.setattr(title, "generate_title", record)

        title.schedule_generation(
            tracker.store,
            ticket_id,
            _config("sk-test", base_url="https://anthropic-gateway.example.com"),
        )

        _wait_for_title(tracker.store, ticket_id, "Generated title")
        assert seen == ["https://anthropic-gateway.example.com"]

    def test_a_user_title_is_never_replaced(
        self, tracker: LocalTracker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticket = tracker.store.create_draft(title="Mine", description="Body", repo="r")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        calls: list[str] = []

        def record(*args: Any, **kwargs: Any) -> str:
            calls.append("called")
            return "Generated title"

        monkeypatch.setattr(title, "generate_title", record)

        title.schedule_generation(tracker.store, ticket.id, _config("sk-test"))

        assert calls == []
        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.title == "Mine"

    def test_without_a_key_nothing_is_scheduled(
        self, tracker: LocalTracker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticket_id = _started_ticket(tracker, "First line")
        calls: list[str] = []

        def record(*args: Any, **kwargs: Any) -> str:
            calls.append("called")
            return "Generated"

        monkeypatch.setattr(title, "generate_title", record)

        title.schedule_generation(tracker.store, ticket_id, _config(None))

        assert calls == []

    def test_a_failed_call_changes_nothing(
        self, tracker: LocalTracker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticket_id = _started_ticket(tracker, "First line")

        def fail(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(title, "generate_title", fail)

        title.schedule_generation(tracker.store, ticket_id, _config("sk-test"))

        # The fallback is still computed from the description, nothing stored.
        stored = tracker.store.get(ticket_id)
        assert stored is not None
        assert stored.title == ""


# ---------------------------------------------------------------------------
# Start paths and the store guard
# ---------------------------------------------------------------------------


class TestStartTriggers:
    def test_the_form_post_starts_the_title_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            title, "generate_title", lambda *args, **kwargs: "Generated title"
        )
        with _server(tmp_path, api_key="sk-test") as (port, tracker, _wake):
            status, _headers, _body = _post(
                port,
                "/new/start",
                {"description": "First line\nrest of the text", "repo": "owner/name"},
            )
            assert status == 303
            (ticket,) = tracker.store.list_tickets()
            _wait_for_title(tracker.store, ticket.id, "Generated title")

            # The live endpoint reflects the title written after Start.
            _status, _headers, raw = _request(port, "GET", f"/api/tickets/{ticket.id}")
        payload = json.loads(raw)
        assert payload["title"] == "Generated title"

    def test_the_json_start_starts_the_title_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            title, "generate_title", lambda *args, **kwargs: "Generated title"
        )
        with _server(tmp_path, api_key="sk-test") as (port, tracker, _wake):
            body = json.dumps(
                {"description": "First line\nrest", "repo": "owner/name"}
            ).encode()
            status, _headers, _raw = _request(
                port,
                "POST",
                "/api/new/start",
                headers={
                    "Content-Type": "application/json",
                    "Origin": _origin(port),
                },
                body=body,
            )
            assert status == 200
            (ticket,) = tracker.store.list_tickets()
            _wait_for_title(tracker.store, ticket.id, "Generated title")
