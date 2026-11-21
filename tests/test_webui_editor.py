"""Tests for the ticket editor: drafts, Save, Start and Discard.

The server runs for real on an ephemeral port, reusing the fixtures of
``tests.test_webui_server``, in the same style as ``tests.test_webui_ticket``.
The important boundaries here are the read-only GET contract, the
create-on-first-content rule, server-side Start validation and the two title
variants (with and without ``webui.anthropic_api_key``).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import (
    STATE_DRAFT,
    STATE_IN_PROGRESS,
    LocalTracker,
)
from symphony_linear.state import load_state
from symphony_linear.tracker import TrackerNotFoundError
from symphony_linear.webui import title
from symphony_linear.webui.server import WebUIServer
from tests.test_webui_server import (
    _PASSWORD,
    _USERNAME,
    _origin,
    _request,
    _write_ticket,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    api_key: str | None = None,
    models: dict[str, str] | None = None,
) -> AppConfig:
    webui: dict[str, object] = {
        "port": 8080,
        "username": _USERNAME,
        "password": _PASSWORD,
    }
    if api_key is not None:
        webui["anthropic_api_key"] = api_key
    data: dict[str, object] = {"webui": webui}
    if models is not None:
        data["models"] = models
    return AppConfig.model_validate(data)


_ALIASES = {"sonnet-4.5": "anthropic/claude-sonnet-4.5"}


@pytest.fixture(autouse=True)
def _no_title_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start schedules a background title call; keep the suite off the network.

    Tests that care about the call stub ``title.generate_title`` themselves.
    """
    monkeypatch.setattr(title, "generate_title", lambda *args, **kwargs: None)


@contextmanager
def _server(
    tmp_path: Path,
    *,
    api_key: str | None = None,
    models: dict[str, str] | None = None,
) -> Iterator[tuple[int, LocalTracker, Mock]]:
    tracker = LocalTracker(tmp_path)
    server = WebUIServer(
        store=tracker.store,
        state=load_state(tmp_path),
        config=_config(api_key=api_key, models=models),
        on_wake=(wake := Mock()),
        port=0,
    )
    server.start()
    try:
        port = server.bound_port
        assert port is not None
        yield port, tracker, wake
    finally:
        server.stop()


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


def _get(port: int, path: str) -> tuple[int, str]:
    status, _headers, body = _request(port, "GET", path)
    return status, body.decode()


def _tickets(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "tickets").glob("*.json"))


def _created(tmp_path: Path) -> LocalTracker:
    """Read the files the server wrote with a fresh store instance."""
    return LocalTracker(tmp_path)


# ---------------------------------------------------------------------------
# GET /new
# ---------------------------------------------------------------------------


class TestNewPage:
    def test_renders_an_empty_editor_and_creates_nothing(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            status, text = _get(port, "/new")
            head_status, _headers, head_body = _request(port, "HEAD", "/new")

        assert status == 200
        assert head_status == 200
        assert head_body == b""
        assert "New ticket" in text
        assert 'id="description"' in text
        assert 'id="repo"' in text
        # Autosave replaces the Save button; Start is the only submit button.
        assert 'id="act-save"' not in text
        assert 'id="act-start"' in text
        assert 'id="act-discard"' in text
        # An abandoned form leaves no ticket and no id counter behind.
        assert _tickets(tmp_path) == []
        assert not (tmp_path / "tickets" / "counter.json").exists()

    def test_title_field_is_shown_without_an_anthropic_key(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert 'id="title"' in text
        assert "You do not write a title" not in text

    def test_title_field_is_hidden_with_an_anthropic_key(self, tmp_path: Path) -> None:
        with _server(tmp_path, api_key="sk-test") as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert 'id="title"' not in text
        assert "You do not write a title. Symphony writes it from your text" in text

    def test_model_chips_are_aliases_and_none_is_selected(self, tmp_path: Path) -> None:
        with _server(tmp_path, models=_ALIASES) as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert 'name="model"' in text
        assert 'value="sonnet-4.5"' in text
        assert ">sonnet-4.5</label>" in text
        assert 'aria-pressed="false"' in text
        assert "checked" not in text
        # The mock's select and its "Default (name)" option are rejected.
        assert "<select" not in text
        assert "Default (" not in text

    def test_no_aliases_means_no_model_control(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert 'name="model"' not in text
        assert "Model" not in text

    def test_recent_repos_are_most_recent_first_and_deduplicated(
        self, tmp_path: Path
    ) -> None:
        _write_ticket(
            tmp_path,
            "SYM-1",
            state=STATE_DRAFT,
            repo="old/repo",
            updated_at="2026-11-19T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-2",
            state=STATE_DRAFT,
            repo="new/repo",
            updated_at="2026-11-21T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-3",
            state=STATE_DRAFT,
            repo="old/repo",
            updated_at="2026-11-20T10:00:00+00:00",
        )

        with _server(tmp_path) as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert text.index('data-repo-chip="new/repo"') < text.index(
            'data-repo-chip="old/repo"'
        )
        assert text.count('data-repo-chip="old/repo"') == 1
        assert text.count("<option ") == 2

    def test_a_full_url_is_the_chip_value_and_a_short_label(
        self, tmp_path: Path
    ) -> None:
        url = "https://gitlab.example.com/team/project.git"
        _write_ticket(tmp_path, "SYM-1", state=STATE_DRAFT, repo=url)

        with _server(tmp_path) as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert f'data-repo-chip="{url}"' in text
        assert f'<option value="{url}">' in text
        assert ">team/project</button>" in text
        assert ">team/project<" in text

    def test_no_recent_block_without_repositories(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert 'id="repos"' not in text
        assert 'class="recent"' not in text


# ---------------------------------------------------------------------------
# GET /tickets/<id>/edit
# ---------------------------------------------------------------------------


class TestEditPage:
    def test_a_draft_is_rendered_with_its_values(self, tmp_path: Path) -> None:
        with _server(tmp_path, models=_ALIASES) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(
                title="A title",
                description="Body **text**",
                repo="owner/name",
                model="sonnet-4.5",
            )
            status, text = _get(port, f"/tickets/{ticket.id}/edit")

        assert status == 200
        assert 'value="A title"' in text
        assert "Body **text**" in text
        assert 'value="owner/name"' in text
        assert 'value="sonnet-4.5" checked' in text
        assert 'aria-pressed="true"' in text
        assert f'action="/tickets/{ticket.id}/edit"' in text
        assert f'formaction="/tickets/{ticket.id}/start"' in text
        assert 'id="editor-tid">' + ticket.id + "</span>" in text

    def test_a_started_ticket_redirects_to_its_page(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(title="Started", description="x")
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
            status, headers, _body = _request(port, "GET", f"/tickets/{ticket.id}/edit")

        assert status == 303
        assert headers["Location"] == f"/tickets/{ticket.id}"

    def test_unknown_ticket_is_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            status, _text = _get(port, "/tickets/SYM-404/edit")

        assert status == 404

    def test_untrusted_values_are_escaped(self, tmp_path: Path) -> None:
        title = '<script>alert("x")</script>'
        repo = '"><img src=x onerror=alert(1)>'
        description = "</textarea><script>alert(2)</script>"
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(
                title=title, description=description, repo=repo
            )
            _status, text = _get(port, f"/tickets/{ticket.id}/edit")

        assert "<script>alert" not in text
        assert "&lt;script&gt;alert" in text
        assert "<img src=x" not in text
        assert "&lt;/textarea&gt;" in text

    def test_get_does_not_write_to_the_ticket_file(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(
                title="Stored", description="Stored body"
            )
            path = tmp_path / "tickets" / f"{ticket.id}.json"
            before = path.read_bytes()
            _get(port, f"/tickets/{ticket.id}/edit")
            after = path.read_bytes()

        assert before == after


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


class TestSave:
    def test_an_empty_new_form_creates_nothing(self, tmp_path: Path) -> None:
        with _server(tmp_path, models=_ALIASES) as (port, _tracker, _wake):
            status, headers, _body = _post(
                port,
                "/new",
                {"title": "   ", "description": " \n ", "repo": "  "},
            )

        assert status == 303
        assert headers["Location"] == "/new"
        assert _tickets(tmp_path) == []

    def test_the_first_content_creates_a_draft_and_redirects_to_it(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            status, headers, _body = _post(
                port,
                "/new",
                {
                    "title": "  A title  ",
                    "description": "Some text",
                    "repo": "  owner/name  ",
                },
            )

        stored = _created(tmp_path).store.list_tickets()
        assert status == 303
        assert len(stored) == 1
        assert headers["Location"] == f"/tickets/{stored[0].id}/edit"
        assert stored[0].state == STATE_DRAFT
        assert stored[0].title == "A title"
        assert stored[0].description == "Some text"
        assert stored[0].repo == "owner/name"

    def test_saving_an_existing_draft_replaces_the_fields(self, tmp_path: Path) -> None:
        with _server(tmp_path, models=_ALIASES) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(
                title="Old", description="Old body", repo="old/repo"
            )
            status, headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/edit",
                {
                    "title": "New",
                    "description": "New body",
                    "repo": "new/repo",
                    "model": "sonnet-4.5",
                },
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert headers["Location"] == f"/tickets/{ticket.id}/edit"
        assert stored is not None
        assert stored.title == "New"
        assert stored.description == "New body"
        assert stored.repo == "new/repo"
        assert stored.model == "sonnet-4.5"

    def test_an_unknown_model_is_ignored(self, tmp_path: Path) -> None:
        with _server(tmp_path, models=_ALIASES) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="x")
            _post(
                port,
                f"/tickets/{ticket.id}/edit",
                {"description": "x", "model": "not-an-alias"},
            )
            stored = tracker.store.get(ticket.id)

        assert stored is not None
        assert stored.model is None

    def test_variant_a_save_preserves_a_stored_title(self, tmp_path: Path) -> None:
        # With an API key the title input is absent, so the form cannot carry
        # one.  A title stored earlier (another config, or a later automatic
        # title) must survive a plain Save.
        with _server(tmp_path, api_key="sk-test") as (port, tracker, _wake):
            ticket = tracker.store.create_draft(title="Stored title", description="old")
            _post(
                port,
                f"/tickets/{ticket.id}/edit",
                {"description": "new body", "repo": "owner/name"},
            )
            stored = tracker.store.get(ticket.id)

        assert stored is not None
        assert stored.title == "Stored title"
        assert stored.description == "new body"

    def test_saving_a_started_ticket_redirects_and_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(
                title="Started", description="fixed text"
            )
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
            status, headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/edit",
                {"title": "Changed", "description": "Changed body"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert headers["Location"] == f"/tickets/{ticket.id}"
        assert stored is not None
        assert stored.title == "Started"
        assert stored.description == "fixed text"

    def test_cross_origin_save_is_forbidden(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            status, _headers, _body = _post(
                port,
                "/new",
                {"description": "hello"},
                origin=False,
            )

        assert status == 403
        assert _tickets(tmp_path) == []


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


class TestStart:
    def test_a_new_start_racing_a_delete_is_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            with mock.patch.object(
                tracker.store,
                "create_started_draft",
                side_effect=TrackerNotFoundError("gone"),
            ):
                status, _headers, _body = _post(
                    port,
                    "/new/start",
                    {
                        "title": "A title",
                        "description": "Some text",
                        "repo": "owner/name",
                    },
                )

        assert status == 404
        assert _tickets(tmp_path) == []

    def test_a_saved_start_racing_a_discard_is_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(
                title="A title", description="Some text", repo="owner/name"
            )
            with mock.patch.object(
                tracker.store,
                "start_draft",
                side_effect=TrackerNotFoundError("gone"),
            ):
                status, _headers, _body = _post(
                    port,
                    f"/tickets/{ticket.id}/start",
                    {
                        "title": "A title",
                        "description": "Some text",
                        "repo": "owner/name",
                    },
                )

        assert status == 404

    def test_start_creates_and_moves_the_ticket_to_in_progress(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, _tracker, wake):
            status, headers, _body = _post(
                port,
                "/new/start",
                {
                    "title": "A title",
                    "description": "Some text",
                    "repo": "owner/name",
                },
            )

        stored = _created(tmp_path).store.list_tickets()
        assert status == 303
        assert len(stored) == 1
        assert headers["Location"] == f"/tickets/{stored[0].id}"
        assert stored[0].state == STATE_IN_PROGRESS
        assert stored[0].title == "A title"
        wake.assert_called_once()

    def test_missing_fields_are_rendered_as_field_errors_without_creating(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, _tracker, wake):
            status, _headers, body = _post(
                port,
                "/new/start",
                {"title": "  ", "description": " ", "repo": ""},
            )

        text = body.decode()
        assert status == 400
        assert text.count('aria-invalid="true"') == 3
        assert text.count('class="field-err"') == 3
        assert "Write a title." in text
        assert "Describe the task." in text
        assert "Name the repository to clone." in text
        assert _tickets(tmp_path) == []
        wake.assert_not_called()

    def test_a_title_is_not_required_with_an_anthropic_key(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path, api_key="sk-test") as (port, _tracker, wake):
            status, headers, _body = _post(
                port,
                "/new/start",
                {"description": "First line\nrest of the text", "repo": "owner/name"},
            )

        stored = _created(tmp_path).store.list_tickets()
        assert status == 303
        assert len(stored) == 1
        assert headers["Location"] == f"/tickets/{stored[0].id}"
        assert stored[0].state == STATE_IN_PROGRESS
        assert stored[0].title == ""
        wake.assert_called_once()

    def test_the_fallback_title_is_displayed_not_stored(self, tmp_path: Path) -> None:
        with _server(tmp_path, api_key="sk-test") as (port, _tracker, _wake):
            _post(
                port,
                "/new/start",
                {"description": "\n  First line  \nsecond", "repo": "owner/name"},
            )
            stored = _created(tmp_path).store.list_tickets()
            _status, text = _get(port, f"/tickets/{stored[0].id}")

        assert stored[0].title == ""
        assert '<h1 class="ttitle" id="ticket-title">First line</h1>' in text

    def test_a_saved_draft_is_validated_then_persisted_and_started(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, wake):
            ticket = tracker.store.create_draft(
                title="Old", description="Old body", repo=""
            )
            status, headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/start",
                {
                    "title": "New title",
                    "description": "New body",
                    "repo": "new/repo",
                },
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert headers["Location"] == f"/tickets/{ticket.id}"
        assert stored is not None
        assert stored.state == STATE_IN_PROGRESS
        assert stored.title == "New title"
        assert stored.description == "New body"
        assert stored.repo == "new/repo"
        wake.assert_called_once()

    def test_a_failed_start_leaves_the_saved_draft_untouched(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, wake):
            ticket = tracker.store.create_draft(
                title="Old", description="Old body", repo="old/repo"
            )
            status, _headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/start",
                {"title": "", "description": "Edited body", "repo": "new/repo"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 400
        assert stored is not None
        assert stored.state == STATE_DRAFT
        assert stored.title == "Old"
        assert stored.description == "Old body"
        assert stored.repo == "old/repo"
        wake.assert_not_called()

    def test_starting_a_started_ticket_redirects(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, wake):
            ticket = tracker.store.create_draft(
                title="Started", description="text", repo="owner/name"
            )
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
            status, headers, _body = _post(
                port,
                f"/tickets/{ticket.id}/start",
                {"title": "Other", "description": "other", "repo": "other/repo"},
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert headers["Location"] == f"/tickets/{ticket.id}"
        assert stored is not None
        assert stored.title == "Started"
        assert stored.description == "text"
        wake.assert_not_called()

    def test_cross_origin_start_is_forbidden(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, wake):
            status, _headers, _body = _post(
                port,
                "/new/start",
                {"title": "T", "description": "d", "repo": "o/n"},
                origin=False,
            )

        assert status == 403
        assert _tickets(tmp_path) == []
        wake.assert_not_called()


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


class TestDiscard:
    def test_the_first_post_renders_the_confirmation_dialog(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="keep me")
            status, _headers, body = _post(port, f"/tickets/{ticket.id}/discard", {})
            stored = tracker.store.get(ticket.id)

        text = body.decode()
        assert status == 200
        assert '<dialog class="confirm" id="discard-dialog"' in text
        assert "open data-auto-open" in text
        assert "Discard this draft?" in text
        assert "Keep the draft" in text
        assert stored is not None
        assert stored.description == "keep me"

    def test_confirm_deletes_the_draft_and_its_file(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="delete me")
            path = tmp_path / "tickets" / f"{ticket.id}.json"
            status, headers, _body = _post(
                port, f"/tickets/{ticket.id}/discard", {"confirm": "1"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert headers["Location"] == "/"
        assert stored is None
        assert not path.exists()

    def test_discarding_a_started_ticket_does_not_delete_it(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="fixed")
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
            status, headers, _body = _post(
                port, f"/tickets/{ticket.id}/discard", {"confirm": "1"}
            )
            stored = tracker.store.get(ticket.id)

        assert status == 303
        assert headers["Location"] == f"/tickets/{ticket.id}"
        assert stored is not None
        assert stored.state == STATE_IN_PROGRESS

    def test_unknown_ticket_is_404(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            status, _headers, _body = _post(
                port, "/tickets/SYM-404/discard", {"confirm": "1"}
            )

        assert status == 404

    def test_discarding_an_unsaved_new_editor_redirects_home(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            ask_status, _headers, ask_body = _post(port, "/new/discard", {})
            status, headers, _body = _post(port, "/new/discard", {"confirm": "1"})

        assert ask_status == 200
        assert b"Discard this draft?" in ask_body
        assert status == 303
        assert headers["Location"] == "/"
        assert _tickets(tmp_path) == []

    def test_get_never_deletes_a_draft(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="safe")
            _get(port, f"/tickets/{ticket.id}/edit")
            stored = tracker.store.get(ticket.id)

        assert stored is not None
        assert (
            json.loads((tmp_path / "tickets" / f"{ticket.id}.json").read_text())[
                "description"
            ]
            == "safe"
        )


# ---------------------------------------------------------------------------
# Editor-only stylesheet additions
# ---------------------------------------------------------------------------


def test_stylesheet_marks_the_selected_model_chip_and_hides_a_closed_dialog(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (port, _tracker, _wake):
        status, headers, body = _request(port, "GET", "/static/symphony.css")

    text = body.decode()
    assert status == 200
    assert headers["Content-Type"].startswith("text/css")
    # Section 12 gives .confirm a display value, which would otherwise override
    # the browser's dialog:not([open]) rule.
    assert "dialog.confirm:not([open]) { display: none; }" in text
    assert ".model-chips input:checked + .chip" in text
