"""Tests for the JSON endpoints behind the live pages and the autosave.

The server runs for real on an ephemeral port, reusing the fixtures of
``tests.test_webui_server`` and ``tests.test_webui_ticket``.  The important
boundaries are the read-only GET contract, the same-origin check on every
POST, "last save wins" for the two autosaves, and the append-only poll.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from symphony_linear.tracker import TrackerNotFoundError
from symphony_linear.local_tracker import (
    STATE_DONE,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    LocalTracker,
)
from symphony_linear.presence import TicketPresence
from symphony_linear.state import TicketStatus
from tests.test_webui_server import _origin, _request, _state_entry
from tests.test_webui_ticket import _MODELS, _server, _ticket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_request(
    port: int,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    origin: bool = True,
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = _origin(port)
    body = json.dumps(payload).encode() if payload is not None else None
    status, _headers, raw = _request(port, method, path, headers=headers, body=body)
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        # A rejected request (no Origin, bad auth) answers with plain text.
        payload = {}
    return status, payload


def _get_json(port: int, path: str) -> Any:
    status, payload = _json_request(port, "GET", path)
    assert status == 200, payload
    return payload


def _post_json(port: int, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    return _json_request(port, "POST", path, payload)


def _ticket_file(tmp_path: Path, ticket_id: str) -> Path:
    return tmp_path / "tickets" / f"{ticket_id}.json"


def _home(port: int) -> str:
    status, _headers, body = _request(port, "GET", "/")
    assert status == 200
    return body.decode()


# ---------------------------------------------------------------------------
# Home and archive polling
# ---------------------------------------------------------------------------


class TestHomePoll:
    def test_the_fragment_is_the_page_body_and_carries_the_count(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            waiting = _ticket(tracker.store, title="Waiting")
            _ticket(tracker.store, state=STATE_IN_PROGRESS, title="Running")

            page = _home(port)
            data = _get_json(port, "/api/home")

        assert data["needs_input_count"] == 1
        assert "Waiting" in data["html"]
        assert "Running" in data["html"]
        assert '<span class="state state--input">Needs Input</span>' in data["html"]
        assert data["version"]
        # The full page embeds the same fragment and its version.
        assert f'data-version="{data["version"]}"' in page
        assert waiting.title in page

    def test_the_version_changes_only_when_the_list_changes(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            _ticket(tracker.store, title="First")
            before = _get_json(port, "/api/home")["version"]

            unchanged = _get_json(port, "/api/home")["version"]
            _ticket(tracker.store, title="Second")
            after = _get_json(port, "/api/home")["version"]

        assert before == unchanged
        assert after != before

    def test_the_needs_input_count_is_in_the_tab_title(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            empty_title = "<title>Tickets · symphony</title>"
            assert empty_title in _home(port)

            _ticket(tracker.store, title="Waiting")
            _ticket(tracker.store, title="Also waiting")

            text = _home(port)

        assert "<title>(2) Tickets · symphony</title>" in text

    def test_the_ticket_page_and_archive_carry_the_count_too(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            waiting = _ticket(tracker.store, title="Waiting")
            _ticket(tracker.store, title="Second waiting")
            ticket_page = _request(port, "GET", f"/tickets/{waiting.id}")[2].decode()
            archive_page = _request(port, "GET", "/archive")[2].decode()

        assert "(2) " in ticket_page.split("<title>", 1)[1]
        assert "(2) " in archive_page.split("<title>", 1)[1]

    def test_an_unseen_agent_reply_marks_the_row_unread(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, title="Waiting")
            tracker.post_comment(ticket.id, "a reply", "final")

            data = _get_json(port, "/api/home")

        assert "row--unread" in data["html"]
        assert "mark--unread" in data["html"]
        assert "Unread." in data["html"]

    def test_a_human_comment_alone_is_not_unread(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, title="Waiting")
            tracker.store.add_human_comment(ticket.id, "my own note")

            data = _get_json(port, "/api/home")

        assert "row--unread" not in data["html"]

    def test_the_seen_marker_clears_the_unread_row(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, title="Waiting")
            comment = tracker.post_comment(ticket.id, "a reply", "final")
            before = _get_json(port, "/api/home")

            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/seen", {"comment_id": comment.id}
            )
            after = _get_json(port, "/api/home")

        assert status == 200
        assert "row--unread" in before["html"]
        assert "row--unread" not in after["html"]

    def test_the_archive_poll_returns_done_rows(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            _ticket(tracker.store, state=STATE_DONE, title="Finished")

            data = _get_json(port, "/api/archive")

        assert "Finished" in data["html"]
        assert data["version"]
        assert data["needs_input_count"] == 0

    def test_reading_is_read_only(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, title="Waiting")
            tracker.post_comment(ticket.id, "a reply", "final")
            path = _ticket_file(tmp_path, ticket.id)
            before = path.read_bytes()

            _request(port, "GET", "/api/home")
            _request(port, "GET", "/api/archive")
            _request(port, "GET", f"/api/tickets/{ticket.id}")

            after = path.read_bytes()

        assert before == after

    def test_rows_carry_a_machine_readable_time(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            _ticket(tracker.store, title="Waiting")

            data = _get_json(port, "/api/home")

        assert 'data-format="relative"' in data["html"]
        assert "<time datetime=" in data["html"]


# ---------------------------------------------------------------------------
# Ticket polling
# ---------------------------------------------------------------------------


class TestTicketPoll:
    def test_after_returns_only_the_newer_entries(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            first = tracker.post_comment(ticket.id, "first reply", "final")
            second = tracker.store.add_human_comment(ticket.id, "second, human")

            data = _get_json(port, f"/api/tickets/{ticket.id}?after={first.id}")

        assert "second, human" in data["entries_html"]
        assert "first reply" not in data["entries_html"]
        assert data["last_comment_id"] == second.id

    def test_an_empty_anchor_returns_every_comment(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.store.add_human_comment(ticket.id, "only comment")

            data = _get_json(port, f"/api/tickets/{ticket.id}?after=")

        assert "only comment" in data["entries_html"]

    def test_an_unknown_anchor_replays_nothing(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.post_comment(ticket.id, "a reply", "final")

            data = _get_json(port, f"/api/tickets/{ticket.id}?after=c-missing")

        assert data["entries_html"].strip() == ""

    def test_the_poll_does_not_render_the_whole_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the delta is rendered: the full-thread pass was thrown away."""
        from symphony_linear.webui import ticket as ticket_module

        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.post_comment(ticket.id, "a reply", "final")

            calls: list[str] = []
            original = ticket_module.build_entries

            def spy(ticket: Any, **kwargs: Any) -> Any:
                calls.append("build_entries")
                return original(ticket, **kwargs)

            monkeypatch.setattr(ticket_module, "build_entries", spy)
            status, data = _json_request(
                port, "GET", f"/api/tickets/{ticket.id}?after="
            )

        assert status == 200
        assert calls == []
        assert "a reply" in data["entries_html"]
        # The ticket poll has no version digest; only the list pages have one.
        assert "version" not in data

    def test_the_snapshot_carries_the_mutable_fragments(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)

            data = _get_json(port, f"/api/tickets/{ticket.id}")

        assert 'class="state state--progress"' in data["state_html"]
        assert 'id="act-send" disabled' in data["actions_html"]
        assert "The agent is working." in data["actions_html"]
        # Shortcut targets live in composer_actions, so the poll's re-render of
        # the action bar keeps them.
        assert 'id="act-stop" data-shortcut="sn"' in data["actions_html"]
        assert "Working, " in data["working_html"]
        assert data["has_composer"] is True
        assert data["page_title"] == f"{ticket.id} · A ticket · symphony"
        assert data["needs_input_count"] == 0

    def test_the_snapshot_carries_the_stored_model(self, tmp_path: Path) -> None:
        with _server(tmp_path, models=_MODELS) as (port, tracker, _state, _wake):
            named = _ticket(tracker.store, model="sonnet")
            plain = _ticket(tracker.store, model=None)

            named_data = _get_json(port, f"/api/tickets/{named.id}")
            plain_data = _get_json(port, f"/api/tickets/{plain.id}")

        # The page rewrites its Details Model cell from this without a reload.
        assert named_data["model"] == "sonnet"
        assert plain_data["model"] == "Default"
        # The composer select follows the same stored value: its own empty
        # string is the Default option.
        assert named_data["composer_model"] == "sonnet"
        assert plain_data["composer_model"] == ""

    def test_a_state_change_refreshes_the_action_bar(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)

            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/state", {"action": "stop"}
            )
            data = _get_json(port, f"/api/tickets/{ticket.id}")

            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT
        assert 'class="state state--input"' in data["state_html"]
        assert 'id="act-send"' in data["actions_html"]
        assert 'id="act-send" disabled' not in data["actions_html"]
        assert "The agent is working." not in data["actions_html"]
        assert data["working_html"] == ""
        wake.assert_called_once()

    def test_the_mode_change_is_visible_to_the_page(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            _post_json(port, f"/api/tickets/{ticket.id}/state", {"action": "done"})

            data = _get_json(port, f"/api/tickets/{ticket.id}")

        assert data["has_composer"] is False

    def test_an_unread_marker_badges_only_older_entries(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.post_comment(ticket.id, "older reply", "final")
            newest = tracker.store.add_human_comment(ticket.id, "newest note")

            text = _request(port, "GET", f"/tickets/{ticket.id}")[2].decode()
            other = _ticket(tracker.store, title="Read")
            tracker.post_comment(other.id, "read reply", "final")
            stored_other = tracker.store.get(other.id)
            assert stored_other is not None
            tracker.store.set_seen_comment(other.id, stored_other.comments[-1].id)
            read_text = _request(port, "GET", f"/tickets/{other.id}")[2].decode()

        assert newest.id in text
        assert text.count('<span class="new">New</span>') == 2
        assert '<span class="new">New</span>' not in read_text

    def test_opening_the_page_does_not_advance_the_marker(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.post_comment(ticket.id, "a reply", "final")
            path = _ticket_file(tmp_path, ticket.id)
            before = json.loads(path.read_text())["seen_comment_id"]

            _request(port, "GET", f"/tickets/{ticket.id}")
            _request(port, "GET", f"/api/tickets/{ticket.id}")
            after = json.loads(path.read_text())["seen_comment_id"]

        assert before is None
        assert after is None

    def test_an_active_poll_registers_only_that_ticket(self, tmp_path: Path) -> None:
        presence = TicketPresence()
        with _server(tmp_path, presence=presence) as (port, tracker, _state, _wake):
            watched = _ticket(tracker.store, title="Watched")
            other = _ticket(tracker.store, title="Other")

            _get_json(port, f"/api/tickets/{watched.id}?after=&active=1")
            _get_json(port, f"/api/tickets/{other.id}?after=")

        assert presence.is_active(watched.id)
        assert not presence.is_active(other.id)

    def test_a_poll_without_active_does_not_register(self, tmp_path: Path) -> None:
        presence = TicketPresence()
        with _server(tmp_path, presence=presence) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)

            _get_json(port, f"/api/tickets/{ticket.id}?after=")

        assert not presence.is_active(ticket.id)


class TestStateActionsApi:
    def test_reopen_without_a_state_entry_goes_in_progress(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_DONE)

            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/state", {"action": "reopen"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert stored is not None
        assert stored.state == STATE_IN_PROGRESS

    def test_reopen_with_a_state_entry_goes_needs_input(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store, state=STATE_DONE)
            state.upsert(_state_entry(ticket.id))

            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/state", {"action": "reopen"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT


# ---------------------------------------------------------------------------
# The reply draft and sending
# ---------------------------------------------------------------------------


class TestReplyDraftApi:
    def test_the_draft_is_saved_and_loaded_when_the_page_opens(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)

            status, payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/draft",
                {"text": "a note\nsecond line"},
            )
            text = _request(port, "GET", f"/tickets/{ticket.id}")[2].decode()

        assert status == 200
        assert payload["saved_at"]
        assert "a note\nsecond line</textarea>" in text

    def test_the_draft_keeps_saving_while_the_agent_works(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/draft", {"text": "note"}
            )

            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert stored is not None
        assert stored.reply_draft == "note"

    def test_a_done_ticket_has_no_draft(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_DONE)

            status, payload = _post_json(
                port, f"/api/tickets/{ticket.id}/draft", {"text": "nope"}
            )

        assert status == 409
        assert "error" in payload

    def test_last_save_wins(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            _post_json(port, f"/api/tickets/{ticket.id}/draft", {"text": "first"})
            _post_json(port, f"/api/tickets/{ticket.id}/draft", {"text": "second"})

            stored = tracker.store.get(ticket.id)

        assert stored is not None
        assert stored.reply_draft == "second"

    def test_sending_clears_the_draft_and_rotates_the_token(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            ticket = _ticket(tracker.store)
            old_token = "old-token"
            _post_json(port, f"/api/tickets/{ticket.id}/draft", {"text": "to be sent"})

            status, payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/reply",
                {"body": "  to be sent  ", "token": old_token},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert stored is not None
        assert stored.reply_draft == ""
        assert [c.body for c in stored.comments] == ["to be sent"]
        assert old_token in stored.accepted_send_tokens
        assert payload["token"] != old_token
        assert payload["duplicate"] is False
        wake.assert_called_once()

    def test_a_reused_token_does_not_post_twice(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            path = f"/api/tickets/{ticket.id}/reply"
            first_status, first = _post_json(
                port, path, {"body": "hello", "token": "same"}
            )
            second_status, second = _post_json(
                port, path, {"body": "hello again", "token": "same"}
            )

            stored = tracker.store.get(ticket.id)

        assert first_status == second_status == 200
        assert first["duplicate"] is False
        # The retry committed nothing new; the page must not clear an edited
        # textarea on the strength of that response.
        assert second["duplicate"] is True
        assert second["token"] != first["token"]
        assert stored is not None
        assert [c.body for c in stored.comments] == ["hello"]

    def test_sending_while_the_agent_works_is_refused(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)

            status, payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/reply",
                {"body": "hello", "token": "t"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 409
        assert "error" in payload
        assert stored is not None
        assert stored.comments == []

    def test_a_retry_of_an_accepted_token_succeeds_after_the_state_moved(
        self, tmp_path: Path
    ) -> None:
        """The token check comes before the state check, inside the lock."""
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            path = f"/api/tickets/{ticket.id}/reply"
            _post_json(port, path, {"body": "hello", "token": "same"})
            # The orchestrator picks the comment up while the browser is
            # still retrying the same Send.
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)

            status, payload = _post_json(port, path, {"body": "hello", "token": "same"})
            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert payload["token"]
        assert payload["duplicate"] is True
        assert stored is not None
        assert [comment.body for comment in stored.comments] == ["hello"]

    def test_a_new_token_is_refused_after_the_state_moved(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)

            status, _payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/reply",
                {"body": "another", "token": "new"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 409
        assert stored is not None
        assert stored.comments == []

    def test_sending_stores_the_chosen_model(self, tmp_path: Path) -> None:
        with _server(tmp_path, models=_MODELS) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, model="sonnet")

            status, _payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/reply",
                {"body": "hello", "token": "t", "model": "opus"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert stored is not None
        assert stored.model == "opus"

    def test_an_empty_reply_is_refused(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)

            status, _payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/reply",
                {"body": "   ", "token": "t"},
            )

        assert status == 400


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetryApi:
    def test_retry_posts_the_fixed_comment_and_keeps_the_composer(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, state, wake):
            ticket = _ticket(tracker.store)
            state.upsert(_state_entry(ticket.id, status=TicketStatus.failed))
            _post_json(
                port, f"/api/tickets/{ticket.id}/draft", {"text": "still writing"}
            )

            status, payload = _post_json(
                port, f"/api/tickets/{ticket.id}/retry", {"retry_token": "retry-1"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert payload["duplicate"] is False
        assert payload["retry_token"] != "retry-1"
        assert stored is not None
        assert [comment.body for comment in stored.comments] == ["Retry."]
        assert stored.reply_draft == "still writing"
        assert "retry-1" in stored.accepted_send_tokens
        wake.assert_called_once()

    def test_a_reused_token_does_not_post_twice(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store)
            state.upsert(_state_entry(ticket.id, status=TicketStatus.failed))
            path = f"/api/tickets/{ticket.id}/retry"

            first_status, first = _post_json(port, path, {"retry_token": "same"})
            second_status, second = _post_json(port, path, {"retry_token": "same"})
            stored = tracker.store.get(ticket.id)

        assert first_status == second_status == 200
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["retry_token"] != first["retry_token"]
        assert stored is not None
        assert [comment.body for comment in stored.comments] == ["Retry."]

    def test_retry_stores_the_chosen_model(self, tmp_path: Path) -> None:
        with _server(tmp_path, models=_MODELS) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store, model="sonnet")
            state.upsert(_state_entry(ticket.id, status=TicketStatus.failed))

            _post_json(
                port,
                f"/api/tickets/{ticket.id}/retry",
                {"retry_token": "retry-1", "model": "opus"},
            )
            stored = tracker.store.get(ticket.id)

        assert stored is not None
        assert stored.model == "opus"

    def test_retry_requires_its_own_token(self, tmp_path: Path) -> None:
        """A Send token must not be accepted on the Retry endpoint."""
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store)
            state.upsert(_state_entry(ticket.id, status=TicketStatus.failed))
            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/retry", {"token": "send-token"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 400
        assert stored is not None
        assert stored.comments == []

    def test_retry_without_a_token_is_refused(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, _payload = _post_json(port, f"/api/tickets/{ticket.id}/retry", {})
            stored = tracker.store.get(ticket.id)

        assert status == 400
        assert stored is not None
        assert stored.comments == []

    def test_retry_on_a_healthy_needs_input_ticket_is_refused(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store)
            state.upsert(_state_entry(ticket.id))
            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/retry", {"retry_token": "retry-1"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 409
        assert stored is not None
        assert stored.comments == []

    def test_retry_while_the_agent_works_is_refused(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            # The failed-turn half passes, so the store's locked state check is
            # what refuses this one.
            state.upsert(_state_entry(ticket.id, status=TicketStatus.failed))
            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/retry", {"retry_token": "retry-1"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 409
        assert stored is not None
        assert stored.comments == []

    def test_a_reused_token_succeeds_after_the_ticket_moves_on(
        self, tmp_path: Path
    ) -> None:
        """A lost retry response is still success after the window closes."""
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store)
            state.upsert(_state_entry(ticket.id, status=TicketStatus.failed))
            path = f"/api/tickets/{ticket.id}/retry"

            first_status, first = _post_json(port, path, {"retry_token": "same"})
            tracker.store.set_state(ticket.id, STATE_QA)
            state.upsert(_state_entry(ticket.id))
            second_status, second = _post_json(port, path, {"retry_token": "same"})
            stored = tracker.store.get(ticket.id)

        assert first_status == second_status == 200
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert stored is not None
        assert [comment.body for comment in stored.comments] == ["Retry."]


# ---------------------------------------------------------------------------
# Seen marker
# ---------------------------------------------------------------------------


class TestSeenApi:
    def test_the_marker_advances_and_never_goes_back(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            first = tracker.store.add_human_comment(ticket.id, "one")
            second = tracker.store.add_human_comment(ticket.id, "two")

            _post_json(
                port, f"/api/tickets/{ticket.id}/seen", {"comment_id": second.id}
            )
            _post_json(port, f"/api/tickets/{ticket.id}/seen", {"comment_id": first.id})
            stored = tracker.store.get(ticket.id)

        assert stored is not None
        assert stored.seen_comment_id == second.id

    def test_a_foreign_comment_is_rejected(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            other = _ticket(tracker.store, title="Other")
            comment = tracker.store.add_human_comment(other.id, "not yours")

            status, payload = _post_json(
                port, f"/api/tickets/{ticket.id}/seen", {"comment_id": comment.id}
            )

        assert status == 400
        assert "error" in payload


# ---------------------------------------------------------------------------
# Editor autosave and actions
# ---------------------------------------------------------------------------


class TestEditorApi:
    def test_a_new_start_racing_a_delete_is_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            with mock.patch.object(
                tracker.store,
                "create_started_draft",
                side_effect=TrackerNotFoundError("gone"),
            ):
                status, payload = _post_json(
                    port,
                    "/api/new/start",
                    {
                        "title": "A title",
                        "description": "Some text",
                        "repo": "owner/name",
                    },
                )

        assert status == 404
        assert "error" in payload

    def test_a_saved_start_racing_a_discard_is_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = tracker.store.create_draft(
                title="A title", description="Some text", repo="owner/name"
            )
            with mock.patch.object(
                tracker.store,
                "start_draft",
                side_effect=TrackerNotFoundError("gone"),
            ):
                status, payload = _post_json(
                    port,
                    f"/api/tickets/{ticket.id}/start",
                    {
                        "title": "A title",
                        "description": "Some text",
                        "repo": "owner/name",
                    },
                )

        assert status == 404
        assert "error" in payload

    def _drafts(self, tmp_path: Path) -> LocalTracker:
        return LocalTracker(tmp_path)

    def test_the_first_content_save_creates_the_draft(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _state, _wake):
            status, payload = _post_json(
                port,
                "/api/new",
                {"title": "A title", "description": "Body", "repo": "owner/name"},
            )
            stored = self._drafts(tmp_path).store.list_tickets()

        assert status == 200
        assert payload["id"] == "SYM-1"
        assert payload["url"] == "/tickets/SYM-1/edit"
        assert payload["actions"]["api_save"] == "/api/tickets/SYM-1/edit"
        assert payload["actions"]["keep"] == "/tickets/SYM-1/edit"
        assert len(stored) == 1
        assert stored[0].description == "Body"

    def test_an_empty_editor_creates_nothing(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _state, _wake):
            status, payload = _post_json(
                port, "/api/new", {"title": "", "description": "   ", "repo": ""}
            )
            stored = self._drafts(tmp_path).store.list_tickets()

        assert status == 200
        assert payload["id"] is None
        assert stored == []

    def test_saving_an_existing_draft_updates_it(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = tracker.store.create_draft(title="Old", description="old")

            status, payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/edit",
                {"title": "New", "description": "new", "repo": "o/n"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert payload["id"] == ticket.id
        assert stored is not None
        assert (stored.title, stored.description, stored.repo) == ("New", "new", "o/n")

    def test_saving_after_start_is_refused(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)

            status, payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/edit",
                {"title": "Late", "description": "late", "repo": "o/n"},
            )

        assert status == 409
        assert "error" in payload

    def test_start_reports_validation_errors(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _state, _wake):
            status, payload = _post_json(
                port, "/api/new/start", {"description": "", "repo": ""}
            )

        assert status == 400
        assert "title" in str(payload["error"]).lower()
        assert "describe" in str(payload["error"]).lower()

    def test_start_moves_the_draft_and_returns_the_ticket_url(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            status, payload = _post_json(
                port,
                "/api/new/start",
                {"title": "Go", "description": "Body", "repo": "o/n"},
            )
            stored = tracker.store.list_tickets()

        assert status == 200
        assert payload["redirect"] == "/tickets/SYM-1"
        assert len(stored) == 1
        assert stored[0].state == STATE_IN_PROGRESS
        wake.assert_called_once()

    def test_discard_deletes_the_draft(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = tracker.store.create_draft(description="x")

            status, payload = _post_json(port, f"/api/tickets/{ticket.id}/discard", {})
            remaining = tracker.store.list_tickets()

        assert status == 200
        assert payload["redirect"] == "/"
        assert remaining == []

    def test_autosave_stores_a_new_repo_url_without_adding_it(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            status, payload = _post_json(
                port,
                "/api/new",
                {
                    "description": "Body",
                    "repo": "owner/new",
                    "repo_new_url": "owner/new",
                    "repo_new_name": "Name",
                },
            )
            stored = tracker.store.get("SYM-1")
            repos = tracker.store.list_repos()

        assert status == 200
        assert payload["id"] == "SYM-1"
        assert stored is not None
        assert stored.repo == "owner/new"
        assert repos == []

    def test_api_start_adds_the_new_repository(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            status, _payload = _post_json(
                port,
                "/api/new/start",
                {
                    "title": "Title",
                    "description": "Body",
                    "repo": "",
                    "repo_new_url": "https://example.com/team/project.git",
                },
            )
            stored = tracker.store.list_tickets()
            repos = tracker.store.list_repos()

        assert status == 200
        assert [ticket.repo for ticket in stored] == [
            "https://example.com/team/project.git"
        ]
        assert [(entry.url, entry.name) for entry in repos] == [
            ("https://example.com/team/project.git", "team/project")
        ]


# ---------------------------------------------------------------------------
# The API goes through the same security choke point
# ---------------------------------------------------------------------------


class TestApiSecurity:
    def test_a_post_without_an_origin_is_forbidden(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, _payload = _json_request(
                port,
                "POST",
                f"/api/tickets/{ticket.id}/draft",
                {"text": "x"},
                origin=False,
            )
            stored = tracker.store.get(ticket.id)

        assert status == 403
        assert stored is not None
        assert stored.reply_draft == ""

    def test_a_cross_origin_post_is_forbidden(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, _headers, _body = _request(
                port,
                "POST",
                f"/api/tickets/{ticket.id}/seen",
                headers={
                    "Origin": "http://evil.example",
                    "Content-Type": "application/json",
                },
                body=b'{"comment_id": "c-1"}',
            )

        assert status == 403

    def test_a_get_needs_no_origin(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _state, _wake):
            status, payload = _json_request(port, "GET", "/api/home", origin=False)

        assert status == 200
        assert "html" in payload
