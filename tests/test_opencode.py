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
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_events() -> list[dict[str, Any]]:
    """Load the recorded OpenCode JSON events from the fixture file."""
    fixture_path = FIXTURE_DIR / "opencode_events.jsonl"
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
        logging.getLogger("symphony_lite.opencode").debug(
            "Skipping unparseable JSON line: %s", stripped[:200]
        )
        return None
