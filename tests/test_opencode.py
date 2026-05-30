"""Tests for the OpenCode adapter module.

Only the JSON event parser is exercised here. We deliberately do not run
the real ``opencode`` binary in tests: it requires a live LLM, model
credentials, and is inherently non-deterministic. The parser is what we
own; everything else is OpenCode's problem.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from symphony_linear.opencode import (
    _assemble_message,
    _extract_context_tokens,
    run_initial,
    run_resume,
)

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
# Unit: --file flag argv construction
# ---------------------------------------------------------------------------


class TestFilesArgv:
    """Verify --file flags are placed correctly in the constructed command."""

    def _make_fake_popen(self) -> MagicMock:
        """Return a mock Popen with valid NDJSON stdout and exit code 0."""
        events = [
            {"type": "step_start", "sessionID": "ses_test", "part": {}},
            {"type": "text", "sessionID": "ses_test", "part": {"text": "ok"}},
            {"type": "step_finish", "sessionID": "ses_test", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        proc = MagicMock(spec=subprocess.Popen)
        proc.returncode = 0
        proc.communicate.return_value = (stdout, b"")
        return proc

    def test_run_initial_no_files(self) -> None:
        """When files is None, no --file flags appear."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.opencode.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                prompt="hello",
                timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--file" not in cmd

    def test_run_initial_empty_files(self) -> None:
        """When files is an empty list, no --file flags appear."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.opencode.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                prompt="hello",
                timeout_seconds=60,
                on_subprocess=lambda p: None,
                files=[],
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--file" not in cmd

    def test_run_initial_single_file(self) -> None:
        """A single file emits one --file <path> pair."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.opencode.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                prompt="hello",
                timeout_seconds=60,
                on_subprocess=lambda p: None,
                files=["/tmp/foo.txt"],
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        file_idx = cmd.index("--file")
        assert cmd[file_idx + 1] == "/tmp/foo.txt"

    def test_run_initial_multiple_files(self) -> None:
        """Multiple files emit --file pairs in order."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.opencode.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                prompt="hello",
                timeout_seconds=60,
                on_subprocess=lambda p: None,
                files=["/a.txt", "/b.txt"],
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        # Find all --file positions
        file_positions = [i for i, a in enumerate(cmd) if a == "--file"]
        assert len(file_positions) == 2
        assert cmd[file_positions[0] + 1] == "/a.txt"
        assert cmd[file_positions[1] + 1] == "/b.txt"

    def test_run_initial_file_before_separator(self) -> None:
        """--file flags appear before the -- separator."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.opencode.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                prompt="hello",
                timeout_seconds=60,
                on_subprocess=lambda p: None,
                files=["/x.txt"],
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        file_idx = cmd.index("--file")
        sep_idx = cmd.index("--")
        assert file_idx < sep_idx

    def test_run_resume_no_files(self) -> None:
        """When files is None, no --file flags appear in resume."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.opencode.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--file" not in cmd

    def test_run_resume_with_files(self) -> None:
        """Files are emitted as --file pairs in the resume command."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.opencode.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                on_subprocess=lambda p: None,
                files=["/f1.txt", "/f2.txt"],
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        file_positions = [i for i, a in enumerate(cmd) if a == "--file"]
        assert len(file_positions) == 2
        assert cmd[file_positions[0] + 1] == "/f1.txt"
        assert cmd[file_positions[1] + 1] == "/f2.txt"
        # --file before --
        assert file_positions[1] < cmd.index("--")

    def test_run_resume_file_before_separator(self) -> None:
        """--file appears before -- in resume command."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.opencode.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                on_subprocess=lambda p: None,
                files=["/f.txt"],
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        file_idx = cmd.index("--file")
        sep_idx = cmd.index("--")
        assert file_idx < sep_idx


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
