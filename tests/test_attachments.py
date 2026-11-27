"""Tests for the attachments helpers."""

from __future__ import annotations

import os
from unittest.mock import Mock

import pytest

from symphony_linear.attachments import (
    AttachmentRef,
    AttachmentResult,
    extract_attachment_refs,
    extract_image_refs,
    process_attachments,
)
from symphony_linear.tracker import (
    AttachmentDownloadError,
    AttachmentTooLargeError,
)

SANDBOX_MOUNT = "/tmp/symphony-attachments"


def _single_url(body: str) -> str:
    """Return the one URL *body* references, for building fake downloads."""
    tracker = Mock()
    tracker.is_upload_url = Mock(return_value=True)
    refs = extract_attachment_refs(body, tracker)
    assert len(refs) == 1
    return refs[0].url


class TestExtractImageRefs:
    """Image extraction from untrusted Markdown bodies."""

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "Hello, world!",
            # A regular Markdown link is not an image.
            "[link](https://example.com)",
            # HTML is intentionally not parsed.
            '<img src="https://example.com/photo.png" alt="photo">',
            # A bare URL must occupy its whole line.
            "see https://example.com/pic.png for details",
        ],
        ids=["empty", "plain-text", "link", "html-img", "inline-bare-url"],
    )
    def test_no_image_refs(self, body: str) -> None:
        assert extract_image_refs(body) == []

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (
                "here is an image: ![alt text](https://example.com/img.png)",
                [("https://example.com/img.png", "alt text")],
            ),
            (
                "![](https://example.com/photo.jpg)",
                [("https://example.com/photo.jpg", "")],
            ),
            (
                '![logo](https://example.com/logo.png "The Logo")',
                [("https://example.com/logo.png", "logo")],
            ),
            (
                "![x](  https://example.com/pic.jpeg  )",
                [("https://example.com/pic.jpeg", "x")],
            ),
            (
                "![foo [bar] baz](https://example.com/img.webp)",
                [("https://example.com/img.webp", "foo [bar] baz")],
            ),
            (
                "![a [b] c [d] e](https://example.com/pic.png)",
                [("https://example.com/pic.png", "a [b] c [d] e")],
            ),
            (
                "![chart](https://example.com/chart.png?width=800&height=600)",
                [("https://example.com/chart.png?width=800&height=600", "chart")],
            ),
            (
                "![&lt;hello&gt;](https://example.com/icon.png)",
                [("https://example.com/icon.png", "&lt;hello&gt;")],
            ),
            (
                "![img](https://cdn.example.com/user_123/image-v2.png)",
                [("https://cdn.example.com/user_123/image-v2.png", "img")],
            ),
        ],
        ids=[
            "alt-text",
            "empty-alt",
            "title",
            "whitespace",
            "bracket-alt",
            "multiple-brackets",
            "query-string",
            "html-entities",
            "special-chars",
        ],
    )
    def test_markdown_images(self, body: str, expected: list[tuple[str, str]]) -> None:
        assert extract_image_refs(body) == expected

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (
                "https://example.com/screenshot.png",
                [("https://example.com/screenshot.png", "")],
            ),
            # The extension match is case-insensitive.
            ("HTTP://EXAMPLE.COM/PHOTO.JPG", [("HTTP://EXAMPLE.COM/PHOTO.JPG", "")]),
            (
                "https://example.com/animation.gif",
                [("https://example.com/animation.gif", "")],
            ),
            (
                "https://example.com/modern.webp",
                [("https://example.com/modern.webp", "")],
            ),
            # Leading / trailing whitespace is tolerated.
            (
                "   https://example.com/pic.png   ",
                [("https://example.com/pic.png", "")],
            ),
            (
                "https://example.com/photo.jpg?v=2&size=large",
                [("https://example.com/photo.jpg?v=2&size=large", "")],
            ),
            # A bare URL line between text lines is found.
            (
                "Some text above\nhttps://example.com/diagram.png\nSome text below",
                [("https://example.com/diagram.png", "")],
            ),
            # Only image-extension bare URLs are extracted.
            ("https://example.com/file.pdf", []),
            ("https://example.com/page", []),
            (
                "https://example.com/doc.pdf\n"
                "https://example.com/img.png\n"
                "https://example.com/video.mp4",
                [("https://example.com/img.png", "")],
            ),
        ],
        ids=[
            "png",
            "uppercase-jpg",
            "gif",
            "webp",
            "whitespace",
            "query-string",
            "among-text",
            "non-image",
            "no-extension",
            "non-image-among-images",
        ],
    )
    def test_bare_urls(self, body: str, expected: list[tuple[str, str]]) -> None:
        assert extract_image_refs(body) == expected

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            # Duplicates are dropped, first occurrence wins.
            (
                "![first](https://example.com/a.png)\n"
                "![second](https://example.com/a.png)",
                [("https://example.com/a.png", "first")],
            ),
            (
                "https://example.com/a.png\n"
                "https://example.com/a.png\n"
                "https://example.com/b.jpg",
                [
                    ("https://example.com/a.png", ""),
                    ("https://example.com/b.jpg", ""),
                ],
            ),
            # A Markdown image always wins over a bare URL for the same URL,
            # whichever comes first in source order.
            (
                "![labeled](https://example.com/photo.png)\n"
                "https://example.com/photo.png",
                [("https://example.com/photo.png", "labeled")],
            ),
            (
                "https://example.com/x.jpg\n![img](https://example.com/x.jpg)",
                [("https://example.com/x.jpg", "img")],
            ),
        ],
        ids=["duplicate-markdown", "duplicate-bare", "markdown-first", "bare-first"],
    )
    def test_duplicates_and_precedence(
        self, body: str, expected: list[tuple[str, str]]
    ) -> None:
        assert extract_image_refs(body) == expected

    def test_images_among_text(self) -> None:
        """Images embedded among text are extracted in source order."""
        body = (
            "Here is some text.\n"
            "![screenshot](https://example.com/ss.png)\n"
            "More text here.\n"
            "https://example.com/diagram.jpg\n"
            "End of message."
        )
        assert extract_image_refs(body) == [
            ("https://example.com/ss.png", "screenshot"),
            ("https://example.com/diagram.jpg", ""),
        ]

    def test_multiple_images(self) -> None:
        body = (
            "![a](https://a.com/1.png)\n"
            "![b](https://b.com/2.jpg)\n"
            "![c](https://c.com/3.gif)"
        )
        assert extract_image_refs(body) == [
            ("https://a.com/1.png", "a"),
            ("https://b.com/2.jpg", "b"),
            ("https://c.com/3.gif", "c"),
        ]
        assert extract_image_refs("\n![a](https://a.com/1.png)\n\n") == [
            ("https://a.com/1.png", "a")
        ]


# ===========================================================================
# extract_attachment_refs
# ===========================================================================


class TestExtractAttachmentRefs:
    """Tests for :func:`extract_attachment_refs`."""

    @staticmethod
    def _tracker(upload_urls: set[str]) -> Mock:
        tracker = Mock()
        tracker.is_upload_url = Mock(side_effect=lambda url: url in upload_urls)
        return tracker

    @pytest.mark.parametrize(
        ("body", "upload_urls", "expected"),
        [
            # Image extraction keeps working with a tracker that rejects links.
            (
                "![a](https://example.com/a.png)\nhttps://example.com/b.jpg",
                set(),
                [
                    AttachmentRef("https://example.com/a.png", "a"),
                    AttachmentRef("https://example.com/b.jpg", ""),
                ],
            ),
            # A plain Markdown link to an upload is extracted.
            (
                "[archive.zip](https://uploads.linear.app/abc/archive.zip)",
                {"https://uploads.linear.app/abc/archive.zip"},
                [
                    AttachmentRef(
                        "https://uploads.linear.app/abc/archive.zip", "archive.zip"
                    )
                ],
            ),
            # A bare upload URL is extracted.
            (
                "https://uploads.linear.app/abc/download",
                {"https://uploads.linear.app/abc/download"},
                [AttachmentRef("https://uploads.linear.app/abc/download", "")],
            ),
            # An optional trailing title on a plain link is accepted.
            (
                '[f.bin](https://uploads.linear.app/a/f.bin "the file")',
                {"https://uploads.linear.app/a/f.bin"},
                [AttachmentRef("https://uploads.linear.app/a/f.bin", "f.bin")],
            ),
            # Image syntax must not also match the plain-link pass.
            (
                "![alt](https://uploads.linear.app/a/pic.png)",
                {"https://uploads.linear.app/a/pic.png"},
                [AttachmentRef("https://uploads.linear.app/a/pic.png", "alt")],
            ),
        ],
        ids=["images", "upload-link", "upload-bare", "link-title", "image-not-link"],
    )
    def test_extracted_refs(
        self, body: str, upload_urls: set[str], expected: list[AttachmentRef]
    ) -> None:
        assert extract_attachment_refs(body, self._tracker(upload_urls)) == expected

    @pytest.mark.parametrize(
        "body",
        [
            # Ordinary repo/PR links are not uploads.
            "[a PR](https://github.com/org/repo/pull/1)",
            # Neither are bare non-image URLs.
            "https://example.com/page",
        ],
        ids=["link", "bare-url"],
    )
    def test_ignored_refs(self, body: str) -> None:
        assert extract_attachment_refs(body, self._tracker(set())) == []

    def test_order_and_dedup(self) -> None:
        """Order is images, then links, then bare uploads; an image wins for a
        URL that is also used as a link."""
        img = "https://example.com/pic.png"
        link = "https://uploads.linear.app/a/one.zip"
        bare = "https://uploads.linear.app/a/two.bin"
        body = f"{bare}\n[one.zip]({link})\n![pic]({img})"
        assert extract_attachment_refs(body, self._tracker({link, bare})) == [
            AttachmentRef(img, "pic"),
            AttachmentRef(link, "one.zip"),
            AttachmentRef(bare, ""),
        ]

        same = "https://uploads.linear.app/abc/x.png"
        assert extract_attachment_refs(
            f"[link text]({same})\n![alt]({same})", self._tracker({same})
        ) == [AttachmentRef(same, "alt")]


# ===========================================================================
# process_attachments
# ===========================================================================


class TestProcessAttachments:
    """Tests for :func:`process_attachments`."""

    @staticmethod
    def _fake_tracker(
        responses: dict[str, tuple[bytes, str | None] | Exception],
        upload_urls: set[str] | None = None,
    ) -> Mock:
        """Return a Mock whose ``download_attachment`` maps URLs to
        ``(data, content_type)`` or raises an exception.

        ``is_upload_url`` reports a URL as uploadable when it is in
        *upload_urls*; when that is omitted it falls back to "in
        *responses*", which is the useful default for tests that only
        exercise markdown-image extraction.
        """
        tracker = Mock()
        tracker.download_attachment = Mock(
            side_effect=lambda url, _responses=responses: (
                _responses[url]
                if not isinstance(_responses.get(url), Exception)
                else (_ for _ in ()).throw(_responses[url])  # type: ignore[union-attr]
            )
        )
        if upload_urls is None:
            upload_urls = {url for url in responses}
        tracker.is_upload_url = Mock(side_effect=lambda url: url in upload_urls)
        return tracker

    def test_multiple_images(self, tmp_path) -> None:
        """Multiple images are downloaded, written, and rewritten in order."""
        body = (
            "![a](https://a.com/1.png)\n"
            "![b](https://b.com/2.jpg)\n"
            "text\n"
            "https://c.com/3.gif"
        )
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {
                "https://a.com/1.png": (b"aaa", "image/png"),
                "https://b.com/2.jpg": (b"bbb", "image/jpeg"),
                "https://c.com/3.gif": (b"ccc", "image/gif"),
            }
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == [
            f"{SANDBOX_MOUNT}/img-0001.png",
            f"{SANDBOX_MOUNT}/img-0002.jpg",
            f"{SANDBOX_MOUNT}/img-0003.gif",
        ]
        assert result.skipped == []
        assert result.rewritten_body == (
            f"![a]({SANDBOX_MOUNT}/img-0001.png)\n"
            f"![b]({SANDBOX_MOUNT}/img-0002.jpg)\n"
            "text\n"
            f"![]({SANDBOX_MOUNT}/img-0003.gif)"
        )
        assert (attachments_dir / "img-0001.png").read_bytes() == b"aaa"
        assert (attachments_dir / "img-0002.jpg").read_bytes() == b"bbb"

    @pytest.mark.parametrize(
        "body", ["", "Just some plain text.\nNo images here."], ids=["empty", "plain"]
    )
    def test_body_without_refs_unchanged(self, tmp_path, body: str) -> None:
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result == AttachmentResult(
            rewritten_body=body, file_paths=[], skipped=[]
        )

    def test_plain_link_to_non_upload_ignored(self, tmp_path) -> None:
        """Ordinary links are left alone; no download is attempted."""
        body = "[a PR](https://github.com/org/repo/pull/1)"
        tracker = self._fake_tracker({})

        result = process_attachments(body, tracker, str(tmp_path / "a"))

        assert result.rewritten_body == body
        assert result.file_paths == []
        assert result.skipped == []
        tracker.download_attachment.assert_not_called()

    @pytest.mark.parametrize(
        ("body", "response", "expected_body", "expected_written"),
        [
            # No URL extension: derive it from the Content-Type.
            (
                "![img](https://example.com/raw/image?id=42)",
                (b"jpegdata", "image/jpeg"),
                f"![img]({SANDBOX_MOUNT}/img-0001.jpg)",
                "img-0001.jpg",
            ),
            # An opaque Content-Type still lets the URL extension decide.
            (
                "![photo](https://uploads.linear.app/abc/photo.png)",
                (b"png", "application/octet-stream"),
                f"![photo]({SANDBOX_MOUNT}/img-0001.png)",
                "img-0001.png",
            ),
            (
                "![modern](https://example.com/modern.webp)",
                (b"webpdata", "image/webp"),
                f"![modern]({SANDBOX_MOUNT}/img-0001.webp)",
                "img-0001.webp",
            ),
            # A bare upload with no link text falls back to the Content-Type.
            (
                "https://uploads.linear.app/abc/download",
                (b"data", "application/zip"),
                f"{SANDBOX_MOUNT}/file-0001.zip",
                "file-0001.zip",
            ),
            # With nothing to name it, a bare upload becomes plain file-NNNN.
            (
                "https://uploads.linear.app/abc/download",
                (b"data", None),
                f"{SANDBOX_MOUNT}/file-0001",
                "file-0001",
            ),
            # An image-syntax ref with no type info still uses its alt text,
            # stored as a generic file and downgraded to a link.
            (
                "![thing](https://example.com/opaque)",
                (b"binary", None),
                f"[thing]({SANDBOX_MOUNT}/file-0001-thing)",
                "file-0001-thing",
            ),
        ],
        ids=[
            "content-type",
            "url-extension",
            "webp",
            "bare-upload",
            "bare-upload-unnamed",
            "alt-text",
        ],
    )
    def test_filename_and_link_derivation(
        self,
        tmp_path,
        body: str,
        response: tuple[bytes, str | None],
        expected_body: str,
        expected_written: str,
    ) -> None:
        url = _single_url(body)
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: response})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.rewritten_body == expected_body
        assert result.skipped == []
        assert (attachments_dir / expected_written).is_file()

    @pytest.mark.parametrize(
        ("body", "response", "expected_body", "expected_written"),
        [
            (
                "![doc](https://example.com/doc.pdf)",
                (b"pdfdata", "application/pdf"),
                f"[doc]({SANDBOX_MOUNT}/file-0001-doc)",
                "file-0001-doc",
            ),
            (
                "![file](https://example.com/download)",
                (b"svgdata", "image/svg+xml"),
                f"[file]({SANDBOX_MOUNT}/file-0001-file)",
                "file-0001-file",
            ),
            # A declared non-image type beats a misleading .png URL.
            (
                "![photo](https://uploads.linear.app/abc/photo.png)",
                (b"<html>", "text/html"),
                f"[photo]({SANDBOX_MOUNT}/file-0001-photo)",
                "file-0001-photo",
            ),
        ],
        ids=["pdf", "svg", "html-over-png"],
    )
    def test_non_image_target_stored_generically(
        self,
        tmp_path,
        body: str,
        response: tuple[bytes, str | None],
        expected_body: str,
        expected_written: str,
    ) -> None:
        url = _single_url(body)
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: response})

        result = process_attachments(body, tracker, str(attachments_dir))

        # Non-images are never handed to the agent as --file arguments.
        assert result.file_paths == []
        assert result.skipped == []
        assert result.rewritten_body == expected_body
        assert (attachments_dir / expected_written).read_bytes() == response[0]

    def test_plain_link_zip_keeps_link_text(self, tmp_path) -> None:
        """A plain link to an upload is downloaded and its link text is kept."""
        url = "https://uploads.linear.app/abc/archive.zip"
        body = f"Please inspect [archive.zip]({url})."
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"PK\x03\x04zip", "application/zip")})

        result = process_attachments(body, tracker, str(attachments_dir))

        sandbox_path = f"{SANDBOX_MOUNT}/file-0001-archive.zip"
        assert result.rewritten_body == f"Please inspect [archive.zip]({sandbox_path})."
        assert result.file_paths == []
        assert result.skipped == []
        assert (
            attachments_dir / "file-0001-archive.zip"
        ).read_bytes() == b"PK\x03\x04zip"

    def test_image_and_generic_share_index(self, tmp_path) -> None:
        """Images and non-image files draw from one shared index."""
        img = "https://example.com/pic.png"
        doc = "https://uploads.linear.app/abc/notes.pdf"
        body = f"![pic]({img})\n[notes.pdf]({doc})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {
                img: (b"png", "image/png"),
                doc: (b"pdf", "application/pdf"),
            }
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == [f"{SANDBOX_MOUNT}/img-0001.png"]
        assert result.rewritten_body == (
            f"![pic]({SANDBOX_MOUNT}/img-0001.png)\n"
            f"[notes.pdf]({SANDBOX_MOUNT}/file-0002-notes.pdf)"
        )
        assert result.next_index == 2
        assert (attachments_dir / "file-0002-notes.pdf").exists()

    def test_generic_name_sanitised_and_truncated(self, tmp_path) -> None:
        """Path separators are replaced and over-long names are truncated."""
        url = "https://uploads.linear.app/abc/evil"
        body = f"[../../etc/passwd]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"data", "application/octet-stream")})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert ".." in result.rewritten_body  # link text preserved in the prompt
        name = next(attachments_dir.iterdir()).name
        assert name.startswith("file-0001-")
        assert "/" not in name and "\\" not in name
        # The path component of the name must not escape the attachments dir.
        assert os.path.basename(name) == name

        long_url = "https://uploads.linear.app/abc/long"
        long_body = f"[{'x' * 200}.zip]({long_url})"
        long_dir = tmp_path / "attachments-long"
        process_attachments(
            long_body,
            self._fake_tracker({long_url: (b"data", "application/zip")}),
            str(long_dir),
        )
        assert len(next(long_dir.iterdir()).name) <= len("file-0001-") + 80

    @pytest.mark.parametrize(
        ("existing_count", "body", "responses", "expected_paths", "expected_next"),
        [
            (
                5,
                "![first](https://example.com/first.png)",
                {"https://example.com/first.png": (b"data", "image/png")},
                [f"{SANDBOX_MOUNT}/img-0006.png"],
                6,
            ),
            (
                3,
                "![a](https://a.com/a.png)\n![b](https://b.com/b.jpg)",
                {
                    "https://a.com/a.png": (b"a", "image/png"),
                    "https://b.com/b.jpg": (b"b", "image/jpeg"),
                },
                [f"{SANDBOX_MOUNT}/img-0004.png", f"{SANDBOX_MOUNT}/img-0005.jpg"],
                5,
            ),
            (
                4,
                "[a.zip](https://uploads.linear.app/abc/a.zip)",
                {"https://uploads.linear.app/abc/a.zip": (b"data", "application/zip")},
                [],
                5,
            ),
        ],
        ids=["single", "multiple", "generic"],
    )
    def test_existing_count_offsets_numbering(
        self,
        tmp_path,
        existing_count: int,
        body: str,
        responses: dict[str, tuple[bytes, str | None]],
        expected_paths: list[str],
        expected_next: int,
    ) -> None:
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(responses)

        result = process_attachments(
            body, tracker, str(attachments_dir), existing_count=existing_count
        )

        assert result.file_paths == expected_paths
        assert result.next_index == expected_next

    def test_next_index_accounts_for_skipped_refs(self, tmp_path) -> None:
        """next_index is existing_count + len(refs) regardless of success.

        If turn 1 has 2 refs and ref #1 fails, the loop still consumes
        index 2 for ref #2.  next_index must be 2 so that turn 2's first
        new file starts at index 3 — not 2 (which would collide with the
        file already written at index 2).
        """
        body = "![bad](https://bad.com/bad.png)\n![good](https://good.com/good.jpg)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {
                "https://bad.com/bad.png": AttachmentDownloadError("nope"),
                "https://good.com/good.jpg": (b"yay", "image/jpeg"),
            }
        )

        result = process_attachments(
            body, tracker, str(attachments_dir), existing_count=0
        )

        # Only one file was written (index 2).
        assert result.file_paths == [f"{SANDBOX_MOUNT}/img-0002.jpg"]
        # But next_index must be 2 because 2 indices were consumed.
        assert result.next_index == 2
        # File at index 1 was never written (ref #1 failed).
        assert not (attachments_dir / "img-0001.png").exists()
        assert (attachments_dir / "img-0002.jpg").exists()

        # Turn 2: using next_index as existing_count, numbering starts at 3.
        tracker2 = self._fake_tracker(
            {"https://fresh.com/new.png": (b"new", "image/png")}
        )
        result2 = process_attachments(
            "![fresh](https://fresh.com/new.png)",
            tracker2,
            str(attachments_dir),
            existing_count=result.next_index,  # = 2
        )
        assert result2.file_paths == [f"{SANDBOX_MOUNT}/img-0003.png"]
        assert result2.next_index == 3

    @pytest.mark.parametrize(
        (
            "body",
            "responses",
            "cap",
            "expected_paths",
            "expected_files",
            "skipped",
            "expected_body",
        ),
        [
            (
                "![a](https://example.com/a.png)\n![b](https://example.com/b.png)",
                {
                    "https://example.com/a.png": (b"12345678", "image/png"),
                    "https://example.com/b.png": (b"12345", "image/png"),
                },
                10,
                [f"{SANDBOX_MOUNT}/img-0001.png"],
                ["img-0001.png"],
                [("https://example.com/b.png", "turn byte cap exceeded")],
                f"![a]({SANDBOX_MOUNT}/img-0001.png)\n![b](https://example.com/b.png)",
            ),
            (
                "![a](https://example.com/a.png)\n"
                "![b](https://example.com/b.png)\n"
                "![c](https://example.com/c.png)",
                {
                    "https://example.com/a.png": (b"1234", "image/png"),
                    "https://example.com/b.png": (b"5678", "image/png"),
                    "https://example.com/c.png": (b"90", "image/png"),
                },
                9,
                [
                    f"{SANDBOX_MOUNT}/img-0001.png",
                    f"{SANDBOX_MOUNT}/img-0002.png",
                ],
                ["img-0001.png", "img-0002.png"],
                [("https://example.com/c.png", "turn byte cap exceeded")],
                f"![a]({SANDBOX_MOUNT}/img-0001.png)\n"
                f"![b]({SANDBOX_MOUNT}/img-0002.png)\n"
                "![c](https://example.com/c.png)",
            ),
            # The cap applies to non-image files too.
            (
                "[big.zip](https://uploads.linear.app/abc/big.zip)",
                {
                    "https://uploads.linear.app/abc/big.zip": (
                        b"12345",
                        "application/zip",
                    )
                },
                4,
                [],
                [],
                [("https://uploads.linear.app/abc/big.zip", "turn byte cap exceeded")],
                "[big.zip](https://uploads.linear.app/abc/big.zip)",
            ),
        ],
        ids=["images", "multiple-skips", "generic"],
    )
    def test_byte_cap(
        self,
        tmp_path,
        body: str,
        responses: dict[str, tuple[bytes, str | None]],
        cap: int,
        expected_paths: list[str],
        expected_files: list[str],
        skipped: list[tuple[str, str]],
        expected_body: str,
    ) -> None:
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(responses)

        result = process_attachments(
            body, tracker, str(attachments_dir), per_turn_byte_cap=cap
        )

        assert result.file_paths == expected_paths
        assert result.skipped == skipped
        assert result.rewritten_body == expected_body
        # Only the files that fit are left behind; a skipped one leaves none.
        assert sorted(p.name for p in attachments_dir.iterdir()) == expected_files

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (AttachmentDownloadError("gone"), "download failed"),
            (AttachmentTooLargeError(">10 MB"), "attachment too large"),
            # Any unexpected exception during download is caught and skipped.
            (ValueError("boom"), "download failed"),
        ],
        ids=["download-error", "too-large", "unexpected"],
    )
    def test_download_failure_is_skipped(
        self, tmp_path, error: Exception, reason: str
    ) -> None:
        url = "https://example.com/missing.png"
        body = f"![bad]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: error})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == []
        assert result.skipped == [(url, reason)]
        assert result.rewritten_body == body

    def test_mixed_success_and_failure(self, tmp_path) -> None:
        """Some images succeed while others fail; body is partially rewritten."""
        body = (
            "![ok](https://ok.com/ok.png)\n"
            "![bad](https://bad.com/bad.png)\n"
            "![fine](https://fine.com/fine.jpg)\n"
            "![huge](https://huge.com/huge.png)"
        )
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {
                "https://ok.com/ok.png": (b"ok", "image/png"),
                "https://bad.com/bad.png": AttachmentDownloadError("nope"),
                "https://fine.com/fine.jpg": (b"fine", "image/jpeg"),
                "https://huge.com/huge.png": AttachmentTooLargeError("huge"),
            }
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == [
            f"{SANDBOX_MOUNT}/img-0001.png",
            f"{SANDBOX_MOUNT}/img-0003.jpg",
        ]
        assert result.skipped == [
            ("https://bad.com/bad.png", "download failed"),
            ("https://huge.com/huge.png", "attachment too large"),
        ]
        assert result.rewritten_body == (
            f"![ok]({SANDBOX_MOUNT}/img-0001.png)\n"
            "![bad](https://bad.com/bad.png)\n"
            f"![fine]({SANDBOX_MOUNT}/img-0003.jpg)\n"
            "![huge](https://huge.com/huge.png)"
        )

    @pytest.mark.parametrize(
        ("body", "responses", "expected_body"),
        [
            (
                "![first](https://example.com/img.png)\n"
                "![second](https://example.com/img.png)",
                {"https://example.com/img.png": (b"data", "image/png")},
                f"![first]({SANDBOX_MOUNT}/img-0001.png)\n"
                f"![second]({SANDBOX_MOUNT}/img-0001.png)",
            ),
            (
                "https://example.com/diag.png\nhttps://example.com/diag.png",
                {"https://example.com/diag.png": (b"data", "image/png")},
                f"![]({SANDBOX_MOUNT}/img-0001.png)\n![]({SANDBOX_MOUNT}/img-0001.png)",
            ),
            # A URL used both as a Markdown image and as a bare URL is
            # rewritten in both forms (regression: only one used to be).
            (
                "![screenshot](https://example.com/img.png)\n"
                "Some text\n"
                "https://example.com/img.png\n"
                "More text",
                {"https://example.com/img.png": (b"data", "image/png")},
                f"![screenshot]({SANDBOX_MOUNT}/img-0001.png)\n"
                "Some text\n"
                f"![]({SANDBOX_MOUNT}/img-0001.png)\n"
                "More text",
            ),
        ],
        ids=["markdown-duplicate", "bare-duplicate", "mixed-forms"],
    )
    def test_duplicate_url_rewritten_everywhere(
        self,
        tmp_path,
        body: str,
        responses: dict[str, tuple[bytes, str | None]],
        expected_body: str,
    ) -> None:
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(responses)

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == [f"{SANDBOX_MOUNT}/img-0001.png"]
        assert result.rewritten_body == expected_body

    @pytest.mark.parametrize(
        ("body", "responses", "expected_body", "expected_files"),
        [
            # Regression: the bare-URL pass used a global ``str.replace``, so
            # rewriting ``.../a`` also rewrote the ``.../a`` prefix of
            # ``.../a/b.zip`` on the following line.
            (
                "https://uploads.linear.app/a\nhttps://uploads.linear.app/a/b.zip",
                {
                    "https://uploads.linear.app/a": (b"1", None),
                    "https://uploads.linear.app/a/b.zip": (b"2", "application/zip"),
                },
                f"{SANDBOX_MOUNT}/file-0001\n{SANDBOX_MOUNT}/file-0002.zip",
                ["file-0001", "file-0002.zip"],
            ),
            # Same regression for an image URL that is a prefix of a later
            # upload: the image is processed first.
            (
                "https://uploads.linear.app/a.png\nhttps://uploads.linear.app/a.png/b.zip",
                {
                    "https://uploads.linear.app/a.png": (b"png", "image/png"),
                    "https://uploads.linear.app/a.png/b.zip": (
                        b"zip",
                        "application/zip",
                    ),
                },
                f"![]({SANDBOX_MOUNT}/img-0001.png)\n{SANDBOX_MOUNT}/file-0002.zip",
                ["img-0001.png", "file-0002.zip"],
            ),
        ],
        ids=["bare", "image"],
    )
    def test_shared_prefix_url_not_corrupted(
        self,
        tmp_path,
        body: str,
        responses: dict[str, tuple[bytes, str | None]],
        expected_body: str,
        expected_files: list[str],
    ) -> None:
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(responses)

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.rewritten_body == expected_body
        assert sorted(p.name for p in attachments_dir.iterdir()) == sorted(
            expected_files
        )

    def test_custom_sandbox_mount(self, tmp_path) -> None:
        """The *sandbox_mount* prefix is honoured in returned paths."""
        body = "![logo](https://example.com/logo.png)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/logo.png": (b"data", "image/png")}
        )

        result = process_attachments(
            body, tracker, str(attachments_dir), sandbox_mount="/mnt/imgs"
        )

        assert result.file_paths == ["/mnt/imgs/img-0001.png"]
        assert result.rewritten_body == "![logo](/mnt/imgs/img-0001.png)"
