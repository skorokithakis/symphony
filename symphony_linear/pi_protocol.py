"""Shared NDJSON protocol helpers for pi-family coding agents."""

from __future__ import annotations

import json
import logging
from typing import Callable

from symphony_linear.turn_activity import (
    AgentStep,
    one_line_summary,
    report_step,
    tool_step_summary,
)

logger = logging.getLogger(__name__)


def _parse_stream(stdout_text: str) -> tuple[str | None, list[dict]]:
    """Parse a lenient pi-family NDJSON stream into its session ID and event list.

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


def _step_line_hook(
    on_step: Callable[[AgentStep], None] | None,
) -> Callable[[str], None] | None:
    """Return the stdout line hook that reports steps, or None without one.

    The pi family arms no grace timer, so this hook exists only to turn lines
    into steps.  Returning None when *on_step* is absent keeps the runner from
    framing stdout into lines for adapters that do not use them.
    """
    if on_step is None:
        return None
    return lambda line: report_step(on_step, _step_from_line, line)


def _step_from_line(line: str) -> AgentStep | None:
    """Return the step a pi-family stdout line describes, or None.

    ``message_update`` is a step only when it ends a text part, and
    ``tool_execution_start`` is a step once the tool starts (unlike OpenCode,
    whose tool event arrives at the end).  OMP sometimes knows why a tool is
    called and puts that one-line intent on the event; when it does, that is a
    better summary than the arguments, which can be a whole file.
    """
    # Most lines are deltas, turn framing or tool progress; skip the JSON
    # parse unless the line is one of the two events that is a step.  The
    # filter looks for "text_end" rather than "message_update": every token
    # delta is a message_update line too, and those are the bulk of a turn.
    if '"tool_execution_start"' not in line and '"text_end"' not in line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None

    if event.get("type") == "message_update":
        assistant_event = event.get("assistantMessageEvent")
        if not isinstance(assistant_event, dict):
            return None
        if assistant_event.get("type") != "text_end":
            return None
        content = assistant_event.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return AgentStep(kind="text", text=content)

    if event.get("type") != "tool_execution_start":
        return None
    tool_name = event.get("toolName")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    intent = event.get("intent")
    if isinstance(intent, str) and intent.strip():
        return AgentStep(kind="tool", tool=tool_name, text=one_line_summary(intent))
    return AgentStep(
        kind="tool",
        tool=tool_name,
        text=tool_step_summary(event.get("args")),
    )


def _validate_final_turn(events: list[dict], error_type: type[Exception]) -> None:
    """Raise an adapter-specific error when the final terminal turn failed.

    Automatic retries emit separate ``turn_end`` events, so only the last one
    controls the result. ``aborted`` is also a failed normally-exited turn;
    callers must preserve signal-cancellation handling before calling this
    helper so a locally killed process remains cancelled.
    """
    for event in reversed(events):
        if event.get("type") != "turn_end":
            continue

        message = event.get("message")
        if not isinstance(message, dict):
            return

        stop_reason = message.get("stopReason")
        if stop_reason not in ("error", "aborted"):
            return

        error_message = message.get("errorMessage")
        if isinstance(error_message, str) and error_message:
            raise error_type(error_message)
        raise error_type(f"pi-family turn ended with stopReason {stop_reason!r}")


def _assemble_final_reply(events: list[dict]) -> str:
    """Return text parts from the final ``turn_end`` event's message.

    pi-family agents keep subagent activity inside tool-execution results, so
    the final turn-end message is already the complete top-level assistant reply.
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
    """Assemble a full agent trace for a timeout diagnostic.

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

    Missing or malformed token fields default to zero.  ``None`` means the
    agent never emitted a turn-end event at all.
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
    """Treat missing or malformed usage values as zero."""
    return value if isinstance(value, int) else 0
