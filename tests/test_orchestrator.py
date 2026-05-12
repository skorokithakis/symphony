"""Unit tests for the daemon orchestrator."""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from symphony_lite.config import AppConfig
from symphony_lite.linear import (
    Comment,
    Issue,
    LinearError,
    LinearNotFoundError,
    Project,
    ProjectLink,
)
from symphony_lite.opencode import (
    OpenCodeCancelled,
    OpenCodeError,
    OpenCodeTimeout,
)
from symphony_lite.orchestrator import Orchestrator, _format_comments_message
from symphony_lite.state import StateManager, TicketState, TicketStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, **overrides: Any) -> AppConfig:
    cfg_dict: dict[str, Any] = {
        "linear": {
            "api_key": "test-api-key",
            "trigger_label": "agent",
            "in_progress_state": "In Progress",
            "needs_input_state": "Needs Input",
            "bot_user_email": "bot@example.com",
        },
        "opencode": {"model": "test/model"},
        "sandbox": {"hide_paths": ["/fake/secret"], "extra_rw_paths": ["/fake/rw"]},
        "poll_interval_seconds": 1,
        "turn_timeout_seconds": 30,
    }
    for k, v in overrides.items():
        if isinstance(v, dict):
            cfg_dict[k].update(v)  # type: ignore[union-attr]
        else:
            cfg_dict[k] = v
    return AppConfig.model_validate(cfg_dict)


def _make_issue(**overrides: Any) -> Issue:
    defaults: dict[str, Any] = {
        "id": "ticket-1", "identifier": "TEAM-1", "title": "Test ticket",
        "state": "In Progress", "labels": ["agent"], "branchName": "feature/test",
        "project": Project(id="proj-1", name="Test Project"), "comments": [],
    }
    defaults.update(overrides)
    return Issue(**defaults)


def _make_comment(id_: str, body: str, user_id: str = "usr-human") -> Comment:
    return Comment(id=id_, body=body, createdAt="2025-06-01T00:00:00Z", user_id=user_id)


class FakeLinearClient:
    def __init__(self, api_key: str = "test-key") -> None:
        self.api_key = api_key
        self.calls: dict[str, list[tuple]] = {}
        self._responses: dict[str, Any] = {}
        self._bot_user_id = "usr-bot"

    def _record(self, method: str, args: tuple) -> None:
        self.calls.setdefault(method, []).append(args)

    def current_user_id(self) -> str:
        self._record("current_user_id", ())
        result = self._responses.get("current_user_id", self._bot_user_id)
        if isinstance(result, Exception):
            raise result
        return result

    def list_triggered_issues(self, label: str, active_states: list[str]) -> list[Issue]:
        self._record("list_triggered_issues", (label, active_states))
        return self._responses.get("list_triggered_issues", [])

    def get_issue(self, issue_id: str) -> Issue:
        self._record("get_issue", (issue_id,))
        result = self._responses.get("get_issue")
        if result is None:
            raise ValueError(f"No get_issue response for {issue_id}")
        if isinstance(result, Exception):
            raise result
        return result

    def get_project(self, project_id: str) -> Project:
        self._record("get_project", (project_id,))
        result = self._responses.get("get_project")
        if result is None:
            raise ValueError(f"No get_project response for {project_id}")
        if isinstance(result, Exception):
            raise result
        return result

    def list_comments_since(self, issue_id: str, comment_id: str | None) -> list[Comment]:
        self._record("list_comments_since", (issue_id, comment_id))
        return self._responses.get("list_comments_since", [])

    def post_comment(self, issue_id: str, body: str) -> Comment:
        self._record("post_comment", (issue_id, body))
        resp = self._responses.get("post_comment")
        if isinstance(resp, Exception):
            raise resp
        count = len([c for c in self.calls.get("post_comment", []) if c[0] == issue_id])
        cid = f"cmt-{issue_id}-{count + 1}"
        return Comment(id=cid, body=body, createdAt="2025-06-01T00:00:00Z", user_id=self._bot_user_id)

    def edit_comment(self, comment_id: str, body: str) -> None:
        self._record("edit_comment", (comment_id, body))
        resp = self._responses.get("edit_comment")
        if isinstance(resp, Exception):
            raise resp

    def transition_to_state(self, issue_id: str, state_name: str) -> None:
        self._record("transition_to_state", (issue_id, state_name))
        resp = self._responses.get("transition_to_state")
        if isinstance(resp, Exception):
            raise resp

    def set_response(self, method: str, value: Any) -> None:
        self._responses[method] = value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_config(tmp_path: Path) -> AppConfig:
    return _make_config(tmp_path)


@pytest.fixture
def state_mgr(tmp_path: Path) -> StateManager:
    mgr = StateManager(tmp_path / "state.json")
    mgr.load()
    return mgr


@pytest.fixture
def linear() -> FakeLinearClient:
    return FakeLinearClient()


@pytest.fixture
def orchestrator(tmp_config: AppConfig, state_mgr: StateManager, linear: FakeLinearClient, tmp_path: Path) -> Orchestrator:
    return Orchestrator(config=tmp_config, state=state_mgr, linear=linear, workspace=tmp_path / "workspaces")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _format_comments_message
# ---------------------------------------------------------------------------


class TestFormatCommentsMessage:
    def test_single_comment(self) -> None:
        msg = _format_comments_message([_make_comment("1", "Hello")])
        assert msg == "[usr-human at 2025-06-01T00:00:00Z]\nHello"

    def test_multiple_comments(self) -> None:
        msg = _format_comments_message([_make_comment("1", "A"), _make_comment("2", "B")])
        expected = "[usr-human at 2025-06-01T00:00:00Z]\nA\n\n[usr-human at 2025-06-01T00:00:00Z]\nB"
        assert msg == expected


# ---------------------------------------------------------------------------
# New ticket pipeline
# ---------------------------------------------------------------------------


class TestNewTicketPipeline:
    def _setup_mocks(self, mock_prepare: Any, mock_run_initial: Any, linear: FakeLinearClient) -> Issue:
        mock_prepare.return_value = "/tmp/workspaces/TEAM-1"
        mock_run_initial.return_value = ("ses-abc", "I have done the work.")
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix the bug"))
        return _make_issue()

    def test_full_happy_path(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        with (
            mock.patch("symphony_lite.orchestrator.prepare") as mock_prepare,
            mock.patch("symphony_lite.orchestrator.run_initial") as mock_run_initial,
        ):
            issue = self._setup_mocks(mock_prepare, mock_run_initial, linear)
            orchestrator._new_ticket_pipeline(issue)

        ts = orchestrator._state.get("ticket-1")
        assert ts is not None
        assert ts.status == TicketStatus.needs_input
        assert ts.session_id == "ses-abc"
        assert ts.last_seen_comment_id is not None
        assert ts.metadata_comment_id is not None

        # B3: verify hide_paths passed to run_initial
        mock_run_initial.assert_called_once()
        _, kwargs = mock_run_initial.call_args
        assert kwargs.get("hide_paths") == ["/fake/secret"]

        # B2: verify on_subprocess passed to prepare
        mock_prepare.assert_called_once()
        _, kw = mock_prepare.call_args
        assert kw.get("on_subprocess") is not None

    def test_missing_repo_saves_setup_error(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        linear.set_response("get_project", Project(id="proj-1", name="Test", links=[]))
        orchestrator._new_ticket_pipeline(_make_issue())
        ts = orchestrator._state.get("ticket-1")
        assert ts is not None
        assert ts.status == TicketStatus.failed
        assert ts.setup_error is not None
        assert ts.last_seen_comment_id is not None  # S3

    def test_no_project_saves_setup_error(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        orchestrator._new_ticket_pipeline(_make_issue(project=None))
        ts = orchestrator._state.get("ticket-1")
        assert ts is not None
        assert ts.status == TicketStatus.failed
        assert ts.setup_error is not None
        assert ts.last_seen_comment_id is not None  # S3

    def test_clone_failure_saves_setup_error(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        from symphony_lite.workspace import CloneFailed
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        with mock.patch("symphony_lite.orchestrator.prepare", side_effect=CloneFailed("fail")):
            orchestrator._new_ticket_pipeline(_make_issue())
        ts = orchestrator._state.get("ticket-1")
        assert ts is not None
        assert ts.status == TicketStatus.failed
        assert ts.setup_error is not None
        assert ts.last_seen_comment_id is not None

    def test_opencode_timeout_advances_last_seen(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        """B1: OpenCode failure sets last_seen_comment_id to error comment id."""
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))
        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"),
            mock.patch("symphony_lite.orchestrator.run_initial", side_effect=OpenCodeTimeout("timeout")),
        ):
            orchestrator._new_ticket_pipeline(_make_issue())
        ts = orchestrator._state.get("ticket-1")
        assert ts is not None
        assert ts.status == TicketStatus.failed
        assert ts.last_seen_comment_id is not None  # B1
        assert ts.session_id is None  # B1: nil'd out

    def test_opencode_error_advances_last_seen(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        """B1: OpenCode error sets last_seen_comment_id and clears session_id."""
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))
        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"),
            mock.patch("symphony_lite.orchestrator.run_initial", side_effect=OpenCodeError("fail")),
        ):
            orchestrator._new_ticket_pipeline(_make_issue())
        ts = orchestrator._state.get("ticket-1")
        assert ts is not None
        assert ts.status == TicketStatus.failed
        assert ts.last_seen_comment_id is not None
        assert ts.session_id is None

    def test_failed_transition_saves_failed(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))
        linear.set_response("transition_to_state", LinearError("nope"))
        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"),
            mock.patch("symphony_lite.orchestrator.run_initial", return_value=("ses-abc", "done")),
        ):
            orchestrator._new_ticket_pipeline(_make_issue())
        ts = orchestrator._state.get("ticket-1")
        assert ts is not None
        assert ts.status == TicketStatus.failed
        assert ts.session_id == "ses-abc"

    def test_cancelled_after_prepare_stops(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        """B2: cancellation checked after prepare returns."""
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))

        with mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"):
            with mock.patch("symphony_lite.orchestrator.run_initial") as mock_oc:
                # Cancel after prepare is mocked to return.
                orchestrator._cancel_ticket("ticket-1")
                orchestrator._new_ticket_pipeline(_make_issue())
        mock_oc.assert_not_called()  # B2: OpenCode never launched


# ---------------------------------------------------------------------------
# Resume pipeline
# ---------------------------------------------------------------------------


class TestResumePipeline:
    def _make_ts(self, **overrides: Any) -> TicketState:
        defaults: dict[str, Any] = {
            "ticket_id": "ticket-1", "ticket_identifier": "TEAM-1",
            "repo_url": "https://github.com/org/repo.git", "workspace_path": "/tmp/ws/TEAM-1",
            "branch": "feature/test", "status": TicketStatus.needs_input,
            "session_id": "ses-abc", "last_seen_comment_id": "cmt-seen-1",
            "metadata_comment_id": "cmt-meta-1",
        }
        defaults.update(overrides)
        return TicketState(**defaults)

    def test_happy_path(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        ts = self._make_ts(); orchestrator._state.upsert(ts)
        linear.set_response("list_comments_since", [_make_comment("c1", "Fix please")])
        with mock.patch("symphony_lite.orchestrator.run_resume", return_value="Done!"):
            orchestrator._resume_pipeline(ts)
        updated = orchestrator._state.get("ticket-1")
        assert updated is not None and updated.status == TicketStatus.needs_input

    def test_bot_comments_filtered(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        ts = self._make_ts(); orchestrator._state.upsert(ts)
        linear.set_response("list_comments_since", [
            _make_comment("c1", "Bot", "usr-bot"), _make_comment("c2", "Human", "usr-human"),
        ])
        with mock.patch("symphony_lite.orchestrator.run_resume", return_value="Done!") as m:
            orchestrator._resume_pipeline(ts)
        m.assert_called_once()
        msg = m.call_args.kwargs.get("message") or (
            m.call_args.args[2] if len(m.call_args.args) > 2 else m.call_args.kwargs["message"])
        assert "Bot" not in msg
        assert "Human" in msg

    def test_resume_timeout_advances_last_seen(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        ts = self._make_ts(); orchestrator._state.upsert(ts)
        linear.set_response("list_comments_since", [_make_comment("c1", "Go")])
        with mock.patch("symphony_lite.orchestrator.run_resume", side_effect=OpenCodeTimeout("t")):
            orchestrator._resume_pipeline(ts)
        updated = orchestrator._state.get("ticket-1")
        assert updated is not None
        assert updated.last_seen_comment_id != "cmt-seen-1"

    def test_no_retry_without_new_comment(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        ts = self._make_ts(); orchestrator._state.upsert(ts)
        linear.set_response("list_comments_since", [_make_comment("c1", "Go")])
        with mock.patch("symphony_lite.orchestrator.run_resume", side_effect=OpenCodeError("e")):
            orchestrator._resume_pipeline(ts)
        updated = orchestrator._state.get("ticket-1")
        assert updated.status == TicketStatus.failed
        new_last_seen = updated.last_seen_comment_id
        linear.set_response("list_comments_since", [])
        with mock.patch("symphony_lite.orchestrator.run_resume") as m:
            orchestrator._resume_pipeline(updated)
        m.assert_not_called()

    def test_bot_id_transient_failure_skips(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        """S2: if getting bot user id fails transiently, skip the ticket."""
        ts = self._make_ts(); orchestrator._state.upsert(ts)
        linear.set_response("current_user_id", LinearError("transient"))
        linear.set_response("list_comments_since", [_make_comment("c1", "Go")])
        with mock.patch("symphony_lite.orchestrator.run_resume") as m:
            orchestrator._resume_pipeline(ts)
        m.assert_not_called()  # skipped due to bot_id None


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


class TestTick:
    def _add_state(self, orchestrator: Orchestrator, **overrides: Any) -> TicketState:
        defaults: dict[str, Any] = {
            "ticket_id": "ticket-1", "ticket_identifier": "TEAM-1",
            "repo_url": "https://github.com/org/repo.git", "workspace_path": "/tmp/ws/TEAM-1",
            "branch": "feature/test", "status": TicketStatus.needs_input,
            "session_id": "ses-abc", "last_seen_comment_id": "cmt-seen-1",
            "metadata_comment_id": "cmt-meta-1",
        }
        defaults.update(overrides)
        ts = TicketState(**defaults)
        orchestrator._state.upsert(ts)
        return ts

    def test_setup_error_skipped(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        self._add_state(orchestrator, status=TicketStatus.failed, setup_error="clone_failed")
        linear.set_response("list_triggered_issues", [_make_issue()])
        linear.set_response("list_comments_since", [])
        with (
            mock.patch.object(orchestrator, "_new_ticket_pipeline") as m_new,
            mock.patch.object(orchestrator, "_resume_pipeline") as m_resume,
        ):
            orchestrator._tick(); time.sleep(0.2)
        m_new.assert_not_called(); m_resume.assert_not_called()

    def test_setup_error_retried_on_comment(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        self._add_state(orchestrator, status=TicketStatus.failed,
                        setup_error="clone_failed", last_seen_comment_id="cmt-old")
        linear.set_response("list_triggered_issues", [_make_issue()])
        linear.set_response("list_comments_since", [_make_comment("c1", "hello")])
        with mock.patch.object(orchestrator, "_new_ticket_pipeline") as m:
            orchestrator._tick(); time.sleep(0.2)
        m.assert_called_once()

    def test_failed_no_session_retried_on_comment(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        """B1: failed + no session_id + human comment → retry initial pipeline."""
        self._add_state(orchestrator, status=TicketStatus.failed,
                        session_id=None, last_seen_comment_id="cmt-old")
        linear.set_response("list_triggered_issues", [_make_issue()])
        linear.set_response("list_comments_since", [_make_comment("c1", "try again")])
        with mock.patch.object(orchestrator, "_new_ticket_pipeline") as m:
            orchestrator._tick(); time.sleep(0.2)
        m.assert_called_once()

    def test_failed_no_session_skipped_without_comment(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """B1: failed + no session + no new comment → skip, don't retry."""
        self._add_state(orchestrator, status=TicketStatus.failed,
                        session_id=None, last_seen_comment_id="cmt-err")
        linear.set_response("list_triggered_issues", [_make_issue()])
        linear.set_response("list_comments_since", [])
        with (
            mock.patch.object(orchestrator, "_new_ticket_pipeline") as m_new,
            mock.patch.object(orchestrator, "_resume_pipeline") as m_resume,
        ):
            orchestrator._tick(); time.sleep(0.2)
        m_new.assert_not_called()
        m_resume.assert_not_called()

    def test_failed_with_session_schedules_resume(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        self._add_state(orchestrator, status=TicketStatus.failed, session_id="ses-abc")
        linear.set_response("list_triggered_issues", [_make_issue()])
        with mock.patch.object(orchestrator, "_resume_pipeline") as m:
            orchestrator._tick(); time.sleep(0.2)
        m.assert_called_once()

    def test_deleted_ticket_removes_workspace(
        self, orchestrator: Orchestrator, linear: FakeLinearClient, tmp_path: Path,
    ) -> None:
        """Ticket gone from Linear (404) → state removed AND workspace removed."""
        ws_root = tmp_path / "workspaces"
        ws_dir = ws_root / "TEAM-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "sentinel").write_text("x")

        self._add_state(orchestrator, workspace_path=str(ws_dir))
        linear.set_response("list_triggered_issues", [])
        linear.set_response("get_issue", LinearNotFoundError("gone"))

        orchestrator._tick(); time.sleep(0.2)

        assert orchestrator._state.get("ticket-1") is None
        assert not ws_dir.exists()

    def test_label_removed_removes_workspace(
        self, orchestrator: Orchestrator, linear: FakeLinearClient, tmp_path: Path,
    ) -> None:
        """Trigger label removed → workspace AND state entry deleted."""
        ws_root = tmp_path / "workspaces"
        ws_dir = ws_root / "TEAM-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "sentinel").write_text("x")

        self._add_state(orchestrator, workspace_path=str(ws_dir))
        linear.set_response("list_triggered_issues", [])
        # Returns issue WITHOUT the trigger label
        linear.set_response("get_issue", _make_issue(labels=[]))

        orchestrator._tick(); time.sleep(0.2)

        assert orchestrator._state.get("ticket-1") is None
        assert not ws_dir.exists()

    def test_archived_ticket_cleaned_up(
        self, orchestrator: Orchestrator, linear: FakeLinearClient, tmp_path: Path,
    ) -> None:
        """Archived ticket → state removed AND workspace removed."""
        ws_root = tmp_path / "workspaces"
        ws_dir = ws_root / "TEAM-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "sentinel").write_text("x")

        self._add_state(orchestrator, workspace_path=str(ws_dir))
        linear.set_response("list_triggered_issues", [])
        linear.set_response("get_issue", _make_issue(
            archivedAt=datetime(2025, 1, 1, tzinfo=timezone.utc),
        ))

        orchestrator._tick(); time.sleep(0.2)

        assert orchestrator._state.get("ticket-1") is None
        assert not ws_dir.exists()

    def test_non_active_non_terminal_state_cleaned_up(
        self, orchestrator: Orchestrator, linear: FakeLinearClient, tmp_path: Path,
    ) -> None:
        """Ticket in non-active, non-terminal state (Backlog) with label still on → cleanup."""
        ws_root = tmp_path / "workspaces"
        ws_dir = ws_root / "TEAM-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "sentinel").write_text("x")

        self._add_state(orchestrator, workspace_path=str(ws_dir))
        linear.set_response("list_triggered_issues", [])
        linear.set_response("get_issue", _make_issue(
            state="Backlog",
            labels=["agent"],  # trigger label still present
        ))

        orchestrator._tick(); time.sleep(0.2)

        assert orchestrator._state.get("ticket-1") is None
        assert not ws_dir.exists()

    def test_terminal_state_cleaned_up(
        self, orchestrator: Orchestrator, linear: FakeLinearClient, tmp_path: Path,
    ) -> None:
        """Ticket in terminal state (Done) → state removed AND workspace removed."""
        ws_root = tmp_path / "workspaces"
        ws_dir = ws_root / "TEAM-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "sentinel").write_text("x")

        self._add_state(orchestrator, workspace_path=str(ws_dir))
        linear.set_response("list_triggered_issues", [])
        linear.set_response("get_issue", _make_issue(
            state="Done",
            labels=["agent"],
        ))

        orchestrator._tick(); time.sleep(0.2)

        assert orchestrator._state.get("ticket-1") is None
        assert not ws_dir.exists()


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


class TestStartupRecovery:
    def _add_working(self, orchestrator: Orchestrator, **overrides: Any) -> TicketState:
        defaults: dict[str, Any] = {
            "ticket_id": "ticket-1", "ticket_identifier": "TEAM-1",
            "repo_url": "https://github.com/org/repo.git", "workspace_path": "/tmp/ws/TEAM-1",
            "branch": "feature/test", "status": TicketStatus.working,
            "session_id": "ses-abc", "last_seen_comment_id": "cmt-seen-1",
            "metadata_comment_id": "cmt-meta-1",
        }
        defaults.update(overrides)
        ts = TicketState(**defaults)
        orchestrator._state.upsert(ts)
        return ts

    def test_bootstrapping_dropped(self, orchestrator: Orchestrator) -> None:
        ts = TicketState(ticket_id="ticket-1", ticket_identifier="TEAM-1",
                         repo_url="https://x", workspace_path="/tmp/x", branch="main",
                         status=TicketStatus.bootstrapping)
        orchestrator._state.upsert(ts)
        orchestrator._recover_state()
        assert orchestrator._state.get("ticket-1") is None

    def test_working_posted_and_transitioned(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        self._add_working(orchestrator)
        orchestrator._recover_state()
        assert len(linear.calls.get("post_comment", [])) >= 1
        assert ("ticket-1", "Needs Input") in linear.calls.get("transition_to_state", [])
        assert orchestrator._state.get("ticket-1").status == TicketStatus.needs_input

    def test_bootstrapping_with_metadata_edits_comment(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """Bootstrapping + metadata_comment_id → edit_comment called, state removed."""
        ts = TicketState(
            ticket_id="ticket-1", ticket_identifier="TEAM-1",
            repo_url="https://x", workspace_path="/tmp/x", branch="main",
            status=TicketStatus.bootstrapping,
            metadata_comment_id="cmt-meta-1",
        )
        orchestrator._state.upsert(ts)
        orchestrator._recover_state()
        assert orchestrator._state.get("ticket-1") is None
        edit_calls = linear.calls.get("edit_comment", [])
        assert len(edit_calls) == 1
        assert edit_calls[0] == (
            "cmt-meta-1",
            "**Symphony**: Restarted before setup completed. "
            "Picking this ticket up again on the next poll.",
        )

    def test_bootstrapping_without_metadata_no_linear_call(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """Bootstrapping without metadata_comment_id → no Linear call, state removed."""
        ts = TicketState(
            ticket_id="ticket-1", ticket_identifier="TEAM-1",
            repo_url="https://x", workspace_path="/tmp/x", branch="main",
            status=TicketStatus.bootstrapping,
            metadata_comment_id=None,
        )
        orchestrator._state.upsert(ts)
        orchestrator._recover_state()
        assert orchestrator._state.get("ticket-1") is None
        assert "edit_comment" not in linear.calls

    def test_bootstrapping_edit_comment_failure_handled(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """edit_comment failure during bootstrapping recovery is logged but does not crash."""
        ts = TicketState(
            ticket_id="ticket-1", ticket_identifier="TEAM-1",
            repo_url="https://x", workspace_path="/tmp/x", branch="main",
            status=TicketStatus.bootstrapping,
            metadata_comment_id="cmt-meta-1",
        )
        orchestrator._state.upsert(ts)
        linear.set_response("edit_comment", LinearError("edit failed"))
        orchestrator._recover_state()
        # Must not have crashed, and state must still be removed.
        assert orchestrator._state.get("ticket-1") is None


# ---------------------------------------------------------------------------
# Cancellation (B1 race protection + S1 registration race)
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_stops_worker(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))

        done = threading.Event()
        def slow_run_initial(*a: Any, **kw: Any) -> tuple[str, str]:
            done.set(); time.sleep(0.3); return ("ses-x", "out")

        with mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"):
            with mock.patch("symphony_lite.orchestrator.run_initial", side_effect=slow_run_initial):
                orchestrator._schedule_task("ticket-1", orchestrator._new_ticket_pipeline, _make_issue())
                done.wait()
                orchestrator._cancel_ticket("ticket-1")
        time.sleep(0.5)
        post_calls = linear.calls.get("post_comment", [])
        assert not any("out" in body for _, body in post_calls)

    def test_register_rejects_cancelled(self, orchestrator: Orchestrator) -> None:
        """S1: registering a process after cancellation kills it and returns False."""
        orchestrator._cancel_ticket("ticket-1")
        proc = subprocess.Popen(["sleep", "60"])
        ok = orchestrator._register_subprocess("ticket-1", proc)
        assert not ok
        assert proc.returncode is not None  # killed


# ---------------------------------------------------------------------------
# Hide paths (B3)
# ---------------------------------------------------------------------------


class TestHidePaths:
    def test_hide_paths_passed_to_run_initial(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))
        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"),
            mock.patch("symphony_lite.orchestrator.run_initial", return_value=("ses", "msg")) as m_oc,
        ):
            orchestrator._new_ticket_pipeline(_make_issue())
        _, kwargs = m_oc.call_args
        assert kwargs.get("hide_paths") == ["/fake/secret"]

    def test_hide_paths_passed_to_run_resume(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        ts = TicketState(ticket_id="ticket-1", ticket_identifier="TEAM-1",
                         repo_url="https://x", workspace_path="/tmp/x", branch="main",
                         status=TicketStatus.needs_input, session_id="ses-abc",
                         last_seen_comment_id="cmt-seen-1")
        orchestrator._state.upsert(ts)
        linear.set_response("list_comments_since", [_make_comment("c1", "Go")])
        with mock.patch("symphony_lite.orchestrator.run_resume", return_value="Done!") as m_oc:
            orchestrator._resume_pipeline(ts)
        _, kwargs = m_oc.call_args
        assert kwargs.get("hide_paths") == ["/fake/secret"]


# ---------------------------------------------------------------------------
# Extra RW paths (mirrors HidePaths but for extra_rw_paths)
# ---------------------------------------------------------------------------


class TestExtraRWPaths:
    def test_extra_rw_passed_to_prepare(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        """prepare() is called with sandbox_extra_rw_paths from config."""
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))
        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1") as m_prep,
            mock.patch("symphony_lite.orchestrator.run_initial", return_value=("ses", "msg")),
        ):
            orchestrator._new_ticket_pipeline(_make_issue())
        _, kwargs = m_prep.call_args
        assert kwargs.get("sandbox_extra_rw_paths") == ["/fake/rw"]

    def test_extra_rw_passed_to_run_initial(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))
        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"),
            mock.patch("symphony_lite.orchestrator.run_initial", return_value=("ses", "msg")) as m_oc,
        ):
            orchestrator._new_ticket_pipeline(_make_issue())
        _, kwargs = m_oc.call_args
        assert kwargs.get("extra_rw_paths") == ["/fake/rw"]

    def test_extra_rw_passed_to_run_resume(self, orchestrator: Orchestrator, linear: FakeLinearClient) -> None:
        ts = TicketState(ticket_id="ticket-1", ticket_identifier="TEAM-1",
                         repo_url="https://x", workspace_path="/tmp/x", branch="main",
                         status=TicketStatus.needs_input, session_id="ses-abc",
                         last_seen_comment_id="cmt-seen-1")
        orchestrator._state.upsert(ts)
        linear.set_response("list_comments_since", [_make_comment("c1", "Go")])
        with mock.patch("symphony_lite.orchestrator.run_resume", return_value="Done!") as m_oc:
            orchestrator._resume_pipeline(ts)
        _, kwargs = m_oc.call_args
        assert kwargs.get("extra_rw_paths") == ["/fake/rw"]


# ---------------------------------------------------------------------------
# Subprocess management
# ---------------------------------------------------------------------------


class TestSubprocessManagement:
    def test_register_and_cancel(self, orchestrator: Orchestrator) -> None:
        proc = subprocess.Popen(["sleep", "60"])
        ok = orchestrator._register_subprocess("ticket-1", proc)
        assert ok
        orchestrator._cancel_ticket("ticket-1")
        assert proc.returncode is not None


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_double_schedule_prevented(self, orchestrator: Orchestrator) -> None:
        def slow() -> None: time.sleep(0.5)
        orchestrator._schedule_task("ticket-1", slow)
        orchestrator._schedule_task("ticket-1", slow)
        with orchestrator._task_lock:
            count = sum(1 for tid in orchestrator._active_tasks if tid == "ticket-1")
        assert count == 1

    def test_concurrent_saves_dont_lose_updates(self, orchestrator: Orchestrator) -> None:
        def worker(i: int) -> None:
            ts = TicketState(ticket_id=f"ticket-{i}", ticket_identifier=f"TEAM-{i}",
                             repo_url="https://x", workspace_path=f"/tmp/{i}", branch="main",
                             status=TicketStatus.needs_input)
            with orchestrator._state_lock:
                orchestrator._state.upsert(ts)
                orchestrator._state.save()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        mgr2 = StateManager(orchestrator._state._path)  # type: ignore[attr-defined]
        mgr2.load()
        assert len(mgr2.tickets) == 10


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_does_not_hang(self, orchestrator: Orchestrator) -> None:
        orchestrator._schedule_task("t-t1", lambda: None)
        time.sleep(0.2)
        start = time.monotonic()
        orchestrator._shutdown_handler()
        assert time.monotonic() - start < 5


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIntegration:
    def test_new_ticket_end_to_end(self, tmp_path: Path, tmp_config: AppConfig, state_mgr: StateManager) -> None:
        import shutil
        if shutil.which("bwrap") is None: pytest.skip("bwrap not available")
        if shutil.which("git") is None: pytest.skip("git not available")

        linear = FakeLinearClient()
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))

        ws_root = tmp_path / "ws"; ws_root.mkdir()
        source_repo = tmp_path / "source"; source_repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(source_repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(source_repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(source_repo), capture_output=True)
        (source_repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(source_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(source_repo), capture_output=True)
        linear.set_response("get_project", Project(
            id="proj-1", name="Test", links=[ProjectLink(label="Repo", url=str(source_repo))]))

        config = _make_config(tmp_path, **{"linear": {"api_key": "test"}})
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=ws_root)  # type: ignore[arg-type]

        with mock.patch("symphony_lite.orchestrator.run_initial", return_value=("ses-int", "Done.")):
            orch._new_ticket_pipeline(_make_issue())

        ts = orch._state.get("ticket-1")
        assert ts is not None
        assert ts.status == TicketStatus.needs_input
        assert Path(ts.workspace_path).is_dir()
        from symphony_lite.workspace import remove
        remove("TEAM-1", str(ws_root))
