"""Tests for the OMP agent adapter.

The tests use captured NDJSON and a mocked shared runner; they never invoke
the real ``omp`` binary or an LLM.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from symphony_linear import agent_runner, opencode
from symphony_linear.omp import (
    OMPCancelled,
    OMPError,
    OMPTimeout,
    _assemble_final_reply,
    _assemble_message,
    _extract_context_tokens,
    _parse_stream,
    run_initial,
    run_resume,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "omp_events.jsonl"
ERROR_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "omp_error_events.jsonl"
)


def _fixture_text() -> str:
    """Return the captured OMP NDJSON stream for a completed simple turn."""
    return FIXTURE_PATH.read_text()


def _error_fixture_text() -> str:
    """Return an OMP stream whose retries all end in a provider failure."""
    return ERROR_FIXTURE_PATH.read_text()


class TestCapturedEvents:
    """Validate extraction against a real OMP event stream."""

    def test_extracts_session_reply_and_context_tokens(self) -> None:
        session_id, events = _parse_stream(_fixture_text())

        assert session_id == "01a035ff-248a-735c-8173-f5ee428fe917"
        assert len(events) == 13
        assert _assemble_final_reply(events) == "hello"
        assert _extract_context_tokens(events) == 3479

    def test_parser_skips_corrupt_non_dict_and_truncated_lines(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stream = _fixture_text() + '\nnull\n["not", "an", "event"]\nnot json\n{"type"'

        with caplog.at_level(logging.DEBUG):
            session_id, events = _parse_stream(stream)

        assert session_id == "01a035ff-248a-735c-8173-f5ee428fe917"
        assert len(events) == 13
        assert any("Skipping" in record.message for record in caplog.records)


class TestFinalTurnExtraction:
    """The success reply and context come only from the final turn_end."""

    def test_last_turn_end_wins_and_keeps_only_text_parts(self) -> None:
        events = [
            {
                "type": "turn_end",
                "message": {
                    "content": [{"type": "text", "text": "Earlier answer"}],
                    "usage": {"input": 1, "cacheRead": 2, "cacheWrite": 3},
                },
            },
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "intent": "A tool after the earlier turn",
            },
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "Hidden reasoning"},
                        {"type": "text", "text": "Final first part"},
                        {"type": "toolCall", "name": "bash"},
                        {"type": "text", "text": "Final second part"},
                    ],
                    "usage": {"input": 7, "cacheRead": 11, "cacheWrite": 13},
                },
            },
        ]

        assert _assemble_final_reply(events) == "Final first part\n\nFinal second part"
        assert _extract_context_tokens(events) == 31

    def test_missing_turn_end_has_no_reply_or_context(self) -> None:
        events = [{"type": "message_update", "assistantMessageEvent": {}}]

        assert _assemble_final_reply(events) == ""
        assert _extract_context_tokens(events) is None

    def test_missing_usage_fields_default_to_zero(self) -> None:
        events = [{"type": "turn_end", "message": {"usage": {"input": 5}}}]

        assert _extract_context_tokens(events) == 5


class TestTimeoutTrace:
    """Timeout diagnostics keep streamed text and tool activity in order."""

    @staticmethod
    def _partial_events() -> list[dict]:
        return [
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_start", "contentIndex": 0},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": "Working",
                },
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_end",
                    "contentIndex": 0,
                    "content": "Working...",
                },
            },
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "intent": "Listing current directory",
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_end",
                    "contentIndex": 0,
                    "content": "Done.",
                },
            },
            {"type": "tool_execution_start", "toolName": "read"},
        ]

    def test_assembly_uses_text_end_and_tool_execution_start(self) -> None:
        assert _assemble_message(self._partial_events()) == (
            "Working...\n\n*Listing current directory*\n\nDone.\n\n*read*"
        )

    def test_timeout_has_salvaged_trace_and_session(self) -> None:
        events = [
            {"type": "session", "id": "omp-timeout"},
            *self._partial_events(),
        ]
        stdout = "\n".join(json.dumps(event) for event in events)

        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(-9, stdout, "still running", "produced no output for 60s"),
        ):
            with pytest.raises(OMPTimeout) as excinfo:
                _run_initial()

        exc = excinfo.value
        assert exc.session_id == "omp-timeout"
        assert exc.partial_message == (
            "Working...\n\n*Listing current directory*\n\nDone.\n\n*read*"
        )
        assert exc.reason == "produced no output for 60s"


class TestCommandConstruction:
    """OMP's CLI differs from OpenCode's flag and attachment syntax."""

    def test_public_signatures_match_opencode(self) -> None:
        assert inspect.signature(run_initial) == inspect.signature(opencode.run_initial)
        assert inspect.signature(run_resume) == inspect.signature(opencode.run_resume)

    def test_initial_command_prefixes_prompt_and_uses_positional_attachments(
        self,
    ) -> None:
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            session_id, message, context_tokens = run_initial(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                prompt="@developer implement this",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
                hide_paths=["/secret"],
                extra_rw_paths=["/shared"],
                attachments_path="/workspace/attachments",
                dir_map=[("/host/mount", "/sandbox/mount")],
                files=["/workspace/attachments/brief.md", "notes.txt"],
                model="anthropic/claude-opus-5",
            )

        assert (session_id, message, context_tokens) == (
            "01a035ff-248a-735c-8173-f5ee428fe917",
            "hello",
            3479,
        )
        assert runner.call_args.kwargs["cmd"] == [
            "omp",
            "-p",
            "--cwd",
            "/workspace",
            "--mode",
            "json",
            "--auto-approve",
            "--model",
            "anthropic/claude-opus-5",
            "@/workspace/attachments/brief.md",
            "@notes.txt",
            "\n@developer implement this",
        ]
        kwargs = runner.call_args.kwargs
        assert kwargs["env"] == {"HOME": str(Path.home())}
        assert kwargs["hide_paths"] == ["/secret"]
        assert kwargs["extra_rw_paths"] == ["/shared"]
        assert kwargs["attachments_path"] == "/workspace/attachments"
        assert kwargs["dir_map"] == [("/host/mount", "/sandbox/mount")]

    def test_secrets_file_exported_to_env(self) -> None:
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            run_initial(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                prompt="hi",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
                secrets_file="/workspace/TEAM-1/secrets.env",
            )

        env = runner.call_args.kwargs["env"]
        assert env["SYMPHONY_SECRETS_FILE"] == "/workspace/TEAM-1/secrets.env"

    def test_no_secrets_file_leaves_env_unset(self) -> None:
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            run_initial(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                prompt="hi",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
            )

        assert "SYMPHONY_SECRETS_FILE" not in runner.call_args.kwargs["env"]

    def test_resume_command_prefixes_even_an_already_prefixed_newline(self) -> None:
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            message, context_tokens = run_resume(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                session_id="omp-session",
                message="\ncontinue",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
                files=["/workspace/attachments/followup.txt"],
                model="anthropic/claude-opus-5",
            )

        assert (message, context_tokens) == ("hello", 3479)
        assert runner.call_args.kwargs["cmd"] == [
            "omp",
            "-p",
            "--cwd",
            "/workspace",
            "--mode",
            "json",
            "--auto-approve",
            "-r",
            "omp-session",
            "--model",
            "anthropic/claude-opus-5",
            "@/workspace/attachments/followup.txt",
            "\n\ncontinue",
        ]

    @pytest.mark.parametrize("session_id", ["", None])
    def test_resume_rejects_empty_session_id(self, session_id: str | None) -> None:
        with patch("symphony_linear.omp.agent_runner.run") as runner:
            with pytest.raises(OMPError, match="non-empty session_id"):
                run_resume(
                    workspace_path="/workspace",
                    tmp_path="/workspace/tmp",
                    session_id=session_id,
                    message="continue",
                    timeout_seconds=60,
                    idle_timeout_seconds=30,
                    on_subprocess=lambda process: None,
                )

        runner.assert_not_called()


class TestExitValidation:
    """The adapter preserves cancellation and validation priority."""

    def test_signal_exit_wins_over_missing_session(self) -> None:
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(-9, "not NDJSON", "", None),
        ):
            with pytest.raises(OMPCancelled, match="killed by signal 9") as excinfo:
                _run_initial()

        assert excinfo.value.session_id is None

    def test_signal_exit_carries_parsed_session_id(self) -> None:
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(-9, _fixture_text(), "", None),
        ):
            with pytest.raises(OMPCancelled, match="killed by signal 9") as excinfo:
                _run_initial()

        assert excinfo.value.session_id == "01a035ff-248a-735c-8173-f5ee428fe917"

    def test_nonzero_exit_wins_over_missing_session_and_keeps_stderr_tail(self) -> None:
        stderr = "bootstrap-start\n" + ("padding\n" * 1000) + "actual failure\n"
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(1, "not NDJSON", stderr, None),
        ):
            with pytest.raises(OMPError) as excinfo:
                _run_initial()

        assert "actual failure" in str(excinfo.value)
        assert "bootstrap-start" not in str(excinfo.value)

    def test_zero_exit_error_turn_raises_last_provider_error(self) -> None:
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(0, _error_fixture_text(), "", None),
        ):
            with pytest.raises(OMPError) as excinfo:
                _run_initial()

        message = str(excinfo.value)
        assert "429 provider rate limit persisted after final retry" in message
        assert "429 retry attempt 3 failed" not in message

    def test_zero_exit_aborted_turn_is_an_error(self) -> None:
        stdout = (
            '{"type":"session","id":"omp-aborted-session"}\n'
            '{"type":"turn_end","message":{"stopReason":"aborted",'
            '"errorMessage":"request aborted"}}'
        )
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(0, stdout, "", None),
        ):
            with pytest.raises(OMPError, match="request aborted"):
                _run_initial()

    def test_signal_exit_wins_over_terminal_aborted_turn(self) -> None:
        stdout = (
            '{"type":"session","id":"omp-aborted-session"}\n'
            '{"type":"turn_end","message":{"stopReason":"aborted",'
            '"errorMessage":"request aborted"}}'
        )
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(-9, stdout, "", None),
        ):
            with pytest.raises(OMPCancelled, match="killed by signal 9"):
                _run_initial()

    def test_zero_exit_without_session_is_an_error(self) -> None:
        with patch(
            "symphony_linear.omp.agent_runner.run",
            return_value=(0, '{"type":"turn_start"}', "", None),
        ):
            with pytest.raises(OMPError, match="No session ID"):
                _run_initial()

    def test_exception_names_alias_agent_runner_exceptions(self) -> None:
        assert OMPError is agent_runner.AgentError
        assert OMPTimeout is agent_runner.AgentTimeout
        assert OMPCancelled is agent_runner.AgentCancelled


def _run_initial() -> tuple[str, str, int | None]:
    """Run a representative initial OMP turn through the mocked runner."""
    return run_initial(
        workspace_path="/workspace",
        tmp_path="/workspace/tmp",
        prompt="do it",
        timeout_seconds=60,
        idle_timeout_seconds=30,
        on_subprocess=lambda process: None,
    )
