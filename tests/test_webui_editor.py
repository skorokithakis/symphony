"""Tests for the ticket editor: drafts, Save, Start and Discard.

The server runs for real on an ephemeral port, reusing the fixtures of
``tests.test_webui_server``, in the same style as ``tests.test_webui_ticket``.
The important boundaries here are the read-only GET contract, the
create-on-first-content rule, server-side Start validation and the two title
variants (with and without ``webui.anthropic_api_key``).
"""

from __future__ import annotations

import json
import re
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
    return sorted((tmp_path / "tickets").glob("SYM-*.json"))


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

    def test_the_title_field_follows_the_anthropic_key(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            _status, without_key = _get(port, "/new")

        assert 'id="title"' in without_key
        assert "You do not write a title" not in without_key

        keyed = tmp_path / "keyed"
        keyed.mkdir()
        with _server(keyed, api_key="sk-test") as (port, _tracker, _wake):
            _status, with_key = _get(port, "/new")

        assert 'id="title"' not in with_key
        assert "You do not write a title. Symphony writes it from your text" in with_key

    def test_model_select_offers_default_then_the_aliases_in_order(
        self, tmp_path: Path
    ) -> None:
        aliases = {
            "sonnet-4.5": "anthropic/claude-sonnet-4.5",
            "opus": "anthropic/claude-opus-5",
        }
        with _server(tmp_path, models=aliases) as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert '<select class="input" id="model" name="model">' in text
        assert '<option value="" selected>Default</option>' in text
        # Configured aliases follow Default, in configuration order.
        assert text.index(">Default</option>") < text.index(">sonnet-4.5</option>")
        assert text.index(">sonnet-4.5</option>") < text.index(">opus</option>")
        assert "checked" not in text
        assert "aria-pressed" not in text

    def test_no_aliases_means_no_model_control(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, _wake):
            _status, text = _get(port, "/new")

        assert 'name="model"' not in text
        assert "Model" not in text

    def test_an_empty_model_alias_is_never_an_option(self, tmp_path: Path) -> None:
        models = {"": "anthropic/empty", "opus": "anthropic/claude-opus-5"}
        with _server(tmp_path, models=models) as (port, tracker, _wake):
            _status, text = _get(port, "/new")
            ticket = tracker.store.create_draft(description="x", model="opus")
            _post(
                port,
                f"/tickets/{ticket.id}/edit",
                {"description": "x", "model": ""},
            )
            stored = tracker.store.get(ticket.id)

        assert '<option value=""></option>' not in text
        assert text.count(">Default</option>") == 1
        assert stored is not None
        assert stored.model is None

    def test_repository_select_lists_names_and_add_last(self, tmp_path: Path) -> None:
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

        # Seeded from the tickets, newest first, de-duplicated by URL.
        assert text.index('<option value="new/repo" selected>new/repo</option>') < (
            text.index('<option value="old/repo">old/repo</option>')
        )
        assert text.count('value="old/repo"') == 1
        # The "Add a repository…" option is last, and the fields work without JS.
        assert text.index(">Add a repository…</option>") > text.index(
            'value="old/repo"'
        )
        assert '<option value=""' in text
        assert 'name="repo_new_url"' in text
        assert 'name="repo_new_name"' in text
        assert 'href="/repos"' in text

    def test_the_repository_select_sorts_names_and_puts_recent_ones_first(
        self, tmp_path: Path
    ) -> None:
        delta = "https://example.com/delta.git"
        echo = "https://example.com/echo.git"
        beta = "https://example.com/beta.git"
        _write_ticket(
            tmp_path,
            "SYM-1",
            state=STATE_DRAFT,
            repo=delta,
            updated_at="2026-11-19T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-2",
            state=STATE_DRAFT,
            repo=echo,
            updated_at="2026-11-21T10:00:00+00:00",
        )
        # Delta's place comes from its newest ticket, not its oldest.
        _write_ticket(
            tmp_path,
            "SYM-3",
            state=STATE_DRAFT,
            repo=delta,
            updated_at="2026-11-22T10:00:00+00:00",
        )
        _write_ticket(
            tmp_path,
            "SYM-4",
            state=STATE_DRAFT,
            repo=beta,
            updated_at="2026-11-20T10:00:00+00:00",
        )

        with _server(tmp_path) as (port, tracker, _wake):
            # The store seeded the list from the tickets: delta, echo, beta.
            tracker.store.rename_repo(beta, "Beta")
            tracker.store.add_repo("https://example.com/zeta.git", "zeta")
            tracker.store.add_repo("https://example.com/alpha.git", "alpha")
            tracker.store.add_repo("https://example.com/foxtrot.git", "foxtrot")
            _status, text = _get(port, "/new")

        select = text.split('id="repo" name="repo"', 1)[1].split("</select>", 1)[0]
        recent = select.split('<optgroup label="Recent">', 1)[1].split("</optgroup>", 1)
        everything = select.split('<optgroup label="All repositories">', 1)[1]
        everything = everything.split("</optgroup>", 1)[0]

        def names(group: str) -> list[str]:
            return re.findall(r">([^<]+)</option>", group)

        # Newest first, de-duplicated by URL, only for managed repositories.
        assert names(recent[0]) == ["delta", "echo", "Beta"]
        # A to Z, ignoring case: Beta sorts before delta.
        assert names(everything) == [
            "alpha",
            "Beta",
            "delta",
            "echo",
            "foxtrot",
            "zeta",
        ]
        # Delta is both the newest and the default, so it appears twice.  Only
        # the Recent copy may be selected; two would let the browser pick the
        # last one.
        assert select.count(" selected>") == 1
        assert " selected>" in recent[0]
        assert " selected>" not in everything

        # Five repositories need no shortcut; the whole list is one glance.
        with _server(tmp_path / "short") as (port, tracker, _wake):
            for name in ["alpha", "beta", "delta", "echo", "foxtrot"]:
                tracker.store.add_repo(f"https://example.com/{name}.git", name)
            _status, short = _get(port, "/new")

        assert '<optgroup label="Recent">' not in short
        assert '<optgroup label="All repositories">' in short

    def test_new_ticket_falls_back_to_the_first_repo_still_in_the_list(
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

        with _server(tmp_path) as (port, tracker, _wake):
            tracker.store.remove_repo("new/repo")
            _status, text = _get(port, "/new")

        assert '<option value="old/repo" selected>old/repo</option>' in text
        assert 'value="new/repo"' not in text

    def test_the_repository_picker_is_only_on_the_editor(self, tmp_path: Path) -> None:
        _write_ticket(tmp_path, "SYM-1", state=STATE_IN_PROGRESS, title="Running")

        with _server(tmp_path) as (port, _tracker, _wake):
            _status, _headers, home = _request(port, "GET", "/")
            _status, _headers, ticket = _request(port, "GET", "/tickets/SYM-1")

        # Ctrl+K is bound in editor.js, which only the editor loads.  A page
        # that carried the dialog would advertise a shortcut that does nothing.
        assert b'id="repo-picker"' not in home
        assert b'id="repo-picker"' not in ticket
        editor_script = re.compile(rb'src="/static/editor\.js\?v=\d+"')
        assert editor_script.search(home) is None
        assert editor_script.search(ticket) is None


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
        assert '<option value="owner/name" selected>owner/name</option>' in text
        assert '<option value="sonnet-4.5" selected>sonnet-4.5</option>' in text
        assert '<option value="" selected>' not in text
        assert f'action="/tickets/{ticket.id}/edit"' in text
        assert f'formaction="/tickets/{ticket.id}/start"' in text
        assert 'id="editor-tid">' + ticket.id + "</span>" in text

    def test_a_stored_model_that_is_no_longer_configured_reads_as_default(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path, models={"opus": "anthropic/claude-opus-5"}) as (
            port,
            tracker,
            _wake,
        ):
            ticket = tracker.store.create_draft(description="x", model="sonnet-4.5")
            _status, text = _get(port, f"/tickets/{ticket.id}/edit")

        assert '<option value="" selected>Default</option>' in text
        assert '<option value="sonnet-4.5"' not in text

    def test_a_draft_repo_not_in_the_list_is_an_extra_option(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="x", repo="gone/repo")
            _status, text = _get(port, f"/tickets/{ticket.id}/edit")

        assert '<option value="gone/repo" selected>gone/repo</option>' in text

    def test_a_started_ticket_redirects_to_its_page(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(title="Started", description="x")
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
            status, headers, _body = _request(port, "GET", f"/tickets/{ticket.id}/edit")

        assert status == 303
        assert headers["Location"] == f"/tickets/{ticket.id}"

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

    @pytest.mark.parametrize(
        "model",
        [
            pytest.param("not-an-alias", id="unknown-alias"),
            pytest.param("", id="default"),
        ],
    )
    def test_a_missing_or_unknown_model_saves_as_none(
        self, tmp_path: Path, model: str
    ) -> None:
        with _server(tmp_path, models=_ALIASES) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="x", model="sonnet-4.5")
            _post(
                port,
                f"/tickets/{ticket.id}/edit",
                {"description": "x", "model": model},
            )
            stored = tracker.store.get(ticket.id)

        assert stored is not None
        assert stored.model is None

    def test_saving_a_new_repo_url_only_stores_it_on_the_draft(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="x", repo="old/repo")
            _post(
                port,
                f"/tickets/{ticket.id}/edit",
                {
                    "description": "x",
                    "repo": "old/repo",
                    "repo_new_url": "  new/repo  ",
                    "repo_new_name": "  A name  ",
                },
            )
            stored = tracker.store.get(ticket.id)
            repos = tracker.store.list_repos()

        assert stored is not None
        assert stored.repo == "new/repo"
        # Save and autosave must not fill the list with typed URLs; Start does.
        assert repos == []

    def test_saving_a_new_url_without_a_name_stores_only_the_url(
        self, tmp_path: Path
    ) -> None:
        url = "https://example.com/team/project.git"
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="x")
            _post(
                port,
                f"/tickets/{ticket.id}/edit",
                {
                    "description": "x",
                    "repo": "",
                    "repo_new_url": url,
                    "repo_new_name": " ",
                },
            )
            stored = tracker.store.get(ticket.id)
            repos = tracker.store.list_repos()

        assert stored is not None
        assert stored.repo == url
        assert repos == []

    def test_starting_the_same_new_url_twice_does_not_duplicate_it(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            for description in ("first", "second"):
                _post(
                    port,
                    "/new/start",
                    {
                        "title": "A title",
                        "description": description,
                        "repo": "",
                        "repo_new_url": "owner/name",
                    },
                )
            repos = tracker.store.list_repos()

        assert [entry.url for entry in repos] == ["owner/name"]

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
        with _server(tmp_path) as (port, tracker, _wake):
            if store_method == "create_started_draft":
                path = "/new/start"
            else:
                ticket = tracker.store.create_draft(
                    title="A title", description="Some text", repo="owner/name"
                )
                path = f"/tickets/{ticket.id}/start"
            with mock.patch.object(
                tracker.store,
                store_method,
                side_effect=TrackerNotFoundError("gone"),
            ):
                status, _headers, _body = _post(port, path, body)
            if store_method == "create_started_draft":
                # A racing delete on a new start leaves no ticket behind.
                assert _tickets(tmp_path) == []

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

    def test_invalid_start_preserves_the_inline_repository_fields(
        self, tmp_path: Path
    ) -> None:
        fields = {
            "title": "A title",
            "description": "",
            "repo": "",
            "repo_new_url": "owner/new",
            "repo_new_name": "Widgets",
        }
        with _server(tmp_path) as (port, tracker, _wake):
            status, _headers, body = _post(port, "/new/start", fields)
            text = body.decode()
            repos_after_error = tracker.store.list_repos()

            # Fixing the only error and starting again registers the name.
            fields["description"] = "Body"
            start_status, _h, _b = _post(port, "/new/start", fields)
            repos = tracker.store.list_repos()

        assert status == 400
        assert '<option value="" selected>Add a repository…</option>' in text
        assert 'value="owner/new"' in text
        assert 'value="Widgets"' in text
        assert repos_after_error == []
        assert start_status == 303
        assert [(entry.url, entry.name) for entry in repos] == [
            ("owner/new", "Widgets")
        ]

    def test_a_rejected_start_leaves_the_repository_list_unchanged(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker, _wake):
            ticket = tracker.store.create_draft(description="Body")
            with mock.patch.object(
                tracker.store, "start_draft", side_effect=ValueError("started")
            ):
                status, _headers, _body = _post(
                    port,
                    f"/tickets/{ticket.id}/start",
                    {
                        "title": "A title",
                        "description": "Body",
                        "repo": "",
                        "repo_new_url": "owner/new",
                        "repo_new_name": "Widgets",
                    },
                )
            repos = tracker.store.list_repos()

        assert status == 303
        assert repos == []

    def test_start_adds_the_new_repository_and_stores_it(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker, wake):
            status, _headers, _body = _post(
                port,
                "/new/start",
                {
                    "title": "A title",
                    "description": "Some text",
                    "repo": "",
                    "repo_new_url": "https://example.com/team/project.git",
                },
            )

        tracker = _created(tmp_path)
        stored = tracker.store.list_tickets()
        repos = tracker.store.list_repos()
        assert status == 303
        assert [(entry.url, entry.name) for entry in repos] == [
            ("https://example.com/team/project.git", "team/project")
        ]
        assert [ticket.repo for ticket in stored] == [
            "https://example.com/team/project.git"
        ]
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


def test_stylesheet_hides_a_closed_dialog_and_has_no_model_chip_rules(
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
    # The model control is a native select now, so the chip rules are gone.
    assert ".model-chips" not in text
