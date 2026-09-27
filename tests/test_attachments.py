"""Tests for the attachments helpers."""

from __future__ import annotations

import os
from unittest.mock import Mock


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


class TestExtractImageRefs:
    # ------- empty / no matches ------------------------------------------

    def test_empty_string(self) -> None:
        """An empty body returns an empty list."""
        assert extract_image_refs("") == []

    def test_no_images_plain_text(self) -> None:
        """Plain text with no image references returns an empty list."""
        assert extract_image_refs("Hello, world!") == []

    def test_no_images_link(self) -> None:
        """A regular Markdown link (not an image) is ignored."""
        assert extract_image_refs("[link](https://example.com)") == []

    # ------- single Markdown image --------------------------------------

    def test_single_markdown_image(self) -> None:
        """A single Markdown image is extracted."""
        body = "here is an image: ![alt text](https://example.com/img.png)"
        assert extract_image_refs(body) == [
            ("https://example.com/img.png", "alt text"),
        ]

    def test_single_image_no_alt(self) -> None:
        """An image with an empty alt-text works."""
        body = "![](https://example.com/photo.jpg)"
        assert extract_image_refs(body) == [
            ("https://example.com/photo.jpg", ""),
        ]

    def test_single_image_with_title(self) -> None:
        """An image with a trailing title in double quotes is parsed correctly."""
        body = '![logo](https://example.com/logo.png "The Logo")'
        assert extract_image_refs(body) == [
            ("https://example.com/logo.png", "logo"),
        ]

    def test_single_image_whitespace_around_url(self) -> None:
        """Whitespace inside the parens around the URL is tolerated."""
        body = "![x](  https://example.com/pic.jpeg  )"
        assert extract_image_refs(body) == [
            ("https://example.com/pic.jpeg", "x"),
        ]

    # ------- multiple images --------------------------------------------

    def test_multiple_images(self) -> None:
        """Multiple images are returned in source order."""
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

    # ------- alt text with brackets -------------------------------------

    def test_alt_text_with_brackets(self) -> None:
        """Alt text containing square brackets is captured."""
        body = "![foo [bar] baz](https://example.com/img.webp)"
        refs = extract_image_refs(body)
        assert len(refs) == 1
        assert refs[0][0] == "https://example.com/img.webp"
        assert refs[0][1] == "foo [bar] baz"

    def test_alt_text_with_multiple_brackets(self) -> None:
        """Alt text with several bracket pairs is captured."""
        body = "![a [b] c [d] e](https://example.com/pic.png)"
        refs = extract_image_refs(body)
        assert len(refs) == 1
        assert refs[0][1] == "a [b] c [d] e"

    # ------- bare URL ---------------------------------------------------

    def test_bare_url_png(self) -> None:
        """A bare .png URL on its own line is detected."""
        body = "https://example.com/screenshot.png"
        assert extract_image_refs(body) == [
            ("https://example.com/screenshot.png", ""),
        ]

    def test_bare_url_jpg(self) -> None:
        """A bare .jpg URL on its own line is detected (case-insensitive)."""
        body = "HTTP://EXAMPLE.COM/PHOTO.JPG"
        assert extract_image_refs(body) == [
            ("HTTP://EXAMPLE.COM/PHOTO.JPG", ""),
        ]

    def test_bare_url_gif(self) -> None:
        """A bare .gif URL on its own line is detected."""
        body = "https://example.com/animation.gif"
        assert extract_image_refs(body) == [
            ("https://example.com/animation.gif", ""),
        ]

    def test_bare_url_webp(self) -> None:
        """A bare .webp URL on its own line is detected."""
        body = "https://example.com/modern.webp"
        assert extract_image_refs(body) == [
            ("https://example.com/modern.webp", ""),
        ]

    def test_bare_url_with_whitespace(self) -> None:
        """Leading / trailing whitespace around a bare URL is tolerated."""
        body = "   https://example.com/pic.png   "
        assert extract_image_refs(body) == [
            ("https://example.com/pic.png", ""),
        ]

    # ------- URL with query string --------------------------------------

    def test_url_with_query_string_markdown(self) -> None:
        """A Markdown image URL with a query string is extracted."""
        body = "![chart](https://example.com/chart.png?width=800&height=600)"
        assert extract_image_refs(body) == [
            ("https://example.com/chart.png?width=800&height=600", "chart"),
        ]

    def test_url_with_query_string_bare(self) -> None:
        """A bare image URL with a query string is detected."""
        body = "https://example.com/photo.jpg?v=2&size=large"
        assert extract_image_refs(body) == [
            ("https://example.com/photo.jpg?v=2&size=large", ""),
        ]

    # ------- mixed text and images --------------------------------------

    def test_mixed_text_and_images(self) -> None:
        """Images embedded among text are extracted while text is ignored."""
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

    # ------- duplicate URLs (dedup) -------------------------------------

    def test_duplicate_markdown_urls(self) -> None:
        """First occurrence of a Markdown URL wins, duplicates dropped."""
        body = (
            "![first](https://example.com/a.png)\n![second](https://example.com/a.png)"
        )
        assert extract_image_refs(body) == [
            ("https://example.com/a.png", "first"),
        ]

    def test_duplicate_bare_urls(self) -> None:
        """First occurrence of a bare URL wins, duplicates dropped."""
        body = (
            "https://example.com/a.png\n"
            "https://example.com/a.png\n"
            "https://example.com/b.jpg"
        )
        assert extract_image_refs(body) == [
            ("https://example.com/a.png", ""),
            ("https://example.com/b.jpg", ""),
        ]

    def test_markdown_wins_over_bare_for_same_url(self) -> None:
        """A Markdown image takes precedence over a later bare URL match."""
        body = (
            "![labeled](https://example.com/photo.png)\nhttps://example.com/photo.png"
        )
        assert extract_image_refs(body) == [
            ("https://example.com/photo.png", "labeled"),
        ]

    def test_duplicate_across_both_passes(self) -> None:
        """Duplicates across both passes are dropped; the Markdown match
        always wins for the same URL, even when the bare URL appears
        earlier in source order (per the spec: 'Markdown matches win
        for the same URL')."""
        body = "https://example.com/x.jpg\n![img](https://example.com/x.jpg)"
        assert extract_image_refs(body) == [
            ("https://example.com/x.jpg", "img"),
        ]

    # ------- non-image extensions (ignored) -----------------------------

    def test_bare_url_non_image_extensions_ignored(self) -> None:
        """Bare URLs with non-image extensions are ignored."""
        body = "https://example.com/file.pdf"
        assert extract_image_refs(body) == []

    def test_bare_url_no_extension(self) -> None:
        """A bare URL with no extension is ignored."""
        body = "https://example.com/page"
        assert extract_image_refs(body) == []

    def test_bare_url_non_image_among_images(self) -> None:
        """Only image-extension bare URLs are extracted."""
        body = (
            "https://example.com/doc.pdf\n"
            "https://example.com/img.png\n"
            "https://example.com/video.mp4"
        )
        assert extract_image_refs(body) == [
            ("https://example.com/img.png", ""),
        ]

    # ------- regression / edge cases ------------------------------------

    def test_url_with_special_chars(self) -> None:
        """URLs containing hyphens, underscores, digits work."""
        body = "![img](https://cdn.example.com/user_123/image-v2.png)"
        assert extract_image_refs(body) == [
            ("https://cdn.example.com/user_123/image-v2.png", "img"),
        ]

    def test_url_not_at_beginning_of_line_bare(self) -> None:
        """A bare URL must occupy its whole line; inline image-urls are not
        treated as bare URLs."""
        body = "see https://example.com/pic.png for details"
        assert extract_image_refs(body) == []

    def test_multiline_body(self) -> None:
        """Images spread across multiple lines are all found in order."""
        body = "\n![a](https://a.com/1.png)\n\n![b](https://b.com/2.jpg)\n"
        assert extract_image_refs(body) == [
            ("https://a.com/1.png", "a"),
            ("https://b.com/2.jpg", "b"),
        ]

    def test_bare_url_sandwiched_between_text(self) -> None:
        """A bare URL line between text lines is found."""
        body = "Some text above\nhttps://example.com/diagram.png\nSome text below"
        assert extract_image_refs(body) == [
            ("https://example.com/diagram.png", ""),
        ]

    def test_html_img_tag_ignored(self) -> None:
        """HTML <img> tags are intentionally not parsed."""
        body = '<img src="https://example.com/photo.png" alt="photo">'
        assert extract_image_refs(body) == []

    def test_markdown_image_with_html_entities_in_alt(self) -> None:
        """Alt text containing HTML-entity-looking sequences is fine."""
        body = "![&lt;hello&gt;](https://example.com/icon.png)"
        assert extract_image_refs(body) == [
            ("https://example.com/icon.png", "&lt;hello&gt;"),
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

    def test_images_are_included_unchanged(self) -> None:
        """Image extraction keeps working with a tracker that rejects links."""
        body = "![a](https://example.com/a.png)\nhttps://example.com/b.jpg"
        refs = extract_attachment_refs(body, self._tracker(set()))
        assert refs == [
            AttachmentRef("https://example.com/a.png", "a"),
            AttachmentRef("https://example.com/b.jpg", ""),
        ]

    def test_plain_link_to_upload_is_picked_up(self) -> None:
        """A plain Markdown link whose URL is an upload is extracted."""
        url = "https://uploads.linear.app/abc/archive.zip"
        body = f"[archive.zip]({url})"
        refs = extract_attachment_refs(body, self._tracker({url}))
        assert refs == [AttachmentRef(url, "archive.zip")]

    def test_plain_link_to_non_upload_ignored(self) -> None:
        """Ordinary repo/PR-style links are not extracted."""
        body = "[a PR](https://github.com/org/repo/pull/1)"
        assert extract_attachment_refs(body, self._tracker(set())) == []

    def test_bare_upload_url_picked_up(self) -> None:
        """A bare URL that is an upload but not an image is extracted."""
        url = "https://uploads.linear.app/abc/download"
        body = url
        refs = extract_attachment_refs(body, self._tracker({url}))
        assert refs == [AttachmentRef(url, "")]

    def test_bare_non_upload_non_image_ignored(self) -> None:
        """A bare non-image URL that is not an upload is ignored."""
        body = "https://example.com/page"
        assert extract_attachment_refs(body, self._tracker(set())) == []

    def test_dedup_images_win_source_order(self) -> None:
        """A URL used both as a plain link and as an image stays an image.

        The image pass runs first, so the image's alt text is retained.
        """
        url = "https://uploads.linear.app/abc/x.png"
        body = f"[link text]({url})\n![alt]({url})"
        refs = extract_attachment_refs(body, self._tracker({url}))
        assert refs == [AttachmentRef(url, "alt")]

    def test_mixed_order_is_images_then_links_then_bare(self) -> None:
        """Extraction order: markdown images, links, then bare uploads."""
        img = "https://example.com/pic.png"
        link = "https://uploads.linear.app/a/one.zip"
        bare = "https://uploads.linear.app/a/two.bin"
        body = f"{bare}\n[one.zip]({link})\n![pic]({img})"
        refs = extract_attachment_refs(body, self._tracker({link, bare}))
        assert refs == [
            AttachmentRef(img, "pic"),
            AttachmentRef(link, "one.zip"),
            AttachmentRef(bare, ""),
        ]

    def test_link_with_title_is_parsed(self) -> None:
        """An optional trailing title on a plain link is accepted."""
        url = "https://uploads.linear.app/a/f.bin"
        body = f'[f.bin]({url} "the file")'
        refs = extract_attachment_refs(body, self._tracker({url}))
        assert refs == [AttachmentRef(url, "f.bin")]

    def test_image_syntax_is_not_double_counted_as_link(self) -> None:
        """Image syntax must not also match the plain-link pass."""
        url = "https://uploads.linear.app/a/pic.png"
        body = f"![alt]({url})"
        refs = extract_attachment_refs(body, self._tracker({url}))
        assert refs == [AttachmentRef(url, "alt")]


# ===========================================================================
# process_attachments
# ===========================================================================


class TestProcessAttachments:
    """Tests for :func:`process_attachments`."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_single_png(self, tmp_path) -> None:
        """One Markdown PNG image is downloaded, written, and rewritten."""
        body = "![screenshot](https://example.com/ss.png)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/ss.png": (b"\x89PNG\r\n\x1a\nfake", "image/png")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert (
            result.rewritten_body
            == "![screenshot](/tmp/symphony-attachments/img-0001.png)"
        )
        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert result.skipped == []

        # File was written on disk
        written = (attachments_dir / "img-0001.png").read_bytes()
        assert written == b"\x89PNG\r\n\x1a\nfake"

    def test_multiple_images(self, tmp_path) -> None:
        """Multiple images are all downloaded and rewritten in order."""
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
            "/tmp/symphony-attachments/img-0001.png",
            "/tmp/symphony-attachments/img-0002.jpg",
            "/tmp/symphony-attachments/img-0003.gif",
        ]
        assert result.skipped == []
        assert (
            result.rewritten_body == "![a](/tmp/symphony-attachments/img-0001.png)\n"
            "![b](/tmp/symphony-attachments/img-0002.jpg)\n"
            "text\n"
            "![](/tmp/symphony-attachments/img-0003.gif)"
        )

    def test_bare_url_becomes_markdown_form(self, tmp_path) -> None:
        """A bare image URL is replaced with ``![](sandbox_path)``."""
        body = "https://example.com/diagram.png"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/diagram.png": (b"data", "image/png")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.rewritten_body == "![](/tmp/symphony-attachments/img-0001.png)"
        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]

    def test_empty_body(self, tmp_path) -> None:
        """An empty body returns the body unchanged with no work done."""
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({})

        result = process_attachments("", tracker, str(attachments_dir))

        assert result == AttachmentResult(rewritten_body="", file_paths=[], skipped=[])

    def test_no_images_in_body(self, tmp_path) -> None:
        """Plain text with no image refs is returned unchanged."""
        body = "Just some plain text.\nNo images here."
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.rewritten_body == body
        assert result.file_paths == []
        assert result.skipped == []

    # ------------------------------------------------------------------
    # Extension handling
    # ------------------------------------------------------------------

    def test_extension_from_content_type(self, tmp_path) -> None:
        """When the URL has no extension, derive it from Content-Type."""
        body = "![img](https://example.com/raw/image?id=42)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/raw/image?id=42": (b"jpegdata", "image/jpeg")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.jpg"]
        assert result.skipped == []
        written = (attachments_dir / "img-0001.jpg").read_bytes()
        assert written == b"jpegdata"

    def test_image_syntax_non_image_saved_as_generic(self, tmp_path) -> None:
        """An image-syntax ref whose target is not an image is saved generically."""
        body = "![doc](https://example.com/doc.pdf)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/doc.pdf": (b"pdfdata", "application/pdf")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == []
        assert result.skipped == []
        # Downgraded refs become plain links, not image syntax.
        assert result.rewritten_body == (
            "[doc](/tmp/symphony-attachments/file-0001-doc)"
        )
        assert (attachments_dir / "file-0001-doc").read_bytes() == b"pdfdata"

    def test_image_syntax_svg_saved_as_generic(self, tmp_path) -> None:
        """A URL with no extension and a non-image Content-Type is generic."""
        body = "![file](https://example.com/download)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/download": (b"svgdata", "image/svg+xml")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == []
        assert result.skipped == []
        assert result.rewritten_body == (
            "[file](/tmp/symphony-attachments/file-0001-file)"
        )
        assert (attachments_dir / "file-0001-file").read_bytes() == b"svgdata"

    def test_no_extension_no_content_type_named_from_alt(self, tmp_path) -> None:
        """With no type info, an image-syntax ref still uses its alt text."""
        body = "![thing](https://example.com/opaque)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({"https://example.com/opaque": (b"binary", None)})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.skipped == []
        assert result.file_paths == []
        assert result.rewritten_body == (
            "[thing](/tmp/symphony-attachments/file-0001-thing)"
        )
        assert (attachments_dir / "file-0001-thing").read_bytes() == b"binary"

    # ------------------------------------------------------------------
    # Non-image uploads
    # ------------------------------------------------------------------

    def test_plain_link_zip_downloaded_but_not_a_file_path(self, tmp_path) -> None:
        """A plain link to an upload is downloaded and its link text is kept."""
        url = "https://uploads.linear.app/abc/archive.zip"
        body = f"Please inspect [archive.zip]({url})."
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"PK\x03\x04zip", "application/zip")})

        result = process_attachments(body, tracker, str(attachments_dir))

        sandbox_path = "/tmp/symphony-attachments/file-0001-archive.zip"
        assert result.rewritten_body == f"Please inspect [archive.zip]({sandbox_path})."
        # Non-images are never handed to the agent as --file arguments.
        assert result.file_paths == []
        assert result.skipped == []
        assert (
            attachments_dir / "file-0001-archive.zip"
        ).read_bytes() == b"PK\x03\x04zip"

    def test_bare_upload_url_becomes_plain_path(self, tmp_path) -> None:
        """A bare non-image upload URL is replaced by the sandbox path."""
        url = "https://uploads.linear.app/abc/download"
        body = url
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"data", "application/zip")})

        result = process_attachments(body, tracker, str(attachments_dir))

        # No link text: fall back to the Content-Type extension.
        sandbox_path = "/tmp/symphony-attachments/file-0001.zip"
        assert result.rewritten_body == sandbox_path
        assert result.file_paths == []
        assert (attachments_dir / "file-0001.zip").read_bytes() == b"data"

    def test_bare_upload_url_no_name_no_content_type(self, tmp_path) -> None:
        """With nothing to name it, a bare upload becomes plain ``file-NNNN``."""
        url = "https://uploads.linear.app/abc/download"
        tracker = self._fake_tracker({url: (b"data", None)})

        result = process_attachments(url, tracker, str(tmp_path / "a"))

        assert result.rewritten_body == "/tmp/symphony-attachments/file-0001"
        assert (tmp_path / "a" / "file-0001").read_bytes() == b"data"

    def test_plain_link_to_non_upload_ignored(self, tmp_path) -> None:
        """Ordinary links are left alone; no download is attempted."""
        body = "[a PR](https://github.com/org/repo/pull/1)"
        tracker = self._fake_tracker({})

        result = process_attachments(body, tracker, str(tmp_path / "a"))

        assert result.rewritten_body == body
        assert result.file_paths == []
        assert result.skipped == []
        tracker.download_attachment.assert_not_called()

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

        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert result.rewritten_body == (
            "![pic](/tmp/symphony-attachments/img-0001.png)\n"
            "[notes.pdf](/tmp/symphony-attachments/file-0002-notes.pdf)"
        )
        assert result.next_index == 2
        assert (attachments_dir / "file-0002-notes.pdf").exists()

    def test_generic_filename_sanitised_and_truncated(self, tmp_path) -> None:
        """Path separators and unsafe characters are replaced, and it truncates."""
        url = "https://uploads.linear.app/abc/evil"
        body = f"[../../etc/passwd]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"data", "application/octet-stream")})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert ".." in result.rewritten_body  # link text preserved in the prompt
        written = [p.name for p in attachments_dir.iterdir()]
        assert len(written) == 1
        name = written[0]
        assert name.startswith("file-0001-")
        assert "/" not in name and "\\" not in name
        # The path component of the name must not escape the attachments dir.
        assert os.path.basename(name) == name

    def test_generic_name_truncated_to_max_length(self, tmp_path) -> None:
        """An over-long link text is truncated to a filesystem-safe length."""
        url = "https://uploads.linear.app/abc/long"
        body = f"[{'x' * 200}.zip]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"data", "application/zip")})

        process_attachments(body, tracker, str(attachments_dir))

        name = next(attachments_dir.iterdir()).name
        assert len(name) <= len("file-0001-") + 80

    def test_generic_respects_existing_count(self, tmp_path) -> None:
        """Non-image numbering continues from *existing_count*."""
        url = "https://uploads.linear.app/abc/a.zip"
        body = f"[a.zip]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"data", "application/zip")})

        result = process_attachments(
            body, tracker, str(attachments_dir), existing_count=4
        )

        assert result.rewritten_body == (
            "[a.zip](/tmp/symphony-attachments/file-0005-a.zip)"
        )
        assert result.next_index == 5

    def test_generic_byte_cap_exceeded(self, tmp_path) -> None:
        """The per-turn cap applies to non-image files too."""
        url = "https://uploads.linear.app/abc/big.zip"
        body = f"[big.zip]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"12345", "application/zip")})

        result = process_attachments(
            body, tracker, str(attachments_dir), per_turn_byte_cap=4
        )

        assert result.file_paths == []
        assert result.skipped == [(url, "turn byte cap exceeded")]
        assert result.rewritten_body == body
        assert not (attachments_dir / "file-0001-big.zip").exists()

    def test_link_and_bare_same_url_both_rewritten(self, tmp_path) -> None:
        """A generic URL appearing as a link and a bare URL is rewritten twice."""
        url = "https://uploads.linear.app/abc/f.bin"
        body = f"[f.bin]({url})\n{url}"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"data", None)})

        result = process_attachments(body, tracker, str(attachments_dir))

        sandbox_path = "/tmp/symphony-attachments/file-0001-f.bin"
        assert result.rewritten_body == f"[f.bin]({sandbox_path})\n{sandbox_path}"

    def test_plain_link_to_image_added_to_file_paths(self, tmp_path) -> None:
        """A plain link whose target really is an image is treated as one."""
        url = "https://uploads.linear.app/abc/pic.png"
        body = f"[pic]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"png", "image/png")})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert result.rewritten_body == (
            "[pic](/tmp/symphony-attachments/img-0001.png)"
        )

    # ------------------------------------------------------------------
    # URL rewriting must be token-bounded (no prefix corruption)
    # ------------------------------------------------------------------

    def test_shared_prefix_url_not_corrupted(self, tmp_path) -> None:
        """A shorter upload URL must not corrupt a longer one sharing its prefix.

        Regression: the bare-URL pass used a global ``str.replace``, so
        rewriting ``.../a`` also rewrote the ``.../a`` prefix of
        ``.../a/b.zip`` on the following line.
        """
        short = "https://uploads.linear.app/a"
        long = "https://uploads.linear.app/a/b.zip"
        body = f"{short}\n{long}"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {short: (b"1", None), long: (b"2", "application/zip")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.rewritten_body == (
            "/tmp/symphony-attachments/file-0001\n"
            "/tmp/symphony-attachments/file-0002.zip"
        )
        assert (attachments_dir / "file-0001").read_bytes() == b"1"
        assert (attachments_dir / "file-0002.zip").read_bytes() == b"2"

    def test_image_url_prefix_does_not_corrupt_longer_generic(self, tmp_path) -> None:
        """Same regression for an image URL that is a prefix of a later upload.

        The image is processed first; its shorter URL must not eat into the
        longer bare URL's line.
        """
        short = "https://uploads.linear.app/a.png"
        long = "https://uploads.linear.app/a.png/b.zip"
        body = f"{short}\n{long}"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {short: (b"png", "image/png"), long: (b"zip", "application/zip")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.rewritten_body == (
            "![](/tmp/symphony-attachments/img-0001.png)\n"
            "/tmp/symphony-attachments/file-0002.zip"
        )
        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]

    # ------------------------------------------------------------------
    # Content-Type classification
    # ------------------------------------------------------------------

    def test_declared_non_image_type_wins_over_png_url(self, tmp_path) -> None:
        """A declared non-image Content-Type beats a misleading .png URL."""
        url = "https://uploads.linear.app/abc/photo.png"
        body = f"![photo]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"<html>", "text/html")})

        result = process_attachments(body, tracker, str(attachments_dir))

        # Not an image: excluded from file_paths and downgraded to a link.
        assert result.file_paths == []
        assert result.rewritten_body == (
            "[photo](/tmp/symphony-attachments/file-0001-photo)"
        )
        assert (attachments_dir / "file-0001-photo").read_bytes() == b"<html>"

    def test_generic_content_type_falls_back_to_url_extension(self, tmp_path) -> None:
        """An opaque octet-stream type still lets the .png URL decide."""
        url = "https://uploads.linear.app/abc/photo.png"
        body = f"![photo]({url})"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({url: (b"png", "application/octet-stream")})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert result.rewritten_body == (
            "![photo](/tmp/symphony-attachments/img-0001.png)"
        )

    # ------------------------------------------------------------------
    # Download errors
    # ------------------------------------------------------------------

    def test_download_error(self, tmp_path) -> None:
        """An ``AttachmentDownloadError`` is recorded as skipped."""
        body = "![bad](https://example.com/missing.png)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/missing.png": AttachmentDownloadError("gone")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == []
        assert result.skipped == [
            ("https://example.com/missing.png", "download failed")
        ]
        assert result.rewritten_body == body

    def test_too_large_error(self, tmp_path) -> None:
        """An ``AttachmentTooLargeError`` is recorded as skipped."""
        body = "![big](https://example.com/huge.jpg)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/huge.jpg": AttachmentTooLargeError(">10 MB")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.skipped == [
            ("https://example.com/huge.jpg", "attachment too large")
        ]
        assert result.rewritten_body == body

    def test_unexpected_download_exception(self, tmp_path) -> None:
        """Any unexpected exception during download is caught and skipped."""
        body = "![err](https://example.com/oops.png)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/oops.png": ValueError("boom")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.skipped == [("https://example.com/oops.png", "download failed")]

    # ------------------------------------------------------------------
    # Per-turn byte cap
    # ------------------------------------------------------------------

    def test_byte_cap_exceeded(self, tmp_path) -> None:
        """When total bytes would exceed the cap, the image is skipped."""
        body = "![a](https://example.com/a.png)\n![b](https://example.com/b.png)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {
                "https://example.com/a.png": (b"12345678", "image/png"),  # 8 bytes
                "https://example.com/b.png": (b"12345", "image/png"),  # 5 bytes
            }
        )

        # cap = 10: first image fits (8), second would push to 13 → skipped
        result = process_attachments(
            body, tracker, str(attachments_dir), per_turn_byte_cap=10
        )

        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert result.skipped == [
            ("https://example.com/b.png", "turn byte cap exceeded")
        ]
        # Only the first image's URL was rewritten.
        assert (
            result.rewritten_body
            == "![a](/tmp/symphony-attachments/img-0001.png)\n![b](https://example.com/b.png)"
        )

    def test_byte_cap_multiple_skips(self, tmp_path) -> None:
        """After the cap is exceeded, subsequent images are also checked."""
        body = (
            "![a](https://example.com/a.png)\n"
            "![b](https://example.com/b.png)\n"
            "![c](https://example.com/c.png)"
        )
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {
                "https://example.com/a.png": (b"1234", "image/png"),  # 4 bytes
                "https://example.com/b.png": (
                    b"5678",
                    "image/png",
                ),  # 4 bytes → total 8, fits
                "https://example.com/c.png": (
                    b"90",
                    "image/png",
                ),  # 2 bytes → total 10 > 9 → skip
            }
        )

        result = process_attachments(
            body, tracker, str(attachments_dir), per_turn_byte_cap=9
        )

        assert result.file_paths == [
            "/tmp/symphony-attachments/img-0001.png",
            "/tmp/symphony-attachments/img-0002.png",
        ]
        assert result.skipped == [
            ("https://example.com/c.png", "turn byte cap exceeded")
        ]

    # ------------------------------------------------------------------
    # Filename numbering (existing_count)
    # ------------------------------------------------------------------

    def test_existing_count_numbering(self, tmp_path) -> None:
        """When *existing_count* is > 0, filenames start after that count."""
        body = "![first](https://example.com/first.png)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/first.png": (b"data", "image/png")}
        )

        result = process_attachments(
            body, tracker, str(attachments_dir), existing_count=5
        )

        assert result.file_paths == ["/tmp/symphony-attachments/img-0006.png"]
        assert (attachments_dir / "img-0006.png").exists()

    def test_existing_count_with_multiple(self, tmp_path) -> None:
        """Numbering continues sequentially from *existing_count*."""
        body = "![a](https://a.com/a.png)\n![b](https://b.com/b.jpg)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {
                "https://a.com/a.png": (b"a", "image/png"),
                "https://b.com/b.jpg": (b"b", "image/jpeg"),
            }
        )

        result = process_attachments(
            body, tracker, str(attachments_dir), existing_count=3
        )

        assert result.file_paths == [
            "/tmp/symphony-attachments/img-0004.png",
            "/tmp/symphony-attachments/img-0005.jpg",
        ]
        assert (attachments_dir / "img-0004.png").exists()
        assert (attachments_dir / "img-0005.jpg").exists()

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
        assert result.file_paths == ["/tmp/symphony-attachments/img-0002.jpg"]
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
        assert result2.file_paths == ["/tmp/symphony-attachments/img-0003.png"]
        assert result2.next_index == 3

    # ------------------------------------------------------------------
    # Mixed success / failure
    # ------------------------------------------------------------------

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
            "/tmp/symphony-attachments/img-0001.png",
            "/tmp/symphony-attachments/img-0003.jpg",
        ]
        assert result.skipped == [
            ("https://bad.com/bad.png", "download failed"),
            ("https://huge.com/huge.png", "attachment too large"),
        ]
        assert result.rewritten_body == (
            "![ok](/tmp/symphony-attachments/img-0001.png)\n"
            "![bad](https://bad.com/bad.png)\n"
            "![fine](/tmp/symphony-attachments/img-0003.jpg)\n"
            "![huge](https://huge.com/huge.png)"
        )

    # ------------------------------------------------------------------
    # Duplicate URLs (the extractor deduplicates)
    # ------------------------------------------------------------------

    def test_duplicate_url_replaced_all_occurrences(self, tmp_path) -> None:
        """When a URL appears twice in the body, ALL occurrences are rewritten."""
        body = (
            "![first](https://example.com/img.png)\n"
            "![second](https://example.com/img.png)"
        )
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/img.png": (b"data", "image/png")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        # Only one file downloaded (extractor deduplicates URLs),
        # but both body occurrences should point to the local file.
        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert (
            result.rewritten_body
            == "![first](/tmp/symphony-attachments/img-0001.png)\n"
            "![second](/tmp/symphony-attachments/img-0001.png)"
        )

    def test_bare_url_duplicate_replaced_all(self, tmp_path) -> None:
        """A bare URL that appears twice is replaced in both places."""
        body = "https://example.com/diag.png\nhttps://example.com/diag.png"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/diag.png": (b"data", "image/png")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert result.rewritten_body == (
            "![](/tmp/symphony-attachments/img-0001.png)\n"
            "![](/tmp/symphony-attachments/img-0001.png)"
        )

    def test_mixed_markdown_and_bare_same_url_both_rewritten(self, tmp_path) -> None:
        """Regression: a URL that appears both as a Markdown image and as a
        bare URL must be rewritten in both forms (not just one)."""
        body = (
            "![screenshot](https://example.com/img.png)\n"
            "Some text\n"
            "https://example.com/img.png\n"
            "More text"
        )
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/img.png": (b"data", "image/png")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.png"]
        assert result.rewritten_body == (
            "![screenshot](/tmp/symphony-attachments/img-0001.png)\n"
            "Some text\n"
            "![](/tmp/symphony-attachments/img-0001.png)\n"
            "More text"
        )

    # ------------------------------------------------------------------
    # Sandbox path format
    # ------------------------------------------------------------------

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

    def test_sandbox_paths_use_forward_slashes(self, tmp_path) -> None:
        """Returned file paths always use forward-slash separators."""
        body = "![img](https://example.com/photo.png)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/photo.png": (b"data", "image/png")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        for p in result.file_paths:
            assert "\\" not in p
            assert p.startswith("/tmp/symphony-attachments/")

    # ------------------------------------------------------------------
    # WebP support
    # ------------------------------------------------------------------

    def test_webp_extension_from_url(self, tmp_path) -> None:
        """WebP images are recognised and persisted."""
        body = "![modern](https://example.com/modern.webp)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/modern.webp": (b"webpdata", "image/webp")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == ["/tmp/symphony-attachments/img-0001.webp"]
        assert result.skipped == []
        assert (attachments_dir / "img-0001.webp").read_bytes() == b"webpdata"
