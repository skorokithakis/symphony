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
    ReplyAnswers,
)
from symphony_linear.presence import TicketPresence
from symphony_linear.state import TicketStatus
from symphony_linear.turn_activity import AgentStep, TurnActivity
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

    def test_the_needs_input_count_is_in_the_tab_title_everywhere(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            empty_title = "<title>Tickets · symphony</title>"
            assert empty_title in _home(port)

            waiting = _ticket(tracker.store, title="Waiting")
            _ticket(tracker.store, title="Also waiting")

            home = _home(port)
            ticket_page = _request(port, "GET", f"/tickets/{waiting.id}")[2].decode()
            archive_page = _request(port, "GET", "/archive")[2].decode()

        assert "<title>(2) Tickets · symphony</title>" in home
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

    def test_the_anchor_selects_the_delta(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.store.add_human_comment(ticket.id, "only comment")
            tracker.post_comment(ticket.id, "a reply", "final")

            empty = _get_json(port, f"/api/tickets/{ticket.id}?after=")
            unknown = _get_json(port, f"/api/tickets/{ticket.id}?after=c-missing")

        assert "only comment" in empty["entries_html"]
        # A reply that arrived after the anchor is still part of the delta.
        assert "a reply" in empty["entries_html"]
        # An id that no longer exists (a deleted ticket's, say) replays nothing.
        assert unknown["entries_html"].strip() == ""

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
        assert 'id="act-send"' not in data["actions_html"]
        assert "The agent is working." not in data["actions_html"]
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
            done_status, _done_payload = _post_json(
                port, f"/api/tickets/{ticket.id}/state", {"action": "done"}
            )
            done_data = _get_json(port, f"/api/tickets/{ticket.id}")

        assert status == 200
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT
        assert 'class="state state--input"' in data["state_html"]
        assert 'id="act-send"' in data["actions_html"]
        assert 'id="act-send" disabled' not in data["actions_html"]
        assert "The agent is working." not in data["actions_html"]
        assert data["working_html"] == ""
        assert done_status == 200
        assert done_data["has_composer"] is False
        assert wake.call_count == 2

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

    def test_only_an_active_poll_registers_presence(self, tmp_path: Path) -> None:
        presence = TicketPresence()
        with _server(tmp_path, presence=presence) as (port, tracker, _state, _wake):
            watched = _ticket(tracker.store, title="Watched")
            other = _ticket(tracker.store, title="Other")

            _get_json(port, f"/api/tickets/{watched.id}?after=&active=1")
            _get_json(port, f"/api/tickets/{other.id}?after=")

        assert presence.is_active(watched.id)
        assert not presence.is_active(other.id)


class TestTicketStepsPoll:
    """The running turn's steps ride in the ticket poll, cursor and all."""

    def test_a_matching_cursor_appends_and_an_unknown_one_replaces(
        self, tmp_path: Path
    ) -> None:
        activity = TurnActivity()
        with _server(tmp_path, turn_activity=activity) as (port, tracker, _s, _w):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            activity.record_step(ticket.id, AgentStep(kind="text", text="Looking."))

            # No cursor: the block's first fill.
            first = _get_json(port, f"/api/tickets/{ticket.id}")
            cursor = f"turn={first['steps_turn']}&step={first['steps_last']}"
            activity.record_step(
                ticket.id, AgentStep(kind="tool", tool="bash", text="pytest -q")
            )
            appended = _get_json(port, f"/api/tickets/{ticket.id}?{cursor}")
            unchanged = _get_json(
                port,
                f"/api/tickets/{ticket.id}?turn={appended['steps_turn']}"
                f"&step={appended['steps_last']}",
            )
            unknown = _get_json(port, f"/api/tickets/{ticket.id}?turn=nope&step=1")

        assert first["steps_replace"] is True
        assert first["steps_total"] == 1
        assert first["steps_last"] == 1
        assert first["steps_turn"]

        # The append carries only the new step, and moves the cursor.
        assert appended["steps_replace"] is False
        assert appended["steps_total"] == 2
        assert appended["steps_last"] == 2
        assert "pytest -q" in appended["steps_html"]
        assert "Looking." not in appended["steps_html"]

        # An empty append repeats the cursor instead of resetting it, so the
        # next poll does not ask for the whole window again.
        assert unchanged["steps_html"] == ""
        assert unchanged["steps_replace"] is False
        assert unchanged["steps_last"] == 2

        # A turn the page never saw replays everything and resets the cursor.
        assert unknown["steps_replace"] is True
        assert unknown["steps_turn"] == first["steps_turn"]
        assert unknown["steps_last"] == 2
        assert "Looking." in unknown["steps_html"]

    def test_no_steps_when_the_ticket_is_not_in_progress(self, tmp_path: Path) -> None:
        """Steps belong to the working line: without it there is no turn to
        watch, even if a stale record survived."""
        activity = TurnActivity()
        with _server(tmp_path, turn_activity=activity) as (port, tracker, _s, _w):
            ticket = _ticket(tracker.store, state=STATE_NEEDS_INPUT)
            activity.record_step(ticket.id, AgentStep(kind="text", text="Left over."))

            data = _get_json(port, f"/api/tickets/{ticket.id}?turn=abc&step=1")

        assert data["working_html"] == ""
        assert data["steps_html"] == ""
        assert data["steps_total"] == 0
        assert data["steps_replace"] is False

    def test_a_lone_surrogate_in_a_step_does_not_break_the_poll(
        self, tmp_path: Path
    ) -> None:
        """A tool that cuts a string mid-emoji leaves a lone surrogate, which
        json.loads accepts; the poll still has to encode its response."""
        activity = TurnActivity()
        with _server(tmp_path, turn_activity=activity) as (port, tracker, _s, _w):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            activity.record_step(
                ticket.id,
                AgentStep(kind="text", text="a cut emoji \ud83d here"),
            )
            activity.record_step(
                ticket.id,
                AgentStep(kind="tool", tool="bash\ud83d", text="ran \ud83d"),
            )

            data = _get_json(port, f"/api/tickets/{ticket.id}")

        assert data["steps_total"] == 2
        assert "\ud83d" not in data["steps_html"]
        assert "a cut emoji" in data["steps_html"]
        assert "bash" in data["steps_html"]

    def test_text_is_plain_markdown_and_tools_are_escaped(self, tmp_path: Path) -> None:
        activity = TurnActivity()
        with _server(tmp_path, turn_activity=activity) as (port, tracker, _s, _w):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            # A question callout in a step must stay a quote: question cards
            # get answer controls from questions.js, and a step is not a
            # question the human can answer.
            activity.record_step(
                ticket.id,
                AgentStep(
                    kind="text",
                    text="**Bold**\n\n> [!QUESTION] Pick\n> - a\n> - b",
                ),
            )
            activity.record_step(
                ticket.id,
                AgentStep(kind="tool", tool="<b>bash</b>", text="a & b"),
            )

            data = _get_json(port, f"/api/tickets/{ticket.id}")

        html = data["steps_html"]
        assert "<strong>Bold</strong>" in html
        assert "qcard" not in html
        assert "&lt;b&gt;bash&lt;/b&gt;" in html
        assert "a &amp; b" in html


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

    def test_the_card_answers_are_saved_with_the_draft_and_kept_when_absent(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            agent = tracker.store.add_comment(ticket.id, "agent reply", kind="final")
            answers = {
                "comment_id": agent.id,
                "questions": [
                    {
                        "question_index": 0,
                        "selected": [1],
                        "edits": {"0": "Redis only"},
                        "other": "",
                    }
                ],
            }
            path = f"/api/tickets/{ticket.id}/draft"

            with_answers, _payload = _post_json(
                port, path, {"text": "a note", "answers": answers}
            )
            saved = tracker.store.get(ticket.id)

            without_answers, _payload = _post_json(
                port, path, {"text": "a longer note"}
            )
            kept = tracker.store.get(ticket.id)

        assert with_answers == 200
        assert saved is not None
        assert saved.reply_answers == ReplyAnswers.model_validate(answers)
        # The text-only autosave must not wipe the snapshot beside it.
        assert without_answers == 200
        assert kept is not None
        assert kept.reply_draft == "a longer note"
        assert kept.reply_answers == saved.reply_answers

    def test_malformed_card_answers_are_rejected(self, tmp_path: Path) -> None:
        malformed = [
            {
                "comment_id": "c-agent",
                "questions": [{"question_index": 0, "selected": "one"}],
            },
            {"comment_id": "c-agent", "questions": "none"},
        ]
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            path = f"/api/tickets/{ticket.id}/draft"
            for answers in malformed:
                status, payload = _post_json(
                    port, path, {"text": "note", "answers": answers}
                )
                assert status == 400
                assert "error" in payload
            stored = tracker.store.get(ticket.id)

        assert stored is not None
        assert stored.reply_answers is None

    def test_oversized_card_answers_are_rejected(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, payload = _post_json(
                port,
                f"/api/tickets/{ticket.id}/draft",
                {
                    "text": "note",
                    "answers": {
                        "comment_id": "c-agent",
                        "questions": [
                            {"question_index": 0, "other": "x" * 200_001},
                        ],
                    },
                },
            )
            stored = tracker.store.get(ticket.id)

        assert status == 413
        assert "error" in payload
        assert stored is not None
        assert stored.reply_answers is None
        assert stored.reply_draft == ""

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

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"token": "send-token"}, id="send-token"),
            pytest.param({}, id="no-token"),
        ],
    )
    def test_retry_requires_its_own_token(
        self, tmp_path: Path, payload: dict[str, Any]
    ) -> None:
        """A Send token, or none at all, must not post anything."""
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store)
            state.upsert(_state_entry(ticket.id, status=TicketStatus.failed))
            status, _payload = _post_json(
                port, f"/api/tickets/{ticket.id}/retry", payload
            )
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
    @pytest.mark.parametrize(
        "store_method",
        [
            pytest.param("create_started_draft", id="new"),
            pytest.param("start_draft", id="saved"),
        ],
    )
    def test_a_start_racing_a_delete_is_404(
        self, tmp_path: Path, store_method: str
    ) -> None:
        body = {"title": "A title", "description": "Some text", "repo": "owner/name"}
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = tracker.store.create_draft(
                title="A title", description="Some text", repo="owner/name"
            )
            path = (
                "/api/new/start"
                if store_method == "create_started_draft"
                else f"/api/tickets/{ticket.id}/start"
            )
            with mock.patch.object(
                tracker.store,
                store_method,
                side_effect=TrackerNotFoundError("gone"),
            ):
                status, error = _post_json(port, path, body)

        assert status == 404
        assert "error" in error

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
