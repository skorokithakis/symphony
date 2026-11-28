"""OpenCode adapter for symphony-lite.

Targets the OpenCode V2 CLI only (``opencode run --standalone --format json
--auto --print-logs``); V1 flags such as ``--dir`` and
``--dangerously-skip-permissions`` no longer exist and V1 is unsupported.

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

**tool_use** – emitted when the assistant's tool call ends:
    {
      "type": "tool_use",
      "sessionID": "ses_...",
      "part": {
        "id": "prt_...",
        "messageID": "msg_...",
        "sessionID": "ses_...",
        "type": "tool",
        "tool": "bash",
        "state": {
          "status": "completed",
          "input": {"command": "ls -la", "description": "List files"},
          "title": "bash"
        }
      }
    }

**background_wait** – emitted when the run is idle but background jobs started
during the prompt are still alive.  The CLI does not exit while they are, and
re-emits the event when the job count changes:
    {
      "type": "background_wait",
      "timestamp": 1732...,
      "sessionID": "ses_...",
      "jobs": [
        {"id": "sh_...", "kind": "shell", "title": "sleep 400"}
      ]
    }

``kind`` values seen in OpenCode 2.3.2 are ``shell``, ``subagent``, ``image``
and ``codemode``.  When a job ends the owning session is notified and may take
another turn (more stdout events follow), after which the CLI either exits or
emits ``background_wait`` again.

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

- ``stderr`` is empty on success; with ``--print-logs`` it carries the
  server log.  V2 reports a failed turn as a stdout event
  ``{"type": "error", "error": {"message": "..."}}`` with exit code 1 and
  often an empty stderr; both the error message(s) and the stderr tail are
  included in ``OpenCodeError``.
- The stream is always valid line-delimited JSON.  Corrupt lines are logged
  and skipped.
- A ``background_wait`` whose jobs are all ``shell`` is treated as the end of
  the turn after a fixed grace period.  OpenCode has no option to bound the
  wait (upstream issue
  https://github.com/anomalyco/opencode/issues/66885), so without this a dev
  server started with ``background: true`` keeps the turn alive until the idle
  watchdog kills it and the reply is posted as a timeout.  The stop is a
  success: the session id, trimmed reply and context tokens are the ones from
  the last turn, plus one line naming the stopped jobs.  A wait that lists any
  other kind of job stays on the existing watchdogs.

-------------------------------------------------------------------------------
Steps
-------------------------------------------------------------------------------

While a turn runs, ``_step_from_line`` turns the two event types that describe
progress into one step each for the web UI: a ``text`` event carries the whole
part once that part ends, and a ``tool_use`` event arrives when the tool has
finished, with the tool's name in ``part.tool`` and its summary built from
``part.state.input``.  Everything else, including the tool's own output, is
not a step.

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
import re
import subprocess
from pathlib import Path
from typing import Callable

from symphony_linear import agent_runner
from symphony_linear.sandbox import SECRETS_ENV_VAR
from symphony_linear.turn_activity import AgentStep, report_step, tool_step_summary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

# Flags common to every turn (OpenCode V2 only; V1 is not supported).
#
# SECURITY: ``--standalone`` must stay.  Without it V2 connects to the user's
# shared background OpenCode server over localhost TCP (the sandbox shares the
# host network namespace), and the agent's tools then run on the host, outside
# bwrap.  ``--standalone`` makes the CLI run its own in-process server inside
# the sandbox.  The idle watchdog also depends on it: ``--print-logs`` only
# emits server logs when the server runs in-process.
#
# ``--auto`` is V2's name for auto-approving permissions (V1 called it
# ``--dangerously-skip-permissions``).  There is no ``--dir``: the sandbox sets
# the working directory to the repo and V2 uses the cwd.
_BASE_FLAGS = ["--standalone", "--format", "json", "--auto", "--print-logs"]

# ``opencode run`` has no option to bound how long it waits for the background
# jobs a prompt started (upstream issue
# https://github.com/anomalyco/opencode/issues/66885).  A shell job that never
# finishes, such as a dev server started with ``background: true``, therefore
# holds the turn open until the idle watchdog kills it and a perfectly good
# reply is posted as a timeout.  Until upstream adds a bound, stop the turn
# ourselves after this grace once the last stdout event waits only on shell
# jobs.  Remove this workaround when the option exists.
_BACKGROUND_WAIT_GRACE_SECONDS = 120.0

# A job title is the raw command, which can be a multi-line heredoc or hundreds
# of characters long.  The stop line is one line in a tracker comment, so keep
# it readable rather than pasting the whole script.
_MAX_JOB_TITLE_LENGTH = 200

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
    on_output: Callable[[], None] | None = None,
    on_step: Callable[[AgentStep], None] | None = None,
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
        on_output: Optional callback invoked from the output drain threads
            on every chunk from stdout or stderr.  The web UI uses it to
            show how long ago the agent last wrote anything.
        on_step: Optional callback invoked from the stdout drain thread with
            each step parsed out of the event stream.  The web UI uses it to
            show what the running turn is doing.
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
        *_BASE_FLAGS,
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
        on_output=on_output,
        on_step=on_step,
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
    on_output: Callable[[], None] | None = None,
    on_step: Callable[[AgentStep], None] | None = None,
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
        on_output: Optional callback invoked from the output drain threads
            on every chunk from stdout or stderr.
        on_step: Optional callback invoked from the stdout drain thread with
            each step parsed out of the event stream.
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
        *_BASE_FLAGS,
        "--session",
        session_id,
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
        on_output=on_output,
        on_step=on_step,
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


def _shell_job_titles(line: str) -> list[str] | None:
    """Return the titles of a shell-only ``background_wait`` event line.

    Returns ``None`` for every other line: a different event, a wait with no
    jobs, or a wait listing any job that is not a ``shell``.  Subagent, image
    and codemode jobs can still make progress on their own, so they stay on
    the existing watchdogs instead of being stopped early.
    """
    # The hook runs on the stdout drain thread for every line of a turn;
    # skip the JSON parse for the overwhelmingly common non-wait events.
    if '"background_wait"' not in line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("type") != "background_wait":
        return None
    jobs = event.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return None
    titles: list[str] = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("kind") != "shell":
            return None
        title = job.get("title")
        if not isinstance(title, str) or not title:
            title = job.get("id")
        titles.append(title if isinstance(title, str) and title else "shell job")
    return titles


def _step_from_line(line: str) -> AgentStep | None:
    """Return the step an OpenCode stdout line describes, or None.

    Only ``text`` and ``tool_use`` are steps: text arrives once per part when
    that part ends, and a tool call arrives once the tool has finished, so the
    tool's own output never enters the list.  A missing or malformed shape is
    simply not a step; :func:`symphony_linear.turn_activity.report_step`
    guards the unexpected.
    """
    # The hook runs for every line of a turn, and most are step_start,
    # step_finish or background_wait; skip the JSON parse for those.
    if '"text"' not in line and '"tool_use"' not in line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    part = event.get("part")
    if not isinstance(part, dict):
        return None

    if event.get("type") == "text":
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return AgentStep(kind="text", text=text)

    if event.get("type") != "tool_use":
        return None
    tool = part.get("tool")
    if not isinstance(tool, str) or not tool:
        return None
    state = part.get("state")
    arguments = state.get("input") if isinstance(state, dict) else None
    return AgentStep(kind="tool", tool=tool, text=tool_step_summary(arguments))


def _single_line(text: str) -> str:
    """Collapse *text* into one truncated line for a tracker comment."""
    collapsed = " ".join(text.split())
    if len(collapsed) > _MAX_JOB_TITLE_LENGTH:
        collapsed = collapsed[: _MAX_JOB_TITLE_LENGTH - 1] + "…"
    return collapsed


def _inline_code(text: str) -> str:
    """Wrap *text* in inline-code fences that survive embedded backticks."""
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest_run + 1)
    # CommonMark strips one space from each side unless the content is all
    # spaces, so padding is what lets a leading or trailing backtick survive.
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _stopped_jobs_line(titles: list[str]) -> str:
    """Build the one reply line naming the jobs a planned stop killed."""
    # A whitespace-only command collapses to nothing once normalised, which
    # would leave an empty code span; name it instead.
    rendered = ", ".join(
        _inline_code(_single_line(title) or "unnamed job") for title in titles
    )
    return f"Stopped background jobs: {rendered}"


def _execute(
    cmd: list[str],
    workspace_path: str,
    tmp_path: str,
    timeout_seconds: int,
    idle_timeout_seconds: int,
    on_subprocess: Callable[[subprocess.Popen[bytes]], None],
    on_output: Callable[[], None] | None = None,
    on_step: Callable[[AgentStep], None] | None = None,
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
    attachments_path: str | None = None,
    dir_map: list[tuple[str, str]] | None = None,
    secrets_file: str | None = None,
) -> tuple[str, str, int | None]:
    """Run an OpenCode command, then parse and validate its JSON event stream."""
    env = {"HOME": str(Path.home())}
    if secrets_file:
        env[SECRETS_ENV_VAR] = secrets_file

    # The titles from the last shell-only background_wait, reported in the
    # reply when the grace timer stops the turn.
    stopped_jobs: list[str] = []

    def _on_stdout_line(line: str) -> float | None:
        # Any line that is not a shell-only background_wait disarms an armed
        # timer: the session got another turn (a job finished and the agent is
        # working again), so the turn is alive after all.
        titles = _shell_job_titles(line)
        if titles is not None:
            stopped_jobs[:] = titles
        # Step extraction shares the hook because the drain thread already
        # frames whole lines here; an extra line hook would reassemble the
        # same stream a second time.
        if on_step is not None:
            report_step(on_step, _step_from_line, line)
        if titles is None:
            return None
        return _BACKGROUND_WAIT_GRACE_SECONDS

    (
        returncode,
        stdout_text,
        stderr_text,
        timeout_reason,
        planned_stop,
    ) = agent_runner.run(
        cmd=cmd,
        workspace_path=workspace_path,
        tmp_path=tmp_path,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        on_subprocess=on_subprocess,
        env=env,
        on_output=on_output,
        on_stdout_line=_on_stdout_line,
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
    # A planned stop is the grace timer we asked the runner to arm, so its
    # kill signal is not an external cancellation: the turn already produced
    # its reply and is treated as a success.  A real external kill leaves
    # ``planned_stop`` false and still maps to OpenCodeCancelled.
    if not planned_stop:
        # Detect external kill (negative returncode = killed by signal).
        # Check this before non-zero exit so we can distinguish.
        if returncode is not None and returncode < 0:
            raise OpenCodeCancelled(
                f"OpenCode process killed by signal {-returncode}",
                session_id=session_id,
            )

        if returncode != 0:
            error_messages = _extract_error_messages(parsed_events)
            detail = ""
            if error_messages:
                detail = "\nerror: " + "; ".join(error_messages)
            raise OpenCodeError(
                f"OpenCode exited with code {returncode}{detail}"
                f"\nstderr: {stderr_text[-2000:]}"
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
    if planned_stop and stopped_jobs:
        # Without this line the reply would silently omit the background work
        # the stop killed, and a reader would not know the turn ended early.
        stopped_line = _stopped_jobs_line(stopped_jobs)
        final_message = (
            f"{final_message}\n\n{stopped_line}" if final_message else stopped_line
        )

    return session_id, final_message, context_tokens


def _extract_error_messages(events: list[dict]) -> list[str]:
    """Return the messages of ``{"type": "error", "error": {"message": ...}}``
    events.

    V2 reports failures as a stdout event with exit code 1 and an empty
    stderr, so these messages are the only diagnostic on such a failure.
    """
    messages: list[str] = []
    for event in events:
        if event.get("type") != "error":
            continue
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                messages.append(message)
    return messages


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
