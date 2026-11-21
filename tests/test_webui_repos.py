"""Tests for the managed repository list page (``/repos``).

The page is the only place the list is edited by hand.  The important
boundaries are that GET writes nothing (the list is seeded when the store
loads), that add and remove go through the ordinary POST pipeline, and that
removing an entry never changes a ticket.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from symphony_linear.local_tracker import STATE_DRAFT, LocalTracker
from symphony_linear.state import load_state
from symphony_linear.webui.server import WebUIServer
from tests.test_webui_editor import _config, _get, _post
from tests.test_webui_server import _write_ticket


@contextmanager
def _server(tmp_path: Path) -> Iterator[int]:
    tracker = LocalTracker(tmp_path)
    server = WebUIServer(
        store=tracker.store,
        state=load_state(tmp_path),
        config=_config(),
        on_wake=lambda: None,
        port=0,
    )
    server.start()
    try:
        port = server.bound_port
        assert port is not None
        yield port
    finally:
        server.stop()


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


class TestReposPage:
    def test_get_lists_names_and_urls_and_does_not_write(self, tmp_path: Path) -> None:
        url = "https://example.com/team/project.git"
        _write_ticket(tmp_path, "SYM-1", state=STATE_DRAFT, repo=url)

        with _server(tmp_path) as port:
            before = _snapshot(tmp_path / "tickets")
            status, text = _get(port, "/repos")
            after = _snapshot(tmp_path / "tickets")

        assert status == 200
        assert ">team/project<" in text
        assert f">{url}<" in text
        assert 'action="/repos/remove"' in text
        assert f'<input type="hidden" name="url" value="{url}">' in text
        assert before == after

    def test_an_empty_list_has_the_add_form(self, tmp_path: Path) -> None:
        with _server(tmp_path) as port:
            status, text = _get(port, "/repos")

        assert status == 200
        assert "No repositories yet." in text
        assert 'action="/repos"' in text
        assert 'name="url"' in text
        assert 'name="name"' in text

    def test_add_a_repository(self, tmp_path: Path) -> None:
        with _server(tmp_path) as port:
            status, headers, _body = _post(
                port,
                "/repos",
                {"url": "owner/name", "name": "Widgets"},
            )

        assert status == 303
        assert headers["Location"] == "/repos"
        tracker = LocalTracker(tmp_path)
        assert [(entry.url, entry.name) for entry in tracker.store.list_repos()] == [
            ("owner/name", "Widgets")
        ]

    def test_add_without_a_name_derives_owner_name(self, tmp_path: Path) -> None:
        with _server(tmp_path) as port:
            _status, _headers, _body = _post(
                port,
                "/repos",
                {"url": "https://example.com/team/project.git", "name": ""},
            )

        tracker = LocalTracker(tmp_path)
        assert [entry.name for entry in tracker.store.list_repos()] == ["team/project"]

    def test_add_without_a_url_shows_an_error_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as port:
            before = _snapshot(tmp_path / "tickets")
            status, _headers, body = _post(port, "/repos", {"url": "  ", "name": "x"})
            after = _snapshot(tmp_path / "tickets")

        assert status == 400
        assert "Enter a repository URL." in body.decode()
        assert before == after

    def test_remove_deletes_the_entry_and_leaves_the_ticket(
        self, tmp_path: Path
    ) -> None:
        _write_ticket(tmp_path, "SYM-1", state=STATE_DRAFT, repo="owner/name")

        with _server(tmp_path) as port:
            status, headers, _body = _post(port, "/repos/remove", {"url": "owner/name"})
            tracker = LocalTracker(tmp_path)
            ticket = tracker.store.get("SYM-1")

        assert status == 303
        assert headers["Location"] == "/repos"
        assert tracker.store.list_repos() == []
        assert ticket is not None
        assert ticket.repo == "owner/name"
