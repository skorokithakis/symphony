"""Tests for the pi agent adapter.

The tests use a captured pi-family NDJSON stream and a mocked shared runner;
they never invoke the real ``pi`` binary or an LLM.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from symphony_linear import agent_runner, opencode, pi, pi_protocol
from symphony_linear.pi import PiCancelled, PiError, PiTimeout, run_initial, run_resume

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "omp_events.jsonl"
ERROR_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "omp_error_events.jsonl"
)
# Real captured pi stream; detects pi format drift from OMP's shared parser.
PI_SUCCESS_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "pi_success_events.jsonl"
)


def _fixture_text(fixture_path: Path = FIXTURE_PATH) -> str:
    """Return a captured pi-family NDJSON stream for a completed simple turn."""
    return fixture_path.read_text()


def _error_fixture_text() -> str:
    """Return a pi-family stream whose retries end in a provider failure."""
    return ERROR_FIXTURE_PATH.read_text()


# This regression test catches pi format drift away from OMP's shared parser.
class TestRealPiStream:
    """Exercise pi's real wire format through the shared adapter."""

    def test_run_initial_extracts_real_pi_stream(self) -> None:
        stream = _fixture_text(PI_SUCCESS_FIXTURE_PATH)
        _, events = pi_protocol._parse_stream(stream)

        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, stream, "", None),
        ):
            session_id, message, context_tokens = pi.run_initial(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                prompt="Reply with exactly: OK",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
            )

        assert (session_id, message, context_tokens) == (
            "01a051fa-999d-7ff3-b776-5fe27cb1e592",
            "OK",
            1067,
        )
        assert pi_protocol._assemble_message(events) == "OK"


class TestCommandConstruction:
    """pi uses pi-family positional attachments and pi-specific flags."""

    def test_public_signatures_match_opencode(self) -> None:
        assert inspect.signature(run_initial) == inspect.signature(opencode.run_initial)
        assert inspect.signature(run_resume) == inspect.signature(opencode.run_resume)

    def test_initial_command_prefixes_prompt_and_uses_positional_attachments(
        self,
    ) -> None:
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
            "01a035ff-248a-735c-8173-f5ee428fe917",
            "hello",
            3479,
        )
        assert runner.call_args.kwargs["cmd"] == [
            "pi",
            "-p",
            "--mode",
            "json",
            "--no-approve",
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
            "symphony_linear.pi.agent_runner.run",
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
            "symphony_linear.pi.agent_runner.run",
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

    def test_resume_uses_session_and_prefixes_an_already_prefixed_newline(
        self,
    ) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _fixture_text(), "", None),
        ) as runner:
            message, context_tokens = run_resume(
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                session_id="pi-session",
                message="\ncontinue",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
                files=["/workspace/attachments/followup.txt"],
                model="anthropic/claude-opus-5",
            )

        assert (message, context_tokens) == ("hello", 3479)
        assert runner.call_args.kwargs["cmd"] == [
            "pi",
            "-p",
            "--mode",
            "json",
            "--no-approve",
            "--session",
            "pi-session",
            "--model",
            "anthropic/claude-opus-5",
            "@/workspace/attachments/followup.txt",
            "\n\ncontinue",
        ]

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


class TestExitValidation:
    """The adapter rejects terminal protocol failures despite a zero exit."""

    def test_zero_exit_error_turn_raises_last_provider_error(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(0, _error_fixture_text(), "", None),
        ):
            with pytest.raises(PiError) as excinfo:
                run_initial(
                    workspace_path="/workspace",
                    tmp_path="/workspace/tmp",
                    prompt="implement this",
                    timeout_seconds=60,
                    idle_timeout_seconds=30,
                    on_subprocess=lambda process: None,
                )

        message = str(excinfo.value)
        assert "429 provider rate limit persisted after final retry" in message
        assert "429 retry attempt 3 failed" not in message


class TestSignalCancellation:
    """A killed pi turn stays a cancellation and salvages its session id."""

    def test_signal_exit_without_session_yields_none(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(-9, "not NDJSON", "", None),
        ):
            with pytest.raises(PiCancelled, match="killed by signal 9") as excinfo:
                run_initial(
                    workspace_path="/workspace",
                    tmp_path="/workspace/tmp",
                    prompt="implement this",
                    timeout_seconds=60,
                    idle_timeout_seconds=30,
                    on_subprocess=lambda process: None,
                )

        assert excinfo.value.session_id is None

    def test_signal_exit_carries_parsed_session_id(self) -> None:
        with patch(
            "symphony_linear.pi.agent_runner.run",
            return_value=(-9, _fixture_text(), "", None),
        ):
            with pytest.raises(PiCancelled, match="killed by signal 9") as excinfo:
                run_initial(
                    workspace_path="/workspace",
                    tmp_path="/workspace/tmp",
                    prompt="implement this",
                    timeout_seconds=60,
                    idle_timeout_seconds=30,
                    on_subprocess=lambda process: None,
                )

        assert excinfo.value.session_id == "01a035ff-248a-735c-8173-f5ee428fe917"


class TestExceptionAliases:
    """Adapter-facing exception names remain runner-compatible."""

    def test_exception_names_alias_agent_runner_exceptions(self) -> None:
        assert PiError is agent_runner.AgentError
        assert PiTimeout is agent_runner.AgentTimeout
        assert PiCancelled is agent_runner.AgentCancelled
