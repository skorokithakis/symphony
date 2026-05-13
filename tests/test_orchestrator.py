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
from symphony_lite.orchestrator import Orchestrator, _ActiveServe, _format_comments_message
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
        "updatedAt": "2025-06-01T00:00:00Z",
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

    def test_fetch_triggered_issues_includes_qa_state_when_set(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """_fetch_triggered_issues passes qa_state in active_states when configured."""
        config = _make_config(tmp_path, linear={"qa_state": "In Review"})
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        linear.set_response("list_triggered_issues", [])
        orch._fetch_triggered_issues()
        calls = linear.calls.get("list_triggered_issues", [])
        assert len(calls) == 1
        _, active_states = calls[0]
        assert "In Review" in active_states
        assert "In Progress" in active_states
        assert "Needs Input" in active_states

    def test_fetch_triggered_issues_excludes_qa_state_when_unset(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """_fetch_triggered_issues does not add a None qa_state to active_states."""
        config = _make_config(tmp_path)  # qa_state defaults to None
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        linear.set_response("list_triggered_issues", [])
        orch._fetch_triggered_issues()
        calls = linear.calls.get("list_triggered_issues", [])
        assert len(calls) == 1
        _, active_states = calls[0]
        assert active_states == ["In Progress", "Needs Input"]

    def test_is_still_triggered_true_for_qa_state(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """_is_still_triggered returns True when issue is in qa_state."""
        config = _make_config(tmp_path, linear={"qa_state": "In Review"})
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        issue = _make_issue(state="In Review", labels=["agent"])
        assert orch._is_still_triggered(issue) is True

    def test_is_still_triggered_false_for_qa_state_when_unset(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """_is_still_triggered returns False for 'In Review' when qa_state is not configured."""
        config = _make_config(tmp_path)  # qa_state is None
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        issue = _make_issue(state="In Review", labels=["agent"])
        assert orch._is_still_triggered(issue) is False

    def test_qa_state_ticket_not_cleaned_up(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Ticket in qa_state is tracked and not cleaned up when qa_state is configured."""
        config = _make_config(tmp_path, linear={"qa_state": "In Review"})
        ws_root = tmp_path / "workspaces"
        ws_dir = ws_root / "TEAM-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "sentinel").write_text("x")

        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=ws_root)  # type: ignore[arg-type]
        ts = TicketState(
            ticket_id="ticket-1", ticket_identifier="TEAM-1",
            repo_url="https://github.com/org/repo.git", workspace_path=str(ws_dir),
            branch="feature/test", status=TicketStatus.needs_input,
            session_id="ses-abc", last_seen_comment_id="cmt-seen-1",
        )
        orch._state.upsert(ts)

        # The issue is in "In Review" state — not in the triggered poll list,
        # but _is_still_triggered should keep it alive.
        linear.set_response("list_triggered_issues", [])
        linear.set_response("get_issue", _make_issue(state="In Review", labels=["agent"]))

        orch._tick(); time.sleep(0.2)

        assert orch._state.get("ticket-1") is not None
        assert ws_dir.exists()

    # --- Correction 4: broad QA gate in step 2 and step 4 ---

    def test_working_ticket_in_qa_state_not_scheduled(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """status=working + linear_state=qa_state → _recover_working_ticket NOT scheduled."""
        config = _make_config(tmp_path, linear={"qa_state": "In Review"})
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        ts = TicketState(
            ticket_id="ticket-1", ticket_identifier="TEAM-1",
            repo_url="https://x", workspace_path="/tmp/ws/TEAM-1",
            branch="main", status=TicketStatus.working,
            session_id="ses-abc",
        )
        orch._state.upsert(ts)

        qa_issue = _make_issue(id="ticket-1", state="In Review", labels=["agent"])
        linear.set_response("list_triggered_issues", [qa_issue])

        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=_make_fake_proc()):
            with mock.patch.object(orch, "_recover_working_ticket") as m_recover:
                orch._tick()
                time.sleep(0.2)

        m_recover.assert_not_called()

    def test_failed_with_session_in_qa_state_not_scheduled(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """status=failed+session_id + linear_state=qa_state → _resume_pipeline NOT scheduled."""
        config = _make_config(tmp_path, linear={"qa_state": "In Review"})
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        ts = TicketState(
            ticket_id="ticket-1", ticket_identifier="TEAM-1",
            repo_url="https://x", workspace_path="/tmp/ws/TEAM-1",
            branch="main", status=TicketStatus.failed,
            session_id="ses-abc", last_seen_comment_id="cmt-seen-1",
        )
        orch._state.upsert(ts)

        qa_issue = _make_issue(id="ticket-1", state="In Review", labels=["agent"])
        linear.set_response("list_triggered_issues", [qa_issue])

        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=_make_fake_proc()):
            with mock.patch.object(orch, "_resume_pipeline") as m_resume:
                orch._tick()
                time.sleep(0.2)

        m_resume.assert_not_called()

    def test_new_issue_in_qa_state_not_scheduled(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """New issue arriving in qa_state with trigger label → _new_ticket_pipeline NOT scheduled."""
        config = _make_config(tmp_path, linear={"qa_state": "In Review"})
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        # No existing state entry — this is a brand-new ticket landing directly in QA.
        qa_issue = _make_issue(id="ticket-1", state="In Review", labels=["agent"])
        linear.set_response("list_triggered_issues", [qa_issue])

        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=_make_fake_proc()):
            with mock.patch.object(orch, "_new_ticket_pipeline") as m_new:
                orch._tick()
                time.sleep(0.2)

        m_new.assert_not_called()


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
# QA serve reconciliation
# ---------------------------------------------------------------------------


def _make_qa_config(tmp_path: Path, qa_state: str = "In Review") -> AppConfig:
    return _make_config(tmp_path, linear={"qa_state": qa_state})


def _make_qa_issue(ticket_id: str = "ticket-1", identifier: str = "TEAM-1", state: str = "In Review") -> Issue:
    return _make_issue(id=ticket_id, identifier=identifier, state=state, labels=["agent"])


def _add_ticket_state(
    orchestrator: Orchestrator,
    ticket_id: str = "ticket-1",
    identifier: str = "TEAM-1",
    workspace_path: str = "/tmp/ws/TEAM-1",
    status: TicketStatus = TicketStatus.needs_input,
) -> TicketState:
    ts = TicketState(
        ticket_id=ticket_id,
        ticket_identifier=identifier,
        repo_url="https://github.com/org/repo.git",
        workspace_path=workspace_path,
        branch="feature/test",
        status=status,
        session_id="ses-abc",
        last_seen_comment_id="cmt-seen-1",
    )
    orchestrator._state.upsert(ts)
    return ts


def _make_fake_proc(returncode: int | None = None) -> mock.MagicMock:
    """Create a fake Popen-like object.

    By default (returncode=None) the process appears to be still running:
    wait() raises TimeoutExpired so the watchdog thread treats it as healthy
    and spawns a drainer, and poll() returns None.  Pass an integer returncode
    to simulate early exit: wait() returns immediately and poll() returns the code.
    """
    proc = mock.MagicMock(spec=subprocess.Popen)
    proc.returncode = returncode
    proc.stdout = None
    proc.stderr = None
    if returncode is None:
        # Simulate a long-running process: wait() always times out, poll() → None.
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="serve", timeout=10)
        proc.poll.return_value = None
    else:
        proc.wait.return_value = returncode
        proc.poll.return_value = returncode
    return proc


def _make_active_serve(
    ticket_id: str = "ticket-1",
    identifier: str = "TEAM-1",
    proc: mock.MagicMock | None = None,
) -> _ActiveServe:
    """Create an _ActiveServe for tests."""
    if proc is None:
        proc = _make_fake_proc(returncode=None)
    return _ActiveServe(
        ticket_id=ticket_id,
        ticket_identifier=identifier,
        proc=proc,
        start_monotonic=time.monotonic(),
    )


class TestReconcileServe:
    def test_noop_when_qa_state_unset(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """When qa_state is not configured, _reconcile_serve is a no-op."""
        assert orchestrator._config.linear.qa_state is None
        issue = _make_issue(state="In Review")
        with mock.patch("symphony_lite.orchestrator.start_serve") as m:
            orchestrator._reconcile_serve([issue], {issue.id: issue})
        m.assert_not_called()
        assert orchestrator._active_serve is None

    def test_single_ticket_enters_qa_starts_serve(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Single ticket in QA state → serve started, _active_serve populated."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch)

        issue = _make_qa_issue()
        fake_proc = _make_fake_proc(returncode=None)

        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=fake_proc) as m_serve:
            orch._reconcile_serve([issue], {issue.id: issue})

        m_serve.assert_called_once()
        assert orch._active_serve is not None
        assert orch._active_serve.ticket_id == "ticket-1"
        assert orch._active_serve.proc is fake_proc

    def test_owner_leaves_qa_kills_serve(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """When the active serve owner leaves QA, the serve is killed."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = _make_fake_proc(returncode=None)
        orch._active_serve = _make_active_serve(proc=fake_proc)

        # No QA tickets this tick — owner left QA.
        orch._reconcile_serve([], {})

        fake_proc.kill.assert_called_once()
        assert orch._active_serve is None

    def test_dead_proc_posts_comment_and_no_reserve_this_tick(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Fix 5: dead proc → comment posted, ticket transitioned to needs_input,
        pruned from qa_tickets so no re-serve happens this tick."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch)

        # Simulate a dead proc (returncode is set, poll() returns non-None).
        dead_proc = _make_fake_proc(returncode=1)
        dead_proc.poll.return_value = 1
        av = _make_active_serve(proc=dead_proc)
        orch._active_serve = av

        issue = _make_qa_issue()
        with mock.patch("symphony_lite.orchestrator.start_serve") as m_serve:
            orch._reconcile_serve([issue], {issue.id: issue})

        # Comment posted and ticket transitioned.
        post_calls = linear.calls.get("post_comment", [])
        assert any("QA serve exited" in body for _, body in post_calls)
        transition_calls = linear.calls.get("transition_to_state", [])
        assert any("ticket-1" == tid for tid, _ in transition_calls)
        # No re-serve this tick (ticket pruned from qa_tickets).
        m_serve.assert_not_called()
        assert orch._active_serve is None

    def test_dead_proc_deduplication_with_watchdog(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Fix 5 dedup: watchdog posts comment + transitions; dead-proc path in reconcile
        skips re-posting but still transitions; _active_serve cleared at end.

        Exercises the real handoff: run _serve_watchdog first, then _reconcile_serve.
        """
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch)

        # Proc that exited non-zero within 10s.
        dead_proc = _make_fake_proc(returncode=2)
        dead_proc.poll.return_value = 2
        av = _make_active_serve(proc=dead_proc)
        orch._active_serve = av

        # Step 1: watchdog runs — posts comment, transitions, sets failure_comment_posted,
        # clears _active_serve.
        orch._serve_watchdog(av)

        assert av.failure_comment_posted
        assert orch._active_serve is None
        post_calls_after_watchdog = list(linear.calls.get("post_comment", []))
        transition_calls_after_watchdog = list(linear.calls.get("transition_to_state", []))
        assert any("QA serve exited" in body for _, body in post_calls_after_watchdog)
        assert any("ticket-1" == tid for tid, _ in transition_calls_after_watchdog)

        # Step 2: simulate the ticket still appearing in QA next tick (e.g. Linear
        # transition failed in the watchdog and ticket is still in qa_state).
        # Restore _active_serve pointing to the same dead av so reconcile detects it.
        orch._active_serve = av

        issue = _make_qa_issue()
        with mock.patch("symphony_lite.orchestrator.start_serve"):
            orch._reconcile_serve([issue], {issue.id: issue})

        # (a) No duplicate "QA serve exited" comment — failure_comment_posted guards it.
        post_calls_after_reconcile = linear.calls.get("post_comment", [])
        serve_exited_comments = [
            body for _, body in post_calls_after_reconcile if "QA serve exited" in body
        ]
        assert len(serve_exited_comments) == 1, (
            f"Expected exactly 1 'QA serve exited' comment, got {len(serve_exited_comments)}"
        )
        # (b) Ticket transitioned to needs_input (at least once total).
        transition_calls_total = linear.calls.get("transition_to_state", [])
        assert any("ticket-1" == tid for tid, _ in transition_calls_total)
        # (c) _active_serve cleared.
        assert orch._active_serve is None

    def test_second_ticket_enters_qa_bumps_incumbent(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Newer ticket (by updated_at) entering QA bumps the incumbent and takes over serving."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch, ticket_id="ticket-1", identifier="TEAM-1")
        _add_ticket_state(orch, ticket_id="ticket-2", identifier="TEAM-2")

        old_proc = _make_fake_proc(returncode=None)
        orch._active_serve = _make_active_serve(proc=old_proc)

        # ticket-1 has an older updated_at; ticket-2 is newer → ticket-2 wins.
        issue1 = _make_qa_issue(ticket_id="ticket-1", identifier="TEAM-1")
        issue1 = issue1.model_copy(update={"updated_at": issue1.updated_at})  # keep old timestamp
        issue2 = _make_issue(
            id="ticket-2", identifier="TEAM-2", state="In Review", labels=["agent"],
            updatedAt="2025-07-01T00:00:00Z",  # newer than issue1's default 2025-06-01
        )
        issues_by_id = {"ticket-1": issue1, "ticket-2": issue2}

        new_proc = _make_fake_proc(returncode=None)
        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=new_proc) as m_serve:
            orch._reconcile_serve([issue1, issue2], issues_by_id)

        # Old serve was killed, new serve started for ticket-2.
        old_proc.kill.assert_called_once()
        m_serve.assert_called_once()
        assert orch._active_serve is not None
        assert orch._active_serve.ticket_id == "ticket-2"
        # ticket-1 (the loser) was bumped: got a comment and transition.
        post_calls = linear.calls.get("post_comment", [])
        assert any("ticket-1" == tid for tid, _ in post_calls)
        transition_calls = linear.calls.get("transition_to_state", [])
        assert any("ticket-1" == tid for tid, _ in transition_calls)

    def test_new_winner_when_no_active_serve(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Two QA tickets, no active serve → newest updated_at wins, loser is bumped."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch, ticket_id="ticket-1", identifier="TEAM-1")
        _add_ticket_state(orch, ticket_id="ticket-2", identifier="TEAM-2")

        # ticket-1 is older, ticket-2 is newer → ticket-2 wins.
        issue1 = _make_qa_issue(ticket_id="ticket-1", identifier="TEAM-1")
        issue2 = _make_issue(
            id="ticket-2", identifier="TEAM-2", state="In Review", labels=["agent"],
            updatedAt="2025-07-01T00:00:00Z",  # newer than issue1's default 2025-06-01
        )
        issues_by_id = {"ticket-1": issue1, "ticket-2": issue2}

        fake_proc = _make_fake_proc(returncode=None)
        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=fake_proc) as m_serve:
            orch._reconcile_serve([issue1, issue2], issues_by_id)

        # start_serve called once for the winner.
        m_serve.assert_called_once()
        # Winner is ticket-2 (newer updated_at).
        assert orch._active_serve is not None
        assert orch._active_serve.ticket_id == "ticket-2"
        # ticket-1 was bumped.
        post_calls = linear.calls.get("post_comment", [])
        assert any("ticket-1" == tid for tid, _ in post_calls)

    def test_qa_ticket_skips_resume_pipeline(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Comments on a QA-state ticket do not trigger _resume_pipeline."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch, status=TicketStatus.needs_input)

        # The issue is in QA state in the fetched list.
        qa_issue = _make_qa_issue(state="In Review")
        linear.set_response("list_triggered_issues", [qa_issue])
        linear.set_response("list_comments_since", [_make_comment("c1", "LGTM")])

        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=_make_fake_proc()):
            with mock.patch.object(orch, "_resume_pipeline") as m_resume:
                orch._tick()
                time.sleep(0.2)

        m_resume.assert_not_called()

    def test_start_serve_raises_serve_script_missing(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """ServeScriptMissing → comment posted on ticket, _active_serve unchanged."""
        from symphony_lite.workspace import ServeScriptMissing
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch)

        issue = _make_qa_issue()
        with mock.patch(
            "symphony_lite.orchestrator.start_serve",
            side_effect=ServeScriptMissing("no serve script"),
        ):
            orch._reconcile_serve([issue], {issue.id: issue})

        assert orch._active_serve is None
        post_calls = linear.calls.get("post_comment", [])
        assert any("ticket-1" == tid for tid, _ in post_calls)
        assert any("QA serve failed to start" in body for _, body in post_calls)

    def test_start_serve_raises_file_not_found(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """FileNotFoundError (bwrap missing) → comment posted, _active_serve unchanged."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch)

        issue = _make_qa_issue()
        with mock.patch(
            "symphony_lite.orchestrator.start_serve",
            side_effect=FileNotFoundError("bwrap not found"),
        ):
            orch._reconcile_serve([issue], {issue.id: issue})

        assert orch._active_serve is None
        post_calls = linear.calls.get("post_comment", [])
        assert any("QA serve failed to start" in body for _, body in post_calls)

    def test_winner_has_no_state_entry_transitions_then_posts_comment(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """QA winner with no state entry → transitioned to needs_input, then comment posted."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        # Deliberately do NOT call _add_ticket_state — state entry is missing.

        issue = _make_qa_issue()
        with mock.patch("symphony_lite.orchestrator.start_serve") as m_serve:
            orch._reconcile_serve([issue], {issue.id: issue})

        m_serve.assert_not_called()
        assert orch._active_serve is None
        transition_calls = linear.calls.get("transition_to_state", [])
        assert any(
            tid == "ticket-1" and state == "Needs Input"
            for tid, state in transition_calls
        ), f"Expected transition for ticket-1 to Needs Input, got {transition_calls}"
        post_calls = linear.calls.get("post_comment", [])
        assert any(
            "Can't start QA" in body and "no workspace exists" in body
            for _, body in post_calls
        ), f"Expected comment with 'Can't start QA' and 'no workspace exists', got {post_calls}"
        assert any(
            "ticket-1" == tid for tid, _ in post_calls
        ), f"Expected post_comment for ticket-1, got {post_calls}"

    def test_winner_has_empty_workspace_path_transitions_then_posts_comment(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """QA winner with empty workspace_path → transitioned to needs_input, then comment posted."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch, workspace_path="")  # state exists but workspace_path is empty

        issue = _make_qa_issue()
        with mock.patch("symphony_lite.orchestrator.start_serve") as m_serve:
            orch._reconcile_serve([issue], {issue.id: issue})

        m_serve.assert_not_called()
        assert orch._active_serve is None
        transition_calls = linear.calls.get("transition_to_state", [])
        assert any(
            tid == "ticket-1" and state == "Needs Input"
            for tid, state in transition_calls
        ), f"Expected transition for ticket-1 to Needs Input, got {transition_calls}"
        post_calls = linear.calls.get("post_comment", [])
        assert any(
            "Can't start QA" in body and "no workspace exists" in body
            for _, body in post_calls
        ), f"Expected comment with 'Can't start QA' and 'no workspace exists', got {post_calls}"
        assert any(
            "ticket-1" == tid for tid, _ in post_calls
        ), f"Expected post_comment for ticket-1, got {post_calls}"

    def test_winner_no_workspace_transition_fails_no_comment(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """When transition raises LinearError, no comment is posted — avoids spam on flaky transitions."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        # No state entry → hits the _bail_qa_no_workspace path.
        linear.set_response("transition_to_state", LinearError("test transition error"))

        issue = _make_qa_issue()
        with mock.patch("symphony_lite.orchestrator.start_serve") as m_serve:
            orch._reconcile_serve([issue], {issue.id: issue})

        m_serve.assert_not_called()
        assert orch._active_serve is None
        # Transition was attempted (recorded before the raise).
        transition_calls = linear.calls.get("transition_to_state", [])
        assert any(
            tid == "ticket-1" and state == "Needs Input"
            for tid, state in transition_calls
        ), f"Expected transition attempt for ticket-1, got {transition_calls}"
        # No comment posted because transition failed.
        post_calls = linear.calls.get("post_comment", [])
        assert not any(
            "ticket-1" == tid for tid, _ in post_calls
        ), f"Expected no post_comment for ticket-1, got {post_calls}"

    def test_watchdog_nonzero_exit_posts_comment_and_transitions(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Watchdog: proc exits non-zero within 10s (not intentional) → failure comment posted
        AND ticket transitioned to needs_input to prevent respawn loop."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.returncode = 1
        fake_proc.stderr = None
        fake_proc.stdout = None
        fake_proc.wait.return_value = 1

        av = _make_active_serve(proc=fake_proc)
        orch._active_serve = av

        orch._serve_watchdog(av)

        assert orch._active_serve is None
        post_calls = linear.calls.get("post_comment", [])
        assert any("rc=1" in body for _, body in post_calls)
        # Transition must also be called to prevent respawn loop.
        transition_calls = linear.calls.get("transition_to_state", [])
        assert any(
            tid == "ticket-1" and state == "Needs Input"
            for tid, state in transition_calls
        )

    def test_watchdog_intentional_kill_suppresses_comment(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Fix 1: watchdog suppresses failure comment when intentional_kill is set."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.returncode = -9
        fake_proc.stderr = None
        fake_proc.stdout = None
        fake_proc.wait.return_value = -9

        av = _make_active_serve(proc=fake_proc)
        av.intentional_kill.set()  # simulate intentional kill
        orch._active_serve = av

        orch._serve_watchdog(av)

        assert orch._active_serve is None
        assert "post_comment" not in linear.calls

    def test_watchdog_zero_exit_clears_silently(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Watchdog: proc exits with rc=0 within 10s → _active_serve cleared, no comment."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.returncode = 0
        fake_proc.stderr = None
        fake_proc.stdout = None
        fake_proc.wait.return_value = 0

        av = _make_active_serve(proc=fake_proc)
        orch._active_serve = av

        orch._serve_watchdog(av)

        assert orch._active_serve is None
        assert "post_comment" not in linear.calls

    def test_watchdog_timeout_no_comment(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Watchdog: proc still alive after 10s → no comment, _active_serve not cleared."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.returncode = None
        fake_proc.stdout = None
        fake_proc.stderr = None
        fake_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="serve", timeout=10)

        av = _make_active_serve(proc=fake_proc)
        orch._active_serve = av

        orch._serve_watchdog(av)

        # No failure comment posted.
        assert "post_comment" not in linear.calls
        # _active_serve not cleared by watchdog.
        assert orch._active_serve is not None

    def test_cancel_ticket_kills_serve(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """_cancel_ticket on the serve owner kills the serve process."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.returncode = None
        orch._active_serve = _make_active_serve(proc=fake_proc)

        orch._cancel_ticket("ticket-1")

        fake_proc.kill.assert_called_once()
        assert orch._active_serve is None

    def test_cancel_ticket_does_not_kill_other_serve(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """_cancel_ticket on a non-owner ticket does not kill the serve."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.returncode = None
        orch._active_serve = _make_active_serve(proc=fake_proc)

        orch._cancel_ticket("ticket-2")

        fake_proc.kill.assert_not_called()
        assert orch._active_serve is not None

    def test_shutdown_kills_serve(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Daemon shutdown kills the active serve process."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.returncode = None
        orch._active_serve = _make_active_serve(proc=fake_proc)

        orch._shutdown_handler()

        fake_proc.kill.assert_called_once()
        assert orch._active_serve is None

    def test_bump_comment_linear_error_does_not_abort(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """LinearError when posting bump comment is caught and logged; reconciliation continues."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch, ticket_id="ticket-1", identifier="TEAM-1")
        _add_ticket_state(orch, ticket_id="ticket-2", identifier="TEAM-2")

        issue1 = _make_qa_issue(ticket_id="ticket-1", identifier="TEAM-1")
        issue2 = _make_qa_issue(ticket_id="ticket-2", identifier="TEAM-2")
        issues_by_id = {"ticket-1": issue1, "ticket-2": issue2}

        # Make post_comment raise LinearError.
        linear.set_response("post_comment", LinearError("network error"))

        fake_proc = _make_fake_proc(returncode=None)
        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=fake_proc):
            # Should not raise.
            orch._reconcile_serve([issue1, issue2], issues_by_id)

        # Winner was still started despite the bump comment failure.
        assert orch._active_serve is not None

    def test_watchdog_output_included_in_comment(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Watchdog failure comment includes captured stdout/stderr in fenced code blocks."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.returncode = 2
        fake_proc.stderr = None
        fake_proc.stdout = None
        fake_proc.wait.return_value = 2

        av = _make_active_serve(proc=fake_proc)
        # Pre-populate the drainer buffers (simulating what the drainer threads would capture).
        av.stderr_head.extend(b"error: something went wrong\nfatal: crash\n")
        av.stdout_head.extend(b"starting up...\n")
        orch._active_serve = av

        orch._serve_watchdog(av)

        post_calls = linear.calls.get("post_comment", [])
        assert post_calls, "Expected a post_comment call"
        body = post_calls[0][1]
        assert "```" in body
        assert "error: something went wrong" in body or "fatal: crash" in body


# ---------------------------------------------------------------------------
# Fix 2: cancelled agent must not post final comment or edit metadata
# ---------------------------------------------------------------------------


class TestFix2CancelledAgentGuards:
    def test_new_pipeline_skips_edit_comment_when_cancelled(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """Fix 2: if cancelled before edit_comment, edit_comment is NOT called."""
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))

        # Cancel the ticket inside run_initial (before edit_comment).
        def cancel_then_return(*a: Any, **kw: Any) -> tuple[str, str]:
            orchestrator._cancel_ticket("ticket-1")
            return ("ses-abc", "Done!")

        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"),
            mock.patch("symphony_lite.orchestrator.run_initial", side_effect=cancel_then_return),
        ):
            orchestrator._new_ticket_pipeline(_make_issue())

        # edit_comment must NOT have been called (cancelled before that point).
        assert "edit_comment" not in linear.calls

    def test_new_pipeline_skips_final_message_when_cancelled(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """Fix 2: if cancelled before _post_final_message, no final comment is posted."""
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))

        def cancel_then_return(*a: Any, **kw: Any) -> tuple[str, str]:
            orchestrator._cancel_ticket("ticket-1")
            return ("ses-abc", "Done!")

        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"),
            mock.patch("symphony_lite.orchestrator.run_initial", side_effect=cancel_then_return),
        ):
            orchestrator._new_ticket_pipeline(_make_issue())

        # No final message comment (only the metadata comment and error comments are allowed).
        post_calls = linear.calls.get("post_comment", [])
        # The final "Done!" message must not appear.
        assert not any("Done!" in body for _, body in post_calls)

    def test_resume_pipeline_skips_final_message_when_cancelled(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """Fix 2: _resume_pipeline skips _post_final_message when cancelled."""
        ts = TicketState(
            ticket_id="ticket-1", ticket_identifier="TEAM-1",
            repo_url="https://x", workspace_path="/tmp/x", branch="main",
            status=TicketStatus.needs_input, session_id="ses-abc",
            last_seen_comment_id="cmt-seen-1",
        )
        orchestrator._state.upsert(ts)
        linear.set_response("list_comments_since", [_make_comment("c1", "Go")])

        def cancel_then_return(*a: Any, **kw: Any) -> str:
            orchestrator._cancel_ticket("ticket-1")
            return "Done!"

        with mock.patch("symphony_lite.orchestrator.run_resume", side_effect=cancel_then_return):
            orchestrator._resume_pipeline(ts)

        post_calls = linear.calls.get("post_comment", [])
        assert not any("Done!" in body for _, body in post_calls)


# ---------------------------------------------------------------------------
# Fix 3: transition-first in loser bump loop
# ---------------------------------------------------------------------------


class TestFix3TransitionFirst:
    def test_transition_fails_no_bump_comment(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Fix 3: if transition_to_state fails for a loser, no bump comment is posted."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch, ticket_id="ticket-1", identifier="TEAM-1")
        _add_ticket_state(orch, ticket_id="ticket-2", identifier="TEAM-2")

        issue1 = _make_qa_issue(ticket_id="ticket-1", identifier="TEAM-1")
        issue2 = _make_issue(
            id="ticket-2", identifier="TEAM-2", state="In Review", labels=["agent"],
            updatedAt="2025-07-01T00:00:00Z",
        )
        issues_by_id = {"ticket-1": issue1, "ticket-2": issue2}

        # Make transition_to_state fail for the loser (ticket-1).
        linear.set_response("transition_to_state", LinearError("transition failed"))

        fake_proc = _make_fake_proc(returncode=None)
        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=fake_proc):
            orch._reconcile_serve([issue1, issue2], issues_by_id)

        # No bump comment for ticket-1 (transition failed → skip comment).
        post_calls = linear.calls.get("post_comment", [])
        assert not any(
            "ticket-1" == tid and "Bumped out of QA" in body
            for tid, body in post_calls
        )

    def test_transition_succeeds_bump_comment_posted(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Fix 3: if transition_to_state succeeds for a loser, bump comment IS posted."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch, ticket_id="ticket-1", identifier="TEAM-1")
        _add_ticket_state(orch, ticket_id="ticket-2", identifier="TEAM-2")

        issue1 = _make_qa_issue(ticket_id="ticket-1", identifier="TEAM-1")
        issue2 = _make_issue(
            id="ticket-2", identifier="TEAM-2", state="In Review", labels=["agent"],
            updatedAt="2025-07-01T00:00:00Z",
        )
        issues_by_id = {"ticket-1": issue1, "ticket-2": issue2}

        fake_proc = _make_fake_proc(returncode=None)
        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=fake_proc):
            orch._reconcile_serve([issue1, issue2], issues_by_id)

        # Bump comment posted for ticket-1 (transition succeeded).
        post_calls = linear.calls.get("post_comment", [])
        assert any(
            "ticket-1" == tid and "Bumped out of QA" in body
            for tid, body in post_calls
        )


# ---------------------------------------------------------------------------
# Fix 4: drainer captures output and caps at 1000 bytes
# ---------------------------------------------------------------------------


class TestFix4Drainer:
    def test_drainer_captures_up_to_cap(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Fix 4: drainer fills buffer up to _DRAINER_CAP bytes and stops appending."""
        import io
        from symphony_lite.orchestrator import _DRAINER_CAP

        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        # 2000 bytes of data — more than the cap.
        data = b"x" * 2000
        pipe = io.BytesIO(data)
        buf: bytearray = bytearray()

        orch._pipe_drainer(pipe, buf)

        assert len(buf) == _DRAINER_CAP
        assert buf == bytearray(b"x" * _DRAINER_CAP)

    def test_drainer_reads_all_data_past_cap(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Fix 4: drainer reads all data even past the cap (no blocking)."""
        import io

        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]

        # 3000 bytes — drainer must consume all of it without blocking.
        data = b"y" * 3000
        pipe = io.BytesIO(data)
        buf: bytearray = bytearray()

        orch._pipe_drainer(pipe, buf)

        # Pipe fully consumed (BytesIO position at end).
        assert pipe.tell() == 3000
        # Buffer capped.
        assert len(buf) == 1000

    def test_drainers_started_immediately_after_start_serve(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """Fix 4: drainer threads are started immediately when serve starts (not after 10s)."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch)

        issue = _make_qa_issue()
        fake_proc = _make_fake_proc(returncode=None)

        drainer_started = threading.Event()
        original_start_drainers = orch._start_drainers

        def patched_start_drainers(av: _ActiveServe) -> None:
            drainer_started.set()
            original_start_drainers(av)

        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=fake_proc):
            with mock.patch.object(orch, "_start_drainers", side_effect=patched_start_drainers):
                orch._reconcile_serve([issue], {issue.id: issue})

        assert drainer_started.is_set(), "_start_drainers was not called"


# ---------------------------------------------------------------------------
# Correction 3: cancel in-flight task + final-transition guard
# ---------------------------------------------------------------------------


class TestCorrection3:
    def test_inflight_task_cancelled_before_serve_starts(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """_reconcile_serve cancels an in-flight agent task before starting the serve."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch)

        # Simulate an in-flight task: insert a non-done Future into _active_tasks
        # and a running subprocess into _subprocesses.
        agent_proc = subprocess.Popen(["sleep", "60"])
        with orch._subprocess_lock:
            orch._subprocesses["ticket-1"] = agent_proc
        not_done_future: mock.MagicMock = mock.MagicMock()
        not_done_future.done.return_value = False
        with orch._task_lock:
            orch._active_tasks["ticket-1"] = not_done_future  # type: ignore[assignment]

        issue = _make_qa_issue()
        new_proc = _make_fake_proc(returncode=None)

        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=new_proc) as m_serve:
            orch._reconcile_serve([issue], {issue.id: issue})

        # The in-flight agent subprocess was killed.
        assert agent_proc.returncode is not None, "agent proc should have been killed"
        # The cancellation flag was set.
        assert orch._is_cancelled("ticket-1")
        # The serve was still started.
        m_serve.assert_called_once()
        assert orch._active_serve is not None
        assert orch._active_serve.ticket_id == "ticket-1"

    def test_no_inflight_task_serve_starts_normally(
        self, tmp_path: Path, state_mgr: StateManager, linear: FakeLinearClient,
    ) -> None:
        """When there is no in-flight task, _reconcile_serve starts the serve without cancelling."""
        config = _make_qa_config(tmp_path)
        orch = Orchestrator(config=config, state=state_mgr, linear=linear, workspace=tmp_path / "ws")  # type: ignore[arg-type]
        _add_ticket_state(orch)

        issue = _make_qa_issue()
        new_proc = _make_fake_proc(returncode=None)

        with mock.patch("symphony_lite.orchestrator.start_serve", return_value=new_proc) as m_serve:
            orch._reconcile_serve([issue], {issue.id: issue})

        m_serve.assert_called_once()
        assert not orch._is_cancelled("ticket-1")

    def test_new_ticket_pipeline_skips_transition_when_cancelled(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """_new_ticket_pipeline does not call transition_to_state if cancelled after final message."""
        linear.set_response("get_project", Project(
            id="proj-1", name="Test",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo.git")]))
        linear.set_response("get_issue", _make_issue(description="Fix"))

        # Cancel the ticket after run_initial returns but before transition.
        def cancel_then_return(*a: Any, **kw: Any) -> tuple[str, str]:
            orchestrator._cancel_ticket("ticket-1")
            return ("ses-abc", "Done!")

        with (
            mock.patch("symphony_lite.orchestrator.prepare", return_value="/tmp/ws/TEAM-1"),
            mock.patch("symphony_lite.orchestrator.run_initial", side_effect=cancel_then_return),
        ):
            orchestrator._new_ticket_pipeline(_make_issue())

        # transition_to_state should NOT have been called with needs_input_state
        # after the final message (the cancel happened mid-run).
        transition_calls = linear.calls.get("transition_to_state", [])
        # The only allowed transition is the early "In Progress" one; needs_input must not appear.
        needs_input_transitions = [
            (tid, state) for tid, state in transition_calls
            if state == orchestrator._config.linear.needs_input_state
        ]
        assert needs_input_transitions == [], (
            f"Expected no needs_input transition after cancellation, got: {needs_input_transitions}"
        )

    def test_resume_pipeline_skips_transition_when_cancelled(
        self, orchestrator: Orchestrator, linear: FakeLinearClient,
    ) -> None:
        """_resume_pipeline does not call transition_to_state if cancelled after final message."""
        ts = TicketState(
            ticket_id="ticket-1", ticket_identifier="TEAM-1",
            repo_url="https://x", workspace_path="/tmp/x", branch="main",
            status=TicketStatus.needs_input, session_id="ses-abc",
            last_seen_comment_id="cmt-seen-1",
        )
        orchestrator._state.upsert(ts)
        linear.set_response("list_comments_since", [_make_comment("c1", "Go")])

        def cancel_then_return(*a: Any, **kw: Any) -> str:
            orchestrator._cancel_ticket("ticket-1")
            return "Done!"

        with mock.patch("symphony_lite.orchestrator.run_resume", side_effect=cancel_then_return):
            orchestrator._resume_pipeline(ts)

        transition_calls = linear.calls.get("transition_to_state", [])
        needs_input_transitions = [
            (tid, state) for tid, state in transition_calls
            if state == orchestrator._config.linear.needs_input_state
        ]
        assert needs_input_transitions == [], (
            f"Expected no needs_input transition after cancellation, got: {needs_input_transitions}"
        )


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
