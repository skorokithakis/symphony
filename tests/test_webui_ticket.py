"""Tests for the ticket page, its reply composer and its state actions.

The server runs for real on an ephemeral port, reusing the fixtures of
``tests.test_webui_server``.  Comments are created through ``LocalTracker`` so
the footer and the stored ``kind`` match what the orchestrator writes.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

from symphony_linear.local_tracker import (
    STATE_DONE,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    LocalTicket,
    LocalTicketStore,
    LocalTracker,
)
from symphony_linear.state import StateManager, load_state
from symphony_linear.tracker import TransitionTarget
from symphony_linear.webui.server import WebUIServer
from tests.test_webui_server import (
    _config,
    _origin,
    _page_body,
    _request,
    _state_entry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _server(
    tmp_path: Path,
) -> Iterator[tuple[int, LocalTracker, StateManager, Mock]]:
    tracker = LocalTracker(tmp_path)
    state = load_state(tmp_path)
    wake = Mock()
    server = WebUIServer(
        store=tracker.store,
        state=state,
        config=_config(),
        on_wake=wake,
        port=0,
    )
    server.start()
    try:
        port = server.bound_port
        assert port is not None
        yield port, tracker, state, wake
    finally:
        server.stop()


def _ticket(
    store: LocalTicketStore,
    *,
    state: str = STATE_NEEDS_INPUT,
    title: str = "A ticket",
    description: str = "Ticket **text**",
    repo: str = "https://github.com/stavros/symphony.git",
    model: str | None = "sonnet-4.5",
) -> LocalTicket:
    ticket = store.create_draft(
        title=title, description=description, repo=repo, model=model
    )
    store.set_state(ticket.id, state)
    return ticket


def _post(
    port: int,
    path: str,
    fields: dict[str, str],
    *,
    origin: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin:
        headers["Origin"] = _origin(port)
    return _request(
        port, "POST", path, headers=headers, body=urlencode(fields).encode()
    )


def _page(port: int, ticket_id: str) -> str:
    status, _headers, body = _request(port, "GET", f"/tickets/{ticket_id}")
    assert status == 200
    return body.decode()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestTicketPage:
    def test_header_and_details_use_the_state_entry(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store)
            state.upsert(
                _state_entry(
                    ticket.id,
                    branch="symphony/sym-1",
                    workspace_path="/tmp/workspaces/sym-1",
                    session_id="0f3c9a1e",
                )
            )

            text = _page(port, ticket.id)

        assert 'class="tid">SYM-1</span>' in text
        assert "A ticket" in text
        assert "stavros/symphony" in text
        assert "state--input" in text
        assert "Needs input" in text
        assert "symphony/sym-1" in text
        assert "sonnet-4.5" in text
        assert "/tmp/workspaces/sym-1" in text
        assert "0f3c9a1e" in text
        assert 'data-copy="/tmp/workspaces/sym-1"' in text
        assert 'data-copy="0f3c9a1e"' in text

    def test_missing_details_are_tolerated(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, model=None)

            status, _headers, body = _request(port, "GET", f"/tickets/{ticket.id}")

        text = body.decode()
        assert status == 200
        assert "Default" in text
        assert "—" in text
        assert "data-copy" not in text

    def test_all_comment_kinds_render_as_their_entry_type(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(
                tracker.store,
                description="The description with a [link](https://example.com).",
            )
            tracker.post_comment(
                ticket.id,
                "**Symphony**\n- workspace: `/tmp/secret-workspace`\n"
                "- session: _pending_",
                "workspace",
            )
            tracker.store.add_human_comment(ticket.id, "A human reply")
            tracker.post_comment(
                ticket.id,
                "# Summary\n\nThe reply body.",
                "context: 37,074 tokens · model: sonnet-4.5",
            )
            tracker.post_comment(
                ticket.id,
                "**Symphony error**: The AI turn failed:\n```\nboom\n```",
                "error",
            )
            tracker.post_comment(
                ticket.id,
                "**Workspace not clean — I did not delete it.**\n\n"
                "This ticket has work:\n\n```\n M uncommitted.txt\n```\n\n"
                "I moved the ticket back to **Needs Input**.",
                "cleanup",
            )
            tracker.post_comment(
                ticket.id,
                "**Symphony**: Restarted — re-running the last turn "
                "(attempt 1 of 3), no reply needed.",
                "restart",
            )
            tracker.post_comment(
                ticket.id,
                "**Restored**\n- workspace: `/tmp/secret-workspace`\n- session: `abc`",
                "workspace",
            )

            text = _page(port, ticket.id)

        # The description is the first user entry and is rendered Markdown.
        assert (
            '<p>The description with a <a href="https://example.com">link</a>.</p>'
            in text
        )
        assert text.index("Ticket text") < text.index("A human reply")
        assert text.index("A human reply") < text.index("The reply body.")

        # Agent reply: sectioned Markdown plus the tokens and model metadata.
        assert "entry--agent" in text
        assert '<details class="sec">' in text
        assert "37,074 tokens" in text
        assert "sonnet-4.5" in text
        assert "Open all" in text
        assert "Close all" in text

        # Error and warning alerts, and the notice variants.
        assert "alert--error" in text
        assert "Symphony error" in text
        assert "boom" in text
        assert "alert--warn" in text
        assert "uncommitted.txt" in text
        assert "Session restored" in text
        assert "Workspace ready" in text
        assert "Restarted — re-running the last turn" in text

        # The workspace metadata comment is a notice, not a printed body, and
        # no footer leaks into the page.
        assert "/tmp/secret-workspace" not in text
        assert "*Symphony ·" not in text
        assert text.count('<p class="notice">') == 3

    def test_long_error_is_behind_show_details(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.post_comment(
                ticket.id,
                "**Symphony error**: The AI turn failed:\n```\n"
                + "\n".join(f"trace line {n}" for n in range(20))
                + "\n```",
                "error",
            )
            text = _page(port, ticket.id)

        assert "Show details" in text
        assert "trace line 19" in text

    def test_short_error_shows_the_body_without_a_toggle(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.post_comment(
                ticket.id,
                "**Symphony error**: The AI turn timed out (idle 1200s).",
                "error",
            )
            text = _page(port, ticket.id)

        assert "The AI turn timed out" in text
        assert "Show details" not in text

    def test_final_kind_with_model_is_an_agent_reply(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            tracker.post_comment(ticket.id, "Done.", "final · model: opus-4")
            text = _page(port, ticket.id)

        assert "entry--agent" in text
        assert "opus-4" in text
        assert "Open all" in text

    def test_description_is_the_first_thread_entry(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, description="Only the description")
            tracker.store.add_human_comment(ticket.id, "Second")
            text = _page(port, ticket.id)

        assert text.index("Ticket text") < text.index("Second")
        assert 'class="who">Ticket text' in text

    def test_draft_redirects_to_the_editor(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = tracker.store.create_draft(description="draft")
            status, headers, _body = _request(port, "GET", f"/tickets/{ticket.id}")

        assert status == 303
        assert headers["Location"] == f"/tickets/{ticket.id}/edit"

    def test_done_page_has_no_composer_and_offers_reopen(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_DONE)
            text = _page(port, ticket.id)

        assert 'class="composer"' not in text
        assert 'class="readonly"' in text
        assert 'value="reopen"' in text
        assert "Reopen" in text
        assert "state--done" in text

    def test_in_progress_page_shows_working_and_a_disabled_send(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            text = _page(port, ticket.id)

        assert 'class="working"' in text
        assert "Working, " in text
        assert 'id="act-send" disabled' in text
        assert "The agent is working. You can send when it stops." in text
        assert 'value="stop"' in text
        assert "state--progress" in text

    def test_needs_input_page_offers_send_qa_and_done(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            text = _page(port, ticket.id)

        assert 'class="composer"' in text
        assert 'name="token" value="' in text
        assert "Send to QA" in text
        assert "Done" in text
        assert 'id="act-send"' in text

    def test_qa_page_offers_back_to_needs_input(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_QA)
            text = _page(port, ticket.id)

        assert "state--qa" in text
        assert "Back to Needs Input" in text
        assert "Send to QA" not in _page_body(text)

    @pytest.mark.parametrize(
        ("state", "buttons"),
        [
            (STATE_IN_PROGRESS, {"act-stop": ("s1", "Stop (s 1)")}),
            (
                STATE_NEEDS_INPUT,
                {
                    "act-done": ("s3", "Done (s 3)"),
                    "act-qa": ("s2", "Send to QA (s 2)"),
                },
            ),
            (
                STATE_QA,
                {
                    "act-done": ("s3", "Done (s 3)"),
                    "act-needs-input": ("s1", "Back to Needs Input (s 1)"),
                },
            ),
            (STATE_DONE, {"act-reopen": ("s1", "Reopen (s 1)")}),
        ],
    )
    def test_each_state_action_carries_its_keyboard_shortcut(
        self, tmp_path: Path, state: str, buttons: dict[str, tuple[str, str]]
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=state)
            text = _page(port, ticket.id)

        for button_id, (shortcut, title) in buttons.items():
            tag = text.split(f'id="{button_id}"', 1)[1].split(">", 1)[0]
            assert f'data-shortcut="{shortcut}"' in tag
            assert f'title="{title}"' in tag

    def test_unknown_ticket_is_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _state, _wake):
            status, _headers, _body = _request(port, "GET", "/tickets/SYM-404")

        assert status == 404

    def test_head_is_served_from_the_get_view(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, headers, body = _request(port, "HEAD", f"/tickets/{ticket.id}")

        assert status == 200
        assert body == b""
        assert int(headers["Content-Length"]) > 0


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    def test_send_adds_a_human_comment_without_changing_the_state(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            ticket = _ticket(tracker.store)

            status, headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/reply",
                {"body": "  a reply  ", "token": "token-1"},
            )

            stored = tracker.store.get(ticket.id)
            assert stored is not None
            assert status == 303
            assert headers["Location"] == f"/tickets/{ticket.id}"
            assert stored.state == STATE_NEEDS_INPUT
            assert [(c.body, c.kind) for c in stored.comments] == [("a reply", None)]
            assert stored.accepted_send_tokens == ["token-1"]
            wake.assert_called_once()

    def test_a_reused_token_does_not_post_twice(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            path = f"/tickets/{ticket.id}/reply"

            first = _post(port, path, {"body": "hello", "token": "same"})
            second = _post(port, path, {"body": "hello again", "token": "same"})
            third = _post(port, path, {"body": "new", "token": "other"})

            stored = tracker.store.get(ticket.id)

        assert first[0] == second[0] == third[0] == 303
        assert stored is not None
        assert [comment.body for comment in stored.comments] == ["hello", "new"]

    def test_send_is_refused_outside_needs_input_and_qa(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            in_progress = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            done = _ticket(tracker.store, state=STATE_DONE)
            statuses = [
                _post(
                    port,
                    f"/tickets/{ticket.id}/reply",
                    {"body": "hello", "token": f"t-{ticket.id}"},
                )[0]
                for ticket in (in_progress, done)
            ]

        assert statuses == [409, 409]

    def test_send_from_qa_keeps_the_ticket_in_qa(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_QA)
            status, _headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/reply",
                {"body": "hello", "token": "token-qa"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert stored is not None
        assert stored.state == STATE_QA

    def test_empty_reply_is_a_bad_request(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, _headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/reply",
                {"body": "   ", "token": "token"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 400
        assert stored is not None
        assert stored.comments == []

    def test_missing_token_is_a_bad_request(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, _headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/reply",
                {"body": "hello"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 400
        assert stored is not None
        assert stored.comments == []

    def test_cross_origin_post_is_forbidden(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, _headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/reply",
                {"body": "hello", "token": "token"},
                origin=False,
            )
            stored = tracker.store.get(ticket.id)

        assert status == 403
        assert stored is not None
        assert stored.comments == []


# ---------------------------------------------------------------------------
# State actions
# ---------------------------------------------------------------------------


class TestStateActions:
    def test_needs_input_actions(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            ticket = _ticket(tracker.store)
            path = f"/tickets/{ticket.id}/state"

            status, _headers, _body = _post(port, path, {"action": "qa"})
            after_qa = tracker.store.get(ticket.id)
            assert status == 303
            assert after_qa is not None
            assert after_qa.state == STATE_QA
            assert after_qa.comments == []

            tracker.store.set_state(ticket.id, STATE_NEEDS_INPUT)
            status, _headers, _body = _post(port, path, {"action": "done"})
            after_done = tracker.store.get(ticket.id)

        assert status == 303
        assert after_done is not None
        assert after_done.state == STATE_DONE
        assert wake.call_count == 2

    def test_qa_actions(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store, state=STATE_QA)
            path = f"/tickets/{ticket.id}/state"

            status, _headers, _body = _post(port, path, {"action": "needs_input"})
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT

    def test_stop_moves_in_progress_to_needs_input(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            ticket = _ticket(tracker.store, state=STATE_IN_PROGRESS)

            status, _headers, _body = _post(
                port, f"/tickets/{ticket.id}/state", {"action": "stop"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT
        wake.assert_called_once()

    def test_reopen_without_a_state_entry_moves_done_to_in_progress(
        self, tmp_path: Path
    ) -> None:
        """No daemon entry: the new-ticket pipeline must pick the ticket up.

        With Needs Input the composer would accept a reply before the daemon
        has restored or created a session, and the fresh-ticket path would
        never deliver it to the agent.
        """
        with _server(tmp_path) as (port, tracker, _state, wake):
            ticket = _ticket(tracker.store, state=STATE_DONE)

            status, _headers, _body = _post(
                port, f"/tickets/{ticket.id}/state", {"action": "reopen"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert stored is not None
        assert stored.state == STATE_IN_PROGRESS
        wake.assert_called_once()

    def test_reopen_with_a_state_entry_moves_done_to_needs_input(
        self, tmp_path: Path
    ) -> None:
        """The daemon still tracks the ticket: do not strand it in In Progress.

        Needs Input lets a reply resume the normal session path.  A second
        Done/Reopen while the entry still exists therefore must not leave the
        page showing "working" with nothing running.
        """
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket = _ticket(tracker.store, state=STATE_DONE)
            state.upsert(_state_entry(ticket.id))

            status, _headers, _body = _post(
                port, f"/tickets/{ticket.id}/state", {"action": "reopen"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT

    def test_an_action_from_the_wrong_state_is_409(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            needs_input = _ticket(tracker.store)
            done = _ticket(tracker.store, state=STATE_DONE)
            in_progress = _ticket(tracker.store, state=STATE_IN_PROGRESS)

            cases = [
                (needs_input, "stop"),
                (needs_input, "reopen"),
                (done, "done"),
                (done, "stop"),
                (in_progress, "qa"),
                (in_progress, "done"),
            ]
            statuses = [
                _post(
                    port,
                    f"/tickets/{ticket.id}/state",
                    {"action": action},
                )[0]
                for ticket, action in cases
            ]

        assert statuses == [409] * len(cases)

    def test_an_unknown_action_is_refused(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, _headers, _body = _post(
                port, f"/tickets/{ticket.id}/state", {"action": "explode"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 409
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT

    def test_state_action_on_an_unknown_ticket_is_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _state, _wake):
            status, _headers, _body = _post(
                port, "/tickets/SYM-404/state", {"action": "done"}
            )

        assert status == 404

    def test_state_action_requires_same_origin(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            status, _headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/state",
                {"action": "done"},
                origin=False,
            )
            stored = tracker.store.get(ticket.id)

        assert status == 403
        assert stored is not None
        assert stored.state == STATE_NEEDS_INPUT

    def test_get_does_not_change_the_store(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = _ticket(tracker.store)
            ticket_path = tmp_path / "tickets" / f"{ticket.id}.json"
            before = ticket_path.read_bytes()

            _page(port, ticket.id)
            _page(port, ticket.id)

            after = ticket_path.read_bytes()

        assert before == after
        assert json.loads(before)["state"] == STATE_NEEDS_INPUT


# ---------------------------------------------------------------------------
# Store integration sanity
# ---------------------------------------------------------------------------


def test_a_freshly_reopened_ticket_refuses_a_reply_until_the_daemon_picks_it_up(
    tmp_path: Path,
) -> None:
    """Done -> In Progress leaves Send unavailable, so no reply is stranded."""
    with _server(tmp_path) as (port, tracker, _state, _wake):
        ticket = _ticket(tracker.store, state=STATE_DONE)

        _post(port, f"/tickets/{ticket.id}/state", {"action": "reopen"})
        status, _headers, _body = _post(
            port,
            f"/tickets/{ticket.id}/reply",
            {"body": "please continue", "token": "after-reopen"},
        )

        stored = tracker.store.get(ticket.id)

    assert status == 409
    assert stored is not None
    assert stored.state == STATE_IN_PROGRESS
    assert stored.comments == []


def test_a_reopened_ticket_accepts_a_reply_once_the_daemon_reaches_needs_input(
    tmp_path: Path,
) -> None:
    """The orchestrator's restore/first-turn path ends in Needs Input."""
    with _server(tmp_path) as (port, tracker, _state, _wake):
        ticket = _ticket(tracker.store, state=STATE_DONE)

        _post(port, f"/tickets/{ticket.id}/state", {"action": "reopen"})
        # What _new_ticket_pipeline does at the end of either path.
        tracker.transition_to(ticket.id, TransitionTarget.needs_input)
        status, _headers, _body = _post(
            port,
            f"/tickets/{ticket.id}/reply",
            {"body": "please continue", "token": "after-reopen"},
        )

        stored = tracker.store.get(ticket.id)

    assert status == 303
    assert stored is not None
    assert stored.state == STATE_NEEDS_INPUT
    assert [comment.body for comment in stored.comments] == ["please continue"]
