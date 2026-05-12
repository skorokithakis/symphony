"""Per-ticket state persistence with atomic writes and thread-safe access.

State is stored in ``<workspace_dir>/state.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class TicketStatus(str, Enum):
    bootstrapping = "bootstrapping"
    working = "working"
    needs_input = "needs_input"
    failed = "failed"


# ---------------------------------------------------------------------------
# Ticket state model
# ---------------------------------------------------------------------------


class TicketState(BaseModel):
    """State for a single ticket being processed by the daemon."""

    ticket_id: str = Field(..., description="Linear ticket ID")
    ticket_identifier: str = Field(..., description="Human-readable identifier (e.g. TEAM-42)")
    project_id: str | None = Field(None, description="Linear project ID")
    repo_url: str = Field(..., description="Clone URL of the repository")
    session_id: str | None = Field(None, description="OpenCode session identifier")
    workspace_path: str = Field(..., description="Path to the workspace on disk")
    branch: str = Field(..., description="Git branch name for this ticket")
    last_seen_comment_id: str | None = Field(None, description="Last Linear comment ID the bot has seen")
    status: TicketStatus = TicketStatus.bootstrapping
    metadata_comment_id: str | None = Field(None, description="Linear comment ID of the bot's metadata comment")
    setup_error: str | None = Field(None, description="Non-null when a setup step failed; prevents re-spam")

    # Audit trail (not part of the ticket spec but useful for debugging)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# State file container
# ---------------------------------------------------------------------------


class StateStore(BaseModel):
    """Container that holds all tracked ticket states."""

    tickets: list[TicketState] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------


class StateManager:
    """Manages loading, saving, and querying per-ticket state.

    Features:

    * **Atomic writes** – data is written to a temporary file in the same
      directory, then renamed over the target, so the file is never left in a
      partially-written state.
    * **Thread safety** – a module-level ``threading.Lock`` serialises all
      read and write operations.
    """

    # Module-level lock shared across all StateManager instances so that even
    # separate instances in different threads are safe.
    _lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: StateStore = StateStore()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> StateStore:
        """Load state from disk, returning the deserialised store.

        If the state file does not exist, an empty store is returned (and
        kept in memory).
        """
        with self._lock:
            if self._path.exists():
                raw = json.loads(self._path.read_text())
                self._store = StateStore.model_validate(raw)
            else:
                self._store = StateStore()
            return self._store

    def save(self) -> None:
        """Persist the current in-memory store to disk atomically."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = self._store.model_dump(mode="json")
            json_text = json.dumps(data, indent=2, ensure_ascii=False)

            # Write to a temp file in the same directory, then atomically rename.
            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                dir=str(self._path.parent),
                delete=False,
            )
            try:
                tmp.write(json_text)
                tmp.flush()
                os.fsync(tmp.fileno())
            finally:
                tmp.close()

            os.replace(tmp.name, str(self._path))

    @property
    def tickets(self) -> list[TicketState]:
        """Return the current list of tracked tickets."""
        return self._store.tickets

    def get(self, ticket_id: str) -> TicketState | None:
        """Look up a ticket by its Linear ticket ID.

        Returns ``None`` if the ticket is not tracked.
        """
        for t in self._store.tickets:
            if t.ticket_id == ticket_id:
                return t
        return None

    def upsert(self, ticket: TicketState) -> None:
        """Insert or update a ticket in the store.

        If a ticket with the same ``ticket_id`` already exists, it is
        replaced; otherwise the new ticket is appended.
        """
        for i, t in enumerate(self._store.tickets):
            if t.ticket_id == ticket.ticket_id:
                self._store.tickets[i] = ticket
                return
        self._store.tickets.append(ticket)

    def remove(self, ticket_id: str) -> bool:
        """Remove a ticket from the store.  Returns ``True`` if it was found."""
        for i, t in enumerate(self._store.tickets):
            if t.ticket_id == ticket_id:
                del self._store.tickets[i]
                return True
        return False

    def clear(self) -> None:
        """Remove all tracked tickets from the in-memory store."""
        self._store.tickets.clear()


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def load_state(workspace_dir: Path) -> StateManager:
    """Create a StateManager and load existing state from disk.

    Reads/writes ``<workspace_dir>/state.json``.

    Returns the manager ready to use.
    """
    mgr = StateManager(workspace_dir / "state.json")
    mgr.load()
    return mgr
