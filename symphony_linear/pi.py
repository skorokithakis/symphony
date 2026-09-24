"""pi adapter for symphony-linear.

Launches pi inside the bwrap sandbox and extracts the session ID, final assistant
reply, and context-window token count from pi's NDJSON stream.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable

from symphony_linear import agent_runner
from symphony_linear.pi_protocol import (
    _assemble_final_reply,
    _assemble_message,
    _extract_context_tokens,
    _parse_stream,
    _token_count as _token_count,
    _validate_final_turn,
)
from symphony_linear.sandbox import SECRETS_ENV_VAR

logger = logging.getLogger(__name__)

# Keep adapter-facing exception names while sharing the agent-neutral classes
# used by the orchestrator-facing runner.
PiError = agent_runner.AgentError
PiTimeout = agent_runner.AgentTimeout
PiCancelled = agent_runner.AgentCancelled


def run_initial(
    workspace_path: str,
    prompt: str,
    *,
    timeout_seconds: int,
    idle_timeout_seconds: int,
    on_subprocess: Callable[[subprocess.Popen[bytes]], None],
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
    attachments_path: str | None = None,
    dir_map: list[tuple[str, str]] | None = None,
    secrets_file: str | None = None,
    tmp_path: str,
    files: list[str] | None = None,
    model: str | None = None,
) -> tuple[str, str, int | None]:
    """Launch pi for a new session.

    Returns ``(session_id, final_message, context_tokens)``. ``files`` are
    passed as pi positional attachment arguments and ``model`` optionally
    overrides the primary agent's model.
    """
    cmd: list[str] = [
        "pi",
        "-p",
        "--mode",
        "json",
        # A persisted interactive approval can load project-local extensions;
        # keep daemon execution deterministic even in a trusted workspace.
        "--no-approve",
    ]
    if model:
        cmd += ["--model", model]
    if files:
        cmd += [f"@{file}" for file in files]

    # pi treats a positional message starting with '@' as a file attachment.
    # Prefix every prompt, even one that already starts with a newline.
    cmd.append("\n" + prompt)

    return _execute(
        cmd=cmd,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        on_subprocess=on_subprocess,
        hide_paths=hide_paths or [],
        extra_rw_paths=extra_rw_paths or [],
        attachments_path=attachments_path,
        dir_map=dir_map,
        secrets_file=secrets_file,
        tmp_path=tmp_path,
    )


def run_resume(
    workspace_path: str,
    session_id: str | None,
    message: str,
    *,
    timeout_seconds: int,
    idle_timeout_seconds: int,
    on_subprocess: Callable[[subprocess.Popen[bytes]], None],
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
    attachments_path: str | None = None,
    dir_map: list[tuple[str, str]] | None = None,
    secrets_file: str | None = None,
    tmp_path: str,
    files: list[str] | None = None,
    model: str | None = None,
) -> tuple[str, int | None]:
    """Resume a pi session with a follow-up message.

    Returns ``(final_message, context_tokens)``. An empty session ID is
    rejected so pi cannot silently start an unrelated new session.
    """
    if not session_id:
        raise PiError(
            "run_resume requires a non-empty session_id; refusing to launch "
            "an empty pi session"
        )

    cmd: list[str] = [
        "pi",
        "-p",
        "--mode",
        "json",
        # Keep the same deterministic trust policy for resumed turns.
        "--no-approve",
        "--session",
        session_id,
    ]
    if model:
        cmd += ["--model", model]
    if files:
        cmd += [f"@{file}" for file in files]

    # See the corresponding initial-turn comment above.
    cmd.append("\n" + message)

    _, final_message, context_tokens = _execute(
        cmd=cmd,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        on_subprocess=on_subprocess,
        hide_paths=hide_paths or [],
        extra_rw_paths=extra_rw_paths or [],
        attachments_path=attachments_path,
        dir_map=dir_map,
        secrets_file=secrets_file,
        tmp_path=tmp_path,
    )
    return final_message, context_tokens


def _execute(
    cmd: list[str],
    workspace_path: str,
    tmp_path: str,
    timeout_seconds: int,
    idle_timeout_seconds: int,
    on_subprocess: Callable[[subprocess.Popen[bytes]], None],
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
    attachments_path: str | None = None,
    dir_map: list[tuple[str, str]] | None = None,
    secrets_file: str | None = None,
) -> tuple[str, str, int | None]:
    """Run a pi command, then parse and validate its NDJSON event stream."""
    env = {"HOME": str(Path.home())}
    if secrets_file:
        env[SECRETS_ENV_VAR] = secrets_file
    returncode, stdout_text, stderr_text, timeout_reason = agent_runner.run(
        cmd=cmd,
        workspace_path=workspace_path,
        tmp_path=tmp_path,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        on_subprocess=on_subprocess,
        env=env,
        hide_paths=hide_paths,
        extra_rw_paths=extra_rw_paths or [],
        attachments_path=attachments_path,
        dir_map=dir_map,
    )

    if timeout_reason is not None:
        stderr_tail = agent_runner._tail(stderr_text)
        try:
            partial_session_id, partial_events = _parse_stream(stdout_text)
            partial_message = _assemble_message(partial_events)
        except Exception:
            logger.debug("Failed to salvage partial output on timeout", exc_info=True)
            partial_session_id = None
            partial_message = ""
        raise PiTimeout(
            f"pi turn timed out: {timeout_reason}\nstderr: {stderr_tail}",
            partial_message=partial_message,
            session_id=partial_session_id,
            reason=timeout_reason,
        )

    logger.debug("=== raw pi stdout ===\n%s\n=== end stdout ===", stdout_text)
    if stderr_text:
        logger.debug("=== raw pi stderr ===\n%s\n=== end stderr ===", stderr_text)

    session_id, parsed_events = _parse_stream(stdout_text)

    # Preserve the runner's cancellation distinction before handling a generic
    # non-zero exit or a terminal protocol failure.
    if returncode is not None and returncode < 0:
        raise PiCancelled(
            f"pi process killed by signal {-returncode}", session_id=session_id
        )

    if returncode != 0:
        raise PiError(
            f"pi exited with code {returncode}\nstderr: {stderr_text[-2000:]}"
        )

    _validate_final_turn(parsed_events, PiError)

    if session_id is None:
        raise PiError(
            "No session ID found in pi JSON stream.\n"
            f"stdout: {stdout_text[:2000]}\n"
            f"stderr: {stderr_text[-2000:]}"
        )

    context_tokens = _extract_context_tokens(parsed_events)

    return session_id, _assemble_final_reply(parsed_events), context_tokens
