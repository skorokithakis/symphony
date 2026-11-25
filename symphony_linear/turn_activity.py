"""In-memory record of when each ticket's agent turn last wrote output.

The web UI's ticket page shows how long ago the running agent last produced
anything, so a human can tell a long tool call from a stalled turn.  The
signal is the same one the idle watchdog uses: a chunk arriving on either
stdout or stderr.

Nothing here is persisted: the age only means something while this process
runs, and the orchestrator clears a ticket's entry when its task ends so a
finished turn never leaves a stale age behind.  The class is thread-safe
because the agent adapter's drain threads write to it while the web UI's
request threads read it.
"""

from __future__ import annotations

import threading
import time


class TurnActivity:
    """The monotonic time of each ticket's last agent-turn output."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_output: dict[str, float] = {}

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

    def clear(self, ticket_id: str) -> None:
        """Forget *ticket_id*'s last output time."""
        with self._lock:
            self._last_output.pop(ticket_id, None)
