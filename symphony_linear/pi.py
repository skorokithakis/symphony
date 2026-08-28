"""Pi adapter for symphony-lite.

Launches Pi inside the bwrap sandbox and extracts the session ID, final
assistant reply, and context-window token count from Pi's NDJSON stream.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable

from symphony_linear import agent_runner

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
    tmp_path: str,
    files: list[str] | None = None,
    model: str | None = None,
) -> tuple[str, str, int | None]:
    """Launch Pi for a new session.

    Returns ``(session_id, final_message, context_tokens)``.  ``files`` are
    passed as Pi positional attachment arguments and ``model`` optionally
    overrides the primary agent's model.

    Pi has no ``--cwd`` flag; the working directory is set by bwrap's
    ``--chdir`` instead.  Pi has no ``--auto-approve`` flag; its ``-a`` /
    ``--approve`` flag means "trust project-local files" — a different and
    riskier semantic that is deliberately not passed here, so untrusted ticket
    repositories cannot inject local extensions or skills.
    """
    cmd: list[str] = ["pi", "-p", "--mode", "json"]
    if model:
        cmd += ["--model", model]
    if files:
        cmd += [f"@{file}" for file in files]

    # Pi treats a positional message starting with '@' as a file attachment.
    # Prefix every prompt, even one that already starts with a newline.
    cmd.append("\n" + prompt)

    return _execute(
        cmd=cmd,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        on_subprocess=on_subprocess,
        hide_paths=hide_paths or [],
        extra_rw_paths=_agent_dir_rw_paths(extra_rw_paths),
        attachments_path=attachments_path,
        dir_map=dir_map,
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
    tmp_path: str,
    files: list[str] | None = None,
    model: str | None = None,
) -> tuple[str, int | None]:
    """Resume a Pi session with a follow-up message.

    Returns ``(final_message, context_tokens)``.  An empty session ID is
    rejected so Pi cannot silently start an unrelated new session.

    IMPORTANT: Pi's ``-r`` / ``--resume`` flag is an INTERACTIVE SESSION
    PICKER that takes NO argument (unlike OMP's ``-r <id>``).  Never use
    ``-r`` here; ``--session <id>`` is the correct non-interactive resume
    flag.  If ``-r`` were used, the session id would be consumed as a
    positional message and Pi would silently start a fresh session.
    """
    if not session_id:
        raise PiError(
            "run_resume requires a non-empty session_id; refusing to launch "
            "an empty Pi session"
        )

    # --session <id> is the non-interactive resume flag.  Do NOT use -r here:
    # Pi's -r/--resume is an interactive session picker with no argument — the
    # session id would be treated as a positional message, silently starting a
    # fresh session instead of resuming the intended one.
    cmd: list[str] = ["pi", "-p", "--mode", "json", "--session", session_id]
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
        extra_rw_paths=_agent_dir_rw_paths(extra_rw_paths),
        attachments_path=attachments_path,
        dir_map=dir_map,
        tmp_path=tmp_path,
    )
    return final_message, context_tokens


def _agent_dir_rw_paths(extra_rw_paths: list[str] | None) -> list[str]:
    """Return extra_rw_paths extended with the Pi agent dir when configured.

    The default ``~/.pi`` location is handled in sandbox.py via
    ``--bind-try``; only the non-default ``PI_CODING_AGENT_DIR`` path needs
    explicit ``--bind`` treatment here.  ``--bind`` is fatal for missing
    paths, so we only add it when the directory actually exists.
    """
    base = list(extra_rw_paths or [])

    agent_dir_env = os.environ.get("PI_CODING_AGENT_DIR")
    if not agent_dir_env:
        return base

    agent_dir = Path(agent_dir_env).expanduser()
    if not os.path.isdir(agent_dir):
        logger.warning(
            "PI_CODING_AGENT_DIR is set to %s but that directory does not exist "
            "on the host. The sandbox read-write bind will be skipped (--bind is "
            "fatal on missing paths). Pi will resolve the path under the "
            "read-only root bind and crash with EROFS at the first session "
            "flush. Create the directory on the host before starting the daemon.",
            agent_dir,
        )
        return base

    resolved = os.path.realpath(agent_dir)
    # Dedupe: skip if the resolved path is already in the list.
    existing_realpaths = {os.path.realpath(p) for p in base}
    if resolved not in existing_realpaths:
        base.append(str(agent_dir))

    return base


def _build_env() -> dict[str, str]:
    """Build the environment dict for the sandboxed Pi process."""
    env: dict[str, str] = {"HOME": str(Path.home())}
    pi_agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if pi_agent_dir:
        env["PI_CODING_AGENT_DIR"] = pi_agent_dir
    return env


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
) -> tuple[str, str, int | None]:
    """Run a Pi command, then parse and validate its NDJSON event stream."""
    returncode, stdout_text, stderr_text, timeout_reason = agent_runner.run(
        cmd=cmd,
        workspace_path=workspace_path,
        tmp_path=tmp_path,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        on_subprocess=on_subprocess,
        env=_build_env(),
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
            f"Pi turn timed out: {timeout_reason}\nstderr: {stderr_tail}",
            partial_message=partial_message,
            session_id=partial_session_id,
            reason=timeout_reason,
        )

    logger.debug("=== raw Pi stdout ===\n%s\n=== end stdout ===", stdout_text)
    if stderr_text:
        logger.debug("=== raw Pi stderr ===\n%s\n=== end stderr ===", stderr_text)

    session_id, parsed_events = _parse_stream(stdout_text)
    context_tokens = _extract_context_tokens(parsed_events)

    # Preserve the runner's cancellation distinction before handling a generic
    # non-zero exit, then validate protocol essentials before assembling text.
    if returncode is not None and returncode < 0:
        raise PiCancelled(f"Pi process killed by signal {-returncode}")

    if returncode != 0:
        raise PiError(
            f"Pi exited with code {returncode}\nstderr: {stderr_text[-2000:]}"
        )

    if session_id is None:
        raise PiError(
            "No session ID found in Pi JSON stream.\n"
            f"stdout: {stdout_text[:2000]}\n"
            f"stderr: {stderr_text[-2000:]}"
        )

    return session_id, _assemble_final_reply(parsed_events), context_tokens


def _parse_stream(stdout_text: str) -> tuple[str | None, list[dict]]:
    """Parse a lenient Pi NDJSON stream into its session ID and event list.

    Corrupt lines and JSON values other than objects are logged at DEBUG and
    skipped.  This also tolerates a truncated final line after a timeout.
    """
    session_id: str | None = None
    parsed_events: list[dict] = []

    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            logger.debug("Skipping unparseable JSON line: %s", stripped[:200])
            continue

        if not isinstance(event, dict):
            logger.debug("Skipping non-dict JSON value: %s", stripped[:200])
            continue

        if session_id is None and event.get("type") == "session":
            candidate = event.get("id")
            if isinstance(candidate, str) and candidate:
                session_id = candidate

        parsed_events.append(event)

    return session_id, parsed_events


def _assemble_final_reply(events: list[dict]) -> str:
    """Return text parts from the final ``turn_end`` event's message.

    Pi keeps subagent activity inside tool-execution results, so the final
    turn-end message is already the complete top-level assistant reply.
    """
    for event in reversed(events):
        if event.get("type") != "turn_end":
            continue

        message = event.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if not isinstance(content, list):
            return ""

        segments: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                segments.append(text)
        return "\n\n".join(segments).strip()

    return ""


def _assemble_message(events: list[dict]) -> str:
    """Assemble a full Pi trace for a timeout diagnostic.

    ``message_update`` text-end events contain each streamed text part in full.
    Tool-execution starts supply concise activity labels while a turn is still
    running.  This function is intentionally only used for timeout salvage.
    """
    segments: list[str] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "message_update":
            assistant_event = event.get("assistantMessageEvent")
            if not isinstance(assistant_event, dict):
                continue
            if assistant_event.get("type") != "text_end":
                continue
            content = assistant_event.get("content")
            if isinstance(content, str) and content:
                segments.append(content)

        elif event_type == "tool_execution_start":
            intent = event.get("intent")
            tool_name = event.get("toolName")
            label = intent if isinstance(intent, str) and intent else tool_name
            if isinstance(label, str) and label:
                segments.append(f"*{label}*")

    return "\n\n".join(segments).strip()


def _extract_context_tokens(events: list[dict]) -> int | None:
    """Return input plus cache reads/writes from the final ``turn_end``.

    Missing or malformed token fields default to zero.  ``None`` means Pi
    never emitted a turn-end event at all.
    """
    for event in reversed(events):
        if event.get("type") != "turn_end":
            continue

        message = event.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            return 0
        return (
            _token_count(usage.get("input"))
            + _token_count(usage.get("cacheRead"))
            + _token_count(usage.get("cacheWrite"))
        )

    return None


def _token_count(value: object) -> int:
    """Treat missing or malformed Pi usage values as zero."""
    return value if isinstance(value, int) else 0
