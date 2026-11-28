"""Daemon metrics, registered on prometheus_client's default registry.

Every metric is a module-level object on the default ``REGISTRY``.  There is
one daemon per process and the web UI server that serves ``/metrics`` lives in
the same process, so passing a registry around would only add plumbing; a
custom registry is what two daemons in one process would need, which is not a
supported configuration.

The counters and histograms live here rather than in the orchestrator so the
web UI can serve the exposition without importing the orchestrator (and its
tracker backends), and so each metric name is declared in exactly one place.
The gauges in this module describe the daemon's current state; like all
Prometheus gauges they go stale if the daemon dies, which is why the state
gauges are sampled once per tick rather than through a callback: a callback
would bind one module-level metric to one orchestrator instance, and the
orchestrator may be constructed more than once per process in tests.

There are deliberately no per-ticket labels.  A label value per ticket makes
one time series per ticket forever (they are never removed from the default
registry), and the ticket ids are unbounded.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from prometheus_client import (
    CONTENT_TYPE_LATEST as CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from symphony_linear.agent_runner import AgentCancelled, AgentTimeout
from symphony_linear.state import TicketStatus

# CONTENT_TYPE_LATEST is imported above and re-exported for the web UI's view,
# so only this module imports prometheus_client.

turns_total = Counter(
    "symphony_turns_total",
    "Agent turns by agent, kind (initial or resume) and outcome.",
    ("agent", "kind", "outcome"),
)

turn_duration_seconds = Histogram(
    "symphony_turn_duration_seconds",
    "Wall-clock duration of an agent turn, by agent and kind.",
    ("agent", "kind"),
)

tickets = Gauge(
    "symphony_tickets",
    "Tickets tracked in state.json, by internal TicketStatus.",
    ("status",),
)

active_tasks = Gauge(
    "symphony_active_tasks",
    "Pipeline tasks currently in flight.",
)

qa_serve_active = Gauge(
    "symphony_qa_serve_active",
    "1 while a QA serve process is running, 0 otherwise.",
)

tick_duration_seconds = Histogram(
    "symphony_tick_duration_seconds",
    "Wall-clock duration of one poll tick.",
)

last_tick_timestamp_seconds = Gauge(
    "symphony_last_tick_timestamp_seconds",
    "Unix time of the last poll tick that did not raise.",
)


def exposition() -> bytes:
    """Return every registered metric in the Prometheus text format."""
    return generate_latest()


def _timeout_outcome(reason: str) -> str:
    """Map an ``AgentTimeout.reason`` to its outcome label.

    ``agent_runner`` builds exactly two reason strings: an absolute cap
    ("exceeded Ns in total") and an idle stall ("produced no output for Ns").
    Matching the absolute one keeps the human-readable reason untouched, which
    the failure comment quotes verbatim.
    """
    return "total_timeout" if "in total" in reason else "idle_timeout"


@contextmanager
def record_turn(agent: str, kind: str) -> Iterator[None]:
    """Record the duration and outcome of the agent call inside the block.

    The block must contain only the adapter call: a failure before it is not a
    turn, so the caller keeps that part outside and this covers the call
    alone.  The exception is re-raised unchanged; this only observes it.
    """
    start = time.monotonic()
    outcome = "success"
    try:
        yield
    except AgentTimeout as exc:
        outcome = _timeout_outcome(exc.reason)
        raise
    except AgentCancelled:
        outcome = "cancelled"
        raise
    except BaseException:
        # Any other failure from the agent call (AgentError, a parsing
        # exception, a bug) is a failed turn.  BaseException rather than
        # Exception because a turn that dies of anything is not a success.
        outcome = "failed"
        raise
    finally:
        turn_duration_seconds.labels(agent=agent, kind=kind).observe(
            time.monotonic() - start
        )
        turns_total.labels(agent=agent, kind=kind, outcome=outcome).inc()


@contextmanager
def record_tick() -> Iterator[None]:
    """Record the duration of a poll tick and, unless it raised, its time."""
    start = time.monotonic()
    raised = False
    try:
        yield
    except BaseException:
        raised = True
        raise
    finally:
        tick_duration_seconds.observe(time.monotonic() - start)
        if not raised:
            last_tick_timestamp_seconds.set(time.time())


def set_ticket_counts(counts: Mapping[str, int]) -> None:
    """Set one ``symphony_tickets`` series per status, including zeros.

    Every status is written on every sample so a status that currently has no
    tickets reads 0 instead of disappearing from the exposition.
    """
    for status in TicketStatus:
        tickets.labels(status=status.value).set(counts.get(status.value, 0))
