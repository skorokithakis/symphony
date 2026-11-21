"""Tests for the web UI Markdown renderer."""

from __future__ import annotations

from symphony_linear.webui.markdown import (
    markdown_to_text,
    render_markdown,
    render_markdown_sections,
)


def test_plain_render_allows_commonmark_and_tables() -> None:
    rendered = render_markdown(
        "**bold** and `code`\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )

    assert "<strong>bold</strong>" in rendered
    assert "<code>code</code>" in rendered
    assert '<div class="scroll-x"><table>' in rendered
    assert "</table></div>" in rendered


def test_plain_render_escapes_raw_html() -> None:
    rendered = render_markdown(
        "<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>"
    )

    assert "<script" not in rendered
    assert "<img" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered


def test_plain_render_rejects_javascript_links() -> None:
    rendered = render_markdown("[click](javascript:alert(1))")

    assert "<a" not in rendered
    assert "javascript:" in rendered  # survives as plain text, not as a link
    assert "href" not in rendered


def test_each_table_is_wrapped() -> None:
    rendered = render_markdown("| a |\n|---|\n| 1 |\n\n| b |\n|---|\n| 2 |\n")

    assert rendered.count('<div class="scroll-x"><table>') == 2
    assert rendered.count("</table></div>") == 2


def test_sections_split_on_h1_and_are_closed_by_default() -> None:
    rendered = render_markdown_sections("# One\n\nfirst body\n\n# Two\n\nsecond body\n")

    assert rendered.startswith('<div class="secs">')
    assert rendered.count('<details class="sec">') == 2
    assert 'class="sec-t">One</span>' in rendered
    assert 'class="sec-t">Two</span>' in rendered
    assert '<div class="sec-b"><p>first body</p>\n</div>' in rendered
    assert " open" not in rendered


def test_sections_use_shallowest_heading_level() -> None:
    rendered = render_markdown_sections(
        "## Two\n\nbody\n\n### Three\n\ndeep body\n\n## Next\n\nmore\n"
    )

    assert rendered.count('<details class="sec">') == 2
    assert '<span class="sec-t">Two</span>' in rendered
    # The deeper heading stays an ordinary heading inside the section body.
    assert "<h3>Three</h3>" in rendered
    assert '<span class="sec-t">Three</span>' not in rendered


def test_text_before_first_heading_stays_visible() -> None:
    rendered = render_markdown_sections("intro paragraph\n\n# One\n\nbody\n")

    assert rendered.startswith('<p>intro paragraph</p>\n<div class="secs">')


def test_no_heading_renders_like_plain_markdown() -> None:
    text = "just **text**\n\n| a |\n|---|\n| 1 |\n"

    assert render_markdown_sections(text) == render_markdown(text)
    assert "<details" not in render_markdown_sections(text)


def test_heading_inside_code_block_is_not_a_section() -> None:
    fenced = "```\n# not a heading\n```\n"
    indented = "    # not a heading\n"

    assert "<details" not in render_markdown_sections(fenced)
    assert "<details" not in render_markdown_sections(indented)
    assert "# not a heading" in render_markdown_sections(fenced)


def test_heading_nested_in_blockquote_does_not_split() -> None:
    rendered = render_markdown_sections("> # Quoted\n\nbody\n")

    assert "<details" not in rendered
    assert "<blockquote>" in rendered


def test_section_title_is_escaped_text() -> None:
    rendered = render_markdown_sections("## A <b> & *B*\n\nbody\n")

    assert '<span class="sec-t">A &lt;b&gt; &amp; B</span>' in rendered
    assert "<b>" not in rendered


def test_word_count_ignores_markup_and_counts_code() -> None:
    rendered = render_markdown_sections(
        "## A\n\n**one** two `three`\n\n```\nfour five\n```\n\n## Empty\n\n"
    )

    assert '<span class="sec-n">5 words</span>' in rendered
    assert '<span class="sec-n">0 words</span>' in rendered


def test_table_inside_a_section_is_wrapped() -> None:
    rendered = render_markdown_sections("# A\n\n| a |\n|---|\n| 1 |\n")

    assert '<div class="scroll-x"><table>' in rendered
    assert "</table></div>" in rendered


def test_markdown_to_text_drops_markup_and_keeps_visible_text() -> None:
    text = markdown_to_text(
        "**Symphony**: Restarted with `code` and a [link](https://example.com)."
    )

    assert text == "Symphony: Restarted with code and a link."
