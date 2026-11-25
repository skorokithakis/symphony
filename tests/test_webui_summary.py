"""Tests for the one-line agent-reply summaries on Needs Input rows.

The HTTP layer is exercised with ``httpx.MockTransport`` so no request leaves
the process, and the render assertions go through the real server.  The
boundaries that matter are the request shape, "only agent replies are
summarised", the no-op write for a deleted ticket, and the display rule (only
Needs Input, only the latest reply, never a failed row).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from symphony_linear import anthropic
from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import (
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    LocalTicketStore,
    LocalTracker,
)
from symphony_linear.state import TicketStatus
from symphony_linear.webui import summary
from tests.test_webui_server import (
    _PASSWORD,
    _USERNAME,
    _request,
    _running_server,
    _state_entry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _reply(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": anthropic.MODEL,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
    )


def _capture(handler: Any) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(recording), seen


def _wait_for_summary(
    store: LocalTicketStore,
    ticket_id: str,
    comment_id: str,
    expected: str,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ticket = store.get(ticket_id)
        if ticket is not None:
            match = next((c for c in ticket.comments if c.id == comment_id), None)
            if match is not None and match.summary == expected:
                return
        time.sleep(0.01)
    raise AssertionError(f"Comment {comment_id} never got the summary {expected!r}")


def _needs_input_ticket(tracker: LocalTracker, title: str = "A ticket") -> str:
    ticket = tracker.store.create_started_draft(title=title, description="Body")
    tracker.store.set_state(ticket.id, STATE_NEEDS_INPUT)
    return ticket.id


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


class TestSummariseReply:
    def test_sends_the_reply_and_returns_one_clean_line(self) -> None:
        transport, seen = _capture(
            lambda request: _reply('"Fixed the widget; please check the logs."\nExtra')
        )

        result = summary.summarise_reply(
            "I fixed the widget. Check the logs.",
            api_key="sk-test",
            transport=transport,
        )

        assert result == "Fixed the widget; please check the logs."
        assert len(seen) == 1
        request = seen[0]
        assert request.method == "POST"
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "sk-test"
        body = json.loads(request.content)
        assert body["model"] == anthropic.MODEL
        assert 0 < body["max_tokens"] <= 256
        prompt = body["messages"][0]["content"]
        assert "I fixed the widget. Check the logs." in prompt
        assert "what it needs from the user next" in prompt

    def test_caps_the_summary_and_the_prompt(self) -> None:
        transport, seen = _capture(lambda request: _reply("word " * 100))

        result = summary.summarise_reply("text", api_key="sk-test", transport=transport)

        assert result is not None
        assert len(result) <= summary._MAX_SUMMARY_CHARS

        transport, seen = _capture(lambda request: _reply("A summary"))
        summary.summarise_reply(
            "x" * (summary._MAX_REPLY_CHARS + 500),
            api_key="sk-test",
            transport=transport,
        )
        prompt = json.loads(seen[0].content)["messages"][0]["content"]
        assert len(prompt) < summary._MAX_REPLY_CHARS + len(summary._PROMPT) + 100

    def test_an_empty_or_failed_reply_stores_nothing(self) -> None:
        for response in (
            httpx.Response(529, json={"error": "overloaded"}),
            httpx.Response(200, json={"content": []}),
            _reply("   \n  "),
        ):
            assert (
                summary.summarise_reply(
                    "text",
                    api_key="sk-test",
                    transport=httpx.MockTransport(lambda request: response),
                )
                is None
            )


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------


class TestReplyCallback:
    def test_without_a_key_there_is_no_callback(self) -> None:
        assert summary.agent_reply_hook(_config(None).webui) is None

    def test_agent_reply_kinds_invoke_the_hook(self, tmp_path: Path) -> None:
        hook = Mock()
        tracker = LocalTracker(tmp_path, on_agent_reply=hook)
        ticket = tracker.store.create_draft(description="x")

        comment = tracker.post_comment(ticket.id, "I fixed it.", "final")

        store, ticket_id, comment_id, reply = hook.call_args.args
        assert store is tracker.store
        assert ticket_id == ticket.id
        assert comment_id == comment.id
        assert reply == "I fixed it."

        for kind in ("final · model: provider/model", "context: 1,234 tokens"):
            hook.reset_mock()
            tracker.post_comment(ticket.id, "reply", kind)
            hook.assert_called_once()

    def test_non_agent_kinds_do_not_invoke_the_hook(self, tmp_path: Path) -> None:
        hook = Mock()
        tracker = LocalTracker(tmp_path, on_agent_reply=hook)
        ticket = tracker.store.create_draft(description="x")

        for kind in ("error", "workspace", "qa", "cleanup", "ignored"):
            tracker.post_comment(ticket.id, "notice", kind)

        hook.assert_not_called()

    def test_the_configured_callback_stores_the_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str | None] = []

        def fake(reply: str, *, api_key: str, base_url: str | None = None) -> str:
            calls.append(base_url)
            return "Fixed the widget; please check it."

        monkeypatch.setattr(summary, "summarise_reply", fake)
        config = _config("sk-test", base_url="https://anthropic-gateway.example.com")
        tracker = LocalTracker(
            tmp_path, on_agent_reply=summary.agent_reply_hook(config.webui)
        )
        ticket = tracker.store.create_draft(description="x")

        comment = tracker.post_comment(ticket.id, "I fixed the widget.", "final")

        _wait_for_summary(
            tracker.store, ticket.id, comment.id, "Fixed the widget; please check it."
        )
        assert calls == ["https://anthropic-gateway.example.com"]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestSetCommentSummary:
    def test_a_deleted_ticket_or_unknown_comment_is_a_no_op(
        self, tmp_path: Path
    ) -> None:
        store = LocalTicketStore(tmp_path / "tickets")
        ticket = store.create_draft(description="x")
        comment = store.add_comment(ticket.id, "body", kind="final")

        assert store.set_comment_summary(ticket.id, "c-missing", "s") is False

        store.delete_draft(ticket.id)

        assert store.set_comment_summary(ticket.id, comment.id, "s") is False

    def test_the_summary_does_not_reorder_the_home_list(self, tmp_path: Path) -> None:
        store = LocalTicketStore(tmp_path / "tickets")
        ticket = store.create_draft(description="x")
        comment = store.add_comment(ticket.id, "body", kind="final")
        before = store.get(ticket.id)
        assert before is not None

        assert store.set_comment_summary(ticket.id, comment.id, "A summary") is True

        after = store.get(ticket.id)
        assert after is not None
        assert after.updated_at == before.updated_at
        assert after.comments[0].summary == "A summary"


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _summary_comment(
    tracker: LocalTracker, ticket_id: str, body: str, summary: str
) -> str:
    comment = tracker.post_comment(ticket_id, body, "final")
    assert tracker.store.set_comment_summary(ticket_id, comment.id, summary)
    return comment.id


class TestHomeRowSummary:
    def test_a_needs_input_row_shows_the_latest_reply_summary(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, tracker, _state):
            ticket_id = _needs_input_ticket(tracker)
            _summary_comment(tracker, ticket_id, "First reply", "Old summary")
            _summary_comment(tracker, ticket_id, "Second reply", "Second summary")
            _status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        assert '<span class="row-summary">Second summary</span>' in text
        assert "Old summary" not in text

    def test_the_summary_is_hidden_off_needs_input_and_on_failed_rows(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, tracker, state):
            waiting = _needs_input_ticket(tracker, "Waiting")
            _summary_comment(tracker, waiting, "Reply", "Waiting summary")
            failed = _needs_input_ticket(tracker, "Failed")
            _summary_comment(tracker, failed, "Reply", "Failed summary")
            state.upsert(_state_entry(failed, status=TicketStatus.failed))
            in_progress = _needs_input_ticket(tracker, "Working")
            _summary_comment(tracker, in_progress, "Reply", "Working summary")
            tracker.store.set_state(in_progress, STATE_IN_PROGRESS)
            _status, _headers, body = _request(port, "GET", "/")

        text = body.decode()
        assert "Waiting summary" in text
        assert "Failed summary" not in text
        assert "Working summary" not in text

    def test_a_reply_without_a_summary_does_not_show_an_older_one(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path) as (port, tracker, _state):
            ticket_id = _needs_input_ticket(tracker)
            _summary_comment(tracker, ticket_id, "First reply", "Outdated summary")
            tracker.post_comment(ticket_id, "Second reply", "final")
            _status, _headers, body = _request(port, "GET", "/")

        assert "Outdated summary" not in body.decode()
