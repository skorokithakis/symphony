"""Tests for file uploads, serving, and the files attached to the thread.

The server runs for real on an ephemeral port, in the style of
``tests.test_webui_ticket``.  The important boundaries are the size limit
before the body is read, the inline/serve distinction for images versus
everything else, the containment checks on both URL parameters, and the move
of pending reply files onto the comment on Send.
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
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    LocalTracker,
)
from symphony_linear.state import load_state
from symphony_linear.webui.api import MAX_UPLOAD_BYTES
from symphony_linear.webui.server import WebUIServer
from tests.test_webui_server import (
    _basic,
    _config,
    _origin,
    _raw_request,
    _request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _server(tmp_path: Path) -> Iterator[tuple[int, LocalTracker]]:
    tracker = LocalTracker(tmp_path)
    server = WebUIServer(
        store=tracker.store,
        state=load_state(tmp_path),
        config=_config(),
        on_wake=Mock(),
        port=0,
    )
    server.start()
    try:
        port = server.bound_port
        assert port is not None
        yield port, tracker
    finally:
        server.stop()


def _upload(
    port: int,
    path: str,
    data: bytes,
    *,
    name: str,
    target: str | None = None,
) -> tuple[int, dict[str, object]]:
    parameters = {"name": name}
    if target is not None:
        parameters["target"] = target
    status, _headers, body = _request(
        port,
        "POST",
        f"{path}?{urlencode(parameters)}",
        headers={
            "Origin": _origin(port),
            "Content-Type": "application/octet-stream",
        },
        body=data,
    )
    return status, json.loads(body) if body else {}


def _delete(port: int, url: str) -> tuple[int, dict[str, object]]:
    status, _headers, body = _request(
        port, "DELETE", url, headers={"Origin": _origin(port)}
    )
    return status, json.loads(body) if body else {}


def _post_json(
    port: int, path: str, payload: dict[str, object]
) -> tuple[int, dict[str, object]]:
    status, _headers, body = _request(
        port,
        "POST",
        path,
        headers={
            "Origin": _origin(port),
            "Content-Type": "application/json",
        },
        body=json.dumps(payload).encode(),
    )
    return status, json.loads(body) if body else {}


def _file_of(response: dict[str, object]) -> dict[str, object]:
    file = response["file"]
    assert isinstance(file, dict)
    return file


def _stored_path(tmp_path: Path, ticket_id: str, name: str) -> Path:
    return tmp_path / "tickets" / "files" / ticket_id / name


def _needs_input_ticket(tracker: LocalTracker) -> str:
    ticket = tracker.store.create_draft(
        description="Ticket text", repo="https://github.com/acme/widgets.git"
    )
    tracker.store.set_state(ticket.id, STATE_NEEDS_INPUT)
    return ticket.id


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class TestUpload:
    def test_first_upload_on_new_creates_the_draft(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker):
            status, response = _upload(
                port, "/api/new/files", b"\x89PNG\r\n", name="shot (1).PNG"
            )

        assert status == 200
        ticket_id = response["id"]
        assert ticket_id == "SYM-1"
        file = _file_of(response)
        assert file["original_name"] == "shot (1).PNG"
        assert file["size"] == 6
        assert file["image"] is True
        assert file["url"] == f"/files/SYM-1/{file['name']}"
        assert " " not in str(file["name"])
        assert "(" not in str(file["name"])
        stored = tracker.store.get("SYM-1")
        assert stored is not None
        assert [record.original_name for record in stored.files] == ["shot (1).PNG"]
        assert _stored_path(tmp_path, "SYM-1", str(file["name"])).read_bytes() == (
            b"\x89PNG\r\n"
        )

    def test_an_oversized_upload_is_refused_before_the_body(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker):
            status, headers, body = _raw_request(
                port,
                "POST",
                "/api/new/files?name=big.bin",
                {
                    "Authorization": _basic(),
                    "Origin": _origin(port),
                    "Content-Length": str(MAX_UPLOAD_BYTES + 1),
                },
            )

        assert status == 413
        assert body == b"Payload Too Large\n"
        assert "application/json" not in headers.get("Content-Type", "")
        # The route view never ran, so no ticket was created.
        assert tracker.store.list_tickets() == []
        assert not (tmp_path / "tickets" / "files").exists()

    def test_upload_requires_a_name(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker):
            ticket = tracker.store.create_draft(description="x")
            status, _headers, _body = _request(
                port,
                "POST",
                f"/api/tickets/{ticket.id}/files",
                headers={"Origin": _origin(port)},
                body=b"x",
            )
        assert status == 400

    def test_a_started_ticket_takes_no_description_files(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker):
            ticket = tracker.store.create_draft(description="x")
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)

            status, response = _upload(
                port,
                f"/api/tickets/{ticket.id}/files",
                b"x",
                name="a.txt",
                target="description",
            )

        assert status == 409
        assert "error" in response
        assert not (tmp_path / "tickets" / "files" / ticket.id).exists()

    def test_a_reply_upload_is_allowed_while_the_agent_works(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker):
            ticket = tracker.store.create_draft(description="x")
            tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)

            status, response = _upload(
                port,
                f"/api/tickets/{ticket.id}/files",
                b"note",
                name="note.txt",
                target="reply",
            )

        assert status == 200
        assert _file_of(response)["original_name"] == "note.txt"

    def test_a_short_body_removes_the_temporary_file(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker):
            # Announce ten bytes, send three, then close the write side.
            import socket

            request = (
                f"POST /api/new/files?name=a.txt HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: {_basic()}\r\n"
                f"Origin: {_origin(port)}\r\n"
                f"Content-Length: 10\r\n"
                f"\r\n"
            ).encode() + b"abc"
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(request)
                sock.shutdown(socket.SHUT_WR)
                response = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    response += chunk

        assert b"400" in response.split(b"\r\n", 1)[0]
        # The empty Draft created by the failed upload is removed again.
        assert tracker.store.list_tickets() == []
        files_root = tmp_path / "tickets" / "files"
        assert not files_root.exists() or not any(files_root.iterdir())

    def test_a_reservation_failure_leaves_no_placeholder_draft(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server(tmp_path) as (port, tracker):

            def fail(*args: object, **kwargs: object) -> None:
                raise OSError("disk full")

            monkeypatch.setattr(tracker.store, "new_upload", fail)
            status, response = _upload(port, "/api/new/files", b"x", name="a.txt")

            assert status == 500
            assert "error" in response
            assert tracker.store.list_tickets() == []

    def test_a_failed_upload_keeps_a_draft_that_was_edited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server(tmp_path) as (port, tracker):

            def fail(ticket_id: str, name: str, *, target: str) -> None:
                tracker.store.update_draft(
                    ticket_id, title="Edited", description="", repo="", model=None
                )
                raise OSError("disk full")

            monkeypatch.setattr(tracker.store, "new_upload", fail)
            status, _response = _upload(port, "/api/new/files", b"x", name="a.txt")
            drafts = tracker.store.list_tickets()

        assert status == 500
        assert len(drafts) == 1
        assert drafts[0].title == "Edited"

    def test_a_failed_upload_keeps_a_draft_that_gained_a_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server(tmp_path) as (port, tracker):
            original = tracker.store.new_upload

            def fail(ticket_id: str, name: str, *, target: str) -> None:
                upload = original(ticket_id, "other.txt", target=target)
                upload.path.write_bytes(b"x")
                tracker.store.register_file(upload, 1)
                raise OSError("disk full")

            monkeypatch.setattr(tracker.store, "new_upload", fail)
            status, _response = _upload(port, "/api/new/files", b"x", name="a.txt")
            drafts = tracker.store.list_tickets()

        assert status == 500
        assert len(drafts) == 1
        assert [record.original_name for record in drafts[0].files] == ["other.txt"]


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


class TestServing:
    def test_every_allowlisted_image_extension_is_inline(self, tmp_path: Path) -> None:
        cases = {
            "a.png": "image/png",
            "b.jpg": "image/jpeg",
            "c.jpeg": "image/jpeg",
            "d.gif": "image/gif",
            "e.webp": "image/webp",
        }
        with _server(tmp_path) as (port, _tracker):
            _status, first = _upload(
                port, "/api/new/files", b"x", name=next(iter(cases))
            )
            ticket_id = str(first["id"])
            uploads = [first]
            for name in list(cases)[1:]:
                _status, response = _upload(
                    port,
                    f"/api/tickets/{ticket_id}/files",
                    b"x",
                    name=name,
                    target="description",
                )
                uploads.append(response)
            results = {
                name: _request(port, "GET", str(_file_of(response)["url"]))
                for name, response in zip(cases, uploads, strict=True)
            }

        for name, expected in cases.items():
            status, headers, _body = results[name]
            assert status == 200
            assert headers["Content-Type"] == expected, name
            assert "Content-Disposition" not in headers, name

    def test_png_is_inline_and_html_and_svg_download(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker):
            status, first = _upload(
                port, "/api/new/files", b"\x89PNG\r\n", name="shot.png"
            )
            assert status == 200
            ticket_id = str(first["id"])

            status, second = _upload(
                port,
                f"/api/tickets/{ticket_id}/files",
                b"<html><script>alert(1)</script></html>",
                name="page.html",
                target="description",
            )
            assert status == 200
            status, third = _upload(
                port,
                f"/api/tickets/{ticket_id}/files",
                b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                name="logo.svg",
                target="description",
            )
            assert status == 200

            png_status, png_headers, png_body = _request(
                port, "GET", str(_file_of(first)["url"])
            )
            html_status, html_headers, html_body = _request(
                port, "GET", str(_file_of(second)["url"])
            )
            svg_status, svg_headers, svg_body = _request(
                port, "GET", str(_file_of(third)["url"])
            )

        assert png_status == 200
        assert png_headers["Content-Type"] == "image/png"
        assert "Content-Disposition" not in png_headers
        assert png_body == b"\x89PNG\r\n"

        assert html_status == 200
        assert html_headers["Content-Type"] == "application/octet-stream"
        assert html_headers["Content-Disposition"].startswith("attachment;")
        assert b"<script>" in html_body

        assert svg_status == 200
        assert svg_headers["Content-Type"] == "application/octet-stream"
        assert svg_headers["Content-Disposition"].startswith("attachment;")

    def test_head_serves_headers_without_a_body(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker):
            _status, response = _upload(port, "/api/new/files", b"bytes", name="a.txt")
            status, headers, body = _request(
                port, "HEAD", str(_file_of(response)["url"])
            )

        assert status == 200
        assert headers["Content-Type"] == "application/octet-stream"
        assert body == b""

    def test_files_require_basic_auth(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker):
            _status, response = _upload(port, "/api/new/files", b"bytes", name="a.txt")
            status, headers, _body = _request(
                port, "GET", str(_file_of(response)["url"]), auth=False
            )

        assert status == 401
        assert headers["WWW-Authenticate"].startswith("Basic")

    def test_an_unregistered_file_on_disk_is_not_served(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker):
            _status, response = _upload(
                port, "/api/new/files", b"registered", name="a.txt"
            )
            file = _file_of(response)
            ticket_id = str(response["id"])
            directory = _stored_path(tmp_path, ticket_id, str(file["name"])).parent
            orphan = directory / "deadbeef-orphan.txt"
            orphan.write_bytes(b"orphan")
            staging = directory / ".upload-abc123"
            staging.write_bytes(b"partial")

            orphan_status, _h, _b = _request(
                port, "GET", f"/files/{ticket_id}/{orphan.name}"
            )
            staging_status, _h, _b = _request(
                port, "GET", f"/files/{ticket_id}/{staging.name}"
            )
            registered_status, _h, _b = _request(port, "GET", str(file["url"]))

        assert orphan_status == 404
        assert staging_status == 404
        assert registered_status == 200

    def test_a_percent_encoded_alias_is_not_served(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, _tracker):
            _status, response = _upload(
                port, "/api/new/files", b"bytes", name="a-b.txt"
            )
            file = _file_of(response)
            canonical = str(file["url"])
            # The stored name starts with the 16-hex prefix, so the first
            # hyphen is the prefix separator.
            alias = canonical.replace("-", "%2D", 1)

            alias_status, _h, _b = _request(port, "GET", alias)
            canonical_status, _h, _b = _request(port, "GET", canonical)

        assert alias_status == 404
        assert canonical_status == 200

    def test_traversal_in_either_path_parameter_is_not_found(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, _tracker):
            _status, response = _upload(port, "/api/new/files", b"bytes", name="a.txt")
            name = str(_file_of(response)["name"])
            for path in (
                "/files/SYM-1/..%2f..%2fSYM-1.json",
                f"/files/SYM-1/%2e%2e%2f{name}",
                "/files/%2e%2e%2fSYM-1/a.txt",
                "/files/..%2fSYM-1/a.txt",
                f"/files/SYM-404/{name}",
            ):
                status, _headers, _body = _request(port, "GET", path)
                assert status == 404, path


# ---------------------------------------------------------------------------
# Deleting and sending
# ---------------------------------------------------------------------------


class TestPendingFiles:
    def test_removing_a_pending_file_deletes_the_bytes(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker):
            ticket_id = _needs_input_ticket(tracker)
            _status, response = _upload(
                port,
                f"/api/tickets/{ticket_id}/files",
                b"zip",
                name="notes.zip",
                target="reply",
            )
            file = _file_of(response)
            path = _stored_path(tmp_path, ticket_id, str(file["name"]))
            assert path.exists()

            status, _body = _delete(port, str(file["delete_url"]))

            assert status == 200
            assert not path.exists()
            stored = tracker.store.get(ticket_id)
            assert stored is not None
            assert stored.pending_files == []

    def test_send_moves_pending_files_onto_the_new_comment(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker):
            ticket_id = _needs_input_ticket(tracker)
            _status, image = _upload(
                port,
                f"/api/tickets/{ticket_id}/files",
                b"\x89PNG\r\n",
                name="shot.png",
                target="reply",
            )
            _status, archive = _upload(
                port,
                f"/api/tickets/{ticket_id}/files",
                b"PK\x03\x04",
                name="notes.zip",
                target="reply",
            )

            status, _body = _post_json(
                port,
                f"/api/tickets/{ticket_id}/reply",
                {"body": "Here they are", "token": "tok"},
            )

            assert status == 200
            stored = tracker.store.get(ticket_id)
            assert stored is not None
            assert stored.pending_files == []
            [comment] = stored.comments
            assert [record.original_name for record in comment.files] == [
                "shot.png",
                "notes.zip",
            ]
            issue = tracker.get_issue(ticket_id)
            [delivered] = tracker.list_comments_since(ticket_id, None)
            assert delivered.body == (
                "Here they are\n\n"
                f"![shot.png](/files/{ticket_id}/{_file_of(image)['name']})\n"
                f"[notes.zip](/files/{ticket_id}/{_file_of(archive)['name']})"
            )
            assert issue.comments[0].body == delivered.body

    def test_discard_deletes_the_drafts_files(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker):
            _status, response = _upload(
                port, "/api/new/files", b"zip", name="notes.zip"
            )
            ticket_id = str(response["id"])
            directory = _stored_path(
                tmp_path, ticket_id, str(_file_of(response)["name"])
            ).parent

            status, _body = _post_json(port, f"/api/tickets/{ticket_id}/discard", {})

        assert status == 200
        assert not directory.exists()
        assert tracker.store.get(ticket_id) is None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class TestPages:
    def test_the_editor_lists_files_without_showing_the_markdown_refs(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, _tracker):
            _status, response = _upload(
                port, "/api/new/files", b"zip", name="notes.zip"
            )
            ticket_id = str(response["id"])
            status, _headers, body = _request(port, "GET", f"/tickets/{ticket_id}/edit")

        text = body.decode()
        assert status == 200
        assert "notes.zip" in text
        assert "Attach files" in text
        # The stored text is what the user typed; no reference was appended
        # into the textarea, which is empty here.
        assert "![notes.zip]" not in text
        assert "](/files/" not in text

    def test_description_files_show_in_the_first_thread_entry(
        self, tmp_path: Path
    ) -> None:
        with _server(tmp_path) as (port, tracker):
            _status, response = _upload(
                port, "/api/new/files", b"\x89PNG\r\n", name="shot.png"
            )
            ticket_id = str(response["id"])
            tracker.store.set_state(ticket_id, STATE_IN_PROGRESS)
            status, _headers, body = _request(port, "GET", f"/tickets/{ticket_id}")

        text = body.decode()
        assert status == 200
        assert 'class="files files--large"' in text
        assert 'class="thumb"' in text
        assert f'data-lightbox="{_file_of(response)["url"]}"' in text

    def test_the_thread_renders_a_thumb_and_a_file_chip(self, tmp_path: Path) -> None:
        with _server(tmp_path) as (port, tracker):
            ticket_id = _needs_input_ticket(tracker)
            _status, image = _upload(
                port,
                f"/api/tickets/{ticket_id}/files",
                b"\x89PNG\r\n",
                name="shot.png",
                target="reply",
            )
            _status, archive = _upload(
                port,
                f"/api/tickets/{ticket_id}/files",
                b"PK\x03\x04",
                name="notes.zip",
                target="reply",
            )
            _post_json(
                port,
                f"/api/tickets/{ticket_id}/reply",
                {"body": "Look", "token": "tok"},
            )
            status, _headers, body = _request(port, "GET", f"/tickets/{ticket_id}")

        text = body.decode()
        assert status == 200
        assert 'class="files files--large"' in text
        assert 'class="thumb"' in text
        assert f'data-lightbox="{_file_of(image)["url"]}"' in text
        assert 'id="lightbox"' in text
        assert "notes.zip" in text
        assert 'class="file"' in text
        # The image link is the thumb's src, not a stray Markdown paragraph.
        assert "![shot.png]" not in text
