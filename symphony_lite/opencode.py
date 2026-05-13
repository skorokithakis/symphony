"""OpenCode adapter for symphony-lite.

Launches OpenCode inside the bwrap sandbox and extracts the session ID and
final assistant message from the JSON event stream.

-------------------------------------------------------------------------------
Event stream format (NDJSON — one JSON object per line, on stdout)
-------------------------------------------------------------------------------

OpenCode ``--format json`` emits newline-delimited JSON to stdout.  Each line
is a single JSON object.  The events observed during testing were:

**step_start** – emitted at the beginning of a tool/turn:
    {
      "type": "step_start",
      "sessionID": "ses_...",
      "part": {
        "id": "prt_...",
        "messageID": "msg_...",
        "sessionID": "ses_...",
        "type": "step-start"
      }
    }

**text** – contains a piece of streaming text from the assistant:
    {
      "type": "text",
      "sessionID": "ses_...",
      "part": {
        "id": "prt_...",
        "messageID": "msg_...",
        "sessionID": "ses_...",
        "type": "text",
        "text": "Hello, world!"
      }
    }

**step_finish** – emitted when the turn completes:
    {
      "type": "step_finish",
      "sessionID": "ses_...",
      "part": {
        "id": "prt_...",
        "reason": "stop",          (or "error", "tool", etc.)
        "messageID": "msg_...",
        "sessionID": "ses_...",
        "type": "step-finish",
        "tokens": { ... },
        "cost": 0.123
      }
    }

**tool_use** – emitted when the assistant invokes a tool:
    {
      "type": "tool_use",
      "sessionID": "ses_...",
      "part": {
        "id": "prt_...",
        "messageID": "msg_...",
        "sessionID": "ses_...",
        "type": "tool-use",
        "tool": "bash",
        "state": {
          "title": "Running shell command",
          "status": "running"
        }
      }
    }

Other event types (e.g. ``"tool_result"``) may appear but are ignored by
this module.

Key observations:
- ``sessionID`` appears at the top level of every event.  We grab it from the
  first event we see.
- The final assistant message is assembled from ``"text"`` and ``"tool_use"``
  events in stream order:

  * ``"text"`` events contribute ``part.text`` (when non-empty).
  * ``"tool_use"`` events contribute ``*<part.state.title>*`` when
    ``part.state.title`` is a non-empty string; otherwise ``*<part.tool>*``
    when ``part.tool`` is a non-empty string; otherwise the event is skipped.
  * All other event types are ignored.

  Non-empty segments are joined with ``"\\n\\n"`` and the result is
  ``.strip()``-ped.  This ensures tool invocations between text bursts are
  visible in the Linear comment rather than silently elided.

- ``stderr`` is empty on success; on failure it contains diagnostic output
  that we include in ``OpenCodeError``.
- The stream is always valid line-delimited JSON.  Corrupt lines are logged
  and skipped.

-------------------------------------------------------------------------------
Design notes
-------------------------------------------------------------------------------

- Each call to ``run_initial`` or ``run_resume`` represents exactly one turn.
- The process is launched via ``run_in_sandbox`` from ``symphony_lite.sandbox``.
- The ``on_subprocess`` callback is invoked immediately after launch so the
  orchestrator can register the Popen handle for kill-on-label-removal.
- Timeout handling uses ``subprocess.Popen.wait(timeout=...)``.  On timeout
  the process is killed and ``OpenCodeTimeout`` is raised.
- If the process was killed by external signal (negative returncode), we raise
  ``OpenCodeCancelled`` to distinguish external kills from failures.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

from symphony_lite.sandbox import run_in_sandbox

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class OpenCodeError(Exception):
    """OpenCode process exited with a non-zero exit code."""


class OpenCodeTimeout(Exception):
    """OpenCode process timed out and was killed."""


class OpenCodeCancelled(Exception):
    """OpenCode process was killed externally (e.g. label removed)."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_initial(
    workspace_path: str,
    prompt: str,
    model: str | None = None,
    *,
    timeout_seconds: int,
    on_subprocess: Callable[[subprocess.Popen[bytes]], None],
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
) -> tuple[str, str]:
    """Launch OpenCode for a new session with *prompt* and return the session
    id and final assistant message.

    Args:
        workspace_path: Path to the workspace directory (host side; will be
            mounted read-write inside the sandbox).
        prompt: The initial prompt/message to send to OpenCode.
        model: Optional model identifier in ``provider/model`` format (e.g.
            ``anthropic/claude-sonnet-4``).  If ``None``, OpenCode uses
            whatever model its own configuration selects.
        timeout_seconds: Maximum number of seconds to wait for the turn to
            complete.  If exceeded the process is killed and
            :class:`OpenCodeTimeout` is raised.
        on_subprocess: Called with the :class:`subprocess.Popen` handle
            immediately after launch.  The caller can use this to register
            the process for external cancellation.
        hide_paths: Paths to conceal inside the sandbox.  Defaults to empty
            list (no extra hiding).
        extra_rw_paths: Additional host paths to bind read-write inside the
            sandbox.  Defaults to empty list.

    Returns:
        A tuple of ``(session_id, final_message)``.

    Raises:
        OpenCodeError: The subprocess exited with a non-zero code.
        OpenCodeTimeout: The turn exceeded *timeout_seconds*.
        OpenCodeCancelled: The process was killed externally.
    """
    cmd: list[str] = [
        "opencode",
        "run",
        "--dir",
        workspace_path,
        "--format",
        "json",
        "--dangerously-skip-permissions",
    ]
    if model:
        cmd += ["-m", model]
    cmd += ["--", prompt]

    return _execute(
        cmd=cmd,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        on_subprocess=on_subprocess,
        hide_paths=hide_paths or [],
        extra_rw_paths=extra_rw_paths or [],
    )


def run_resume(
    workspace_path: str,
    session_id: str,
    message: str,
    *,
    timeout_seconds: int,
    on_subprocess: Callable[[subprocess.Popen[bytes]], None],
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
) -> str:
    """Resume an existing OpenCode session with a follow-up *message*.

    The model is determined by the existing session and is not passed on the
    resume command line.

    Args:
        workspace_path: Path to the workspace directory (host side).
        session_id: The OpenCode session identifier to resume.
        message: The follow-up message to send.
        timeout_seconds: Maximum seconds before raising
            :class:`OpenCodeTimeout`.
        on_subprocess: Called with the Popen handle immediately after launch.
        hide_paths: Paths to conceal inside the sandbox.  Defaults to empty
            list (no extra hiding).
        extra_rw_paths: Additional host paths to bind read-write inside the
            sandbox.  Defaults to empty list.

    Returns:
        The final assistant message for the turn.

    Raises:
        OpenCodeError: The subprocess exited with a non-zero code.
        OpenCodeTimeout: The turn exceeded *timeout_seconds*.
        OpenCodeCancelled: The process was killed externally.
    """
    cmd: list[str] = [
        "opencode",
        "run",
        "--dir",
        workspace_path,
        "--session",
        session_id,
        "--format",
        "json",
        "--dangerously-skip-permissions",
        "--",
        message,
    ]

    _, final_message = _execute(
        cmd=cmd,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        on_subprocess=on_subprocess,
        hide_paths=hide_paths or [],
        extra_rw_paths=extra_rw_paths or [],
    )
    return final_message


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _execute(
    cmd: list[str],
    workspace_path: str,
    timeout_seconds: int,
    on_subprocess: Callable[[subprocess.Popen[bytes]], None],
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
) -> tuple[str, str]:
    """Launch *cmd* inside the sandbox and parse the JSON event stream.

    Returns ``(session_id, final_message)``.
    """
    home = str(Path.home())

    proc = run_in_sandbox(
        cmd=cmd,
        workspace_path=workspace_path,
        hide_paths=hide_paths or [],
        env={"HOME": home},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        extra_rw_paths=extra_rw_paths or [],
    )

    # Let the caller register the Popen handle immediately.
    on_subprocess(proc)

    # Parse the JSON stream from stdout with a timeout.
    session_id: str | None = None
    parsed_events: list[dict] = []
    stderr_tail: str = ""

    try:
        # ------------------------------------------------------------------
        # Read stdout line-by-line within the timeout window.
        # ------------------------------------------------------------------
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_bytes, stderr_bytes = proc.communicate()
            stderr_tail = _tail(stderr_bytes.decode(errors="replace"))
            raise OpenCodeTimeout(
                f"OpenCode turn timed out after {timeout_seconds}s\n"
                f"stderr: {stderr_tail}"
            )

        # ------------------------------------------------------------------
        # Decode outputs once.
        # ------------------------------------------------------------------
        stdout_text = stdout_bytes.decode(errors="replace")
        stderr_tail = stderr_bytes.decode(errors="replace") if stderr_bytes else ""

        # Raw OpenCode output; only useful when diagnosing parse/protocol issues.
        logger.debug("=== raw OpenCode stdout ===\n%s\n=== end stdout ===", stdout_text)
        if stderr_tail:
            logger.debug(
                "=== raw OpenCode stderr ===\n%s\n=== end stderr ===", stderr_tail
            )

        # ------------------------------------------------------------------
        # Parse NDJSON events.
        # ------------------------------------------------------------------
        for line in stdout_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                logger.debug("Skipping unparseable JSON line: %s", stripped[:200])
                continue

            # Capture session id from the first event that has one.
            if session_id is None:
                sid = event.get("sessionID")
                if sid:
                    session_id = sid

            parsed_events.append(event)

            # step_finish marks the end of the turn — we can stop reading
            # (though we've already read everything since communicate returned).

        # ------------------------------------------------------------------
        # Validate.
        # ------------------------------------------------------------------
        exit_code = proc.returncode

        # Detect external kill (negative returncode = killed by signal).
        # Check this before non-zero exit so we can distinguish.
        if exit_code is not None and exit_code < 0:
            raise OpenCodeCancelled(f"OpenCode process killed by signal {-exit_code}")

        if exit_code != 0:
            raise OpenCodeError(
                f"OpenCode exited with code {exit_code}\nstderr: {stderr_tail[:2000]}"
            )

        if session_id is None:
            raise OpenCodeError(
                "No session ID found in OpenCode JSON stream.\n"
                f"stdout: {stdout_text[:2000]}\n"
                f"stderr: {stderr_tail[:2000]}"
            )

        final_message = _assemble_message(parsed_events)

        return session_id, final_message

    finally:
        # Ensure the process is reaped if not already.
        if proc.returncode is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


def _assemble_message(events: list[dict]) -> str:
    """Build the final assistant message from a list of parsed NDJSON event dicts.

    Walks *events* in order and collects non-empty segments:

    * ``"text"`` events contribute ``part.text`` when non-empty.
    * ``"tool_use"`` events contribute ``*<part.state.title>*`` when the title
      is a non-empty string; otherwise ``*<part.tool>*`` when the tool name is
      a non-empty string; otherwise the event is skipped entirely.
    * All other event types are ignored.

    Segments are joined with ``"\\n\\n"`` and the result is ``.strip()``-ped.
    """
    segments: list[str] = []
    for event in events:
        event_type = event.get("type")
        part = event.get("part", {})

        if event_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                segments.append(text)

        elif event_type == "tool_use":
            state = part.get("state") or {}
            title = state.get("title")
            if isinstance(title, str) and title:
                segments.append(f"*{title}*")
            else:
                tool = part.get("tool")
                if isinstance(tool, str) and tool:
                    segments.append(f"*{tool}*")
            # If neither title nor tool is available, skip the event.

    return "\n\n".join(segments).strip()


def _tail(text: str, lines: int = 30) -> str:
    """Return the last *lines* lines of *text*."""
    all_lines = text.splitlines()
    return "\n".join(all_lines[-lines:])
