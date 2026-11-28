"""In-memory record of each ticket's running agent turn.

The web UI's ticket page shows how long ago the running agent last produced
anything, so a human can tell a long tool call from a stalled turn.  The
signal is the same one the idle watchdog uses: a chunk arriving on either
stdout or stderr.

The same record also holds the turn's steps, so the page can show what the
agent is doing rather than only how recently it did it.  A step is one piece
of agent text written between tool calls, or one tool call.  A turn's steps
live under a random identifier that changes with every cleared turn, which is
what lets a polling page tell a growing turn from a brand-new one.

Nothing here is persisted: the age and the steps only mean something while
this process runs, and the orchestrator clears a ticket's entry when its task
ends so a finished turn never leaves a stale age or step list behind.  The
class is thread-safe because the agent adapter's drain threads write to it
while the web UI's request threads read it.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable, Literal

logger = logging.getLogger(__name__)

StepKind = Literal["text", "tool"]

# A step list is for a human watching a live turn, not an audit log.  A long
# turn can emit thousands of steps, and older ones only push the useful recent
# ones out of the page's scrollback, so keep a bounded window instead of
# growing without limit for as long as a turn runs.
_MAX_STEPS_PER_TURN = 200

# One text part can be an entire page of prose.  The page shows a preview of
# the turn, not its transcript, and every step is copied into an HTTP response
# on each poll.
_MAX_STEP_TEXT_LENGTH = 2000

# A tool's input is frequently a whole file or command.  The human needs to
# recognise the call, not read its arguments again.
_MAX_TOOL_SUMMARY_LENGTH = 120


@dataclass(frozen=True)
class AgentStep:
    """One step parsed from a single agent output line.

    The adapter leaves out the turn's own bookkeeping (its identifier and the
    step number) because only :class:`TurnActivity` assigns those.
    """

    kind: StepKind
    text: str
    tool: str | None = None


@dataclass(frozen=True)
class TurnStep:
    """One numbered step of a running turn, as handed to the web UI."""

    number: int
    kind: StepKind
    text: str
    tool: str | None = None


# A caller's position in a turn: the turn identifier it last saw, and the
# number of the last step it received.
StepCursor = tuple[str, int]


@dataclass(frozen=True)
class StepPage:
    """The steps a caller is missing, plus what it must do with them.

    ``replace`` means the caller must discard its own list before using
    ``steps``; otherwise it appends them to what it already has.
    """

    turn_identifier: str | None
    steps: tuple[TurnStep, ...]
    total_steps: int
    replace: bool


def tool_step_summary(arguments: object) -> str:
    """Return the one-line summary of a tool call's input, or ``""``.

    One rule for every tool and every agent, so nothing here has to know the
    tools a given CLI offers: the input object's string values, in the
    object's own (JSON document) order, joined with ``, ``.  A call whose input
    carries no strings, or no input at all, gives ``""`` so the page can show
    the tool's name alone rather than "bash: bash".
    """
    values: list[str] = []
    if isinstance(arguments, dict):
        for value in arguments.values():
            if isinstance(value, str):
                collapsed = " ".join(value.split())
                if collapsed:
                    values.append(collapsed)
    return _cut_summary(", ".join(values))


def one_line_summary(text: str) -> str:
    """Collapse *text* onto one line and cut it like a tool summary.

    OMP's own one-line intent for a tool call is a better summary than the
    arguments, but it is agent-written text and can be long; it gets the same
    cut as every other summary so one step is never a paragraph.
    """
    return _cut_summary(" ".join(text.split()))


def _cut_summary(summary: str) -> str:
    if len(summary) > _MAX_TOOL_SUMMARY_LENGTH:
        return summary[: _MAX_TOOL_SUMMARY_LENGTH - 1] + "…"
    return summary


def report_step(
    on_step: Callable[[AgentStep], None],
    step_from_line: Callable[[str], AgentStep | None],
    line: str,
) -> None:
    """Build the step *line* describes and report it, dropping it on error.

    This runs on an adapter's stdout drain thread, where an escaping exception
    kills the thread and stalls the turn until the idle watchdog fires.  The
    line and the JSON it holds are agent-controlled, so an unanticipated shape
    must cost only this one step.  This is a deliberate exception to the
    convention of letting errors propagate.
    """
    try:
        step = step_from_line(line)
        if step is not None:
            on_step(step)
    except Exception:
        logger.debug("Dropping an unreadable turn step", exc_info=True)


class _Turn:
    """One recorded turn: its identity, its recent steps and its numbering."""

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        # Numbers are never reused or renumbered when old steps are dropped,
        # so a caller's cursor stays comparable across a trim.
        self.steps: deque[TurnStep] = deque(maxlen=_MAX_STEPS_PER_TURN)
        self.total_steps = 0


class TurnActivity:
    """The monotonic time of each ticket's last agent-turn output, and its steps."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_output: dict[str, float] = {}
        self._turns: dict[str, _Turn] = {}

    def record_output(self, ticket_id: str) -> None:
        """Record that the turn for *ticket_id* wrote output just now."""
        with self._lock:
            self._last_output[ticket_id] = time.monotonic()

    def output_age(self, ticket_id: str) -> float | None:
        """Return seconds since *ticket_id* last wrote output, or None."""
        # Lookup and age are one observation under the lock, as in
        # presence.TicketPresence.is_active.
        with self._lock:
            last_output = self._last_output.get(ticket_id)
            if last_output is None:
                return None
            return time.monotonic() - last_output

    def record_step(self, ticket_id: str, step: AgentStep) -> None:
        """Append *step* to *ticket_id*'s running turn.

        A new identifier is minted when the first step after a clear arrives;
        every later step of the same turn keeps it.
        """
        with self._lock:
            turn = self._turns.get(ticket_id)
            if turn is None:
                turn = _Turn(uuid.uuid4().hex)
                self._turns[ticket_id] = turn
            turn.total_steps += 1
            turn.steps.append(
                TurnStep(
                    number=turn.total_steps,
                    kind=step.kind,
                    text=_valid_unicode(_step_text(step)),
                    tool=_valid_unicode(step.tool) if step.tool is not None else None,
                )
            )

    def read_steps(self, ticket_id: str, cursor: StepCursor | None) -> StepPage:
        """Return the steps of *ticket_id*'s turn that *cursor* is missing.

        *cursor* is the ``(turn_identifier, last_step_number)`` pair the caller
        last received, or ``None`` when it has nothing.  ``replace`` is true
        when the caller's list is unusable and it must start over: it has no
        cursor, no turn has recorded a step, the turn it knew was cleared and a
        new one minted a different identifier, or its cursor is older than the
        oldest step still kept (a page that slept through more than a window
        would otherwise append with a gap).
        """
        with self._lock:
            turn = self._turns.get(ticket_id)
            if turn is None:
                return StepPage(
                    turn_identifier=None,
                    steps=(),
                    total_steps=0,
                    replace=True,
                )
            oldest_kept = turn.steps[0].number if turn.steps else 0
            if (
                cursor is None
                or cursor[0] != turn.identifier
                or cursor[1] < oldest_kept - 1
            ):
                return StepPage(
                    turn_identifier=turn.identifier,
                    steps=tuple(turn.steps),
                    total_steps=turn.total_steps,
                    replace=True,
                )
            return StepPage(
                turn_identifier=turn.identifier,
                steps=tuple(step for step in turn.steps if step.number > cursor[1]),
                total_steps=turn.total_steps,
                replace=False,
            )

    def clear(self, ticket_id: str) -> None:
        """Forget *ticket_id*'s last output time and its turn's steps."""
        with self._lock:
            self._last_output.pop(ticket_id, None)
            self._turns.pop(ticket_id, None)


def _step_text(step: AgentStep) -> str:
    """Return the step's display text, cut to the per-step length cap."""
    text = step.text.strip()
    if len(text) > _MAX_STEP_TEXT_LENGTH:
        text = text[: _MAX_STEP_TEXT_LENGTH - 1] + "…"
    return text


def _valid_unicode(text: str) -> str:
    """Replace the code points strict UTF-8 cannot encode.

    ``json.loads`` accepts an escaped lone surrogate such as ``"\\ud83d"``, so
    a step can carry one when a tool cut a string in the middle of an emoji.
    The poll then has to encode the step into its JSON response, where such a
    code point raises and fails every poll of that ticket until the step leaves
    the window.  Sanitise at the point a step enters the record, so one bad
    step costs its own characters and never the page.
    """
    return text.encode("utf-8", errors="replace").decode("utf-8")
