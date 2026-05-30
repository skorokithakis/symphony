"""Tests for the attachments helpers."""

from __future__ import annotations

from unittest.mock import Mock


from symphony_linear.attachments import (
    AttachmentResult,
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
    ) -> Mock:
        """Return a Mock whose ``download_attachment`` maps URLs to
        ``(data, content_type)`` or raises an exception."""
        tracker = Mock()
        tracker.download_attachment = Mock(
            side_effect=lambda url, _responses=responses: (
                _responses[url]
                if not isinstance(_responses.get(url), Exception)
                else (_ for _ in ()).throw(_responses[url])  # type: ignore[union-attr]
            )
        )
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

    def test_unsupported_extension_from_url(self, tmp_path) -> None:
        """A Markdown image with an unsupported extension (.pdf) is skipped."""
        body = "![doc](https://example.com/doc.pdf)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/doc.pdf": (b"pdfdata", "application/pdf")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == []
        assert result.skipped == [
            ("https://example.com/doc.pdf", "unsupported type: application/pdf")
        ]
        # Body is left untouched for skipped URLs.
        assert result.rewritten_body == body

    def test_unsupported_content_type(self, tmp_path) -> None:
        """A URL with no extension and a non-image Content-Type is skipped."""
        body = "![file](https://example.com/download)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker(
            {"https://example.com/download": (b"svgdata", "image/svg+xml")}
        )

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.file_paths == []
        assert result.skipped == [
            ("https://example.com/download", "unsupported type: image/svg+xml")
        ]

    def test_no_extension_no_content_type(self, tmp_path) -> None:
        """A URL with no extension and no Content-Type is skipped."""
        body = "![thing](https://example.com/opaque)"
        attachments_dir = tmp_path / "attachments"
        tracker = self._fake_tracker({"https://example.com/opaque": (b"binary", None)})

        result = process_attachments(body, tracker, str(attachments_dir))

        assert result.skipped == [
            ("https://example.com/opaque", "unsupported type: unknown")
        ]

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
