"""Tests for the built-in local tracker backend and its on-disk store."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from symphony_linear.attachments import (
    extract_attachment_refs,
    process_attachments,
)
from symphony_linear.config import _NtfyConfig
from symphony_linear.local_tracker import (
    NTFY_ICON_URL,
    STATE_DONE,
    STATE_DRAFT,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    LocalFile,
    LocalTicket,
    LocalTicketStore,
    LocalTracker,
    image_content_type,
    markdown_link_text,
    publish_needs_input,
    stored_file_name,
)
from symphony_linear.presence import ACTIVE_WINDOW_SECONDS, TicketPresence
from symphony_linear.tracker import (
    AttachmentDownloadError,
    Tracker,
    TrackerError,
    TrackerNotFoundError,
    TransitionTarget,
    is_bot_comment,
    model_for_issue,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tracker(tmp_path: Path) -> LocalTracker:
    return LocalTracker(tmp_path)


@pytest.fixture
def store(tracker: LocalTracker) -> LocalTicketStore:
    return tracker.store


def _new_triggered(tracker: LocalTracker, description: str = "work") -> str:
    """Create a draft and move it to In Progress, returning its id."""
    ticket = tracker.store.create_draft(description=description)
    tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
    return ticket.id


# ---------------------------------------------------------------------------
# Protocol conformance and issue conversion
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_is_a_tracker(self, tracker: LocalTracker) -> None:
        assert isinstance(tracker, Tracker)

    def test_new_draft_defaults(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft(
            title="Title", description="Body", repo="owner/name"
        )

        assert ticket.id == "SYM-1"
        assert ticket.identifier == "SYM-1"
        assert ticket.state == STATE_DRAFT
        assert ticket.comments == []
        assert ticket.created_at
        assert ticket.updated_at
        assert ticket.state_changed_at

    def test_draft_is_not_triggered(self, tracker: LocalTracker) -> None:
        store = tracker.store
        ticket = store.create_draft(description="draft")
        assert tracker.list_triggered_issues() == []
        assert tracker.is_still_triggered(tracker.get_issue(ticket.id)) is False

    def test_triggered_states_are_listed(self, tracker: LocalTracker) -> None:
        store = tracker.store
        ids = [
            store.create_draft(description=description).id
            for description in ("one", "two")
        ]
        store.set_state(ids[0], STATE_IN_PROGRESS)
        store.set_state(ids[1], STATE_NEEDS_INPUT)

        triggered = tracker.list_triggered_issues()

        assert [issue.id for issue in triggered] == ids
        assert all(tracker.is_still_triggered(issue) for issue in triggered)
        assert all(
            issue.state in {STATE_IN_PROGRESS, STATE_NEEDS_INPUT} for issue in triggered
        )

    def test_get_issue_carries_ticket_fields(self, tracker: LocalTracker) -> None:
        store = tracker.store
        ticket = store.create_draft(
            title="A title",
            description="A **body**",
            repo="https://github.com/acme/widgets.git",
        )
        store.set_state(ticket.id, STATE_IN_PROGRESS)

        issue = tracker.get_issue(ticket.id)

        assert issue.id == ticket.id
        assert issue.identifier == ticket.id
        assert issue.title == "A title"
        assert issue.description == "A **body**"
        assert issue.state == STATE_IN_PROGRESS
        assert issue.branch_name is None
        assert issue.project is None
        assert issue.labels == []
        assert issue.comments == []
        assert issue.archived_at is None

    def test_get_issue_unknown_raises(self, tracker: LocalTracker) -> None:
        with pytest.raises(TrackerNotFoundError):
            tracker.get_issue("SYM-404")

    def test_model_becomes_label_and_resolves(self, tracker: LocalTracker) -> None:
        store = tracker.store
        ticket = store.create_draft(description="x", model="Strong")
        store.set_state(ticket.id, STATE_IN_PROGRESS)

        issue = tracker.get_issue(ticket.id)

        assert issue.labels == ["Model: Strong"]
        assert (
            model_for_issue(issue, {"strong": "anthropic/claude-opus-5"})
            == "anthropic/claude-opus-5"
        )


# ---------------------------------------------------------------------------
# Persistence and id allocation
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_reload_from_disk(self, tmp_path: Path) -> None:
        tracker = LocalTracker(tmp_path)
        ticket = tracker.store.create_draft(
            title="Persisted",
            description="Body",
            repo="owner/name",
            model="Strong",
        )
        tracker.store.add_human_comment(ticket.id, "a human reply")
        tracker.store.set_state(ticket.id, STATE_NEEDS_INPUT)

        reloaded = LocalTracker(tmp_path)
        restored = reloaded.store.get(ticket.id)

        assert restored is not None
        assert restored.title == "Persisted"
        assert restored.description == "Body"
        assert restored.repo == "owner/name"
        assert restored.model == "Strong"
        assert restored.state == STATE_NEEDS_INPUT
        assert [c.body for c in restored.comments] == ["a human reply"]

        issue = reloaded.get_issue(ticket.id)
        assert issue.title == "Persisted"
        assert issue.state == STATE_NEEDS_INPUT

    def test_ids_are_never_reused_after_draft_deletion(self, tmp_path: Path) -> None:
        tracker = LocalTracker(tmp_path)
        first = tracker.store.create_draft(description="first")
        assert first.id == "SYM-1"

        tracker.store.delete_draft(first.id)
        second = tracker.store.create_draft(description="second")
        assert second.id == "SYM-2"

        reloaded = LocalTracker(tmp_path)
        third = reloaded.store.create_draft(description="third")
        assert third.id == "SYM-3"

    def test_counter_survives_lost_counter_file(self, tmp_path: Path) -> None:
        """If counter.json disappears, existing ticket ids still can't be reused."""
        tracker = LocalTracker(tmp_path)
        tracker.store.create_draft(description="first")
        (tmp_path / "tickets" / "counter.json").unlink()

        reloaded = LocalTracker(tmp_path)
        second = reloaded.store.create_draft(description="second")

        assert second.id == "SYM-2"

    def test_one_file_per_ticket_plus_counter(self, tmp_path: Path) -> None:
        tracker = LocalTracker(tmp_path)
        ticket = tracker.store.create_draft(description="one")
        tracker.store.create_draft(description="two")
        tracker.store.delete_draft(ticket.id)

        names = sorted(path.name for path in (tmp_path / "tickets").iterdir())

        assert names == ["SYM-2.json", "counter.json", "repos.json"]

    def test_concurrent_creates_get_unique_ids(self, store: LocalTicketStore) -> None:
        ids: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                ticket = store.create_draft(description="concurrent")
                with lock:
                    ids.append(ticket.id)
            except BaseException as exc:  # pragma: no cover - reported below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(ids) == 20
        assert len(set(ids)) == 20
        assert len(store.list_tickets()) == 20


# ---------------------------------------------------------------------------
# Managed repository list
# ---------------------------------------------------------------------------


def _write_ticket_file(directory: Path, ticket: LocalTicket) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{ticket.id}.json").write_text(ticket.model_dump_json())


class TestRepos:
    def test_seeded_from_existing_tickets_newest_first(self, tmp_path: Path) -> None:
        tickets = tmp_path / "tickets"
        for ticket_id, repo, updated_at in (
            ("SYM-1", "old/repo", "2026-11-19T10:00:00+00:00"),
            (
                "SYM-2",
                "https://example.com/team/new.git",
                "2026-11-21T10:00:00+00:00",
            ),
            ("SYM-3", "old/repo", "2026-11-20T10:00:00+00:00"),
        ):
            _write_ticket_file(
                tickets,
                LocalTicket(id=ticket_id, repo=repo, updated_at=updated_at),
            )

        repos = LocalTracker(tmp_path).store.list_repos()

        assert [(entry.url, entry.name) for entry in repos] == [
            ("https://example.com/team/new.git", "team/new"),
            ("old/repo", "old/repo"),
        ]
        assert (tickets / "repos.json").exists()

    def test_an_empty_list_is_not_reseeded(self, tmp_path: Path) -> None:
        tracker = LocalTracker(tmp_path)
        assert tracker.store.list_repos() == []

        tracker.store.create_draft(description="x", repo="owner/name")
        tracker.store.add_repo("extra/repo", "Extra")

        reloaded = LocalTracker(tmp_path)
        assert [(entry.url, entry.name) for entry in reloaded.store.list_repos()] == [
            ("extra/repo", "Extra")
        ]

    def test_add_strips_the_url_and_derives_an_empty_name(self, tmp_path: Path) -> None:
        store = LocalTracker(tmp_path).store

        entry = store.add_repo("  https://example.com/team/project.git  ")

        assert entry.url == "https://example.com/team/project.git"
        assert entry.name == "team/project"
        assert LocalTracker(tmp_path).store.list_repos()[0].name == "team/project"

    def test_add_with_the_same_url_is_a_no_op(self, tmp_path: Path) -> None:
        store = LocalTracker(tmp_path).store
        first = store.add_repo("owner/name", "First")

        second = store.add_repo(" owner/name ", "Second")

        assert second.url == first.url
        assert second.name == "First"
        assert [entry.url for entry in store.list_repos()] == ["owner/name"]

    def test_add_rejects_an_empty_url(self, store: LocalTicketStore) -> None:
        with pytest.raises(ValueError):
            store.add_repo("   ")

    def test_remove_leaves_tickets_unchanged(self, tmp_path: Path) -> None:
        tracker = LocalTracker(tmp_path)
        tracker.store.add_repo("owner/name")
        ticket = tracker.store.create_draft(description="x", repo="owner/name")

        tracker.store.remove_repo("owner/name")

        assert tracker.store.list_repos() == []
        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.repo == "owner/name"

    def test_rename_keeps_the_url_and_leaves_tickets_unchanged(
        self, tmp_path: Path
    ) -> None:
        tracker = LocalTracker(tmp_path)
        tracker.store.add_repo("owner/name", "Old")
        ticket = tracker.store.create_draft(description="x", repo="owner/name")

        entry = tracker.store.rename_repo(" owner/name ", "New")

        assert entry.url == "owner/name"
        assert entry.name == "New"
        assert [
            (saved.url, saved.name)
            for saved in LocalTracker(tmp_path).store.list_repos()
        ] == [("owner/name", "New")]
        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.repo == "owner/name"

    def test_rename_of_an_unknown_url_raises(self, store: LocalTicketStore) -> None:
        with pytest.raises(TrackerNotFoundError):
            store.rename_repo("owner/name", "New")

    def test_a_refused_start_does_not_add_the_new_repository(
        self, tmp_path: Path
    ) -> None:
        tracker = LocalTracker(tmp_path)
        ticket = tracker.store.create_draft(description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)

        with pytest.raises(ValueError):
            tracker.store.start_draft(
                ticket.id,
                title="",
                description="x",
                repo="owner/name",
                model=None,
                new_repo=("owner/name", "Widgets"),
            )

        assert tracker.store.list_repos() == []

    def test_a_failed_ticket_write_leaves_the_repository_list_untouched(
        self, tmp_path: Path
    ) -> None:
        tracker = LocalTracker(tmp_path)
        ticket = tracker.store.create_draft(description="x")
        repos_path = tmp_path / "tickets" / "repos.json"
        before = repos_path.read_bytes()

        with patch.object(tracker.store, "_persist", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                tracker.store.start_draft(
                    ticket.id,
                    title="",
                    description="x",
                    repo="owner/new",
                    model=None,
                    new_repo=("owner/new", "Widgets"),
                )

        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.state == STATE_DRAFT
        assert tracker.store.list_repos() == []
        assert repos_path.read_bytes() == before

    def test_a_failed_ticket_create_leaves_the_repository_list_untouched(
        self, tmp_path: Path
    ) -> None:
        tracker = LocalTracker(tmp_path)
        repos_path = tmp_path / "tickets" / "repos.json"
        before = repos_path.read_bytes()

        with patch.object(tracker.store, "_persist", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                tracker.store.create_started_draft(
                    description="x",
                    repo="owner/new",
                    new_repo=("owner/new", "Widgets"),
                )

        assert tracker.store.list_tickets() == []
        assert tracker.store.list_repos() == []
        assert repos_path.read_bytes() == before

    def test_removing_an_unknown_url_is_a_no_op(self, store: LocalTicketStore) -> None:
        store.add_repo("owner/name")

        store.remove_repo("other/repo")

        assert [entry.url for entry in store.list_repos()] == ["owner/name"]


# ---------------------------------------------------------------------------
# Draft editing API
# ---------------------------------------------------------------------------


class TestDraftEditing:
    def test_update_draft_replaces_fields_and_persists(
        self, tmp_path: Path, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="old")

        updated = store.update_draft(
            ticket.id,
            title="New",
            description="New body",
            repo="acme/widgets",
            model="Cheap",
        )

        assert updated.title == "New"
        assert updated.description == "New body"
        assert updated.repo == "acme/widgets"
        assert updated.model == "Cheap"
        assert updated.created_at == ticket.created_at

        reloaded = LocalTicketStore(tmp_path / "tickets")
        assert reloaded.get(ticket.id) == updated

    def test_update_draft_fields_changes_only_the_supplied_ones(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(
            title="Old", description="Body", repo="acme/widgets", model="Cheap"
        )

        updated = store.update_draft_fields(ticket.id, {"description": "New body"})

        assert updated.title == "Old"
        assert updated.description == "New body"
        assert updated.repo == "acme/widgets"
        assert updated.model == "Cheap"

    def test_update_draft_fields_rejects_unknown_keys(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")

        with pytest.raises(ValueError):
            store.update_draft_fields(ticket.id, {"state": STATE_DONE})

    def test_start_draft_stored_keeps_the_stored_fields(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(title="Old", description="Body", repo="owner/name")
        store.update_draft_fields(ticket.id, {"title": "New"})

        started = store.start_draft_stored(ticket.id, add_repo=True)

        assert started.title == "New"
        assert started.state == STATE_IN_PROGRESS
        assert [entry.url for entry in store.list_repos()] == ["owner/name"]

    def test_update_started_ticket_raises(self, tracker: LocalTracker) -> None:
        ticket_id = _new_triggered(tracker)

        with pytest.raises(ValueError):
            tracker.store.update_draft(
                ticket_id, title="", description="", repo="", model=None
            )

    def test_delete_started_ticket_raises(self, tracker: LocalTracker) -> None:
        ticket_id = _new_triggered(tracker)

        with pytest.raises(ValueError):
            tracker.store.delete_draft(ticket_id)

    def test_unknown_ticket_raises(self, store: LocalTicketStore) -> None:
        with pytest.raises(TrackerNotFoundError):
            store.update_draft("SYM-404", title="", description="", repo="", model=None)
        with pytest.raises(TrackerNotFoundError):
            store.delete_draft("SYM-404")


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


class TestComments:
    def test_bot_comment_has_footer_and_kind(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")

        comment = tracker.post_comment(ticket.id, "hello", "final")

        expected = "hello\n\n*Symphony · final*"
        assert comment.body == expected
        assert is_bot_comment(comment.body)

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.comments[0].body == expected
        assert stored.comments[0].kind == "final"
        assert stored.comments[0].created_at == comment.created_at

    def test_human_comment_has_no_footer_and_named_author(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")

        stored_comment = store.add_human_comment(ticket.id, "please fix")
        comments = tracker.list_comments_since(ticket.id, None)

        assert stored_comment.kind is None
        assert stored_comment.body == "please fix"
        assert not is_bot_comment(stored_comment.body)
        assert comments[0].user_id == "user"
        assert comments[0].body == "please fix"

    def test_list_comments_since_is_positional(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        first = tracker.post_comment(ticket.id, "one", "update")
        second = store.add_human_comment(ticket.id, "two")
        third = tracker.post_comment(ticket.id, "three", "final")

        all_comments = tracker.list_comments_since(ticket.id, None)
        after_first = tracker.list_comments_since(ticket.id, first.id)
        after_second = tracker.list_comments_since(ticket.id, second.id)

        assert [c.id for c in all_comments] == [first.id, second.id, third.id]
        assert [c.id for c in after_first] == [second.id, third.id]
        assert [c.id for c in after_second] == [third.id]

    def test_list_comments_since_unknown_anchor_is_empty(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        tracker.post_comment(ticket.id, "one", "update")

        assert tracker.list_comments_since(ticket.id, "c-missing") == []

    def test_edit_comment_rewrites_body_and_kind(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        comment = tracker.post_comment(ticket.id, "first", "update")

        tracker.edit_comment(comment.id, "second", "workspace")

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.comments[0].body == "second\n\n*Symphony · workspace*"
        assert stored.comments[0].kind == "workspace"
        assert stored.comments[0].id == comment.id

    def test_edit_unknown_comment_raises(self, tracker: LocalTracker) -> None:
        with pytest.raises(TrackerNotFoundError):
            tracker.edit_comment("c-missing", "body", "update")

    def test_comments_on_unknown_ticket_raise(self, tracker: LocalTracker) -> None:
        with pytest.raises(TrackerNotFoundError):
            tracker.post_comment("SYM-404", "body", "update")
        with pytest.raises(TrackerNotFoundError):
            tracker.list_comments_since("SYM-404", None)
        with pytest.raises(TrackerNotFoundError):
            tracker.store.add_human_comment("SYM-404", "body")

    def test_add_human_comment_once_ignores_a_used_token(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_NEEDS_INPUT)

        first = store.add_human_comment_once(ticket.id, "hello", token="t1")
        second = store.add_human_comment_once(ticket.id, "hello again", token="t1")
        third = store.add_human_comment_once(ticket.id, "different", token="t2")

        assert first is not None
        assert second is None
        assert third is not None
        stored = store.get(ticket.id)
        assert stored is not None
        assert [comment.body for comment in stored.comments] == ["hello", "different"]
        assert stored.accepted_send_tokens == ["t1", "t2"]

    def test_add_human_comment_once_is_safe_under_concurrency(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_NEEDS_INPUT)
        created: list[object] = []
        barrier = threading.Barrier(8)

        def attempt() -> None:
            barrier.wait()
            created.append(
                store.add_human_comment_once(ticket.id, "retry", token="same")
            )

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        stored = store.get(ticket.id)
        assert stored is not None
        assert len(stored.comments) == 1
        assert sum(result is not None for result in created) == 1


# ---------------------------------------------------------------------------
# Workflow states
# ---------------------------------------------------------------------------


class TestStates:
    def test_transition_mapping(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        # A Draft is only left by a user action (Start); the tracker never
        # moves one, so begin from the state Start leaves it in.
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_IN_PROGRESS)

        tracker.transition_to(ticket.id, TransitionTarget.in_progress)
        assert tracker.get_issue(ticket.id).state == STATE_IN_PROGRESS
        tracker.transition_to(ticket.id, TransitionTarget.needs_input)
        assert tracker.get_issue(ticket.id).state == STATE_NEEDS_INPUT
        tracker.transition_to(ticket.id, TransitionTarget.qa)
        issue = tracker.get_issue(ticket.id)
        assert issue.state == STATE_QA
        assert tracker.is_in_qa(issue) is True

    def test_qa_is_always_enabled(self, tracker: LocalTracker) -> None:
        assert tracker.qa_enabled is True

    def test_transition_names(self, tracker: LocalTracker) -> None:
        assert (
            tracker.transition_name_for(TransitionTarget.in_progress)
            == STATE_IN_PROGRESS
        )
        assert (
            tracker.transition_name_for(TransitionTarget.needs_input)
            == STATE_NEEDS_INPUT
        )
        assert tracker.transition_name_for(TransitionTarget.qa) == STATE_QA

    def test_same_state_transition_keeps_state_changed_at(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_IN_PROGRESS)
        first = store.get(ticket.id)
        assert first is not None

        tracker.transition_to(ticket.id, TransitionTarget.in_progress)
        second = store.get(ticket.id)
        assert second is not None

        assert second.state_changed_at == first.state_changed_at
        assert second.updated_at == first.updated_at

    def test_done_and_draft_are_not_triggered(self, tracker: LocalTracker) -> None:
        ticket_id = _new_triggered(tracker)

        for state in (STATE_DONE, STATE_DRAFT):
            tracker.store.set_state(ticket_id, state)
            issue = tracker.get_issue(ticket_id)
            assert tracker.is_still_triggered(issue) is False
            assert tracker.list_triggered_issues() == []

    def test_invalid_state_is_rejected(self, tracker: LocalTracker) -> None:
        ticket = tracker.store.create_draft(description="x")

        with pytest.raises(ValueError, match="Unknown ticket state"):
            tracker.store.set_state(ticket.id, "Archive")

    def test_set_state_if_refuses_the_wrong_current_state(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")

        assert (
            store.set_state_if(ticket.id, frozenset({STATE_NEEDS_INPUT}), STATE_QA)
            is False
        )
        assert store.get(ticket.id).state == STATE_DRAFT  # type: ignore[union-attr]

        assert store.set_state_if(ticket.id, frozenset({STATE_DRAFT}), STATE_QA) is True
        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.state == STATE_QA

    def test_set_state_if_changed_reports_only_a_write(
        self, store: LocalTicketStore
    ) -> None:
        """The notification gate must not fire for a refused or no-op move."""
        ticket = store.create_draft(description="x")

        assert (
            store.set_state_if_changed(
                ticket.id, frozenset({STATE_NEEDS_INPUT}), STATE_QA
            )
            is False
        )
        assert (
            store.set_state_if_changed(ticket.id, frozenset({STATE_DRAFT}), STATE_QA)
            is True
        )
        # Already in the target state: set_state_if says the action won, but
        # nothing was written and there is nothing to notify about.
        assert (
            store.set_state_if_changed(ticket.id, frozenset({STATE_QA}), STATE_QA)
            is False
        )
        assert store.set_state_if(ticket.id, frozenset({STATE_QA}), STATE_QA) is True

    def test_human_trigger_description_names_done(self, tracker: LocalTracker) -> None:
        assert STATE_DONE in tracker.human_trigger_description()


# ---------------------------------------------------------------------------
# Repository and unsupported operations
# ---------------------------------------------------------------------------


class TestRepoAndAttachments:
    def test_shorthand_repo_becomes_github_ssh(self, tracker: LocalTracker) -> None:
        ticket = tracker.store.create_draft(description="x", repo="acme/widgets")

        assert tracker.repo_url_for(tracker.get_issue(ticket.id)) == (
            "git@github.com:acme/widgets.git"
        )

    def test_shorthand_with_git_suffix_is_not_doubled(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x", repo="acme/widgets.git")

        assert tracker.repo_url_for(tracker.get_issue(ticket.id)) == (
            "git@github.com:acme/widgets.git"
        )

    def test_full_clone_url_passes_through(self, tracker: LocalTracker) -> None:
        for url in (
            "https://github.com/acme/widgets.git",
            "git@gitlab.example.com:acme/widgets.git",
            "ssh://git@example.com/acme/widgets.git",
        ):
            ticket = tracker.store.create_draft(description="x", repo=url)
            assert tracker.repo_url_for(tracker.get_issue(ticket.id)) == url
            tracker.store.delete_draft(ticket.id)

    def test_empty_repo_raises_user_facing_error(self, tracker: LocalTracker) -> None:
        ticket = tracker.store.create_draft(description="x")

        with pytest.raises(TrackerError, match="No repository"):
            tracker.repo_url_for(tracker.get_issue(ticket.id))

    def test_repo_for_unknown_ticket_raises(self, tracker: LocalTracker) -> None:
        ticket = tracker.store.create_draft(description="x", repo="acme/widgets")
        issue = tracker.get_issue(ticket.id)
        tracker.store.delete_draft(ticket.id)

        with pytest.raises(TrackerNotFoundError):
            tracker.repo_url_for(issue)

    def test_attachments_are_unsupported(self, tracker: LocalTracker) -> None:
        assert tracker.is_upload_url("https://uploads.linear.app/x") is False
        with pytest.raises(AttachmentDownloadError):
            tracker.download_attachment("https://uploads.linear.app/x")

    def test_ensure_trigger_setup_is_a_noop(self, tracker: LocalTracker) -> None:
        tracker.ensure_trigger_setup(MagicMock(), ["Model: Strong"])


class TestReplyDraftAndSeenMarker:
    def test_a_reply_draft_round_trips_and_is_not_activity(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        before = store.get(ticket.id)
        assert before is not None

        updated = store.set_reply_draft(ticket.id, "a note\nsecond line")

        assert updated.reply_draft == "a note\nsecond line"
        assert updated.reply_draft_saved_at is not None
        # The Home list orders by updated_at; typing a note must not reorder it.
        assert updated.updated_at == before.updated_at

    def test_sending_clears_the_reply_draft(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_NEEDS_INPUT)
        store.set_reply_draft(ticket.id, "will be sent")

        store.add_human_comment_once(ticket.id, "will be sent", token="tok")

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.reply_draft == ""
        assert stored.reply_draft_saved_at is None

    def test_seen_marker_only_moves_forward(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft(description="x")
        first = store.add_human_comment(ticket.id, "one")
        second = store.add_human_comment(ticket.id, "two")
        third = store.add_human_comment(ticket.id, "three")

        store.set_seen_comment(ticket.id, second.id)
        store.set_seen_comment(ticket.id, first.id)  # a stale tab

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.seen_comment_id == second.id

        store.set_seen_comment(ticket.id, third.id)
        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.seen_comment_id == third.id

    def test_seen_marker_rejects_a_foreign_comment(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        other = store.create_draft(description="y")
        foreign = store.add_human_comment(other.id, "not here")

        with pytest.raises(ValueError):
            store.set_seen_comment(ticket.id, foreign.id)

    def test_seen_marker_is_not_reordered_as_activity(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        comment = store.add_human_comment(ticket.id, "one")
        before = store.get(ticket.id)
        assert before is not None

        store.set_seen_comment(ticket.id, comment.id)

        after = store.get(ticket.id)
        assert after is not None
        assert after.updated_at == before.updated_at

    def test_only_the_most_recent_send_tokens_are_kept(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_NEEDS_INPUT)
        for index in range(25):
            store.add_human_comment_once(ticket.id, "x", token=f"tok-{index}")

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.accepted_send_tokens == [f"tok-{index}" for index in range(5, 25)]

    def test_an_old_ticket_file_loads_with_the_new_fields_defaulted(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "tickets"
        directory.mkdir()
        (directory / "SYM-1.json").write_text(
            '{"id": "SYM-1", "title": "Old", "state": "Needs Input"}'
        )

        store = LocalTicketStore(directory)
        ticket = store.get("SYM-1")

        assert ticket is not None
        assert ticket.reply_draft == ""
        assert ticket.reply_draft_saved_at is None
        assert ticket.seen_comment_id is None
        assert ticket.comments == []


# ---------------------------------------------------------------------------
# Automatic titles
# ---------------------------------------------------------------------------


class TestAutomaticTitle:
    def test_set_title_if_empty_writes_and_persists(
        self, tmp_path: Path, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="First line\nrest")
        store.set_state(ticket.id, STATE_IN_PROGRESS)

        assert store.set_title_if_empty(ticket.id, "Generated title") is True

        restored = LocalTracker(tmp_path).store.get(ticket.id)
        assert restored is not None
        assert restored.title == "Generated title"

    def test_set_title_if_empty_never_replaces_an_existing_title(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(title="Mine", description="Body")

        assert store.set_title_if_empty(ticket.id, "Generated title") is False

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.title == "Mine"

    def test_set_title_if_empty_rejects_an_unknown_ticket(
        self, store: LocalTicketStore
    ) -> None:
        with pytest.raises(TrackerNotFoundError):
            store.set_title_if_empty("SYM-404", "Generated title")

    def test_the_issue_title_falls_back_to_the_first_line(
        self, tracker: LocalTracker
    ) -> None:
        """The orchestrator must not receive an empty prompt heading."""
        ticket_id = _new_triggered(tracker, "\n  First line  \nsecond")

        issue = tracker.get_issue(ticket_id)

        assert issue.title == "First line"


# ---------------------------------------------------------------------------
# Uploaded files
# ---------------------------------------------------------------------------


def _put_file(
    store: LocalTicketStore,
    ticket_id: str,
    original_name: str,
    data: bytes,
    *,
    target: str = "description",
) -> LocalFile:
    """Write one file through the same reserve/write/register path the web UI uses."""
    upload = store.new_upload(ticket_id, original_name, target=target)
    upload.path.write_bytes(data)
    return store.register_file(upload, len(data))


class TestUploadedFiles:
    def test_is_upload_url_accepts_only_own_relative_paths(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "shot.png", b"png")

        assert tracker.is_upload_url(f"/files/{ticket.id}/{record.name}") is True
        assert tracker.is_upload_url("https://uploads.linear.app/x.png") is False
        assert tracker.is_upload_url(f"http://evil/files/{ticket.id}/x.png") is False
        assert tracker.is_upload_url("/files/../x.png") is False
        assert tracker.is_upload_url("/files/SYM-1") is False

    def test_download_returns_the_image_type_from_the_stored_extension(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        png = _put_file(tracker.store, ticket.id, "shot.png", b"png-bytes")
        archive = _put_file(tracker.store, ticket.id, "notes.zip", b"zip-bytes")

        data, content_type = tracker.download_attachment(
            f"/files/{ticket.id}/{png.name}"
        )
        assert data == b"png-bytes"
        assert content_type == "image/png"

        data, content_type = tracker.download_attachment(
            f"/files/{ticket.id}/{archive.name}"
        )
        assert data == b"zip-bytes"
        assert content_type == "application/octet-stream"

    def test_download_rejects_unknown_and_escaping_paths(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "a.txt", b"a")

        for url in (
            "https://uploads.linear.app/x.png",
            f"/files/{ticket.id}/../../{ticket.id}.json",
            f"/files/../{ticket.id}/{record.name}",
            f"/files/{ticket.id}/missing.txt",
            "/files/SYM-404/a.txt",
        ):
            with pytest.raises(AttachmentDownloadError):
                tracker.download_attachment(url)

    def test_the_body_refs_are_built_fresh_and_not_stored(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="Ticket text")
        record = _put_file(tracker.store, ticket.id, "notes.zip", b"zip")

        issue = tracker.get_issue(ticket.id)

        assert issue.description == (
            f"Ticket text\n\n[notes.zip](/files/{ticket.id}/{record.name})"
        )
        # Reading the issue again must not have appended a second copy.
        assert tracker.get_issue(ticket.id).description == issue.description
        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.description == "Ticket text"

    def test_odd_names_still_round_trip_through_the_link_regexes(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="Ticket text")
        names = [
            "a b (c) [d] naïve.PNG",
            ".hidden.zip",
            "..." + "x" * 300 + ".txt",
            "only-extension.png",
            "evil](x).jpg",
        ]
        records = [
            _put_file(tracker.store, ticket.id, name, f"data-{index}".encode())
            for index, name in enumerate(names)
        ]

        body = tracker.get_issue(ticket.id).description
        assert len(extract_attachment_refs(body, tracker)) == len(records)
        for index, record in enumerate(records):
            assert " " not in record.name and "(" not in record.name
            assert (
                tracker.download_attachment(f"/files/{ticket.id}/{record.name}")[0]
                == f"data-{index}".encode()
            )

    def test_a_name_that_is_only_an_extension_is_not_an_image(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")

        record = _put_file(tracker.store, ticket.id, ".png", b"not an image")

        assert image_content_type(record.name) is None
        assert stored_file_name(".png").endswith("-png")

    def test_process_attachments_sees_the_image_and_writes_the_zip(
        self, tracker: LocalTracker, tmp_path: Path
    ) -> None:
        ticket = tracker.store.create_draft(description="Ticket text")
        png = _put_file(tracker.store, ticket.id, "shot.png", b"\x89PNG\r\n")
        archive = _put_file(tracker.store, ticket.id, "notes.zip", b"PK\x03\x04")

        issue = tracker.get_issue(ticket.id)
        refs = extract_attachment_refs(issue.description, tracker)
        assert [ref.url.rsplit("/", 1)[-1] for ref in refs] == [
            png.name,
            archive.name,
        ]

        attachments_dir = tmp_path / "sandbox-attachments"
        result = process_attachments(issue.description, tracker, str(attachments_dir))

        assert result.skipped == []
        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert (attachments_dir / "img-0001.png").read_bytes() == b"\x89PNG\r\n"
        assert (attachments_dir / "file-0002-notes.zip").read_bytes() == b"PK\x03\x04"
        assert "/tmp/symphony-attachments/file-0002-notes.zip" in (
            result.rewritten_body
        )

    def test_pending_files_move_to_the_comment_on_send(
        self, tracker: LocalTracker, tmp_path: Path
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        tracker.store.set_state(ticket.id, STATE_NEEDS_INPUT)
        png = _put_file(
            tracker.store, ticket.id, "shot.png", b"\x89PNG\r\n", target="reply"
        )
        archive = _put_file(
            tracker.store, ticket.id, "notes.zip", b"PK\x03\x04", target="reply"
        )

        comment = tracker.store.add_human_comment_once(
            ticket.id, "Here it is", token="tok"
        )

        assert comment is not None
        assert [item.name for item in comment.files] == [png.name, archive.name]
        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.pending_files == []
        [delivered] = tracker.list_comments_since(ticket.id, None)
        assert delivered.body == (
            f"Here it is\n\n"
            f"![shot.png](/files/{ticket.id}/{png.name})\n"
            f"[notes.zip](/files/{ticket.id}/{archive.name})"
        )

        # The same body the orchestrator sees must go through the existing
        # attachment pipeline: image to the model, zip to the sandbox.
        attachments_dir = tmp_path / "comment-attachments"
        result = process_attachments(delivered.body, tracker, str(attachments_dir))
        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert (attachments_dir / "img-0001.png").read_bytes() == b"\x89PNG\r\n"
        assert (attachments_dir / "file-0002-notes.zip").read_bytes() == b"PK\x03\x04"

    def test_deleting_a_file_removes_the_bytes_and_the_metadata(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        tracker.store.set_state(ticket.id, STATE_NEEDS_INPUT)
        record = _put_file(
            tracker.store, ticket.id, "notes.zip", b"zip", target="reply"
        )
        path = tracker.store.file_path(ticket.id, record.name)
        assert path.exists()

        tracker.store.delete_file(ticket.id, record.name, target="reply")

        assert not path.exists()
        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.pending_files == []

    def test_discarding_a_draft_deletes_its_files(self, tracker: LocalTracker) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "shot.png", b"png")
        directory = tracker.store.file_path(ticket.id, record.name).parent

        tracker.store.delete_draft(ticket.id)

        assert not directory.exists()
        assert tracker.store.get(ticket.id) is None

    def test_started_tickets_keep_their_files(self, tracker: LocalTracker) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "shot.png", b"png")

        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        tracker.store.set_state(ticket.id, STATE_DONE)

        assert tracker.store.file_path(ticket.id, record.name).exists()

    def test_path_traversal_is_rejected_by_the_store(
        self, tracker: LocalTracker, tmp_path: Path
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "a.txt", b"a")

        with pytest.raises(ValueError):
            tracker.store.file_path(ticket.id, "../SYM-2/a.txt")
        with pytest.raises(ValueError):
            tracker.store.file_path(ticket.id, "..")
        with pytest.raises((ValueError, TrackerNotFoundError)):
            tracker.store.file_path(f"../{ticket.id}", record.name)
        with pytest.raises(TrackerNotFoundError):
            tracker.store.new_upload("../SYM-2", "a.txt", target="description")

    def test_a_symlink_out_of_the_ticket_directory_is_rejected(
        self, tracker: LocalTracker, tmp_path: Path
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "a.txt", b"a")
        directory = tracker.store.file_path(ticket.id, record.name).parent
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"secret")
        (directory / "link").symlink_to(outside)

        with pytest.raises(ValueError):
            tracker.store.file_path(ticket.id, "link")
        with pytest.raises(AttachmentDownloadError):
            tracker.download_attachment(f"/files/{ticket.id}/link")

    def test_deleting_a_draft_does_not_follow_a_symlinked_ticket_directory(
        self, tracker: LocalTracker
    ) -> None:
        first = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, first.id, "a.txt", b"a")
        second = tracker.store.create_draft(description="y")
        files_root = tracker.store.files_root
        (files_root / second.id).symlink_to(files_root / first.id)

        tracker.store.delete_draft(second.id)

        # The second Draft is gone, but the first Draft's files must not have
        # been deleted through the symlink.
        assert tracker.store.get(second.id) is None
        assert tracker.store.file_path(first.id, record.name).exists()

    def test_description_files_are_refused_on_a_started_ticket(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)

        with pytest.raises(ValueError):
            tracker.store.new_upload(ticket.id, "a.txt", target="description")

    def test_reply_files_are_refused_on_a_draft_or_done_ticket(
        self, tracker: LocalTracker
    ) -> None:
        draft = tracker.store.create_draft(description="x")
        with pytest.raises(ValueError):
            tracker.store.new_upload(draft.id, "a.txt", target="reply")

        tracker.store.set_state(draft.id, STATE_DONE)
        with pytest.raises(ValueError):
            tracker.store.new_upload(draft.id, "a.txt", target="reply")

    # ------------------------------------------------------------------
    # Read only registered uploads
    # ------------------------------------------------------------------

    def test_only_registered_files_are_readable(self, tracker: LocalTracker) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "a.txt", b"registered")
        directory = tracker.store.file_path(ticket.id, record.name).parent
        orphan = directory / "deadbeef-orphan.txt"
        orphan.write_bytes(b"orphan")

        assert tracker.store.read_file(ticket.id, record.name) == b"registered"
        with pytest.raises(FileNotFoundError):
            tracker.store.read_file(ticket.id, orphan.name)
        with pytest.raises(AttachmentDownloadError):
            tracker.download_attachment(f"/files/{ticket.id}/{orphan.name}")

    def test_a_staging_file_is_not_readable(self, tracker: LocalTracker) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "a.txt", b"registered")
        directory = tracker.store.file_path(ticket.id, record.name).parent
        staging = directory / ".upload-abc123"
        staging.write_bytes(b"partial")

        with pytest.raises(FileNotFoundError):
            tracker.store.read_file(ticket.id, staging.name)
        with pytest.raises(AttachmentDownloadError):
            tracker.download_attachment(f"/files/{ticket.id}/{staging.name}")

    def test_a_registered_name_that_is_a_directory_is_not_readable(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "a.txt", b"a")
        path = tracker.store.file_path(ticket.id, record.name)
        path.unlink()
        path.mkdir()

        with pytest.raises(FileNotFoundError):
            tracker.store.read_file(ticket.id, record.name)
        with pytest.raises(AttachmentDownloadError):
            tracker.download_attachment(f"/files/{ticket.id}/{record.name}")

    # ------------------------------------------------------------------
    # Exact upload URLs
    # ------------------------------------------------------------------

    def test_is_upload_url_never_raises_and_rejects_aliases(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "a.txt", b"a")
        canonical = f"/files/{ticket.id}/{record.name}"
        encoded = canonical.replace("-", "%2D", 1)

        assert tracker.is_upload_url(canonical) is True
        for url in (
            "http://[::1",
            "http://[::1/files/SYM-1/a.txt",
            f"{canonical}?x=1",
            f"{canonical}#frag",
            encoded,
            f"https://example.invalid{canonical}",
        ):
            assert tracker.is_upload_url(url) is False, url
            with pytest.raises(AttachmentDownloadError):
                tracker.download_attachment(url)

    def test_a_malformed_link_does_not_abort_the_other_attachments(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        record = _put_file(tracker.store, ticket.id, "a.txt", b"a")
        canonical = f"/files/{ticket.id}/{record.name}"
        body = f"[broken](http://[::1)\n\n[txt]({canonical})"

        refs = extract_attachment_refs(body, tracker)

        assert [ref.url for ref in refs] == [canonical]

    # ------------------------------------------------------------------
    # File names and bot detection
    # ------------------------------------------------------------------

    def test_a_file_name_cannot_mark_a_reply_as_a_bot_comment(
        self, tracker: LocalTracker
    ) -> None:
        ticket = tracker.store.create_draft(description="x")
        tracker.store.set_state(ticket.id, STATE_NEEDS_INPUT)
        _put_file(
            tracker.store,
            ticket.id,
            "*Symphony · final.png",
            b"\x89PNG\r\n",
            target="reply",
        )

        comment = tracker.store.add_human_comment_once(
            ticket.id, "A real reply", token="tok"
        )

        assert comment is not None
        [delivered] = tracker.list_comments_since(ticket.id, None)
        assert is_bot_comment(delivered.body) is False
        assert len(extract_attachment_refs(delivered.body, tracker)) == 1

    def test_link_text_flattens_unicode_line_separators(self) -> None:
        text = markdown_link_text("a\u2028b\u2029c\u0085d*Symphony · x")

        assert "\u2028" not in text
        assert "\u2029" not in text
        assert "\u0085" not in text
        assert "*Symphony · " not in text


# ---------------------------------------------------------------------------
# Cleanup of the first-upload placeholder Draft
# ---------------------------------------------------------------------------


class TestDeleteDraftIfUntouched:
    def test_a_pristine_draft_is_deleted(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft()

        assert store.delete_draft_if_untouched(ticket.id) is True
        assert store.get(ticket.id) is None

    @pytest.mark.parametrize("field", ["title", "description", "repo", "model"])
    def test_an_edited_draft_is_kept(self, store: LocalTicketStore, field: str) -> None:
        ticket = store.create_draft()
        values: dict[str, object] = {
            "title": "",
            "description": "",
            "repo": "",
            "model": None,
        }
        values[field] = "edited" if field != "model" else "sonnet-4.5"
        store.update_draft(
            ticket.id,
            title=str(values["title"]),
            description=str(values["description"]),
            repo=str(values["repo"]),
            model=values["model"] if isinstance(values["model"], str) else None,
        )

        assert store.delete_draft_if_untouched(ticket.id) is False
        assert store.get(ticket.id) is not None

    def test_a_draft_that_gained_a_file_is_kept(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft()
        _put_file(store, ticket.id, "a.txt", b"a")

        assert store.delete_draft_if_untouched(ticket.id) is False
        assert store.get(ticket.id) is not None

    def test_a_started_ticket_is_never_deleted(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft()
        store.set_state(ticket.id, STATE_IN_PROGRESS)

        assert store.delete_draft_if_untouched(ticket.id) is False
        assert store.get(ticket.id) is not None

    def test_an_unknown_ticket_is_false(self, store: LocalTicketStore) -> None:
        assert store.delete_draft_if_untouched("SYM-404") is False


# ---------------------------------------------------------------------------
# Orchestrator transitions versus user actions
# ---------------------------------------------------------------------------


class TestOrchestratorTransitions:
    """The tracker leaves Draft only by a user action, and Done by needs_input.

    The needs_input exception is the dirty-workspace refusal: it must be able
    to move a Done ticket back, or the next tick deletes uncommitted work.
    """

    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            # Draft is never left by the orchestrator, for any target.
            (STATE_DRAFT, TransitionTarget.in_progress, STATE_DRAFT),
            (STATE_DRAFT, TransitionTarget.qa, STATE_DRAFT),
            (STATE_DRAFT, TransitionTarget.needs_input, STATE_DRAFT),
            # Done is only left for needs_input (the refusal); a queued worker
            # cannot pull it back to In Progress or QA.
            (STATE_DONE, TransitionTarget.in_progress, STATE_DONE),
            (STATE_DONE, TransitionTarget.qa, STATE_DONE),
            (STATE_DONE, TransitionTarget.needs_input, STATE_NEEDS_INPUT),
            # Triggered states keep their ordinary transitions.
            (STATE_IN_PROGRESS, TransitionTarget.in_progress, STATE_IN_PROGRESS),
            (STATE_IN_PROGRESS, TransitionTarget.qa, STATE_QA),
            (STATE_IN_PROGRESS, TransitionTarget.needs_input, STATE_NEEDS_INPUT),
            (STATE_NEEDS_INPUT, TransitionTarget.in_progress, STATE_IN_PROGRESS),
            (STATE_NEEDS_INPUT, TransitionTarget.qa, STATE_QA),
            (STATE_NEEDS_INPUT, TransitionTarget.needs_input, STATE_NEEDS_INPUT),
            (STATE_QA, TransitionTarget.in_progress, STATE_IN_PROGRESS),
            (STATE_QA, TransitionTarget.qa, STATE_QA),
            (STATE_QA, TransitionTarget.needs_input, STATE_NEEDS_INPUT),
        ],
    )
    def test_transition_table(
        self,
        tracker: LocalTracker,
        store: LocalTicketStore,
        source: str,
        target: TransitionTarget,
        expected: str,
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, source)

        tracker.transition_to(ticket.id, target)

        issue = tracker.get_issue(ticket.id)
        assert issue.state == expected
        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.state == expected


# ---------------------------------------------------------------------------
# ntfy notifications
# ---------------------------------------------------------------------------


def _recording_transport(
    seen: list[httpx.Request], *, status: int = 200
) -> httpx.MockTransport:
    """Return a transport that records each request and answers with *status*."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return httpx.MockTransport(handle)


class TestNtfyNotifications:
    def test_publish_posts_json_to_the_server_root(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(title="Fix the frobnicator", description="x")
        seen: list[httpx.Request] = []

        publish_needs_input(
            _NtfyConfig(
                url="https://example.com/ntfy/d%C3%A9ployer", token="tk_secret"
            ),
            ticket,
            transport=_recording_transport(seen),
        )

        assert len(seen) == 1
        request = seen[0]
        # The JSON publish targets the server root, not the topic URL from the
        # config, and the self-hosted path prefix stays exactly as written.
        assert str(request.url) == "https://example.com/ntfy/"
        assert request.headers["Authorization"] == "Bearer tk_secret"
        body = json.loads(request.content)
        # Only the final segment is percent-decoded into the topic name.  The
        # icon is always present, even with no public_url, because ntfy fetches
        # it from GitHub rather than from the credential-protected UI.
        assert body == {
            "topic": "déployer",
            "title": "Needs Input",
            "message": "SYM-1: Fix the frobnicator",
            "icon": NTFY_ICON_URL,
        }

    def test_non_ascii_title_and_click_link(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft(title="Déployer — 日本語", description="x")
        seen: list[httpx.Request] = []

        publish_needs_input(
            _NtfyConfig(url="https://ntfy.sh/mytopic"),
            ticket,
            public_url="https://symphony.example.com",
            transport=_recording_transport(seen),
        )

        body = json.loads(seen[0].content)
        # A Title header would be latin-1 and could not carry this; the JSON
        # body is UTF-8.
        assert body["message"] == "SYM-1: Déployer — 日本語"
        assert body["click"] == "https://symphony.example.com/tickets/SYM-1"
        # public_url drives the click target only; the icon never depends on it.
        assert body["icon"] == NTFY_ICON_URL

    def test_the_display_title_fallback_is_used(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft(description="First line\nsecond line")
        seen: list[httpx.Request] = []

        publish_needs_input(
            _NtfyConfig(url="https://ntfy.sh/mytopic"),
            ticket,
            transport=_recording_transport(seen),
        )

        assert json.loads(seen[0].content)["message"] == "SYM-1: First line"

    def test_a_non_2xx_response_only_warns(
        self, store: LocalTicketStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        ticket = store.create_draft(title="t", description="x")

        with caplog.at_level(logging.WARNING, logger="symphony_linear.local_tracker"):
            publish_needs_input(
                _NtfyConfig(url="https://ntfy.sh/mytopic"),
                ticket,
                transport=_recording_transport([], status=500),
            )

        assert "HTTP 500" in caplog.text

    def test_a_transport_error_only_warns(
        self, store: LocalTicketStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        ticket = store.create_draft(title="t", description="x")

        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        with caplog.at_level(logging.WARNING, logger="symphony_linear.local_tracker"):
            publish_needs_input(
                _NtfyConfig(url="https://ntfy.sh/mytopic"),
                ticket,
                transport=httpx.MockTransport(explode),
            )

        assert "ConnectError" in caplog.text

    def test_a_broken_http_client_does_not_fail_the_transition(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An OSError from client setup (e.g. a bad SSL_CERT_FILE) is contained."""
        tracker = LocalTracker(tmp_path, ntfy=_NtfyConfig(url="https://ntfy.sh/t"))
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)

        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("bad SSL_CERT_FILE")

        monkeypatch.setattr("symphony_linear.local_tracker.httpx.Client", explode)

        with caplog.at_level(logging.WARNING, logger="symphony_linear.local_tracker"):
            tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "OSError" in caplog.text

    def test_transition_to_notifies_only_on_a_real_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker = LocalTracker(tmp_path, ntfy=_NtfyConfig(url="https://ntfy.sh/t"))
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **_kwargs: calls.append(notified.id),
        )

        tracker.transition_to(ticket.id, TransitionTarget.needs_input)
        # The second call is the no-op a turn's failure path plus its normal
        # end-of-turn transition would produce: one notification, not two.
        tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        assert calls == [ticket.id]

    def test_other_targets_do_not_notify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker = LocalTracker(tmp_path, ntfy=_NtfyConfig(url="https://ntfy.sh/t"))
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **_kwargs: calls.append(notified.id),
        )

        tracker.transition_to(ticket.id, TransitionTarget.qa)
        tracker.transition_to(ticket.id, TransitionTarget.in_progress)

        assert calls == []

    def test_a_refused_transition_does_not_notify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker = LocalTracker(tmp_path, ntfy=_NtfyConfig(url="https://ntfy.sh/t"))
        ticket = tracker.store.create_draft(title="t", description="x")
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **_kwargs: calls.append(notified.id),
        )

        tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        assert calls == []
        assert tracker.store.get(ticket.id).state == STATE_DRAFT  # type: ignore[union-attr]

    def test_no_ntfy_block_never_publishes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the config block nothing is sent, even on a real change."""
        tracker = LocalTracker(tmp_path)
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **_kwargs: calls.append(notified.id),
        )

        tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        assert calls == []
        assert tracker.store.get(ticket.id).state == STATE_NEEDS_INPUT  # type: ignore[union-attr]

    def test_a_watched_ticket_skips_the_notification(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        presence = TicketPresence()
        tracker = LocalTracker(
            tmp_path, ntfy=_NtfyConfig(url="https://ntfy.sh/t"), presence=presence
        )
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        presence.mark_active(ticket.id)
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **_kwargs: calls.append(notified.id),
        )

        with caplog.at_level(logging.INFO, logger="symphony_linear.local_tracker"):
            tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        assert calls == []
        assert "Skipping ntfy notification" in caplog.text
        # The suppression is only about the push: the transition itself stands.
        stored = tracker.store.get(ticket.id)
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT

    def test_presence_for_another_ticket_does_not_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the page of the ticket that changed suppresses its own push."""
        presence = TicketPresence()
        tracker = LocalTracker(
            tmp_path, ntfy=_NtfyConfig(url="https://ntfy.sh/t"), presence=presence
        )
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        presence.mark_active("SYM-999")
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **_kwargs: calls.append(notified.id),
        )

        tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        assert calls == [ticket.id]

    def test_a_stale_presence_signal_does_not_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 1000.0}
        monkeypatch.setattr(
            "symphony_linear.presence.time.monotonic", lambda: clock["now"]
        )
        presence = TicketPresence()
        tracker = LocalTracker(
            tmp_path, ntfy=_NtfyConfig(url="https://ntfy.sh/t"), presence=presence
        )
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        presence.mark_active(ticket.id)
        clock["now"] += ACTIVE_WINDOW_SECONDS + 1
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **_kwargs: calls.append(notified.id),
        )

        tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        assert calls == [ticket.id]


# ---------------------------------------------------------------------------
# The Send transaction
# ---------------------------------------------------------------------------


class TestSendTransaction:
    def test_a_reply_is_refused_while_a_turn_runs(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_IN_PROGRESS)

        with pytest.raises(ValueError):
            store.add_human_comment_once(ticket.id, "late reply", token="t1")

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.comments == []

    def test_a_reply_is_refused_on_a_done_ticket(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_DONE)

        with pytest.raises(ValueError):
            store.add_human_comment_once(ticket.id, "too late", token="t1")

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.comments == []

    def test_a_refused_send_does_not_move_pending_files(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_NEEDS_INPUT)
        record = _put_file(store, ticket.id, "shot.png", b"\x89PNG\r\n", target="reply")
        store.set_state(ticket.id, STATE_IN_PROGRESS)

        with pytest.raises(ValueError):
            store.add_human_comment_once(ticket.id, "late", token="t1")

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.comments == []
        assert [item.name for item in stored.pending_files] == [record.name]

    def test_an_accepted_token_succeeds_after_the_state_changed(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_NEEDS_INPUT)
        first = store.add_human_comment_once(ticket.id, "hello", token="t1")
        assert first is not None

        # The orchestrator picks the comment up and moves the ticket while the
        # browser is still waiting to retry.
        store.set_state(ticket.id, STATE_IN_PROGRESS)
        retry = store.add_human_comment_once(ticket.id, "hello", token="t1")

        stored = store.get(ticket.id)
        assert retry is None
        assert stored is not None
        assert [comment.body for comment in stored.comments] == ["hello"]

    def test_a_new_token_is_still_refused_after_the_state_changed(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_NEEDS_INPUT)
        store.add_human_comment_once(ticket.id, "hello", token="t1")
        store.set_state(ticket.id, STATE_IN_PROGRESS)

        with pytest.raises(ValueError):
            store.add_human_comment_once(ticket.id, "another", token="t2")


# ---------------------------------------------------------------------------
# Start is one store operation
# ---------------------------------------------------------------------------


class TestStartTransaction:
    def test_start_commits_the_fields_and_the_state_together(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="old", repo="old/repo")

        started = store.start_draft(
            ticket.id,
            title="A title",
            description="The validated text",
            repo="owner/name",
            model="Strong",
        )

        assert started.state == STATE_IN_PROGRESS
        stored = store.get(ticket.id)
        assert stored is not None
        assert (
            stored.title,
            stored.description,
            stored.repo,
            stored.model,
        ) == ("A title", "The validated text", "owner/name", "Strong")
        assert stored.state == STATE_IN_PROGRESS
        assert stored.state_changed_at == started.state_changed_at

    def test_a_tabs_emptied_fields_are_replaced_by_the_started_ones(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="real text", repo="owner/name")
        # Another tab's autosave lands first and empties everything.
        store.update_draft(ticket.id, title="", description="", repo="", model=None)

        store.start_draft(
            ticket.id,
            title="T",
            description="The text the user pressed Start with",
            repo="owner/name",
            model=None,
        )

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.state == STATE_IN_PROGRESS
        assert stored.repo == "owner/name"
        assert stored.description == "The text the user pressed Start with"

    def test_an_autosave_after_start_is_refused(self, store: LocalTicketStore) -> None:
        ticket = store.create_draft(description="x", repo="owner/name")
        store.start_draft(
            ticket.id,
            title="T",
            description="D",
            repo="owner/name",
            model=None,
        )

        with pytest.raises(ValueError):
            store.update_draft(ticket.id, title="", description="", repo="", model=None)

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.repo == "owner/name"
        assert stored.state == STATE_IN_PROGRESS

    def test_start_refuses_a_ticket_that_is_not_a_draft(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_draft(description="x")
        store.set_state(ticket.id, STATE_NEEDS_INPUT)

        with pytest.raises(ValueError):
            store.start_draft(
                ticket.id, title="T", description="D", repo="o/n", model=None
            )

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT
        assert stored.description == "x"

    def test_stop_right_after_start_does_not_crash(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        """Known and accepted: Stop before the daemon sees Start is a no-op.

        The orchestrator treats a Needs Input ticket with no state entry as a
        new ticket, so the turn is not stopped; the sequence must at least stay
        a pair of ordinary store writes.
        """
        ticket = store.create_started_draft(
            title="T", description="D", repo="o/n", model=None
        )

        stopped = store.set_state_if(
            ticket.id, frozenset({STATE_IN_PROGRESS}), STATE_NEEDS_INPUT
        )
        assert stopped is True
        tracker.transition_to(ticket.id, TransitionTarget.in_progress)

        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.state == STATE_IN_PROGRESS

    def test_create_started_draft_writes_one_started_ticket(
        self, store: LocalTicketStore
    ) -> None:
        ticket = store.create_started_draft(
            title="T", description="D", repo="o/n", model=None
        )

        assert ticket.state == STATE_IN_PROGRESS
        assert ticket.created_at == ticket.state_changed_at
        stored = store.get(ticket.id)
        assert stored is not None
        assert stored.state == STATE_IN_PROGRESS


# ---------------------------------------------------------------------------
# What the tracker reports as updatedAt, and why
# ---------------------------------------------------------------------------


class TestIssueUpdatedAt:
    def test_updated_at_is_the_state_change_not_the_latest_activity(
        self, tracker: LocalTracker, store: LocalTicketStore
    ) -> None:
        older = _new_triggered(tracker)
        store.set_state(older, STATE_QA)
        newer = _new_triggered(tracker)
        store.set_state(newer, STATE_QA)

        # A late comment on the older QA ticket is activity, not a new QA
        # entrant, so it must not steal the serve.
        store.add_human_comment(older, "a late comment")

        older_ticket = store.get(older)
        assert older_ticket is not None
        assert older_ticket.updated_at != older_ticket.state_changed_at

        issues = {issue.id: issue for issue in tracker.list_triggered_issues()}
        assert issues[older].updated_at == datetime.fromisoformat(
            older_ticket.state_changed_at
        )
        winner = max(issues.values(), key=lambda issue: issue.updated_at)
        assert winner.id == newer
