"""OpenCode adapter for symphony-lite.

Launches OpenCode inside the bwrap sandbox and extracts the session ID,
final assistant message, and context-window token count from the
JSON event stream.

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
        "tokens": {
          "input": 12345,          -- prompt tokens consumed
          "output": 200,           -- completion tokens generated
          "total": 12545,
          "reasoning": 0,
          "cache": {
            "read": 5000,          -- tokens read from semantic cache
            "write": 10000         -- tokens written to semantic cache
          }
        },
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
  first event we see; that value is the *main* (top-level) session.  Events
  whose top-level ``sessionID`` differs come from subagent sessions.
- Message assembly has two modes, serving different readers:

  * :func:`_assemble_message` (the full trace) collects ``"text"`` and
    ``"tool_use"`` events in stream order:

    + ``"text"`` events contribute ``part.text`` (when non-empty).
    + ``"tool_use"`` events contribute ``*<part.state.title>*`` when
      ``part.state.title`` is a non-empty string; otherwise ``*<part.tool>*``
      when ``part.tool`` is a non-empty string; otherwise the event is
      skipped.
    + All other event types are ignored.

    Non-empty segments are joined with ``"\\n\\n"`` and the result is
    ``.strip()``-ped.  This is the timeout diagnostic (the only trace of a
    killed turn) and the fallback for the trimmed assembly.

  * :func:`_assemble_final_reply` (the trimmed reply, success path only)
    first drops events whose top-level ``sessionID`` differs from the main
    session id, then keeps only the ``"text"`` segments that appear *after*
    the last ``"tool_use"`` event — i.e. the assistant's closing reply.
    If nothing survives (the turn ended on a tool call), it falls back to
    the full assembly, so the worst case equals the full trace.

- ``stderr`` is empty on success; on failure it contains diagnostic output
  that we include in ``OpenCodeError``.
- The stream is always valid line-delimited JSON.  Corrupt lines are logged
  and skipped.

-------------------------------------------------------------------------------
Context tokens
-------------------------------------------------------------------------------

The *context token count* estimates the number of tokens the model is
processing in its context window at the end of the turn.  It is computed from
the last ``step_finish`` event's ``tokens`` fields:

    context_tokens = input + cache.read + cache.write

- ``input``: prompt tokens consumed in this turn.
- ``cache.read``: tokens read from the semantic cache (previously cached context).
- ``cache.write``: tokens written to the semantic cache (newly cached context).

Missing keys default to 0 (including the ``cache`` sub-dict).  If no
``step_finish`` event appeared in the stream, the context token count is
``None``.  Only the last ``step_finish`` event is used; sub-agent events
are not distinguished.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

from symphony_linear import agent_runner
from symphony_linear.sandbox import SECRETS_ENV_VAR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sandbox environment
# ---------------------------------------------------------------------------

# Answer the three permissions that default to "ask" in OpenCode 1.18.16 so
# they can never be raised. This must stay: 'opencode run' auto-approves
# permission requests only for the top-level session — its event loop does
# `if (event.properties.sessionID !== mainSessionID) continue` before the
# auto-approve branch, so an ask raised by a SUBAGENT session (any session
# created by the task tool) is never answered and the turn hangs until the
# daemon's absolute timeout. '--dangerously-skip-permissions' is a hidden
# alias of '--auto' implemented behind that same session-id filter, so it
# does not help either. The sandbox uses --clearenv, so the variable only
# reaches OpenCode because this adapter passes it in the runner env dict.
OPENCODE_PERMISSION = (
    '{"external_directory":"allow","doom_loop":"allow","read":"allow"}'
)

# ---------------------------------------------------------------------------
# Typed exception aliases
# ---------------------------------------------------------------------------

OpenCodeError = agent_runner.AgentError
OpenCodeTimeout = agent_runner.AgentTimeout
OpenCodeCancelled = agent_runner.AgentCancelled


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
    """Launch OpenCode for a new session with *prompt* and return the session
    id, final assistant message, and context-window token count.

    Args:
        workspace_path: Path to the workspace directory (host side; will be
            mounted read-write inside the sandbox).
        prompt: The initial prompt/message to send to OpenCode.
        timeout_seconds: Maximum number of seconds to wait for the turn to
            complete.  If exceeded the process is killed and
            :class:`OpenCodeTimeout` is raised with the absolute-cap reason.
        idle_timeout_seconds: Maximum number of seconds the turn may go
            without producing any output on stdout or stderr.  If exceeded
            the process is killed and :class:`OpenCodeTimeout` is raised
            with the idle-stall reason.
        on_subprocess: Called with the :class:`subprocess.Popen` handle
            immediately after launch.  The caller can use this to register
            the process for external cancellation.
        hide_paths: Paths to conceal inside the sandbox.  Defaults to empty
            list (no extra hiding).
        extra_rw_paths: Additional host paths to bind read-write inside the
            sandbox.  Defaults to empty list.
        attachments_path: Optional host path to a per-ticket attachments
            directory.  Passed through to the sandbox.
        dir_map: Pre-resolved ``(host_source, sandbox_dest)`` bind pairs from
            ``workspace.ensure_dir_map``.  Passed through to the sandbox.
        secrets_file: Host path to the per-ticket secrets file installed by
            ``workspace.ensure_secrets_file``, exported as
            ``SYMPHONY_SECRETS_FILE``.  ``None`` leaves the variable unset.
        tmp_path: Host path to the per-ticket tmp directory, mounted at
            ``/tmp`` inside the sandbox.  Must exist on the host (bwrap
            ``--bind`` is fatal otherwise).
        files: File paths to attach to the turn via ``--file``.  Each path is
            emitted as a ``--file <path>`` pair before the ``--`` separator.
            Defaults to ``None`` (no files).
        model: Optional per-issue model override for the primary agent.  When
            set, ``--model <model>`` is passed on the command line and beats
            the agent's configured model.  Defaults to ``None`` (agent
            default applies).

    Returns:
        A tuple of ``(session_id, final_message, context_tokens)`` where
        *context_tokens* is ``int`` or ``None``.

    Raises:
        OpenCodeError: The subprocess exited with a non-zero code.
        OpenCodeTimeout: The turn exceeded *timeout_seconds*, or produced no
            output for *idle_timeout_seconds*.
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
        "--print-logs",
    ]
    if model:
        cmd += ["--model", model]
    if files:
        for f in files:
            cmd += ["--file", f]
    cmd += ["--", prompt]

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
    """Resume an existing OpenCode session with a follow-up *message*.

    By default the model is decided by the existing session and not passed
    on the resume command line.  When *model* is set, ``--model <model>``
    is appended to the command line and overrides the session's model (the
    CLI flag takes precedence over the session's configured model).

    Args:
        workspace_path: Path to the workspace directory (host side).
        session_id: The OpenCode session identifier to resume. Must be a
            non-empty string: an empty session id would make OpenCode start
            a fresh session, and the agent would lose all ticket context.
        message: The follow-up message to send.
        timeout_seconds: Maximum seconds before raising
            :class:`OpenCodeTimeout` with the absolute-cap reason.
        idle_timeout_seconds: Maximum seconds the turn may go without
            producing output on stdout or stderr before raising
            :class:`OpenCodeTimeout` with the idle-stall reason.
        on_subprocess: Called with the Popen handle immediately after launch.
        hide_paths: Paths to conceal inside the sandbox.  Defaults to empty
            list (no extra hiding).
        extra_rw_paths: Additional host paths to bind read-write inside the
            sandbox.  Defaults to empty list.
        attachments_path: Optional host path to a per-ticket attachments
            directory.  Passed through to the sandbox.
        dir_map: Pre-resolved ``(host_source, sandbox_dest)`` bind pairs from
            ``workspace.ensure_dir_map``.  Passed through to the sandbox.
        secrets_file: Host path to the per-ticket secrets file installed by
            ``workspace.ensure_secrets_file``, exported as
            ``SYMPHONY_SECRETS_FILE``.  ``None`` leaves the variable unset.
        tmp_path: Host path to the per-ticket tmp directory, mounted at
            ``/tmp`` inside the sandbox.  Must exist on the host (bwrap
            ``--bind`` is fatal otherwise).
        files: File paths to attach to the turn via ``--file``.  Each path is
            emitted as a ``--file <path>`` pair before the ``--`` separator.
            Defaults to ``None`` (no files).
        model: Optional per-issue model override for the primary agent.  When
            set, ``--model <model>`` is passed on the command line and beats
            the session's configured model.  Defaults to ``None`` (session
            model applies).

    Returns:
        A tuple of ``(final_message, context_tokens)`` where *context_tokens*
        is ``int`` or ``None``.

    Raises:
        OpenCodeError: The subprocess exited with a non-zero code, or
            *session_id* was empty.
        OpenCodeTimeout: The turn exceeded *timeout_seconds*, or produced no
            output for *idle_timeout_seconds*.
        OpenCodeCancelled: The process was killed externally.
    """
    if not session_id:
        raise OpenCodeError(
            "run_resume requires a non-empty session_id; refusing to launch "
            "an empty OpenCode session"
        )
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
        "--print-logs",
    ]
    if model:
        cmd += ["--model", model]
    if files:
        for f in files:
            cmd += ["--file", f]
    cmd += ["--", message]

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_stream(stdout_text: str) -> tuple[str | None, list[dict]]:
    """Parse a lenient NDJSON stream and return session id and events.

    Corrupt lines are logged at DEBUG and skipped.  JSON values that are
    not dicts (e.g. ``null``) are also skipped.  A mid-line-truncated
    final line that can't be parsed as JSON is silently dropped, which is
    the desired behaviour for partially received timeout output.

    Does **not** call :func:`_assemble_message` — the caller decides when
    (and whether) to assemble.  On the happy path assembly should happen
    *after* validation; on the timeout path a defensive try/except wraps
    the call so no partial-output garbage can mask the timeout.

    Returns:
        ``(session_id, parsed_events)`` — ``session_id`` is ``None`` when
        no event carried one; ``parsed_events`` may be empty.
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

        # Skip JSON values that aren't dicts (e.g. ``null``, ``42``).
        if not isinstance(event, dict):
            logger.debug("Skipping non-dict JSON value: %s", stripped[:200])
            continue

        if session_id is None:
            sid = event.get("sessionID")
            if sid:
                session_id = sid

        parsed_events.append(event)

    return session_id, parsed_events


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
    """Run an OpenCode command, then parse and validate its JSON event stream."""
    env = {
        "HOME": str(Path.home()),
        "OPENCODE_PERMISSION": OPENCODE_PERMISSION,
    }
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
        # Best-effort salvage of partial output — must never mask the timeout.
        try:
            partial_session_id, partial_events = _parse_stream(stdout_text)
            partial_message = _assemble_message(partial_events)
        except Exception:
            logger.debug("Failed to salvage partial output on timeout", exc_info=True)
            partial_session_id = None
            partial_message = ""
        raise OpenCodeTimeout(
            f"OpenCode turn timed out: {timeout_reason}\nstderr: {stderr_tail}",
            partial_message=partial_message,
            session_id=partial_session_id,
            reason=timeout_reason,
        )

    # Raw OpenCode output; only useful when diagnosing parse/protocol issues.
    logger.debug("=== raw OpenCode stdout ===\n%s\n=== end stdout ===", stdout_text)
    if stderr_text:
        logger.debug("=== raw OpenCode stderr ===\n%s\n=== end stderr ===", stderr_text)

    # ----------------------------------------------------------------------
    # Parse NDJSON events (lenient — corrupt lines are skipped).
    # ----------------------------------------------------------------------
    session_id, parsed_events = _parse_stream(stdout_text)

    # ----------------------------------------------------------------------
    # Extract context tokens from the last step_finish.
    # ----------------------------------------------------------------------
    context_tokens = _extract_context_tokens(parsed_events)

    # ----------------------------------------------------------------------
    # Validate.
    # ----------------------------------------------------------------------
    # Detect external kill (negative returncode = killed by signal).
    # Check this before non-zero exit so we can distinguish.
    if returncode is not None and returncode < 0:
        raise OpenCodeCancelled(
            f"OpenCode process killed by signal {-returncode}", session_id=session_id
        )

    if returncode != 0:
        raise OpenCodeError(
            f"OpenCode exited with code {returncode}\nstderr: {stderr_text[-2000:]}"
        )

    if session_id is None:
        raise OpenCodeError(
            "No session ID found in OpenCode JSON stream.\n"
            f"stdout: {stdout_text[:2000]}\n"
            f"stderr: {stderr_text[-2000:]}"
        )

    # ----------------------------------------------------------------------
    # Assemble final message (after validation so malformed-but-parseable
    # output doesn't mask exit-code / signal / missing-session errors).
    # A successful turn posts only the closing reply; the full trace stays
    # available as the fallback (and for the timeout path above).
    # ----------------------------------------------------------------------
    final_message = _assemble_final_reply(parsed_events, session_id)

    return session_id, final_message, context_tokens


def _assemble_final_reply(events: list[dict], session_id: str) -> str:
    """Assemble the trimmed final reply for a successful turn.

    The full :func:`_assemble_message` trace makes tracker comments hard to
    read — the reader must scroll past narration and tool titles to reach
    the answer.  For a successful turn the comment is trimmed to the
    assistant's closing reply:

    * Events whose top-level ``sessionID`` is set and differs from
      *session_id* (subagent chatter) are dropped.
    * Only ``"text"`` segments that appear *after* the last ``"tool_use"``
      event are kept — anything said between tool calls is dropped.

    If nothing survives (the turn ended on a tool call, or there was no
    text at all), fall back to :func:`_assemble_message` over the *full*
    event list, so the worst case equals the pre-trimming behaviour.

    Only the success path of :func:`_execute` uses this.  The timeout path
    keeps the full trace: it is the only diagnostic on a killed turn.
    """
    # (a) Drop events whose top-level sessionID is set and differs from the
    # main session id — this kills subagent chatter.
    filtered: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        sid = event.get("sessionID")
        if sid and sid != session_id:
            continue
        filtered.append(event)

    # (b) Find the last tool_use event so only text after it survives.
    last_tool_use = -1
    for i, event in enumerate(filtered):
        if event.get("type") == "tool_use":
            last_tool_use = i

    segments: list[str] = []
    for event in filtered[last_tool_use + 1 :]:
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            part = {}
        text = part.get("text")
        if isinstance(text, str) and text:
            segments.append(text)

    trimmed = "\n\n".join(segments).strip()

    # (c) Empty result (turn ended on a tool call) → full assembly fallback.
    if trimmed:
        return trimmed
    return _assemble_message(events)


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
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        part = event.get("part")
        if not isinstance(part, dict):
            part = {}

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


def _extract_context_tokens(events: list[dict]) -> int | None:
    """Compute the context-window token count from the last ``step_finish``.

    Returns the sum of ``input`` + ``cache.read`` + ``cache.write`` from the
    most recent ``step_finish`` event's ``tokens`` dict.  Missing keys and a
    missing ``cache`` sub-dict each default to 0.

    Returns ``None`` only when no ``step_finish`` event was seen at all.
    When a ``step_finish`` exists but its token data is missing or malformed,
    it yields 0 (all missing fields default to 0).
    """
    last_tokens: dict = {}
    found = False
    for event in reversed(events):
        if event.get("type") == "step_finish":
            found = True
            part = event.get("part")
            if not isinstance(part, dict):
                part = {}
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                last_tokens = tokens
            break

    if not found:
        return None

    input_tokens = last_tokens.get("input", 0) or 0
    cache = last_tokens.get("cache")
    if isinstance(cache, dict):
        cache_read = cache.get("read", 0) or 0
        cache_write = cache.get("write", 0) or 0
    else:
        cache_read = 0
        cache_write = 0
    return input_tokens + cache_read + cache_write
