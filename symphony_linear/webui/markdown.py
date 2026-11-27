"""Render untrusted Markdown (agent replies, user text) as safe HTML.

Two entry points:

* :func:`render_markdown` renders CommonMark plus tables; it is used for user
  messages and notices.
* :func:`render_markdown_sections` renders the same, but turns an agent reply
  into one collapsed ``<details class="sec">`` per heading of the shallowest
  heading level present, inside a ``<div class="secs">``.  A top-level
  ``> [!QUESTION] ...`` callout in that reply becomes a static question card
  instead of a quote.

The input is model output and is therefore untrusted.  Raw HTML is never
emitted: the parser runs with ``html=False``, so tag-like input is escaped as
text, and link destinations go through markdown-it's default ``validateLink``
(``javascript:``, ``vbscript:``, ``file:`` and non-image ``data:`` URLs are
rejected).  Section titles and card fields are escaped plain text, not HTML.

No syntax highlighting and no linkify: both are off in the CommonMark preset.
"""

from __future__ import annotations

import copy
import html
import re
from collections.abc import Sequence

from markdown_it import MarkdownIt
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

# One parser for the whole process.  Parsing and rendering keep no mutable
# state on the instance, so sharing it between the web server's handler
# threads is safe.
_MD = MarkdownIt("commonmark", {"html": False})
_MD.enable("table")


def _table_open(
    tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """Open a table inside a horizontal-scroll wrapper.

    Raw HTML is disabled, so this rule is the only path by which a ``<table>``
    can reach the page.
    """
    return '<div class="scroll-x">' + _MD.renderer.renderToken(
        tokens, idx, options, env
    )


def _table_close(
    tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """Close a table and its horizontal-scroll wrapper."""
    return "</table></div>\n"


_MD.renderer.rules["table_open"] = _table_open
_MD.renderer.rules["table_close"] = _table_close


def _question_card_render(
    tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """Emit a card token's pre-built HTML.

    The card's inner HTML is assembled by :func:`_question_card_html` from
    already-rendered inline fragments, so this rule only has to return it.
    """
    return tokens[idx].content


def _inline_text(children: Sequence[Token]) -> str:
    """Return the visible text of an inline token's children.

    Markup is dropped (emphasis markers, backticks, link destinations, image
    targets) while the text itself is kept, so titles and word counts describe
    what the reader sees rather than the Markdown source.
    """
    parts: list[str] = []
    for child in children:
        if child.type in ("text", "code_inline", "image"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return "".join(parts)


def _word_count(tokens: Sequence[Token]) -> int:
    """Count the whitespace-separated words of a block token stream.

    A question card carries the count of its own visible text in ``meta``,
    because the paragraphs and the list it was built from are no longer in the
    stream.
    """
    count = 0
    parts: list[str] = []
    for token in tokens:
        if token.type == "inline":
            parts.append(_inline_text(token.children or []))
        elif token.type in ("fence", "code_block"):
            parts.append(token.content)
        elif token.type == "question_card":
            count += int(token.meta.get("words", 0))
    return count + len(" ".join(parts).split())


def _heading_level(token: Token) -> int | None:
    """Return the level of a top-level heading token, or ``None``.

    Headings nested in a blockquote or a list item are deliberately excluded:
    splitting the token stream inside one would emit unbalanced HTML.
    """
    if token.type != "heading_open" or token.level != 0:
        return None
    return int(token.tag[1:])


def render_markdown(text: str) -> str:
    """Render *text* as safe HTML: CommonMark plus tables, no raw HTML."""
    return _MD.render(text)


def markdown_to_text(text: str) -> str:
    """Return the visible plain text of *text*, with markup dropped.

    Used where the design has no room for block markup, such as a one-line
    system notice or an alert title.  Code blocks keep their content; block
    boundaries become newlines.
    """
    env: EnvType = {}
    tokens = _MD.parse(text, env)
    parts: list[str] = []
    for token in tokens:
        if token.type == "inline":
            parts.append(_inline_text(token.children or []))
        elif token.type in ("fence", "code_block"):
            parts.append(token.content)
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Question cards
# ---------------------------------------------------------------------------

#: The callout marker the agent is asked to use for questions.  The match is
#: exact and at the start of the first paragraph, so a NOTE or WARNING callout,
#: or a quoted instruction that merely mentions the marker mid-sentence, keeps
#: rendering as a quote.
_CARD_MARKER = "[!QUESTION]"

#: A choose-many item starts with an empty checkbox.  Checked boxes and plain
#: items both count as "not an empty checkbox", so a list mixing the two is the
#: malformed case the ticket says to render as a quote.
_CARD_CHECKBOX = "[ ] "

#: Characters that can open an inline construct anywhere in a line.  The
#: backslash comes first so the escapes added here are not escaped again.
_INLINE_ESCAPES = ("\\", "`", "[", "<", "&")

#: Characters that can open a block construct only in the first column.
_BLOCK_MARKERS = frozenset("#>-+=~")

#: An ordered-list marker in the first column, whose delimiter also needs an
#: escape.
_ORDERED_MARKER_RE = re.compile(r"\d+[.)]")


def _escape_underscores(text: str) -> str:
    """Escape underscores that can open emphasis or a horizontal rule.

    An underscore between two alphanumeric characters is intraword and inert
    (``max_retries``), so leaving it alone keeps identifiers readable.  A
    leading, trailing or repeated underscore (``_production_``, ``__init__``,
    ``___``) can become emphasis or a rule once the plain form is copied into
    the answer, so it is escaped.
    """
    escaped: list[str] = []
    for index, character in enumerate(text):
        if character == "_":
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if not (previous.isalnum() and following.isalnum()):
                escaped.append("\\")
        escaped.append(character)
    return "".join(escaped)


def _question_plain_text(text: str) -> str:
    """Return *text* as one line that stays literal at the start of a line.

    A later stage copies these forms into a human comment: the question after
    ``> `` on a quote line, an option label after ``- `` or at the start of a
    paragraph.  That comment is rendered as Markdown, so the form must produce
    the same visible text and nothing else in all three positions.  Everything
    that can open a construct is escaped: a backslash first, then backticks,
    ``[``, ``<`` and ``&`` anywhere, underscores that are not intraword, and a
    block marker or an ordered-list delimiter only in the first column.
    Ordinary punctuation is left alone so the raw comment stays readable for the
    agent and in an edit field.  Every ``*`` becomes U+2217, exactly as
    ``markdown_link_text`` does, because ``*Symphony · `` is the bot footer
    marker ``is_bot_comment`` looks for and a question must not make the human's
    answer look like a bot comment.  All whitespace, including the Unicode line
    separators, collapses to one space.
    """
    plain = " ".join(text.split())
    for character in _INLINE_ESCAPES:
        plain = plain.replace(character, "\\" + character)
    plain = _escape_underscores(plain)
    plain = plain.replace("*", "\u2217")
    if plain[:1] in _BLOCK_MARKERS:
        return "\\" + plain
    ordered = _ORDERED_MARKER_RE.match(plain)
    if ordered is not None:
        delimiter = ordered.end() - 1
        return plain[:delimiter] + "\\" + plain[delimiter:]
    return plain


def _starts_with_checkbox(children: Sequence[Token]) -> bool:
    """Tell whether a list item's first inline token is an empty checkbox.

    Classification and stripping share this predicate so a code span holding
    the literal ``[ ]`` cannot be read as a checkbox in one place and left
    intact in the other.
    """
    return (
        bool(children)
        and children[0].type == "text"
        and children[0].content.startswith(_CARD_CHECKBOX)
    )


def _without_checkbox_prefix(children: Sequence[Token]) -> list[Token]:
    """Drop the leading ``[ ] `` of a choose-many item, if it has one."""
    if not _starts_with_checkbox(children):
        return list(children)
    first = copy.copy(children[0])
    first.content = first.content[len(_CARD_CHECKBOX) :]
    return ([first] if first.content else []) + list(children[1:])


def _leading_strong(
    children: Sequence[Token],
) -> tuple[list[Token], list[Token]] | None:
    """Split a leading bold span into its label and the remaining tokens.

    The label of an option is its leading bold span when it starts with one,
    else the whole option text.  Tokens are split rather than the rendered
    string so the label stays escaped by the inline renderer.  Markdown-it
    puts an empty text token before a paragraph that opens with emphasis, so
    those are skipped rather than mistaken for a plain option.
    """
    start = 0
    while (
        start < len(children)
        and children[start].type == "text"
        and not children[start].content
    ):
        start += 1
    if start == len(children) or children[start].type != "strong_open":
        return None
    for index in range(start + 1, len(children)):
        if (
            children[index].type == "strong_close"
            and children[index].level == children[start].level
        ):
            return list(children[start + 1 : index]), list(children[index + 1 :])
    return None


def _question_card_options(
    tokens: Sequence[Token], start: int, end: int
) -> tuple[list[list[Token]], int] | None:
    """Read one tight bullet list of single-paragraph items.

    Returns each item's inline children and the index just after the closing
    ``bullet_list_close``, or ``None`` for any other shape.  A nested list, an
    item with several blocks and an item with no inline content are all
    "not clearly well formed", which must fall back to a quote rather than
    lose text.
    """
    if start >= end or tokens[start].type != "bullet_list_open":
        return None
    items: list[list[Token]] = []
    index = start + 1
    while index < end and tokens[index].type != "bullet_list_close":
        if tokens[index].type != "list_item_open":
            return None
        index += 1
        if index >= end or tokens[index].type != "paragraph_open":
            return None
        index += 1
        if index >= end or tokens[index].type != "inline":
            return None
        items.append(list(tokens[index].children or []))
        index += 1
        if index >= end or tokens[index].type != "paragraph_close":
            return None
        index += 1
        if index >= end or tokens[index].type != "list_item_close":
            return None
        index += 1
    if index >= end or not items:
        return None
    return items, index + 1


def _question_card_html(
    tokens: Sequence[Token], start: int, end: int, position: int, env: EnvType
) -> tuple[str, int] | None:
    """Build the card for the blockquote at ``tokens[start:end]``, or ``None``.

    ``None`` leaves the blockquote to the normal renderer, which is the
    documented fallback: a callout that is not clearly well formed, or that
    uses any block shape this function does not understand, keeps every word of
    the reply visible as an ordinary quote.  ``position`` is the card's
    zero-based place among the questions of the reply; the browser reads it,
    with the kind and the one-line plain-text forms, from the data attributes.
    The returned count is the visible word count of the card, which the caller
    stores on the card token so a section that holds only a card does not claim
    "0 words".

    The first paragraph holds the question: the marker, then the text either on
    the same line or after a line break.  Later paragraphs are prose.  A single
    bullet list supplies the options.  Detection is on the token stream rather
    than on the source text so the exact Markdown rules the page already uses
    decide where a paragraph ends.
    """
    if end - start < 4:
        return None
    if (
        tokens[start].type != "blockquote_open"
        or tokens[start + 1].type != "paragraph_open"
        or tokens[start + 2].type != "inline"
        or tokens[start + 3].type != "paragraph_close"
        or tokens[end - 1].type != "blockquote_close"
    ):
        return None

    children = list(tokens[start + 2].children or [])
    if (
        not children
        or children[0].type != "text"
        or not children[0].content.startswith(_CARD_MARKER)
    ):
        return None
    first = copy.copy(children[0])
    first.content = first.content[len(_CARD_MARKER) :].lstrip()
    question_children = ([first] if first.content else []) + children[1:]
    while question_children and question_children[0].type in ("softbreak", "hardbreak"):
        question_children.pop(0)
    if not _inline_text(question_children).strip():
        return None

    prose: list[Token] = []
    prose_after: list[Token] = []
    options: list[list[Token]] | None = None
    index = start + 4
    while index < end - 1:
        token = tokens[index]
        if token.type == "paragraph_open":
            if (
                index + 2 >= end
                or tokens[index + 1].type != "inline"
                or tokens[index + 2].type != "paragraph_close"
            ):
                return None
            # Prose after the list keeps its place below the options rather
            # than moving above them.
            target = prose_after if options is not None else prose
            target.extend(tokens[index : index + 3])
            index += 3
        elif token.type == "bullet_list_open":
            if options is not None:
                return None
            parsed = _question_card_options(tokens, index, end - 1)
            if parsed is None:
                return None
            options, index = parsed
        # Any other block (a heading, a nested quote, a code block) is a
        # shape the cards do not promise, so the whole callout stays a quote.
        else:
            return None

    kind = "free-text"
    rendered_options: list[tuple[str, str, str]] = []
    words = len(_inline_text(question_children).split())
    if options is not None:
        checkboxes = [_starts_with_checkbox(item) for item in options]
        if any(checkboxes) and not all(checkboxes):
            return None
        kind = "choose-many" if all(checkboxes) else "choose-one"
        for item in options:
            option_tokens = _without_checkbox_prefix(item)
            option_text = _inline_text(option_tokens)
            if not option_text.strip():
                return None
            words += len(option_text.split())
            strong = _leading_strong(option_tokens)
            label_tokens: list[Token]
            detail_tokens: list[Token]
            if strong is None:
                label_tokens, detail_tokens = option_tokens, []
            else:
                label_tokens, detail_tokens = strong
            if not _inline_text(label_tokens).strip():
                return None
            rendered_options.append(
                (
                    _MD.renderer.renderInline(label_tokens, _MD.options, env),
                    _MD.renderer.renderInline(detail_tokens, _MD.options, env)
                    if detail_tokens
                    else "",
                    _question_plain_text(_inline_text(label_tokens)),
                )
            )

    parts = [
        f'<div class="qcard" data-kind="{kind}" data-question-index="{position}">',
        f'<p class="qcard-q" data-text="{html.escape(_question_plain_text(_inline_text(question_children)))}">'
        f"{_MD.renderer.renderInline(question_children, _MD.options, env)}</p>",
    ]
    if prose:
        parts.append(
            '<div class="qcard-prose">'
            + _MD.renderer.render(prose, _MD.options, env)
            + "</div>"
        )
    if rendered_options:
        parts.append('<ul class="qcard-opts">')
        for label_html, detail_html, label_text in rendered_options:
            detail = (
                f'<span class="qcard-d">{detail_html}</span>' if detail_html else ""
            )
            parts.append(
                f'<li class="qcard-opt" data-text="{html.escape(label_text)}">'
                '<span class="qcard-t">'
                f'<span class="qcard-l">{label_html}</span>{detail}'
                "</span></li>"
            )
        parts.append("</ul>")
    if prose_after:
        parts.append(
            '<div class="qcard-prose">'
            + _MD.renderer.render(prose_after, _MD.options, env)
            + "</div>"
        )
    parts.append("</div>")
    return "".join(parts), words + _word_count(prose) + _word_count(prose_after)


def _replace_question_cards(tokens: list[Token], env: EnvType) -> None:
    """Replace every well-formed top-level question callout with a card token.

    Only a top-level blockquote can become a card; one nested in a list or in
    another quote keeps rendering as a quote.  Replacing whole token runs, not
    the rendered string, keeps escaping in the renderer and lets the section
    pass notice a card without searching HTML.
    """
    position = 0
    index = 0
    while index < len(tokens):
        if tokens[index].type != "blockquote_open" or tokens[index].level != 0:
            index += 1
            continue
        depth = 0
        close = index
        while close < len(tokens):
            if tokens[close].type == "blockquote_open":
                depth += 1
            elif tokens[close].type == "blockquote_close":
                depth -= 1
                if depth == 0:
                    break
            close += 1
        if close >= len(tokens):
            return
        card = _question_card_html(tokens, index, close + 1, position, env)
        if card is None:
            index += 1
            continue
        card_html, card_words = card
        card_token = Token("question_card", "", 0)
        card_token.content = card_html
        card_token.meta["words"] = card_words
        tokens[index : close + 1] = [card_token]
        position += 1
        index += 1


_MD.renderer.rules["question_card"] = _question_card_render


def render_markdown_sections(text: str) -> str:
    """Render *text* as safe HTML, one collapsible section per heading.

    The shallowest heading level present at the top level of the document
    defines the sections, and they are closed by default, except that a
    section holding a question card renders open.  Deeper headings inside a
    section render as ordinary headings.  Text before the first section heading
    stays visible above the ``<div class="secs">`` wrapper.  A text with no
    top-level heading renders exactly like :func:`render_markdown`.
    """
    env: EnvType = {}
    tokens = _MD.parse(text, env)
    _replace_question_cards(tokens, env)
    levels = [level for token in tokens if (level := _heading_level(token)) is not None]
    if not levels:
        return _MD.renderer.render(tokens, _MD.options, env)

    shallowest = min(levels)
    starts = [
        index
        for index, token in enumerate(tokens)
        if _heading_level(token) == shallowest
    ]

    rendered: list[str] = [_MD.renderer.render(tokens[: starts[0]], _MD.options, env)]
    rendered.append('<div class="secs">')
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(tokens)
        body_start = start + 1
        title = ""
        if body_start < end and tokens[body_start].type == "inline":
            title = _inline_text(tokens[body_start].children or []).strip()
            body_start += 1
        if body_start < end and tokens[body_start].type == "heading_close":
            body_start += 1
        body = tokens[body_start:end]
        count = _word_count(body)
        unit = "word" if count == 1 else "words"
        opened = " open" if any(t.type == "question_card" for t in body) else ""
        rendered.append(
            f'<details class="sec"{opened}><summary>'
            f'<span class="sec-t">{html.escape(title)}</span>'
            f'<span class="sec-n">{count} {unit}</span>'
            "</summary>"
            f'<div class="sec-b">{_MD.renderer.render(body, _MD.options, env)}</div>'
            "</details>"
        )
    rendered.append("</div>")
    return "".join(rendered)
