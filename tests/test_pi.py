"""Tests for the Pi agent adapter.

The tests use captured NDJSON and a mocked shared runner; they never invoke
the real ``pi`` binary or an LLM.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from symphony_linear import agent_runner
from symphony_linear.pi import (
    PiCancelled,
    PiError,
    PiTimeout,
    _assemble_final_reply,
    _assemble_message,
    _extract_context_tokens,
    _parse_stream,
    run_initial,
    run_resume,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "pi_events.jsonl"


def _fixture_text() -> str:
    """Return the captured Pi NDJSON stream for a completed simple turn."""
    return FIXTURE_PATH.read_text()


# ---------------------------------------------------------------------------
# Fixture parsing
# ---------------------------------------------------------------------------


class TestCapturedEvents:
    """Validate extraction against a real Pi event stream."""

    def test_extracts_session_reply_and_context_tokens(self) -> None:
        session_id, events = _parse_stream(_fixture_text())

        assert session_id == "01b035ff-248a-735c-8173-f5ee428fe918"
        assert len(events) == 13
        assert _assemble_final_reply(events) == "hello"
        # input(5) + cacheRead(10) + cacheWrite(3000) = 3015
        assert _extract_context_tokens(events) == 3015

    def test_parser_skips_corrupt_non_dict_and_truncated_lines(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stream = _fixture_text() + '\nnull\n["not", "an", "event"]\nnot json\n{"type"'

        with caplog.at_level(logging.DEBUG):
            session_id, events = _parse_stream(stream)

        assert session_id == "01b035ff-248a-735c-8173-f5ee428fe918"
        assert len(events) == 13
        assert any("Skipping" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# turn_end extraction
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Timeout trace
# ---------------------------------------------------------------------------


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
            {"type": "session", "id": "pi-timeout"},
            *self._partial_events(),
        ]
        stdout = "\n".join(json.dumps(event) for event in events)

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(-9, stdout, "still running", "produced no output for 60s"),
        ):
            with pytest.raises(PiTimeout) as excinfo:
                _run_initial()

        exc = excinfo.value
        assert exc.session_id == "pi-timeout"
        assert exc.partial_message == (
            "Working...\n\n*Listing current directory*\n\nDone.\n\n*read*"
        )
        assert exc.reason == "produced no output for 60s"


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


class TestCommandConstruction:
    """Pi's CLI differs from OMP's flag and attachment syntax."""

    def test_initial_command_no_cwd_no_auto_approve(self) -> None:
        """Pi has no --cwd flag and no --auto-approve flag."""
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            run_initial(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                prompt="do it",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
            )

        cmd = runner.call_args.kwargs["cmd"]
        assert "--cwd" not in cmd
        assert "--auto-approve" not in cmd

    def test_initial_command_structure_flags_ordering_and_attachments(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
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
            "01b035ff-248a-735c-8173-f5ee428fe918",
            "hello",
            3015,
        )
        assert runner.call_args.kwargs["cmd"] == [
            "pi",
            "-p",
            "--mode",
            "json",
            "--model",
            "anthropic/claude-opus-5",
            "@/workspace/attachments/brief.md",
            "@notes.txt",
            "\n@developer implement this",
        ]
        kwargs = runner.call_args.kwargs
        assert kwargs["hide_paths"] == ["/secret"]
        assert kwargs["attachments_path"] == "/workspace/attachments"
        assert kwargs["dir_map"] == [("/host/mount", "/sandbox/mount")]

    def test_initial_omits_model_when_not_given(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            run_initial(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                prompt="do it",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
            )

        cmd = runner.call_args.kwargs["cmd"]
        assert "--model" not in cmd

    def test_resume_uses_session_flag_not_dash_r(self) -> None:
        """Verify --session <id> is used, NOT -r (Pi's interactive picker)."""
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            run_resume(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                session_id="pi-session-abc",
                message="continue",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
            )

        cmd = runner.call_args.kwargs["cmd"]
        assert "-r" not in cmd
        assert "--session" in cmd
        idx = cmd.index("--session")
        assert cmd[idx + 1] == "pi-session-abc"

    def test_resume_command_full_structure(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            message, context_tokens = run_resume(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                session_id="pi-session-abc",
                message="\ncontinue",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
                files=["/workspace/attachments/followup.txt"],
                model="anthropic/claude-opus-5",
            )

        assert (message, context_tokens) == ("hello", 3015)
        assert runner.call_args.kwargs["cmd"] == [
            "pi",
            "-p",
            "--mode",
            "json",
            "--session",
            "pi-session-abc",
            "--model",
            "anthropic/claude-opus-5",
            "@/workspace/attachments/followup.txt",
            "\n\ncontinue",
        ]

    def test_newline_prefix_unconditional_even_when_already_present(self) -> None:
        """Prompt already starting with newline still gets an extra prefix."""
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            run_initial(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                prompt="\nalready prefixed",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
            )

        cmd = runner.call_args.kwargs["cmd"]
        assert cmd[-1] == "\n\nalready prefixed"

    def test_newline_prefix_on_at_sign_prompt(self) -> None:
        """Prompt starting with '@' must be prefixed to avoid attachment parse."""
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            run_initial(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                prompt="@developer do this",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
            )

        cmd = runner.call_args.kwargs["cmd"]
        assert cmd[-1] == "\n@developer do this"

    def test_resume_newline_prefix_unconditional(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            run_resume(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                session_id="pi-session-abc",
                message="@reply to this",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
            )

        cmd = runner.call_args.kwargs["cmd"]
        assert cmd[-1] == "\n@reply to this"

    @pytest.mark.parametrize("session_id", ["", None])
    def test_resume_rejects_empty_session_id(self, session_id: str | None) -> None:
        with patch("symphony_linear.pi.agent_runner.run") as runner:
            with pytest.raises(PiError, match="non-empty session_id"):
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


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class TestEnvironment:
    """Pi adapter env: HOME always, PI_CODING_AGENT_DIR only when set."""

    def test_env_always_contains_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            _run_initial()

        env = runner.call_args.kwargs["env"]
        assert "HOME" in env
        assert env["HOME"] == str(Path.home())

    def test_env_contains_pi_coding_agent_dir_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/pi/agent")

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            _run_initial()

        env = runner.call_args.kwargs["env"]
        assert env.get("PI_CODING_AGENT_DIR") == "/custom/pi/agent"

    def test_env_omits_pi_coding_agent_dir_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            _run_initial()

        env = runner.call_args.kwargs["env"]
        assert "PI_CODING_AGENT_DIR" not in env


# ---------------------------------------------------------------------------
# Agent dir extra_rw_paths handling
# ---------------------------------------------------------------------------


class TestAgentDirRwPaths:
    """PI_CODING_AGENT_DIR is appended to extra_rw_paths when set and is a dir."""

    def test_agent_dir_appended_when_env_set_and_dir_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "pi_agent"
        agent_dir.mkdir()
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            _run_initial()

        extra_rw = runner.call_args.kwargs["extra_rw_paths"]
        assert str(agent_dir) in extra_rw

    def test_agent_dir_not_appended_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            _run_initial(extra_rw_paths=[])

        extra_rw = runner.call_args.kwargs["extra_rw_paths"]
        assert extra_rw == []

    def test_agent_dir_not_appended_when_dir_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing_dir = tmp_path / "nonexistent_pi_agent"
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(missing_dir))

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            _run_initial()

        extra_rw = runner.call_args.kwargs["extra_rw_paths"]
        assert str(missing_dir) not in extra_rw

    def test_warning_emitted_when_dir_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        missing_dir = tmp_path / "nonexistent_pi_agent"
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(missing_dir))

        with caplog.at_level(logging.WARNING, logger="symphony_linear.pi"):
            with patch(
                "symphony_linear.pi.agent_runner.run",
                return_value=(0, _fixture_text(), "", None),
            ):
                _run_initial()

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warning_messages) == 1
        assert str(missing_dir) in warning_messages[0]
        assert "EROFS" in warning_messages[0]

    def test_no_warning_when_env_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)

        with caplog.at_level(logging.WARNING, logger="symphony_linear.pi"):
            with patch(
                "symphony_linear.pi.agent_runner.run",
                return_value=(0, _fixture_text(), "", None),
            ):
                _run_initial()

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warning_messages == []

    def test_no_warning_when_dir_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        agent_dir = tmp_path / "pi_agent"
        agent_dir.mkdir()
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

        with caplog.at_level(logging.WARNING, logger="symphony_linear.pi"):
            with patch(
                "symphony_linear.pi.agent_runner.run",
                return_value=(0, _fixture_text(), "", None),
            ):
                _run_initial()

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warning_messages == []

    def test_agent_dir_deduplicated_when_already_in_extra_rw_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "pi_agent"
        agent_dir.mkdir()
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            # Pass the same path in extra_rw_paths already
            _run_initial(extra_rw_paths=[str(agent_dir)])

        extra_rw = runner.call_args.kwargs["extra_rw_paths"]
        # Should appear exactly once
        assert extra_rw.count(str(agent_dir)) == 1


# ---------------------------------------------------------------------------
# Exit validation
# ---------------------------------------------------------------------------


class TestExitValidation:
    """The adapter preserves cancellation and validation priority."""

    def test_signal_exit_wins_over_missing_session(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(-9, "not NDJSON", "", None),
        ):
            with pytest.raises(PiCancelled, match="killed by signal 9"):
                _run_initial()

    def test_nonzero_exit_wins_over_missing_session_and_keeps_stderr_tail(
        self,
    ) -> None:
        stderr = "bootstrap-start\n" + ("padding\n" * 1000) + "actual failure\n"
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(1, "not NDJSON", stderr, None),
        ):
            with pytest.raises(PiError) as excinfo:
                _run_initial()

        assert "actual failure" in str(excinfo.value)
        assert "bootstrap-start" not in str(excinfo.value)

    def test_zero_exit_without_session_is_an_error(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, '{"type":"turn_start"}', "", None),
        ):
            with pytest.raises(PiError, match="No session ID"):
                _run_initial()

    def test_exception_names_alias_agent_runner_exceptions(self) -> None:
        assert PiError is agent_runner.AgentError
        assert PiTimeout is agent_runner.AgentTimeout
        assert PiCancelled is agent_runner.AgentCancelled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_initial(
    extra_rw_paths: list[str] | None = None,
) -> tuple[str, str, int | None]:
    """Run a representative initial Pi turn through the mocked runner."""
    return run_initial(
        workspace_path="/workspace",
        tmp_path="/workspace/tmp",
        prompt="do it",
        timeout_seconds=60,
        idle_timeout_seconds=30,
        on_subprocess=lambda process: None,
        extra_rw_paths=extra_rw_paths,
    )
