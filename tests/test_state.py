"""Tests for state persistence, atomic writes, and thread safety."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from symphony_lite.state import (
    StateManager,
    StateStore,
    TicketState,
    TicketStatus,
    load_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticket(ticket_id: str = "1", **overrides: object) -> TicketState:
    defaults = {
        "ticket_id": ticket_id,
        "ticket_identifier": f"TEAM-{ticket_id}",
        "project_id": None,
        "repo_url": f"https://github.com/org/repo-{ticket_id}.git",
        "session_id": None,
        "workspace_path": f"/tmp/ws/{ticket_id}",
        "branch": f"feature/ticket-{ticket_id}",
        "last_seen_comment_id": None,
        "status": TicketStatus.bootstrapping,
        "metadata_comment_id": None,
    }
    defaults.update(overrides)
    return TicketState(**defaults)


# ---------------------------------------------------------------------------
# TicketState model
# ---------------------------------------------------------------------------


class TestTicketState:
    def test_create_minimal(self) -> None:
        ts = TicketState(
            ticket_id="42",
            ticket_identifier="TEAM-42",
            repo_url="https://github.com/foo/bar.git",
            workspace_path="/tmp/ws/42",
            branch="feature/ticket-42",
        )
        assert ts.ticket_id == "42"
        assert ts.status == TicketStatus.bootstrapping
        assert ts.created_at is not None
        assert ts.updated_at is not None

    def test_json_roundtrip(self) -> None:
        ts = _make_ticket("1", status=TicketStatus.working)
        data = ts.model_dump(mode="json")
        restored = TicketState.model_validate(data)
        assert restored.ticket_id == ts.ticket_id
        assert restored.status == ts.status
        assert restored.created_at == ts.created_at


# ---------------------------------------------------------------------------
# StateStore model
# ---------------------------------------------------------------------------


class TestStateStore:
    def test_empty_by_default(self) -> None:
        store = StateStore()
        assert store.tickets == []

    def test_serialize_deserialize(self) -> None:
        store = StateStore(tickets=[_make_ticket("1"), _make_ticket("2")])
        dumped = store.model_dump(mode="json")
        json_text = json.dumps(dumped)

        reloaded_raw = json.loads(json_text)
        reloaded = StateStore.model_validate(reloaded_raw)
        assert len(reloaded.tickets) == 2
        assert reloaded.tickets[0].ticket_id == "1"


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------


class TestStateManager:
    def test_load_nonexistent_creates_empty(self) -> None:
        mgr = StateManager(Path("/nonexistent/state.json"))
        store = mgr.load()
        assert store.tickets == []

    def test_save_and_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mgr = StateManager(path)
        mgr.load()
        mgr.upsert(_make_ticket("1"))
        mgr.save()

        # Reload from disk into a new manager
        mgr2 = StateManager(path)
        mgr2.load()
        assert len(mgr2.tickets) == 1
        assert mgr2.tickets[0].ticket_id == "1"

    def test_atomic_write_no_partial_file(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mgr = StateManager(path)
        mgr.load()
        mgr.upsert(_make_ticket("1"))
        mgr.save()

        # Verify the file contains complete, valid JSON.
        raw = path.read_text()
        parsed = json.loads(raw)
        assert "tickets" in parsed
        assert len(parsed["tickets"]) == 1

    def test_atomic_write_does_not_leave_temp_files(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mgr = StateManager(path)
        mgr.load()
        mgr.upsert(_make_ticket("1"))
        mgr.save()

        # Only the final file should remain (no .tmp or .json temp leftovers).
        siblings = list(path.parent.iterdir())
        assert len(siblings) == 1
        assert siblings[0].name == "state.json"

    def test_upsert_insert_and_update(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mgr = StateManager(path)
        mgr.load()

        t1 = _make_ticket("1")
        mgr.upsert(t1)
        assert len(mgr.tickets) == 1

        # Update the same ticket
        t2 = _make_ticket("1", status=TicketStatus.working)
        mgr.upsert(t2)
        assert len(mgr.tickets) == 1
        assert mgr.tickets[0].status == TicketStatus.working

    def test_get_existing(self, tmp_path: Path) -> None:
        mgr = StateManager(tmp_path / "state.json")
        mgr.load()
        mgr.upsert(_make_ticket("42"))
        t = mgr.get("42")
        assert t is not None
        assert t.ticket_id == "42"

    def test_get_missing(self, tmp_path: Path) -> None:
        mgr = StateManager(tmp_path / "state.json")
        mgr.load()
        assert mgr.get("999") is None

    def test_remove(self, tmp_path: Path) -> None:
        mgr = StateManager(tmp_path / "state.json")
        mgr.load()
        mgr.upsert(_make_ticket("1"))
        assert mgr.remove("1") is True
        assert mgr.remove("1") is False
        assert len(mgr.tickets) == 0

    def test_clear(self, tmp_path: Path) -> None:
        mgr = StateManager(tmp_path / "state.json")
        mgr.load()
        mgr.upsert(_make_ticket("1"))
        mgr.upsert(_make_ticket("2"))
        mgr.clear()
        assert len(mgr.tickets) == 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestConcurrentWrites:
    def test_no_corruption_under_concurrent_save(self, tmp_path: Path) -> None:
        """Many threads repeatedly upsert+save; final state must be consistent."""
        path = tmp_path / "state.json"
        mgr = StateManager(path)
        mgr.load()
        num_tickets = 20
        num_threads = 8
        ready = threading.Barrier(num_threads + 1)  # +1 for main

        def worker(worker_id: int) -> None:
            ready.wait()
            for i in range(100):
                t = _make_ticket(str(worker_id * 100 + i))
                mgr.upsert(t)
                mgr.save()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        ready.wait()
        for t in threads:
            t.join()

        # Reload from disk and verify integrity.
        mgr2 = StateManager(path)
        store = mgr2.load()
        assert len(store.tickets) > 0
        # Every ticket_id should have valid data.
        for ticket in store.tickets:
            assert ticket.ticket_id
            assert ticket.ticket_identifier
            assert ticket.status in TicketStatus


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


class TestLoadState:
    def test_returns_manager(self) -> None:
        mgr = load_state(Path("/nonexistent"))
        assert isinstance(mgr, StateManager)
        assert mgr.tickets == []
