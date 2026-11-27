"""Tests for state persistence, atomic writes, and thread safety."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from symphony_linear.state import (
    SessionRecord,
    StateManager,
    TicketState,
    TicketStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticket(ticket_id: str = "1", **overrides: Any) -> TicketState:
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

    def test_get(self, tmp_path: Path) -> None:
        mgr = StateManager(tmp_path / "state.json")
        mgr.load()
        mgr.upsert(_make_ticket("42"))

        ticket = mgr.get("42")
        assert ticket is not None
        assert ticket.ticket_id == "42"
        assert mgr.get("999") is None

    def test_remove(self, tmp_path: Path) -> None:
        mgr = StateManager(tmp_path / "state.json")
        mgr.load()
        mgr.upsert(_make_ticket("1"))
        assert mgr.remove("1") is True
        assert mgr.remove("1") is False
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
        num_threads = 8
        ready = threading.Barrier(num_threads + 1)  # +1 for main

        def worker(worker_id: int) -> None:
            ready.wait()
            for i in range(100):
                t = _make_ticket(str(worker_id * 100 + i))
                mgr.upsert(t)
                mgr.save()

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(num_threads)
        ]
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
# SessionRecord / session accessors
# ---------------------------------------------------------------------------


class TestSessionRecord:
    def test_legacy_record_without_agent_defaults_to_none(self) -> None:
        sr = SessionRecord.model_validate(
            {"session_id": "ses-abc", "last_seen_comment_id": "cmt-42"}
        )
        assert sr.agent is None


class TestStateManagerSessions:
    def test_session_survives_save_load(self, tmp_path: Path) -> None:
        mgr = StateManager(tmp_path / "state.json")
        mgr.load()
        mgr.set_session(
            "ticket-1",
            SessionRecord(session_id="ses-abc", last_seen_comment_id="cmt-42"),
        )
        mgr.save()

        mgr2 = StateManager(tmp_path / "state.json")
        mgr2.load()
        retrieved = mgr2.get_session("ticket-1")
        assert retrieved is not None
        assert retrieved.session_id == "ses-abc"
        assert retrieved.last_seen_comment_id == "cmt-42"

    def test_session_independent_of_tickets(self, tmp_path: Path) -> None:
        """Sessions survive ticket removal from tickets list."""
        mgr = StateManager(tmp_path / "state.json")
        mgr.load()
        mgr.set_session("ticket-1", SessionRecord(session_id="ses-abc"))
        # No ticket in tickets list — sessions are independent.
        assert mgr.get("ticket-1") is None
        assert mgr.get_session("ticket-1") is not None

    def test_legacy_state_json_without_sessions_loads(self, tmp_path: Path) -> None:
        """A state.json without the 'sessions' key must load cleanly with sessions={}."""
        old_state: dict[str, Any] = {"tickets": []}
        path = tmp_path / "state.json"
        path.write_text(json.dumps(old_state))

        mgr = StateManager(path)
        store = mgr.load()
        assert store.sessions == {}
        assert store.tickets == []


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_old_state_json_without_new_fields_loads(self, tmp_path: Path) -> None:
        """A state.json missing later fields must load with their defaults."""
        old_state: dict[str, Any] = {
            "tickets": [
                {
                    "ticket_id": "42",
                    "ticket_identifier": "TEAM-42",
                    "project_id": None,
                    "repo_url": "https://github.com/org/repo-42.git",
                    "session_id": None,
                    "workspace_path": "/tmp/ws/42",
                    "branch": "feature/ticket-42",
                    "last_seen_comment_id": None,
                    "status": "bootstrapping",
                    "metadata_comment_id": None,
                }
            ]
        }
        path = tmp_path / "state.json"
        path.write_text(json.dumps(old_state))

        mgr = StateManager(path)
        store = mgr.load()
        assert len(store.tickets) == 1
        assert store.provisioned_label_name is None
        assert store.tickets[0].agent is None
        assert store.tickets[0].attachment_count == 0
