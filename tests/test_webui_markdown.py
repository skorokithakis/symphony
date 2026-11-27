"""Tests for the web UI Markdown renderer."""

from __future__ import annotations

import html
import re

from symphony_linear.tracker import is_bot_comment
from symphony_linear.webui.markdown import (
    Question,
    QuestionOption,
    markdown_to_text,
    parse_questions,
    render_markdown,
    render_markdown_sections,
)


def _code_span(text: str) -> str:
    """Wrap *text* in a code span that preserves it literally."""
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest_run + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


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


def test_question_callout_renders_a_choose_one_card() -> None:
    rendered = render_markdown_sections(
        "# Storage\n\n"
        "> [!QUESTION] Which storage should the cache use?\n"
        "> - **Redis**: survives a restart.\n"
        "> - **In-memory**: no dependency.\n"
    )

    # A section holding a question opens so the question is not hidden, and the
    # card's own words count towards the section total.
    assert '<details class="sec" open>' in rendered
    assert '<span class="sec-n">13 words</span>' in rendered
    assert (
        '<div class="qcard" data-kind="choose-one" data-question-index="0">' in rendered
    )
    assert 'data-text="Which storage should the cache use?"' in rendered
    assert 'data-text="Redis"' in rendered
    assert '<span class="qcard-l">Redis</span>' in rendered
    assert '<span class="qcard-d">: survives a restart.</span>' in rendered
    assert 'data-text="In-memory"' in rendered


def test_question_callout_renders_a_choose_many_card() -> None:
    rendered = render_markdown_sections(
        "> [!QUESTION] Which should I keep?\n"
        "> - [ ] **The retry loop**: it hides real failures.\n"
        "> - [ ] **The cron shim**: it duplicates the scheduler.\n"
    )

    assert 'data-kind="choose-many"' in rendered
    assert 'data-text="The retry loop"' in rendered
    assert 'data-text="The cron shim"' in rendered
    assert "[ ]" not in rendered


def test_question_card_marks_recommended_options() -> None:
    rendered = render_markdown_sections(
        "> [!QUESTION] Which one?\n"
        "> - **Redis (recommended)**: survives.\n"
        "> - **In-memory** (Recommended): no dependency.\n"
        "> - On disk (RECOMMENDED)\n"
    )

    assert rendered.count("data-recommended") == 3
    assert rendered.count('<span class="qcard-rec">recommended</span>') == 3
    assert 'data-text="Redis"' in rendered
    assert 'data-text="In-memory"' in rendered
    assert 'data-text="On disk"' in rendered
    assert "(recommended)" not in rendered.lower()

    many = render_markdown_sections(
        "> [!QUESTION] Which ones?\n"
        "> - [ ] **Lint (recommended)**: slow.\n"
        "> - [ ] **Tests (recommended)**: everything.\n"
        "> - [ ] Types\n"
    )
    assert 'data-kind="choose-many"' in many
    assert many.count("data-recommended") == 2
    assert many.count('<span class="qcard-rec">recommended</span>') == 2

    # A code span holding the same words is literal text, not a marker.
    literal = render_markdown_sections(
        "> [!QUESTION] Which one?\n> - `(recommended)` Redis\n"
    )
    assert "data-recommended" not in literal
    assert "<code>(recommended)</code>" in literal
    assert 'data-text="(recommended) Redis"' in literal

    # An empty bold span left by the marker is skipped and the normal
    # leading-bold split runs again on what remains.
    leading = render_markdown_sections(
        "> [!QUESTION] Which one?\n> - **(recommended)** **Redis**: fast\n"
    )
    assert 'data-text="Redis"' in leading
    assert '<span class="qcard-l">Redis</span>' in leading
    assert '<span class="qcard-d">: fast</span>' in leading

    # The gap at the cut is normalised across inline token boundaries.
    cross = render_markdown_sections(
        "> [!QUESTION] Which one?\n"
        "> - Use (recommended) *Redis*\n"
        "> - Use *Redis* (recommended) for sessions\n"
    )
    assert 'data-text="Use Redis"' in cross
    assert 'data-text="Use Redis for sessions"' in cross
    assert '<span class="qcard-l">Use <em>Redis</em></span>' in cross
    assert '<span class="qcard-l">Use <em>Redis</em> for sessions</span>' in cross

    # Removing the marker must not leave a space before punctuation.
    spaced = render_markdown_sections(
        "> [!QUESTION] Which one?\n> - **Redis** (recommended) : slow\n"
    )
    assert 'data-text="Redis"' in spaced
    assert '<span class="qcard-d">: slow</span>' in spaced
    assert " : slow" not in spaced

    # Removing the marker must not leave an empty option behind.
    empty = render_markdown_sections("> [!QUESTION] Which one?\n> - (recommended)\n")
    assert "qcard" not in empty
    assert "<blockquote>" in empty


def test_question_callout_renders_a_free_text_card() -> None:
    rendered = render_markdown_sections(
        "> [!QUESTION]\n> What should the new service be called?\n"
    )

    assert 'data-kind="free-text"' in rendered
    assert 'data-text="What should the new service be called?"' in rendered
    assert "qcard-opts" not in rendered


def test_malformed_question_callout_renders_as_a_quote() -> None:
    mixed = render_markdown_sections("> [!QUESTION] Which?\n> - [ ] yes\n> - no\n")
    empty = render_markdown_sections("> [!QUESTION]\n> - yes\n")

    for rendered in (mixed, empty):
        assert "qcard" not in rendered
        assert "<blockquote>" in rendered
        assert "[!QUESTION]" in rendered
    assert "[ ] yes" in mixed
    assert "no" in mixed


def test_question_card_checkbox_is_literal_text_only() -> None:
    rendered = render_markdown_sections(
        "> [!QUESTION] Which?\n> - `[ ]` Alpha\n> - `[ ]` Bravo\n"
    )

    assert 'data-kind="choose-one"' in rendered
    assert "<code>[ ]</code>" in rendered
    assert 'data-text="\\[ ] Alpha"' in rendered


def test_question_card_plain_text_is_inert_markdown() -> None:
    hostile = [
        "[x]: /other",
        "[click](https://example.com)",
        "<https://example.com>",
        "```python",
        "# Heading",
        "## Heading",
        "- item",
        "+ item",
        "> quote",
        "=== setext",
        "~~~ fence",
        "1. first",
        "2) second",
        "&amp; and <b>",
        "C:\\temp\\x",
        "_production_",
        "__init__",
        "___",
    ]
    for text in hostile:
        rendered = render_markdown_sections(
            f"> [!QUESTION] {_code_span(text)}\n> - {_code_span(text)}\n"
        )
        forms = [
            html.unescape(value)
            for value in re.findall(r'data-text="([^"]*)"', rendered)
        ]
        assert len(forms) == 2
        question_form, option_form = forms

        # The next stage quotes the question and puts the option in a list in
        # one comment, so all three placements must stay literal text.
        comment = f"> {question_form}\n\n{question_form}\n\n- {option_form}\n"
        as_html = render_markdown(comment)
        assert "<a " not in as_html
        assert "<h" not in as_html
        assert "<pre" not in as_html
        assert "<ol" not in as_html
        assert "<em>" not in as_html
        assert "<strong>" not in as_html
        assert "<hr" not in as_html
        assert as_html.count("<blockquote>") == 1
        assert as_html.count("<ul>") == 1
        assert as_html.count("<li>") == 1

        expected = " ".join(text.split()).replace("*", "\u2217")
        assert markdown_to_text(f"> {question_form}") == expected
        assert markdown_to_text(question_form) == expected
        assert markdown_to_text(f"- {option_form}") == expected

    ordinary = render_markdown_sections("> [!QUESTION] In-memory and max_retries\n")
    assert 'data-text="In-memory and max_retries"' in ordinary


def test_question_card_plain_text_neutralises_the_bot_marker() -> None:
    rendered = render_markdown_sections(
        "> [!QUESTION] Is `*Symphony · final*` real?\n"
        "> - **\\*Symphony · final\\***: no, that is the footer.\n"
    )
    plain_forms = re.findall(r'data-text="([^"]*)"', rendered)

    assert len(plain_forms) == 2
    assert "\u2217" in plain_forms[0]
    assert "\u2217" in plain_forms[1]
    assert not any(is_bot_comment(value) for value in plain_forms)


def test_parse_questions_returns_a_choose_one_card() -> None:
    questions = parse_questions(
        "> [!QUESTION] Which storage should the cache use?\n"
        "> - **Redis (recommended)**: survives a restart.\n"
        "> - In-memory\n"
    )

    assert questions == [
        Question(
            kind="choose-one",
            question="Which storage should the cache use?",
            options=(
                QuestionOption(
                    label="Redis",
                    detail="survives a restart.",
                    recommended=True,
                ),
                QuestionOption(label="In-memory", detail=""),
            ),
        )
    ]


def test_parse_questions_returns_a_choose_many_card() -> None:
    questions = parse_questions(
        "> [!QUESTION] Which should I keep?\n"
        "> - [ ] **The retry loop**: it hides real failures.\n"
        "> - [ ] The cron shim\n"
    )

    assert questions == [
        Question(
            kind="choose-many",
            question="Which should I keep?",
            options=(
                QuestionOption(
                    label="The retry loop", detail="it hides real failures."
                ),
                QuestionOption(label="The cron shim", detail=""),
            ),
        )
    ]


def test_parse_questions_returns_a_free_text_card() -> None:
    questions = parse_questions(
        "> [!QUESTION]\n> What should the\n> new service be called?\n"
    )

    assert questions == [
        Question(
            kind="free-text",
            question="What should the new service be called?",
            options=(),
        )
    ]


def test_parse_questions_ignores_malformed_callouts() -> None:
    assert parse_questions("> [!QUESTION] Which?\n> - [ ] yes\n> - no\n") == []
    assert parse_questions("> [!QUESTION]\n> - yes\n") == []
    assert parse_questions("> [!NOTE] not a question\n") == []
    assert parse_questions("- > [!QUESTION] nested\n  > - a\n") == []


def test_parse_questions_uses_an_image_alt_as_the_reader_sees_it() -> None:
    text = "> [!QUESTION] Replace ![**the spinner**](spinner.png) with what?\n"

    questions = parse_questions(text)

    assert [question.question for question in questions] == [
        "Replace the spinner with what?"
    ]
    # The page's data-text form is the literal Markdown a browser copies into a
    # human comment, so it deliberately keeps the image description's markup.
    assert 'data-text="Replace ∗∗the spinner∗∗ with what?"' in (
        render_markdown_sections(text)
    )


def test_parse_questions_and_the_page_agree_about_what_is_a_question() -> None:
    text = (
        "> [!QUESTION] first?\n"
        "> - a\n"
        "\n"
        "> [!NOTE] not one\n"
        "\n"
        "> [!QUESTION] mixed?\n"
        "> - [ ] a\n"
        "> - b\n"
        "\n"
        "- > [!QUESTION] nested?\n"
        "\n"
        "> [!QUESTION] second?\n"
    )
    questions = parse_questions(text)

    assert [question.question for question in questions] == ["first?", "second?"]
    assert render_markdown_sections(text).count('<div class="qcard"') == len(questions)
