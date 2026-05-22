"""Tests for the OpenCode adapter module.

Only the JSON event parser is exercised here. We deliberately do not run
the real ``opencode`` binary in tests: it requires a live LLM, model
credentials, and is inherently non-deterministic. The parser is what we
own; everything else is OpenCode's problem.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from symphony_linear.opencode import _assemble_message, _extract_context_tokens

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# Ensure DEBUG logs are visible during test runs.
logging.basicConfig(level=logging.DEBUG)


# ---------------------------------------------------------------------------
# Unit: JSON event parser (uses fixture data)
# ---------------------------------------------------------------------------


class TestParseEventsFromFixture:
    """Parse the fixture NDJSON file and verify basic event structure."""

    def test_fixture_has_three_events(self) -> None:
        """The captured fixture should contain exactly three events."""
        events = _load_fixture_events()
        assert len(events) == 3

    def test_fixture_contains_session_id(self) -> None:
        """Every event should carry a sessionID."""
        events = _load_fixture_events()
        for evt in events:
            assert "sessionID" in evt
            assert isinstance(evt["sessionID"], str)
            assert evt["sessionID"].startswith("ses_")

    def test_fixture_event_types(self) -> None:
        """The three events should be step_start, text, step_finish."""
        events = _load_fixture_events()
        types = [evt["type"] for evt in events]
        assert types == ["step_start", "text", "step_finish"]

    def test_extract_text_and_session_id(self) -> None:
        """Simulate the parser logic: session_id from first event,
        text from text events, detect step_finish."""
        events = _load_fixture_events()

        session_id: str | None = None
        text_parts: list[str] = []
        finished = False

        for evt in events:
            if session_id is None:
                session_id = evt.get("sessionID")
            if evt.get("type") == "text":
                text_parts.append(evt["part"]["text"])
            if evt.get("type") == "step_finish":
                finished = True

        assert session_id == "ses_1e3790378ffecZySU3wIpFOoIz"
        assert "".join(text_parts) == "hi"
        assert finished is True

    def test_parse_corrupt_line_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        """A corrupt JSON line should be logged and skipped."""
        corrupt_line = "not valid json {{{"
        with caplog.at_level(logging.DEBUG):
            _parse_one_line(corrupt_line)
        # Should have a debug log about skipping.
        assert any("Skipping" in rec.message for rec in caplog.records), (
            f"Expected debug skip message in: {[r.message for r in caplog.records]}"
        )

    def test_parse_empty_line(self) -> None:
        """Empty lines should be skipped silently."""
        result = _parse_one_line("")
        assert result is None
        result = _parse_one_line("   ")
        assert result is None


# ---------------------------------------------------------------------------
# Unit: context token extraction from step_finish events
# ---------------------------------------------------------------------------


class TestExtractContextTokens:
    """Verify context-token computation from the last step_finish event."""

    def test_existing_fixture_context_tokens(self) -> None:
        """Fixture 1: input=6, cache.read=0, cache.write=23097 → 23103."""
        events = _load_fixture_events("opencode_events.jsonl")
        assert _extract_context_tokens(events) == 23103

    def test_tool_use_fixture_context_tokens(self) -> None:
        """Fixture 2: input=10, cache.read=0, cache.write=80 → 90."""
        events = _load_fixture_events("opencode_events_tool_use.jsonl")
        assert _extract_context_tokens(events) == 90

    def test_multi_step_fixture_last_wins(self) -> None:
        """Multiple step_finish events — last one's tokens are used.
        First: input=20+read=0+write=65=85. Last: input=30+read=0+write=145=175.
        """
        events = _load_fixture_events("opencode_events_multi_step.jsonl")
        assert _extract_context_tokens(events) == 175

    def test_no_step_finish_returns_none(self) -> None:
        """Events with no step_finish → context_tokens is None."""
        events = [
            _make_text("Just a text event, no step_finish."),
        ]
        assert _extract_context_tokens(events) is None

    def test_step_finish_no_cache_subdict(self) -> None:
        """Missing 'cache' key defaults to 0 for read and write."""
        event = {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": {
                "type": "step-finish",
                "tokens": {"input": 42, "output": 10},
            },
        }
        assert _extract_context_tokens([event]) == 42

    def test_step_finish_missing_input_defaults_zero(self) -> None:
        """Missing 'input' key defaults to 0."""
        event = {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": {
                "type": "step-finish",
                "tokens": {"output": 10, "cache": {"read": 5, "write": 7}},
            },
        }
        assert _extract_context_tokens([event]) == 12

    def test_step_finish_none_values_treated_as_zero(self) -> None:
        """None values for numeric fields are treated as 0."""
        event = {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": {
                "type": "step-finish",
                "tokens": {
                    "input": None,
                    "cache": {"read": None, "write": None},
                },
            },
        }
        assert _extract_context_tokens([event]) == 0

    def test_step_finish_part_is_none(self) -> None:
        """part = None (null in JSON) → treated as {} → returns 0."""
        event = {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": None,
        }
        assert _extract_context_tokens([event]) == 0

    def test_step_finish_no_tokens_key(self) -> None:
        """No 'tokens' key at all → defaults to 0."""
        event = {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": {"type": "step-finish"},
        }
        assert _extract_context_tokens([event]) == 0

    def test_step_finish_tokens_is_not_a_dict(self) -> None:
        """tokens is a string (not a dict) → treated as missing → returns 0."""
        event = {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": {
                "type": "step-finish",
                "tokens": "invalid",
            },
        }
        assert _extract_context_tokens([event]) == 0


# ---------------------------------------------------------------------------
# Unit: message assembly (text + tool_use segments)
# ---------------------------------------------------------------------------


class TestAssembleMessage:
    """Verify the segment-based message assembly logic."""

    def test_existing_fixture_still_yields_hi(self) -> None:
        """The original single-text-burst fixture must still produce 'hi'."""
        events = _load_fixture_events()
        assert _assemble_message(events) == "hi"

    def test_tool_use_with_title(self) -> None:
        """tool_use with a state.title produces *<title>* between text bursts."""
        events = [
            _make_text("Hello"),
            _make_tool_use(tool="bash", title="Running shell command"),
            _make_text("Done."),
        ]
        result = _assemble_message(events)
        assert result == "Hello\n\n*Running shell command*\n\nDone."

    def test_tool_use_with_no_title_falls_back_to_tool_name(self) -> None:
        """tool_use with no title but a tool name produces *<tool>*."""
        events = [
            _make_text("Before"),
            _make_tool_use(tool="read_file", title=""),
            _make_text("After"),
        ]
        result = _assemble_message(events)
        assert result == "Before\n\n*read_file*\n\nAfter"

    def test_tool_use_with_neither_title_nor_tool_is_skipped(self) -> None:
        """tool_use with no title and no tool name contributes no segment."""
        events = [
            _make_text("Only text"),
            _make_tool_use(tool="", title=""),
            _make_text("More text"),
        ]
        result = _assemble_message(events)
        assert result == "Only text\n\nMore text"

    def test_tool_use_fixture_full_sequence(self) -> None:
        """The tool_use fixture file produces the expected assembled message."""
        events = _load_fixture_events("opencode_events_tool_use.jsonl")
        result = _assemble_message(events)
        assert (
            result == "Let me check that for you.\n\n*Running shell command*\n\nDone."
        )

    def test_italics_use_single_asterisks(self) -> None:
        """Tool labels must use *foo* (single asterisk), not _foo_ or **foo**."""
        events = [_make_tool_use(tool="bash", title="My Tool")]
        result = _assemble_message(events)
        assert result == "*My Tool*"
        assert "_My Tool_" not in result
        assert "**My Tool**" not in result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_events(
    filename: str = "opencode_events.jsonl",
) -> list[dict[str, Any]]:
    """Load the recorded OpenCode JSON events from the fixture file."""
    fixture_path = FIXTURE_DIR / filename
    raw = fixture_path.read_text()
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            events.append(json.loads(stripped))
    return events


def _parse_one_line(line: str) -> dict[str, Any] | None:
    """Simulate the parser logic on a single line (used by unit tests)."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        logging.getLogger("symphony_linear.opencode").debug(
            "Skipping unparseable JSON line: %s", stripped[:200]
        )
        return None


def _make_text(text: str) -> dict[str, Any]:
    """Build a minimal ``text`` event dict."""
    return {
        "type": "text",
        "sessionID": "ses_test",
        "part": {"type": "text", "text": text},
    }


def _make_tool_use(tool: str, title: str) -> dict[str, Any]:
    """Build a minimal ``tool_use`` event dict."""
    return {
        "type": "tool_use",
        "sessionID": "ses_test",
        "part": {
            "type": "tool-use",
            "tool": tool,
            "state": {"title": title, "status": "running"},
        },
    }
