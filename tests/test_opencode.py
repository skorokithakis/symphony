"""Tests for the OpenCode adapter module.

Only the JSON event parser is exercised here. We deliberately do not run
the real ``opencode`` binary in tests: it requires a live LLM, model
credentials, and is inherently non-deterministic. The parser is what we
own; everything else is OpenCode's problem.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from symphony_linear import agent_runner
from symphony_linear.opencode import (
    OpenCodeCancelled,
    OpenCodeError,
    OpenCodeTimeout,
    _assemble_final_reply,
    _assemble_message,
    _extract_context_tokens,
    _parse_stream,
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
# Unit: trimmed final-reply assembly (success path)
# ---------------------------------------------------------------------------


class TestAssembleFinalReply:
    """Verify the trimmed final-reply assembly used on successful turns.

    Success-path comments must contain only the closing reply: no tool-title
    lines, no mid-turn narration, no subagent chatter.  A turn that ends on a
    tool call falls back to the full assembly.
    """

    def test_tool_calls_keep_only_closing_reply(self) -> None:
        """Text before/between tool calls is dropped; only the text after the
        last tool_use survives, with no tool-title lines."""
        events = [
            _make_text("Let me check that for you."),
            _make_tool_use(tool="bash", title="Running shell command"),
            _make_text("Halfway there, still working."),
            _make_tool_use(tool="read", title="Reading file"),
            _make_text("Done. Here is the full answer."),
        ]
        result = _assemble_final_reply(events, "ses_test")
        assert result == "Done. Here is the full answer."

    def test_no_tool_use_keeps_all_text(self) -> None:
        """Without any tool_use, all text segments survive (nothing to trim)."""
        events = [
            _make_text("First burst."),
            _make_text("Second burst."),
        ]
        result = _assemble_final_reply(events, "ses_test")
        assert result == "First burst.\n\nSecond burst."

    def test_turn_ending_on_tool_call_falls_back_to_full_assembly(self) -> None:
        """No text after the last tool_use → fall back to the full trace, so
        the comment is never empty."""
        events = [
            _make_text("Let me check that for you."),
            _make_tool_use(tool="bash", title="Running shell command"),
            _make_text("Halfway there, still working."),
            _make_tool_use(tool="read", title="Reading file"),
        ]
        result = _assemble_final_reply(events, "ses_test")
        assert result == (
            "Let me check that for you.\n\n*Running shell command*\n\n"
            "Halfway there, still working.\n\n*Reading file*"
        )

    def test_foreign_session_events_are_dropped(self) -> None:
        """Events whose top-level sessionID differs from the main session
        (subagent chatter) never appear in the trimmed reply — even when they
        fall after the main session's last tool_use."""
        events = [
            _make_text("Main session intro."),
            _make_tool_use(tool="read", title="Reading file"),
            {
                "type": "tool_use",
                "sessionID": "ses_sub",
                "part": {
                    "type": "tool-use",
                    "tool": "bash",
                    "state": {"title": "Subagent tool", "status": "running"},
                },
            },
            {
                "type": "text",
                "sessionID": "ses_sub",
                "part": {"type": "text", "text": "Subagent chatter."},
            },
            _make_text("Closing reply."),
        ]
        result = _assemble_final_reply(events, "ses_test")
        assert result == "Closing reply."

    def test_foreign_session_tool_use_does_not_reset_trim_window(self) -> None:
        """A subagent tool_use must not count as the last tool_use: the main
        session's own last tool call is what delimits the closing reply."""
        events = [
            _make_text("Intro."),
            _make_tool_use(tool="bash", title="Main tool"),
            {
                "type": "text",
                "sessionID": "ses_sub",
                "part": {"type": "text", "text": "Subagent chatter."},
            },
            {
                "type": "tool_use",
                "sessionID": "ses_sub",
                "part": {
                    "type": "tool-use",
                    "tool": "bash",
                    "state": {"title": "Subagent tool", "status": "running"},
                },
            },
            _make_text("Closing reply."),
        ]
        result = _assemble_final_reply(events, "ses_test")
        assert result == "Closing reply."

    def test_success_path_through_run_initial_returns_trimmed_reply(self) -> None:
        """The _execute success path uses the trimmed assembly end to end."""
        events = [
            {"type": "step_start", "sessionID": "ses_main", "part": {}},
            {
                "type": "text",
                "sessionID": "ses_main",
                "part": {"type": "text", "text": "Working on it..."},
            },
            {
                "type": "tool_use",
                "sessionID": "ses_main",
                "part": {
                    "type": "tool-use",
                    "tool": "bash",
                    "state": {"title": "Running shell command", "status": "running"},
                },
            },
            {
                "type": "text",
                "sessionID": "ses_main",
                "part": {"type": "text", "text": "Done. The answer is 42."},
            },
            {"type": "step_finish", "sessionID": "ses_main", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        proc = _FakePopen(stdout=stdout, exit_code=0)
        with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
            session_id, final_message, _ = run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="do it",
                timeout_seconds=10,
                idle_timeout_seconds=10,
                on_subprocess=lambda p: None,
            )
        assert session_id == "ses_main"
        assert final_message == "Done. The answer is 42."


# ---------------------------------------------------------------------------
# Unit: _parse_stream shared helper + OpenCodeTimeout
# ---------------------------------------------------------------------------


class TestParseStream:
    """Verify the lenient NDJSON stream parser (returns session_id + events only)."""

    def test_parse_stream_extracts_session_id_and_events(self) -> None:
        """A valid stream yields session_id and parsed events."""
        events = [
            {"type": "step_start", "sessionID": "ses_abc", "part": {}},
            {"type": "text", "sessionID": "ses_abc", "part": {"text": "Hello"}},
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {"tokens": {"input": 10}},
            },
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        session_id, parsed = _parse_stream(stdout)
        assert session_id == "ses_abc"
        assert len(parsed) == 3
        msg = _assemble_message(parsed)
        assert msg == "Hello"

    def test_partial_stream_with_truncated_last_line(self) -> None:
        """A mid-line-truncated last event is skipped without error."""
        stdout = (
            json.dumps({"type": "step_start", "sessionID": "ses_123", "part": {}})
            + "\n"
            + json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_123",
                    "part": {"text": "Partial output"},
                }
            )
            + "\n"
            + '{"type":"text","sessionID":"ses_123","part":{"text":"trunc'  # truncated
        )
        session_id, parsed = _parse_stream(stdout)
        assert session_id == "ses_123"
        assert len(parsed) == 2
        msg = _assemble_message(parsed)
        assert msg == "Partial output"

    def test_empty_stdout_yields_no_session_and_empty_events(self) -> None:
        """Empty stdout produces no session_id and empty events list."""
        session_id, parsed = _parse_stream("")
        assert session_id is None
        assert parsed == []

    def test_garbage_stdout_yields_no_session_and_empty_events(self) -> None:
        """Completely unparseable stdout is silently skipped."""
        session_id, parsed = _parse_stream("not json at all\n{garbage}\nbork")
        assert session_id is None
        assert parsed == []

    def test_blank_lines_are_skipped(self) -> None:
        """Blank lines between events are skipped silently."""
        events = [
            {"type": "text", "sessionID": "ses_x", "part": {"text": "A"}},
        ]
        stdout = "\n\n" + json.dumps(events[0]) + "\n\n"
        session_id, parsed = _parse_stream(stdout)
        assert session_id == "ses_x"
        assert len(parsed) == 1

    def test_tool_use_events_parsed_in_partial_stream(self) -> None:
        """tool_use events in a partial stream are captured as events."""
        events = [
            {"type": "step_start", "sessionID": "ses_t", "part": {}},
            {"type": "text", "sessionID": "ses_t", "part": {"text": "Running..."}},
            {
                "type": "tool_use",
                "sessionID": "ses_t",
                "part": {
                    "tool": "bash",
                    "state": {"title": "Installing deps", "status": "running"},
                },
            },
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        _, parsed = _parse_stream(stdout)
        msg = _assemble_message(parsed)
        assert msg == "Running...\n\n*Installing deps*"

    def test_step_finish_in_partial_stream(self) -> None:
        """step_finish events are parsed but contribute nothing to message."""
        events = [
            {
                "type": "step_finish",
                "sessionID": "ses_f",
                "part": {"reason": "stop", "tokens": {"input": 5}},
            },
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        _, parsed = _parse_stream(stdout)
        msg = _assemble_message(parsed)
        assert msg == ""

    def test_null_json_line_is_skipped(self) -> None:
        """A line containing valid JSON ``null`` is skipped (not a dict)."""
        stdout = (
            json.dumps({"type": "text", "sessionID": "ses_n", "part": {"text": "Hi"}})
            + "\nnull\n"
        )
        session_id, parsed = _parse_stream(stdout)
        assert session_id == "ses_n"
        assert len(parsed) == 1

    def test_nested_null_json_is_skipped(self) -> None:
        """A line containing a JSON array is skipped (not a dict)."""
        stdout = (
            json.dumps({"type": "text", "sessionID": "ses_arr", "part": {"text": "X"}})
            + "\n[]\n"
        )
        session_id, parsed = _parse_stream(stdout)
        assert session_id == "ses_arr"
        assert len(parsed) == 1

    def test_event_with_null_part_does_not_break_assembly(self) -> None:
        """An event with ``"part": null`` is parseable but assembly handles None."""
        event = {"type": "text", "sessionID": "ses_p", "part": None}
        stdout = json.dumps(event) + "\n"
        session_id, parsed = _parse_stream(stdout)
        assert session_id == "ses_p"
        assert len(parsed) == 1
        # _assemble_message must not raise on part=None.
        msg = _assemble_message(parsed)
        assert msg == ""


class TestExecuteTimeout:
    """Verify the two-tier timeout path through :func:`_execute`.

    Driven by a fake Popen backed by real ``os.pipe()`` fds — never the real
    ``opencode`` binary or an LLM. The drain threads in ``_execute`` block on
    readline() until the write ends close, so silent and slowly-writing
    processes behave realistically, and the short injectable timeouts keep
    the suite fast.
    """

    @staticmethod
    def _run_initial(
        proc: "_FakePopen",
        *,
        timeout_seconds: int = 10,
        idle_timeout_seconds: int = 100,
    ) -> OpenCodeTimeout:
        """Run run_initial against *proc* and return the raised timeout."""
        with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
            with pytest.raises(OpenCodeTimeout) as excinfo:
                run_initial(
                    workspace_path="/ws",
                    tmp_path="/ws/tmp",
                    prompt="do it",
                    timeout_seconds=timeout_seconds,
                    idle_timeout_seconds=idle_timeout_seconds,
                    on_subprocess=lambda p: None,
                )
        return excinfo.value

    def test_timeout_carries_partial_message_and_session(self) -> None:
        """Timeout via _execute salvages session_id and partial message."""
        stdout = (
            json.dumps({"type": "step_start", "sessionID": "ses_timeout", "part": {}})
            + "\n"
            + json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_timeout",
                    "part": {"text": "I was in the middle of..."},
                }
            )
            + "\n"
        ).encode()
        proc = _FakePopen(stdout=stdout, exit_code=None)
        exc = self._run_initial(proc, idle_timeout_seconds=0.2)
        assert exc.session_id == "ses_timeout"
        assert exc.partial_message == "I was in the middle of..."
        assert proc.killed
        # The pre-buffered output was consumed, then the process went quiet.
        assert "no output" in exc.reason

    def test_timeout_keeps_tool_titles_in_partial_message(self) -> None:
        """The timeout path uses the full assembly, not the trimmed reply:
        tool-title lines must survive in the salvaged partial message (they
        are the only diagnostic on a killed turn)."""
        stdout = (
            json.dumps({"type": "step_start", "sessionID": "ses_tt", "part": {}})
            + "\n"
            + json.dumps(
                {"type": "text", "sessionID": "ses_tt", "part": {"text": "Working..."}}
            )
            + "\n"
            + json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "ses_tt",
                    "part": {
                        "type": "tool-use",
                        "tool": "bash",
                        "state": {"title": "Installing deps", "status": "running"},
                    },
                }
            )
            + "\n"
        ).encode()
        proc = _FakePopen(stdout=stdout, exit_code=None)
        exc = self._run_initial(proc, idle_timeout_seconds=0.2)
        assert exc.partial_message == "Working...\n\n*Installing deps*"

    def test_idle_timeout_kills_silent_process(self) -> None:
        """(a) A process that stops producing output is killed at the idle
        deadline, with the idle variant of the timeout."""
        proc = _FakePopen(exit_code=None)  # silent, never exits
        exc = self._run_initial(proc, idle_timeout_seconds=0.2)
        assert "no output" in exc.reason
        assert "total" not in exc.reason
        assert proc.killed

    def test_activity_resets_idle_deadline_absolute_cap_fires(self) -> None:
        """(b) A process that keeps writing survives past the idle window
        and is not killed before the absolute cap.

        The idle window (0.2s) is deliberately shorter than the total budget
        (0.5s): a writer feeding a line every 10ms keeps pushing the idle
        deadline, so the kill must come from the absolute cap, not the
        watchdog."""
        proc = _FakePopen(exit_code=None)  # never exits on its own
        stop = threading.Event()

        def writer() -> None:
            while not stop.is_set():
                try:
                    os.write(proc.write_stderr, b"[log] working\n")
                except OSError:
                    return  # kill() closed the write end — done
                time.sleep(0.01)

        writer_thread = threading.Thread(target=writer, daemon=True)
        writer_thread.start()
        try:
            exc = self._run_initial(proc, timeout_seconds=0.5, idle_timeout_seconds=0.2)
        finally:
            stop.set()
            writer_thread.join()
        assert "total" in exc.reason
        assert "no output" not in exc.reason
        assert proc.killed

    def test_absolute_cap_fires_on_silent_process(self) -> None:
        """(c) The absolute turn_timeout_seconds cap still fires when the
        idle window is longer than the total budget."""
        proc = _FakePopen(exit_code=None)
        exc = self._run_initial(proc, timeout_seconds=0.5, idle_timeout_seconds=100)
        assert "total" in exc.reason
        assert "no output" not in exc.reason
        assert proc.killed

    def test_timeout_with_null_line_does_not_mask_timeout(self) -> None:
        """A ``null`` line in partial stdout is skipped; timeout still raised."""
        stdout = (
            json.dumps({"type": "step_start", "sessionID": "ses_nl", "part": {}})
            + "\nnull\n"
            + json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_nl",
                    "part": {"text": "Working..."},
                }
            )
            + "\n"
        ).encode()
        proc = _FakePopen(stdout=stdout, exit_code=None)
        exc = self._run_initial(proc, idle_timeout_seconds=0.2)
        assert exc.session_id == "ses_nl"
        assert exc.partial_message == "Working..."

    def test_timeout_with_null_part_does_not_mask_timeout(self) -> None:
        """An event with ``"part": null`` does not mask the timeout."""
        stdout = (
            json.dumps({"type": "step_start", "sessionID": "ses_np", "part": {}})
            + "\n"
            + json.dumps({"type": "text", "sessionID": "ses_np", "part": None})
            + "\n"
            + json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_np",
                    "part": {"text": "More text"},
                }
            )
            + "\n"
        ).encode()
        proc = _FakePopen(stdout=stdout, exit_code=None)
        exc = self._run_initial(proc, idle_timeout_seconds=0.2)
        assert exc.session_id == "ses_np"
        # null-part text event contributes nothing, "More text" does.
        assert exc.partial_message == "More text"

    def test_timeout_with_pure_garbage_stdout(self) -> None:
        """Completely unparseable partial stdout still raises timeout."""
        proc = _FakePopen(stdout=b"not json\n{garbage}", exit_code=None)
        exc = self._run_initial(proc, idle_timeout_seconds=0.2)
        assert exc.session_id is None
        assert exc.partial_message == ""

    def test_timeout_with_no_session_id(self) -> None:
        """Timeout with no sessionID in partial stdout still raises."""
        stdout = (
            json.dumps({"type": "text", "part": {"text": "No session here."}}) + "\n"
        ).encode()
        proc = _FakePopen(stdout=stdout, exit_code=None)
        exc = self._run_initial(proc, idle_timeout_seconds=0.2)
        assert exc.session_id is None
        assert exc.partial_message == "No session here."

    def test_success_path_returns_parsed_output(self) -> None:
        """A process that writes valid NDJSON and exits 0 succeeds."""
        events = [
            {"type": "step_start", "sessionID": "ses_ok", "part": {}},
            {"type": "text", "sessionID": "ses_ok", "part": {"text": "all done"}},
            {"type": "step_finish", "sessionID": "ses_ok", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        proc = _FakePopen(stdout=stdout, exit_code=0)
        with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
            session_id, final_message, _ = run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="do it",
                timeout_seconds=10,
                idle_timeout_seconds=10,
                on_subprocess=lambda p: None,
            )
        assert session_id == "ses_ok"
        assert final_message == "all done"
        assert proc.killed is False

    def test_process_exit_reaped_promptly_within_idle_window(self) -> None:
        """A process that exits well inside both windows is reaped as soon
        as it exits — the watchdog must not sleep out the idle window.

        Regression: the loop used to sleep until the nearer deadline before
        noticing the exit; on the first iteration that is the full idle
        window, so a healthy 3-minute turn would not be detected until the
        window expired (up to 20 minutes late in production). The loop must
        block on the process instead, waking the moment it exits."""
        events = [
            {"type": "step_start", "sessionID": "ses_fast", "part": {}},
            {"type": "text", "sessionID": "ses_fast", "part": {"text": "fast done"}},
            {"type": "step_finish", "sessionID": "ses_fast", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        proc = _FakePopen(exit_code=None)  # alive at start, exits after 50ms

        def exit_soon() -> None:
            time.sleep(0.05)
            os.write(proc.write_stdout, stdout)
            proc.set_exit_code(0)

        threading.Thread(target=exit_soon, daemon=True).start()
        started = time.monotonic()
        with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
            session_id, final_message, _ = run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="do it",
                timeout_seconds=30,
                idle_timeout_seconds=5,
                on_subprocess=lambda p: None,
            )
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, (
            f"_execute took {elapsed:.2f}s — the exit was not noticed promptly"
        )
        assert session_id == "ses_fast"
        assert final_message == "fast done"


class TestExecuteExitErrors:
    """Verify the error paths through :func:`_execute` for exited processes."""

    @staticmethod
    def _run_initial(proc: "_FakePopen") -> OpenCodeError:
        """Run run_initial against *proc* and return the raised error."""
        with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
            with pytest.raises(OpenCodeError) as excinfo:
                run_initial(
                    workspace_path="/ws",
                    tmp_path="/ws/tmp",
                    prompt="do it",
                    timeout_seconds=10,
                    idle_timeout_seconds=10,
                    on_subprocess=lambda p: None,
                )
        return excinfo.value

    def test_nonzero_exit_reports_stderr_tail(self) -> None:
        """A non-zero exit reports the *tail* of stderr, not the head.

        The head of OpenCode's stderr is always bootstrap noise ("loading
        path=..."); the real failure reason is at the end of the stream.
        Regression: the error used to embed ``stderr[:2000]``, so long
        startup logs pushed the actual error out of the comment entirely.
        """
        stderr_head = "loading path=/usr/bin/opencode\n" * 100
        stderr_middle = "[log] working on task\n" * 200
        stderr_tail = "AI_APICallError: Overloaded\n" * 3
        proc = _FakePopen(
            stderr=(stderr_head + stderr_middle + stderr_tail).encode(),
            exit_code=1,
        )
        exc = self._run_initial(proc)
        msg = str(exc)
        assert "AI_APICallError: Overloaded" in msg
        assert "loading path" not in msg

    def test_signal_exit_raises_open_code_cancelled(self) -> None:
        """A signal exit retains a parsed session ID for recovery."""
        proc = _FakePopen(
            stdout=(FIXTURE_DIR / "opencode_events.jsonl").read_bytes(),
            exit_code=-9,
        )
        with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
            with pytest.raises(
                OpenCodeCancelled, match="killed by signal 9"
            ) as excinfo:
                run_initial(
                    workspace_path="/ws",
                    tmp_path="/ws/tmp",
                    prompt="do it",
                    timeout_seconds=10,
                    idle_timeout_seconds=10,
                    on_subprocess=lambda p: None,
                )

        assert excinfo.value.session_id == "ses_1e3790378ffecZySU3wIpFOoIz"


class TestOpenCodeTimeoutAttributes:
    """Verify OpenCodeTimeout carries partial_message, session_id, and reason."""

    def test_legacy_exceptions_alias_agent_runner_exceptions(self) -> None:
        """Orchestrator-facing OpenCode exception names remain compatible."""
        assert OpenCodeError is agent_runner.AgentError
        assert OpenCodeTimeout is agent_runner.AgentTimeout
        assert OpenCodeCancelled is agent_runner.AgentCancelled

    def test_timeout_with_partial_output(self) -> None:
        """Full partial output is captured as attributes."""
        exc = OpenCodeTimeout(
            "timed out",
            partial_message="Hello\n\n*Running command*",
            session_id="ses_partial",
            reason="produced no output for 1200s",
        )
        assert exc.partial_message == "Hello\n\n*Running command*"
        assert exc.session_id == "ses_partial"
        assert exc.reason == "produced no output for 1200s"
        assert "timed out" in str(exc)

    def test_timeout_with_no_partial_output(self) -> None:
        """partial_message and session_id default to empty/None."""
        exc = OpenCodeTimeout("timed out", reason="exceeded 1800s in total")
        assert exc.partial_message == ""
        assert exc.session_id is None
        assert exc.reason == "exceeded 1800s in total"

    def test_timeout_with_message_only_no_session(self) -> None:
        """Partial text without a sessionID event yields a message but no session."""
        exc = OpenCodeTimeout(
            "timed out",
            partial_message="Partial text",
            session_id=None,
            reason="produced no output for 1200s",
        )
        assert exc.partial_message == "Partial text"
        assert exc.session_id is None

    def test_timeout_requires_reason(self) -> None:
        """reason is a required keyword argument."""
        with pytest.raises(TypeError):
            OpenCodeTimeout("timed out")  # type: ignore[call-arg]

    def test_timeout_carries_reason(self) -> None:
        """The reason distinguishes idle stalls from the absolute cap."""
        exc = OpenCodeTimeout("timed out", reason="produced no output for 1200s")
        assert exc.reason == "produced no output for 1200s"


# ---------------------------------------------------------------------------
# Unit: --print-logs flag argv construction
# ---------------------------------------------------------------------------


class TestPrintLogsFlag:
    """--print-logs must be on every opencode run command line: it mirrors
    OpenCode's internal log to stderr, which is the only liveness signal
    while a subagent task runs."""

    @staticmethod
    def _make_fake_popen() -> _FakePopen:
        events = [
            {"type": "step_start", "sessionID": "ses_test", "part": {}},
            {"type": "text", "sessionID": "ses_test", "part": {"text": "ok"}},
            {"type": "step_finish", "sessionID": "ses_test", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        return _FakePopen(stdout=stdout, exit_code=0)

    def test_run_initial_has_print_logs(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--print-logs" in cmd

    def test_run_resume_has_print_logs(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--print-logs" in cmd

    def test_print_logs_does_not_leak_into_stdout_parsing(self) -> None:
        """Log output must not appear on stdout: NDJSON parsing is unaffected."""
        events = [
            {"type": "step_start", "sessionID": "ses_p", "part": {}},
            {"type": "text", "sessionID": "ses_p", "part": {"text": "clean"}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        proc = _FakePopen(
            stdout=stdout, stderr=b"[log] 12:00:00 something\n", exit_code=0
        )
        with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
            session_id, final_message, _ = run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        assert session_id == "ses_p"
        assert final_message == "clean"


# ---------------------------------------------------------------------------
# Unit: --file flag argv construction
# ---------------------------------------------------------------------------


class TestFilesArgv:
    """Verify --file flags are placed correctly in the constructed command."""

    def _make_fake_popen(self) -> _FakePopen:
        """Return a mock Popen with valid NDJSON stdout and exit code 0."""
        events = [
            {"type": "step_start", "sessionID": "ses_test", "part": {}},
            {"type": "text", "sessionID": "ses_test", "part": {"text": "ok"}},
            {"type": "step_finish", "sessionID": "ses_test", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        return _FakePopen(stdout=stdout, exit_code=0)

    def test_run_initial_no_files(self) -> None:
        """When files is None, no --file flags appear."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--file" not in cmd

    def test_run_initial_empty_files(self) -> None:
        """When files is an empty list, no --file flags appear."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
                files=[],
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--file" not in cmd

    def test_run_initial_single_file(self) -> None:
        """A single file emits one --file <path> pair."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
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
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
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
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
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
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--file" not in cmd

    def test_run_resume_with_files(self) -> None:
        """Files are emitted as --file pairs in the resume command."""
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
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
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
                files=["/f.txt"],
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        file_idx = cmd.index("--file")
        sep_idx = cmd.index("--")
        assert file_idx < sep_idx


# ---------------------------------------------------------------------------
# Unit: --model flag argv construction
# ---------------------------------------------------------------------------


class TestModelFlag:
    """A per-issue model override must reach argv as --model <id> on both
    the initial and the resume command, before the ``--`` separator."""

    @staticmethod
    def _make_fake_popen() -> _FakePopen:
        events = [
            {"type": "step_start", "sessionID": "ses_test", "part": {}},
            {"type": "text", "sessionID": "ses_test", "part": {"text": "ok"}},
            {"type": "step_finish", "sessionID": "ses_test", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        return _FakePopen(stdout=stdout, exit_code=0)

    def test_run_initial_with_model(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
                model="anthropic/claude-opus-5",
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "anthropic/claude-opus-5"
        assert idx < cmd.index("--")

    def test_run_initial_without_model(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--model" not in cmd

    def test_run_resume_with_model(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
                model="anthropic/claude-opus-5",
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "anthropic/claude-opus-5"
        assert idx < cmd.index("--")

    def test_run_resume_without_model(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        cmd = mock_sandbox.call_args.kwargs["cmd"]
        assert "--model" not in cmd


# ---------------------------------------------------------------------------
# Unit: empty-session guard
# ---------------------------------------------------------------------------


class TestRunResumeGuard:
    """run_resume must refuse to launch an empty OpenCode session."""

    @pytest.mark.parametrize("session_id", ["", None])
    def test_empty_session_id_raises(self, session_id: str | None) -> None:
        with pytest.raises(OpenCodeError, match="non-empty session_id"):
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id=session_id,
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )


# ---------------------------------------------------------------------------
# Unit: tmp_path is forwarded to the sandbox
# ---------------------------------------------------------------------------


class TestTmpPathForwarded:
    """run_initial / run_resume pass tmp_path through to run_in_sandbox."""

    @staticmethod
    def _make_fake_popen() -> _FakePopen:
        events = [
            {"type": "step_start", "sessionID": "ses_test", "part": {}},
            {"type": "text", "sessionID": "ses_test", "part": {"text": "ok"}},
            {"type": "step_finish", "sessionID": "ses_test", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        return _FakePopen(stdout=stdout, exit_code=0)

    def test_run_initial_forwards_tmp_path(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
                tmp_path="/ws/tmp",
            )
        assert mock_sandbox.call_args.kwargs["tmp_path"] == "/ws/tmp"

    def test_run_resume_forwards_tmp_path(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
                tmp_path="/ws/tmp",
            )
        assert mock_sandbox.call_args.kwargs["tmp_path"] == "/ws/tmp"


# ---------------------------------------------------------------------------
# Unit: OPENCODE_PERMISSION is injected into the sandbox env
# ---------------------------------------------------------------------------


class TestOpenCodePermissionEnv:
    """Every turn must pass OPENCODE_PERMISSION to run_in_sandbox so the three
    permissions that default to 'ask' (external_directory, doom_loop, read)
    are pre-answered. Without it, an ask raised by a subagent session is
    never replied to (OpenCode's auto-approve is filtered to the top-level
    session) and the turn hangs until the daemon's absolute timeout."""

    @staticmethod
    def _make_fake_popen() -> _FakePopen:
        events = [
            {"type": "step_start", "sessionID": "ses_test", "part": {}},
            {"type": "text", "sessionID": "ses_test", "part": {"text": "ok"}},
            {"type": "step_finish", "sessionID": "ses_test", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        return _FakePopen(stdout=stdout, exit_code=0)

    @staticmethod
    def _assert_permission(mock_sandbox: MagicMock) -> None:
        env = mock_sandbox.call_args.kwargs["env"]
        assert "OPENCODE_PERMISSION" in env
        permission = json.loads(env["OPENCODE_PERMISSION"])
        assert permission == {
            "external_directory": "allow",
            "doom_loop": "allow",
            "read": "allow",
        }

    def test_run_initial_injects_open_code_permission(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        self._assert_permission(mock_sandbox)

    def test_run_resume_injects_open_code_permission(self) -> None:
        fake_proc = self._make_fake_popen()
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox", return_value=fake_proc
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        self._assert_permission(mock_sandbox)


# ---------------------------------------------------------------------------
# Unit: per-repo secrets file is exported to the sandbox env
# ---------------------------------------------------------------------------


class TestSecretsFileEnv:
    """A secrets_file is exported as SYMPHONY_SECRETS_FILE; None leaves it unset."""

    @staticmethod
    def _make_fake_popen() -> _FakePopen:
        events = [
            {"type": "step_start", "sessionID": "ses_test", "part": {}},
            {"type": "text", "sessionID": "ses_test", "part": {"text": "ok"}},
            {"type": "step_finish", "sessionID": "ses_test", "part": {}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        return _FakePopen(stdout=stdout, exit_code=0)

    def test_run_initial_exports_secrets_file(self) -> None:
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox",
            return_value=self._make_fake_popen(),
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
                secrets_file="/ws/TEAM-1/secrets.env",
            )
        env = mock_sandbox.call_args.kwargs["env"]
        assert env["SYMPHONY_SECRETS_FILE"] == "/ws/TEAM-1/secrets.env"

    def test_run_initial_omits_secrets_file_when_none(self) -> None:
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox",
            return_value=self._make_fake_popen(),
        ) as mock_sandbox:
            run_initial(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                prompt="hello",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
            )
        assert "SYMPHONY_SECRETS_FILE" not in mock_sandbox.call_args.kwargs["env"]

    def test_run_resume_exports_secrets_file(self) -> None:
        with patch(
            "symphony_linear.agent_runner.run_in_sandbox",
            return_value=self._make_fake_popen(),
        ) as mock_sandbox:
            run_resume(
                workspace_path="/ws",
                tmp_path="/ws/tmp",
                session_id="ses_x",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=60,
                on_subprocess=lambda p: None,
                secrets_file="/ws/TEAM-1/secrets.env",
            )
        env = mock_sandbox.call_args.kwargs["env"]
        assert env["SYMPHONY_SECRETS_FILE"] == "/ws/TEAM-1/secrets.env"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePopen:
    """Minimal Popen stand-in backed by real ``os.pipe()`` fds.

    The drain threads in ``_execute`` block on readline() until the write
    ends are closed, so silent and slowly-writing processes behave like the
    real thing without launching any subprocess or LLM. ``write_stdout`` /
    ``write_stderr`` stay open for the test to feed more output (a writer
    thread); ``kill()`` / ``set_exit_code()`` close them so the drains see
    EOF. ``wait()`` blocks until the process exits or the timeout expires,
    matching the real Popen semantics the watchdog loop depends on.
    """

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int | None = None,
    ) -> None:
        read_fd, self.write_stdout = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb")
        read_fd, self.write_stderr = os.pipe()
        self.stderr = os.fdopen(read_fd, "rb")
        self._exit_code = exit_code
        self._exited = threading.Event()
        self.killed = False
        if stdout:
            os.write(self.write_stdout, stdout)
        if stderr:
            os.write(self.write_stderr, stderr)
        if exit_code is not None:
            self._close_write_ends()
            self._exited.set()

    def set_exit_code(self, code: int) -> None:
        """Exit the process with *code*: wake wait() and close the pipes."""
        self._exit_code = code
        self._close_write_ends()
        self._exited.set()

    def _close_write_ends(self) -> None:
        for fd in (self.write_stdout, self.write_stderr):
            try:
                os.close(fd)
            except OSError:
                pass

    def poll(self) -> int | None:
        return self._exit_code

    def kill(self) -> None:
        self.killed = True
        self._exit_code = -9
        self._close_write_ends()
        self._exited.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)
        assert self._exit_code is not None
        return self._exit_code

    @property
    def returncode(self) -> int | None:
        return self._exit_code


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
