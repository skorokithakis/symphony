"""Tests for the bearer-authenticated /api/v1 agent API.

The server runs for real on an ephemeral port, like the other web UI tests.
The point of these tests is the contract an external agent relies on: the
guide covers every route, the error shape and hints, the state rules the
endpoints reuse from the browser UI, and the mutation side effects (repository
list, wake callback, no state change on a comment).
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import Mock

import pytest

from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import (
    STATE_DRAFT,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    TARGET_REPLY,
    LocalTicketStore,
    LocalTracker,
)
from symphony_linear.state import StateManager, TicketStatus, load_state
from symphony_linear.webui import agent_api
from symphony_linear.webui.routes import ROUTES
from symphony_linear.webui.server import WebUIServer
from tests.test_webui_server import _request, _state_entry, _write_ticket

_TOKEN = "agent-token-123"
_PASSWORD = "secret"

_GUIDE_PATH = Path(agent_api.__file__).resolve().parent / "agent_api.md"

#: A route pattern's one path parameter, for turning the compiled pattern back
#: into the placeholder form the guide uses.
_PATH_PARAM = re.compile(r"\(\?P<(\w+)>\[\^/\]\+\)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    api_token: str | None = _TOKEN,
    models: dict[str, str] | None = None,
) -> AppConfig:
    block: dict[str, object] = {
        "port": 8080,
        "password": _PASSWORD,
    }
    if api_token is not None:
        block["api_token"] = api_token
    return AppConfig.model_validate({"webui": block, "models": models or {}})


@contextmanager
def _server(
    tmp_path: Path,
    *,
    api_token: str | None = _TOKEN,
    models: dict[str, str] | None = None,
) -> Iterator[tuple[int, LocalTracker, StateManager, Mock]]:
    tracker = LocalTracker(tmp_path)
    state = load_state(tmp_path)
    wake = Mock()
    server = WebUIServer(
        store=tracker.store,
        state=state,
        config=_config(api_token=api_token, models=models),
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


def _call(
    port: int,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    token: str | None = _TOKEN,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Make one API request and decode its JSON (if any) response."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload).encode()
    elif method in ("POST", "PATCH"):
        body = b"{}"
    status, response_headers, raw = _request(
        port, method, path, auth=False, headers=headers, body=body
    )
    decoded = json.loads(raw) if raw else {}
    assert isinstance(decoded, dict)
    return status, decoded, response_headers


def _ticket(store: LocalTicketStore, *, state: str = STATE_NEEDS_INPUT) -> str:
    ticket = store.create_draft(
        title="A ticket",
        description="Do the thing.",
        repo="acme/widgets",
    )
    if state != STATE_DRAFT:
        store.set_state(ticket.id, state)
    return ticket.id


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------


class TestGuide:
    def test_every_api_route_is_mentioned_in_the_guide(self) -> None:
        text = _GUIDE_PATH.read_text(encoding="utf-8")
        documented: list[str] = []
        for route in ROUTES:
            pattern = route.pattern.pattern
            if "/api/v1" not in pattern:
                continue
            path = _PATH_PARAM.sub(r"{\1}", pattern.strip("^$"))
            documented.append(f"{route.method} {path}")

        assert documented
        for entry in documented:
            assert entry in text, f"{entry!r} is missing from agent_api.md"

    def test_the_guide_is_served_as_markdown(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _state, _wake):
            status, headers, body = _request(
                port,
                "GET",
                "/api/v1",
                auth=False,
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )

        assert status == 200
        assert headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert body == _GUIDE_PATH.read_bytes()

    def test_the_guide_is_served_without_a_token(self, tmp_path: Path) -> None:
        # The reason the exemption exists: an agent reads the guide before it
        # has been given the key.  The guide must therefore not document
        # itself as requiring one.
        with _server(tmp_path) as (port, _tracker, _state, _wake):
            status, _headers, body = _request(port, "GET", "/api/v1", auth=False)

        assert status == 200
        assert body == _GUIDE_PATH.read_bytes()
        assert "needs no token" in body.decode()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    def test_an_unknown_api_path_gets_a_json_hint(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _t, _s, _wake):
            status, payload, _headers = _call(port, "GET", "/api/v1/nope")

        assert status == 404
        assert set(payload) == {"error", "hint"}
        assert "GET /api/v1" in payload["hint"]

    def test_a_wrong_method_gets_a_json_405_with_allow(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _t, _s, _wake):
            status, payload, headers = _call(port, "PUT", "/api/v1/tickets")

        assert status == 405
        assert "GET" in headers["Allow"]
        assert "POST" in headers["Allow"]
        assert "hint" in payload

    def test_a_body_over_the_cap_gets_a_json_413(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _t, _s, _wake):
            status, headers, body = _request(
                port,
                "POST",
                "/api/v1/tickets",
                auth=False,
                headers={"Authorization": f"Bearer {_TOKEN}"},
                body=b"x" * (1024 * 1024 + 1),
            )

        assert status == 413
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert set(json.loads(body)) == {"error", "hint"}

    def test_an_unknown_ticket_gets_a_json_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _t, _s, _wake):
            status, payload, _headers = _call(port, "GET", "/api/v1/tickets/SYM-99")

        assert status == 404
        assert "GET /api/v1/tickets" in payload["hint"]


# ---------------------------------------------------------------------------
# Listing and detail
# ---------------------------------------------------------------------------


class TestTicketsRead:
    def test_list_filters_by_state_and_is_newest_first(self, tmp_path: Path) -> None:
        _write_ticket(
            tmp_path,
            "SYM-1",
            state=STATE_NEEDS_INPUT,
            updated_at="2026-11-20T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-2",
            state=STATE_NEEDS_INPUT,
            updated_at="2026-11-20T11:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-3",
            state=STATE_IN_PROGRESS,
            updated_at="2026-11-20T12:00:00+00:00",
        )

        with _server(tmp_path) as (port, _t, _s, _wake):
            status, payload, _headers = _call(
                port, "GET", "/api/v1/tickets?state=Needs%20Input"
            )

        assert status == 200
        assert [ticket["id"] for ticket in payload["tickets"]] == ["SYM-2", "SYM-1"]
        assert payload["tickets"][0]["allowed_actions"] == ["qa", "done", "reply"]

    def test_list_filters_strictly_by_updated_since(self, tmp_path: Path) -> None:
        update = "2026-11-20T10:00:00+00:00"
        _write_ticket(
            tmp_path,
            "SYM-1",
            state=STATE_NEEDS_INPUT,
            updated_at="2026-11-20T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-2",
            state=STATE_IN_PROGRESS,
            updated_at="2026-11-20T12:00:00+00:00",
        )

        with _server(tmp_path) as (port, _t, _s, _wake):
            status, payload, _headers = _call(
                port,
                "GET",
                f"/api/v1/tickets?updated_since={update.replace('+', '%2B')}",
            )

        assert status == 200
        assert [ticket["id"] for ticket in payload["tickets"]] == ["SYM-2"]

    def test_unknown_state_and_bad_timestamp_are_400s(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _t, _s, _wake):
            bad_state = _call(port, "GET", "/api/v1/tickets?state=Nope")
            bad_time = _call(port, "GET", "/api/v1/tickets?updated_since=yesterday")

        assert bad_state[0] == 400
        assert "Needs Input" in bad_state[1]["hint"]
        assert bad_time[0] == 400
        assert "ISO-8601" in bad_time[1]["hint"]

    def test_detail_carries_comments_and_read_only_agent_status(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, state, _wake):
            ticket_id = _ticket(tracker.store)
            tracker.post_comment(ticket_id, "All done.\n", "final")
            tracker.store.add_human_comment(ticket_id, "Thanks.")
            state.upsert(_state_entry(ticket_id, status=TicketStatus.working))

            status, payload, _headers = _call(
                port, "GET", f"/api/v1/tickets/{ticket_id}"
            )

        assert status == 200
        assert payload["description"] == "Do the thing."
        assert payload["agent_status"] == "working"
        assert [comment["author"] for comment in payload["comments"]] == [
            "agent",
            "human",
        ]
        assert payload["comments"][0]["body"].endswith("*")
        assert [comment["kind"] for comment in payload["comments"]] == [
            "final",
            None,
        ]
        # A GET must not advance the seen marker or otherwise write.
        assert state.tickets[0].status == TicketStatus.working

    def test_comment_kind_parses_the_model_and_token_footer(
        self, tmp_path: Path
    ) -> None:
        kind = "context: 1,234 tokens · model: anthropic/claude-x"
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket_id = _ticket(tracker.store)
            tracker.post_comment(ticket_id, "Working.\n", kind)
            tracker.store.add_human_comment(ticket_id, "Thanks.")

            status, payload, _headers = _call(
                port, "GET", f"/api/v1/tickets/{ticket_id}"
            )

        assert status == 200
        assert payload["comments"][0]["kind"] == kind
        assert payload["comments"][0]["body"].endswith("*Symphony · " + kind + "*")
        assert payload["comments"][1]["kind"] is None


# ---------------------------------------------------------------------------
# Creating and editing tickets
# ---------------------------------------------------------------------------


class TestTicketsWrite:
    def test_create_draft_then_patch_then_start(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            created = _call(
                port,
                "POST",
                "/api/v1/tickets",
                {
                    "description": "Do the thing.",
                    "repo": "acme/widgets",
                    "title": "A title",
                },
            )
            ticket_id = created[1]["id"]
            # A Draft is not added to the managed repository list.
            assert tracker.store.list_repos() == []
            wake.assert_not_called()

            patched = _call(
                port,
                "PATCH",
                f"/api/v1/tickets/{ticket_id}",
                {"title": "Renamed", "description": "Do the other thing."},
            )
            started = _call(port, "POST", f"/api/v1/tickets/{ticket_id}/start")
            stored = tracker.store.get(ticket_id)
            repos = tracker.store.list_repos()

        assert created[0] == 201
        assert created[1]["state"] == STATE_DRAFT
        assert created[1]["allowed_actions"] == ["edit", "start", "delete"]
        assert patched[1]["title"] == "Renamed"
        assert patched[1]["description"] == "Do the other thing."
        assert started[0] == 200
        assert started[1]["state"] == STATE_IN_PROGRESS
        assert started[1]["allowed_actions"] == ["stop"]
        assert stored is not None and stored.state == STATE_IN_PROGRESS
        # Start reads the stored fields under the store lock, so the PATCH is
        # what the agent will see rather than an earlier snapshot.
        assert stored.title == "Renamed"
        assert stored.description == "Do the other thing."
        # Start adds the repository to the managed list, exactly like the
        # editor.
        assert [(entry.url, entry.name) for entry in repos] == [
            ("acme/widgets", "acme/widgets")
        ]
        wake.assert_called_once()

    def test_start_is_offered_only_when_the_draft_would_start(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = tracker.store.create_draft(title="Has a title")
            before = _call(port, "GET", f"/api/v1/tickets/{ticket.id}")
            patched = _call(
                port,
                "PATCH",
                f"/api/v1/tickets/{ticket.id}",
                {"description": "Do the thing.", "repo": "acme/widgets"},
            )

        # Without a description or repository, Start would be refused, so it
        # is not advertised.
        assert before[1]["allowed_actions"] == ["edit", "delete"]
        assert patched[1]["allowed_actions"] == ["edit", "start", "delete"]

    def test_start_validates_the_stored_ticket_under_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket = tracker.store.create_draft(
                title="A title",
                description="Do the thing.",
                repo="acme/widgets",
            )
            # The endpoint's read will see this complete snapshot, while the
            # store holds a Draft that lost its repository.
            snapshot = ticket.model_copy(update={"repo": "acme/widgets"})
            tracker.store.update_draft_fields(ticket.id, {"repo": ""})

            original_get = LocalTicketStore.get

            def stale_get(self: LocalTicketStore, ticket_id: str) -> Any:
                if ticket_id == ticket.id:
                    return snapshot
                return original_get(self, ticket_id)

            monkeypatch.setattr(LocalTicketStore, "get", stale_get)
            status, payload, _headers = _call(
                port, "POST", f"/api/v1/tickets/{ticket.id}/start"
            )
            stored = original_get(tracker.store, ticket.id)

        assert status == 400
        assert "repository" in payload["error"].lower()
        assert stored is not None and stored.state == STATE_DRAFT

    def test_start_clears_an_alias_that_is_no_longer_configured(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path, models={"strong": "anthropic/claude-opus-5"}) as (
            port,
            tracker,
            _state,
            _wake,
        ):
            ticket = tracker.store.create_draft(
                title="A title",
                description="Do the thing.",
                repo="acme/widgets",
                model="gone",
            )
            status, payload, _headers = _call(
                port, "POST", f"/api/v1/tickets/{ticket.id}/start"
            )
            stored = tracker.store.get(ticket.id)

        assert status == 200
        assert payload["state"] == STATE_IN_PROGRESS
        assert stored is not None
        assert stored.model is None

    def test_create_and_start_in_one_call(self, tmp_path: Path) -> None:
        with _server(
            tmp_path, models={"sonnet-4.5": "anthropic/claude-sonnet-4-5"}
        ) as (port, tracker, _state, wake):
            status, payload, _headers = _call(
                port,
                "POST",
                "/api/v1/tickets",
                {
                    "description": "Do the thing.",
                    "repo": "acme/widgets",
                    "title": "A title",
                    "model": "sonnet-4.5",
                    "start": True,
                },
            )
            stored = tracker.store.get(payload["id"])

        assert status == 201
        assert payload["state"] == STATE_IN_PROGRESS
        assert stored is not None
        assert stored.model == "sonnet-4.5"
        wake.assert_called_once()

    def test_start_requires_a_title_without_automatic_titles(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            status, payload, _headers = _call(
                port,
                "POST",
                "/api/v1/tickets",
                {
                    "description": "Do the thing.",
                    "repo": "acme/widgets",
                    "start": True,
                },
            )
            tickets = tracker.store.list_tickets()

        assert status == 400
        assert "title" in payload["error"].lower()
        assert tickets == []

    def test_an_unknown_model_alias_is_rejected(self, tmp_path: Path) -> None:
        with _server(tmp_path, models={"strong": "anthropic/claude-opus-5"}) as (
            port,
            _t,
            _s,
            _wake,
        ):
            status, payload, _headers = _call(
                port,
                "POST",
                "/api/v1/tickets",
                {"description": "x", "repo": "acme/widgets", "model": "nope"},
            )

        assert status == 400
        assert "strong" in payload["hint"]

    def test_a_non_draft_cannot_be_edited_started_or_deleted(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket_id = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            edited = _call(
                port, "PATCH", f"/api/v1/tickets/{ticket_id}", {"title": "x"}
            )
            started = _call(port, "POST", f"/api/v1/tickets/{ticket_id}/start")
            deleted = _call(port, "DELETE", f"/api/v1/tickets/{ticket_id}")

        for status, payload, _headers in (edited, started, deleted):
            assert status == 409
            assert payload["error"]
            assert payload["hint"]

    def test_delete_removes_a_draft(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket_id = _ticket(tracker.store, state=STATE_DRAFT)
            status, payload, _headers = _call(
                port, "DELETE", f"/api/v1/tickets/{ticket_id}"
            )
            remaining = tracker.store.list_tickets()

        assert status == 200
        assert payload == {"deleted": ticket_id}
        assert remaining == []


# ---------------------------------------------------------------------------
# Comments and actions
# ---------------------------------------------------------------------------


class TestWorkLoop:
    def test_a_comment_is_refused_while_working_and_accepted_in_needs_input(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            ticket_id = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            refused = _call(
                port,
                "POST",
                f"/api/v1/tickets/{ticket_id}/comments",
                {"body": "Please continue."},
            )
            wake.assert_not_called()

            tracker.store.set_state(ticket_id, STATE_NEEDS_INPUT)
            accepted = _call(
                port,
                "POST",
                f"/api/v1/tickets/{ticket_id}/comments",
                {"body": "Please continue."},
            )
            stored = tracker.store.get(ticket_id)

        assert refused[0] == 409
        assert "Needs Input" in refused[1]["hint"]
        assert accepted[0] == 201
        assert accepted[1]["author"] == "human"
        assert accepted[1]["body"] == "Please continue."
        assert stored is not None
        assert [comment.body for comment in stored.comments] == ["Please continue."]
        # Posting a comment never transitions the ticket itself.
        assert stored.state == STATE_NEEDS_INPUT
        wake.assert_called_once()

    def test_an_api_reply_does_not_consume_the_browser_composer(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket_id = _ticket(tracker.store, state=STATE_NEEDS_INPUT)
            tracker.store.set_reply_draft(ticket_id, "half-written browser note")
            upload = tracker.store.new_upload(
                ticket_id, "shot.png", target=TARGET_REPLY
            )
            upload.path.write_bytes(b"png")
            tracker.store.register_file(upload, 3)

            status, payload, _headers = _call(
                port,
                "POST",
                f"/api/v1/tickets/{ticket_id}/comments",
                {"body": "API reply."},
            )
            stored = tracker.store.get(ticket_id)

        assert status == 201
        assert payload["body"] == "API reply."
        assert stored is not None
        # The browser's unsent text and attachment are not this caller's to
        # consume; only the browser's Send clears them.
        assert stored.reply_draft == "half-written browser note"
        assert [record.original_name for record in stored.pending_files] == ["shot.png"]
        assert stored.comments[-1].files == []

    def test_an_action_uses_the_ticket_page_transition_table(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, wake):
            ticket_id = _ticket(tracker.store, state=STATE_NEEDS_INPUT)
            status, payload, _headers = _call(
                port,
                "POST",
                f"/api/v1/tickets/{ticket_id}/actions",
                {"action": "qa"},
            )
            stored = tracker.store.get(ticket_id)

        assert status == 200
        assert payload["state"] == STATE_QA
        assert payload["allowed_actions"] == ["needs_input", "done", "reply"]
        assert stored is not None and stored.state == STATE_QA
        wake.assert_called_once()

    def test_an_unavailable_action_lists_the_allowed_ones(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket_id = _ticket(tracker.store, state=STATE_IN_PROGRESS)
            status, payload, _headers = _call(
                port,
                "POST",
                f"/api/v1/tickets/{ticket_id}/actions",
                {"action": "done"},
            )

        assert status == 409
        assert "stop" in payload["hint"]


# ---------------------------------------------------------------------------
# Repositories and models
# ---------------------------------------------------------------------------


class TestReposAndModels:
    def test_repos_add_rename_and_delete_leave_tickets_alone(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _state, _wake):
            ticket_id = tracker.store.create_draft(
                description="x", repo="acme/widgets"
            ).id

            added = _call(
                port,
                "POST",
                "/api/v1/repos",
                {"url": "acme/widgets", "name": "Widgets"},
            )
            listed = _call(port, "GET", "/api/v1/repos")
            renamed = _call(
                port,
                "PATCH",
                "/api/v1/repos",
                {"url": "acme/widgets", "name": "Widgets monorepo"},
            )
            missing = _call(
                port,
                "PATCH",
                "/api/v1/repos",
                {"url": "acme/nope", "name": "X"},
            )
            deleted = _call(port, "DELETE", "/api/v1/repos?url=acme%2Fwidgets")
            stored = tracker.store.get(ticket_id)

        assert added[0] == 201
        assert added[1] == {"url": "acme/widgets", "name": "Widgets"}
        assert listed[1] == {"repos": [{"url": "acme/widgets", "name": "Widgets"}]}
        assert renamed[0] == 200
        assert renamed[1]["name"] == "Widgets monorepo"
        assert missing[0] == 404
        assert deleted[0] == 200
        assert tracker.store.list_repos() == []
        assert stored is not None and stored.repo == "acme/widgets"

    def test_models_lists_the_configured_aliases_in_order(self, tmp_path: Path) -> None:
        models = {
            "strong": "anthropic/claude-opus-5",
            "cheap": "openai/gpt-5-mini",
        }
        with _server(tmp_path, models=models) as (port, _t, _s, _wake):
            status, payload, _headers = _call(port, "GET", "/api/v1/models")

        assert status == 200
        assert payload == {"models": ["strong", "cheap"]}


# ---------------------------------------------------------------------------
# The bearer exception still holds for every new route
# ---------------------------------------------------------------------------


class TestAuthOnEveryRoute:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/tickets"),
            ("POST", "/api/v1/tickets"),
            ("GET", "/api/v1/tickets/SYM-1"),
            ("PATCH", "/api/v1/tickets/SYM-1"),
            ("DELETE", "/api/v1/tickets/SYM-1"),
            ("POST", "/api/v1/tickets/SYM-1/start"),
            ("POST", "/api/v1/tickets/SYM-1/actions"),
            ("POST", "/api/v1/tickets/SYM-1/comments"),
            ("GET", "/api/v1/repos"),
            ("POST", "/api/v1/repos"),
            ("PATCH", "/api/v1/repos"),
            ("DELETE", "/api/v1/repos?url=x"),
            ("GET", "/api/v1/models"),
        ],
    )
    def test_every_route_needs_the_bearer_token(
        self, tmp_path: Path, method: str, path: str
    ) -> None:
        with _server(tmp_path) as (port, _t, _s, _wake):
            status, payload, _headers = _call(port, method, path, token=None)

        assert status == 401
        assert set(payload) == {"error", "hint"}

    def test_the_refusal_points_at_the_anonymous_guide(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _t, _s, _wake):
            status, payload, _headers = _call(
                port, "GET", "/api/v1/tickets", token=None
            )

        assert status == 401
        assert "GET /api/v1" in payload["hint"]
