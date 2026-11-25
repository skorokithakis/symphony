"""In-memory record of which ticket pages a human is watching.

The web UI's ticket page polls every few seconds and marks its ticket active
while the tab is visible, the window has focus and the user interacted
recently.  The built-in tracker consults this record before sending the
"Needs Input" Web Push notification, because the reply that just landed is
already on the reader's screen when they are looking at the ticket.

Nothing here is persisted: presence only means anything while this process
runs, and a restart simply lets the next poll re-register it.  The class is
thread-safe because the web UI handles each request on its own thread while
the orchestrator's worker threads read the same instance.
"""

from __future__ import annotations

import threading
import time

#: How long a presence signal counts for.  The page polls every 3 seconds, so
#: this tolerates a few missed polls (a slow request, a laptop that has just
#: woken) without keeping the gate shut long after the reader navigated away.
ACTIVE_WINDOW_SECONDS = 15.0


class TicketPresence:
    """The monotonic time of each ticket page's last active signal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_active: dict[str, float] = {}

    def mark_active(self, ticket_id: str) -> None:
        """Record that a human is looking at *ticket_id* right now."""
        with self._lock:
            self._last_active[ticket_id] = time.monotonic()

    def is_active(self, ticket_id: str) -> bool:
        """Return True when *ticket_id* had an active signal recently."""
        # Both the lookup and the age comparison happen under the lock, so the
        # read is one consistent observation of the record.
        with self._lock:
            last_active = self._last_active.get(ticket_id)
            return (
                last_active is not None
                and time.monotonic() - last_active <= ACTIVE_WINDOW_SECONDS
            )
