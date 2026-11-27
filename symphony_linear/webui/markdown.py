"""Render untrusted Markdown (agent replies, user text) as safe HTML.

Three entry points:

* :func:`render_markdown` renders CommonMark plus tables; it is used for user
  messages and notices.
* :func:`render_markdown_sections` renders the same, but turns an agent reply
  into one collapsed ``<details class="sec">`` per heading of the shallowest
  heading level present, inside a ``<div class="secs">``.  A section that opens
  with a paragraph previews that paragraph's first two lines in its summary,
  and a section holding question cards counts them there.  A top-level
  ``> [!QUESTION] ...`` callout in that reply becomes a static question card
  instead of a quote.
* :func:`parse_questions` returns those cards as structured data, using the
  same detection the renderer uses, so the JSON API and the page cannot
  disagree about what counts as a question.

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
from dataclasses import dataclass
from typing import Literal

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


def _structured_inline_text(children: Sequence[Token]) -> str:
    """Return the visible text of inline children for the structured API.

    This is :func:`_inline_text` with one difference: an image contributes the
    text of its parsed alt Markdown rather than the raw description.  The
    page's ``data-text`` attributes keep the literal description because the
    browser copies them into the human's reply as literal Markdown, but the
    JSON API describes what the reader sees, so ``![**bold**](x)`` reads
    ``bold`` there.
    """
    parts: list[str] = []
    for child in children:
        if child.type == "image":
            parts.append(_structured_inline_text(child.children or []))
        elif child.type in ("text", "code_inline"):
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


def _section_preview(body: Sequence[Token]) -> str | None:
    """Return a section's first paragraph as one line of plain text.

    Only a section whose first block is a paragraph gets a preview.  A table,
    code block, list, quote or question card at the top means the section's
    shape is not prose, and its opening line is punctuation or a header row
    rather than a sentence, so a preview would be noise.  The text is returned
    plain, not escaped, because the caller has to escape it: this function
    cannot tell whether a later stage wants Markdown or HTML.
    """
    if not body or body[0].type != "paragraph_open":
        return None
    if len(body) < 2 or body[1].type != "inline":
        return None
    text = _collapsed_text(body[1].children or [])
    return text or None


def _question_count(body: Sequence[Token]) -> int:
    """Count the question cards a section body holds."""
    return sum(1 for token in body if token.type == "question_card")


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

_QuestionKind = Literal["choose-one", "choose-many", "free-text"]


@dataclass(frozen=True)
class QuestionOption:
    """One option of a question card, as the reader sees it.

    ``detail`` is the text after a leading bold label, and is ``""`` when the
    option has no such split.  Both fields are plain text with whitespace
    collapsed, not the escaped ``data-text`` form: a consumer that copies them
    into a comment must apply the escaping the renderer already applies.
    ``recommended`` is the agent's marked recommendation and is independent of
    the option's label text, which has no marker.
    """

    label: str
    detail: str
    recommended: bool = False


@dataclass(frozen=True)
class Question:
    """One well-formed question callout of an agent reply.

    ``kind`` is ``choose-one`` for a bullet list, ``choose-many`` for a task
    list and ``free-text`` for a callout with no list.  ``options`` is empty
    for ``free-text``.
    """

    kind: _QuestionKind
    question: str
    options: tuple[QuestionOption, ...] = ()


@dataclass(frozen=True)
class _CardOption:
    """An option's tokens, split into its label and its detail."""

    label: tuple[Token, ...]
    detail: tuple[Token, ...]
    recommended: bool = False


@dataclass(frozen=True)
class _Card:
    """A well-formed question callout, still holding its tokens for rendering."""

    kind: _QuestionKind
    question: tuple[Token, ...]
    options: tuple[_CardOption, ...]
    prose: tuple[Token, ...]
    prose_after: tuple[Token, ...]
    words: int


#: The agent's recommendation marker in an option's text.  Only a plain text
#: token can carry it; the same words in a code span are literal option text.
_RECOMMENDED_MARKER_RE = re.compile(r"\(recommended\)", re.IGNORECASE)

#: Punctuation that must not be separated from the preceding word when the
#: recommendation marker is removed.
_MARKER_PUNCTUATION = frozenset(",.;:!?)]}%")


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


def _trim_trailing_whitespace(tokens: list[Token]) -> None:
    """Remove whitespace at the end of *tokens*, crossing inline closers."""
    index = len(tokens) - 1
    while index >= 0:
        token = tokens[index]
        if token.type != "text":
            index -= 1
            continue
        if not token.content.strip():
            del tokens[index]
            index -= 1
            continue
        token.content = token.content.rstrip()
        return


def _trim_leading_whitespace(tokens: list[Token]) -> None:
    """Remove whitespace at the start of *tokens*, crossing inline openers."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type != "text":
            index += 1
            continue
        if not token.content.strip():
            del tokens[index]
            continue
        token.content = token.content.lstrip()
        return


def _strip_recommended(
    children: Sequence[Token],
) -> tuple[list[Token], bool]:
    """Remove the first ``(recommended)`` marker from an option's tokens.

    The agent may put the marker inside the bold label, after it, or at the end
    of a plain option, so the whole inline run is searched rather than only the
    label.  Only a plain text token counts: a code span holding the same words
    is literal option text.

    The marker is removed with the whitespace at the cut normalised across
    token boundaries: exactly one space remains between two words (even when
    the neighbour is inside emphasis, bold, a code span or a link), none remains
    before punctuation, and a cut at the start or end of the option leaves no
    leading or trailing space.  Returns the changed tokens and whether a marker
    was found; a later marker is left alone.
    """
    for index, token in enumerate(children):
        if token.type != "text":
            continue
        match = _RECOMMENDED_MARKER_RE.search(token.content)
        if match is None:
            continue
        start, end = match.span()
        before = token.content[:start]
        after = token.content[end:]
        before_tokens = list(children[:index])
        after_tokens = list(children[index + 1 :])
        if before:
            prefix = copy.copy(token)
            prefix.content = before
            before_tokens.append(prefix)
        if after:
            suffix = copy.copy(token)
            suffix.content = after
            after_tokens.insert(0, suffix)

        # Decide from the untrimmed visible text whether the cut had a space,
        # then remove the whitespace from the two sides and put back exactly
        # the space the new neighbours need.
        before_visible = _inline_text(before_tokens)
        after_visible = _inline_text(after_tokens)
        had_space = before_visible != before_visible.rstrip() or (
            after_visible != after_visible.lstrip()
        )
        _trim_trailing_whitespace(before_tokens)
        _trim_leading_whitespace(after_tokens)
        before_text = _inline_text(before_tokens).strip()
        after_text = _inline_text(after_tokens).strip()
        # A cut at the start or end of the option must not leave a line break
        # either.
        if not before_text:
            while after_tokens and after_tokens[0].type in ("softbreak", "hardbreak"):
                del after_tokens[0]
        if not after_text:
            while before_tokens and before_tokens[-1].type in (
                "softbreak",
                "hardbreak",
            ):
                before_tokens.pop()
        add_space = False
        if before_text and after_text:
            if after_text[0] in _MARKER_PUNCTUATION:
                add_space = False
            elif before_text[-1].isalnum() and after_text[0].isalnum():
                add_space = True
            else:
                add_space = had_space
        if add_space:
            space = Token("text", "", 0)
            space.content = " "
            space.level = token.level
            before_tokens.append(space)
        return before_tokens + after_tokens, True
    return list(children), False


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


def _split_option_label(
    children: Sequence[Token],
) -> tuple[list[Token], list[Token]]:
    """Return an option's label and detail tokens.

    A leading bold span is the label when it has visible text.  An empty span is
    what remains when the bold text was only the recommendation marker: drop it
    and the whitespace after it, then run the same split on what remains.  That
    keeps every option on the normal leading-bold path, including one whose
    real label is a second bold span.
    """
    current = list(children)
    while True:
        strong = _leading_strong(current)
        if strong is None:
            return current, []
        label_tokens, detail_tokens = strong
        if _inline_text(label_tokens).strip():
            return label_tokens, detail_tokens
        current = list(detail_tokens)
        while current and current[0].type in ("softbreak", "hardbreak"):
            current = current[1:]
        while current and current[0].type == "text" and not current[0].content.strip():
            current = current[1:]
        if current and current[0].type == "text":
            first = copy.copy(current[0])
            first.content = first.content.lstrip()
            current = ([first] if first.content else []) + list(current[1:])
        if not current:
            return [], []


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


def _collapsed_text(tokens: Sequence[Token]) -> str:
    """Return the visible text of *tokens* with whitespace collapsed.

    This feeds the API, which describes what the reader sees, so images use
    :func:`_structured_inline_text`'s parsed alt text instead of the literal
    description the page keeps for its ``data-text`` attributes.
    """
    return " ".join(_structured_inline_text(tokens).split())


def _parse_question_card(tokens: Sequence[Token], start: int, end: int) -> _Card | None:
    """Read the callout at ``tokens[start:end]``, or return ``None``.

    ``None`` leaves the blockquote to the normal renderer, which is the
    documented fallback: a callout that is not clearly well formed, or that
    uses any block shape this function does not understand, keeps every word of
    the reply visible as an ordinary quote.  The rendered card and
    :func:`parse_questions` both go through this one function, so the page and
    the JSON API cannot disagree about what is a question.

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
    question_text = _inline_text(question_children)
    if not question_text.strip():
        return None

    prose: list[Token] = []
    prose_after: list[Token] = []
    item_tokens: list[list[Token]] | None = None
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
            target = prose_after if item_tokens is not None else prose
            target.extend(tokens[index : index + 3])
            index += 3
        elif token.type == "bullet_list_open":
            if item_tokens is not None:
                return None
            parsed = _question_card_options(tokens, index, end - 1)
            if parsed is None:
                return None
            item_tokens, index = parsed
        # Any other block (a heading, a nested quote, a code block) is a
        # shape the cards do not promise, so the whole callout stays a quote.
        else:
            return None

    kind: _QuestionKind = "free-text"
    options: list[_CardOption] = []
    words = len(question_text.split())
    if item_tokens is not None:
        checkboxes = [_starts_with_checkbox(item) for item in item_tokens]
        if any(checkboxes) and not all(checkboxes):
            return None
        kind = "choose-many" if all(checkboxes) else "choose-one"
        for item in item_tokens:
            option_tokens, recommended = _strip_recommended(
                _without_checkbox_prefix(item)
            )
            option_text = _inline_text(option_tokens)
            if not option_text.strip():
                return None
            words += len(option_text.split())
            label_tokens, detail_tokens = _split_option_label(option_tokens)
            if not _inline_text(label_tokens).strip():
                return None
            options.append(
                _CardOption(tuple(label_tokens), tuple(detail_tokens), recommended)
            )

    return _Card(
        kind=kind,
        question=tuple(question_children),
        options=tuple(options),
        prose=tuple(prose),
        prose_after=tuple(prose_after),
        words=words + _word_count(prose) + _word_count(prose_after),
    )


#: Punctuation an agent puts between a bold option label and its detail
#: (``**Redis**: survives a restart.``).  The card shows it because the label
#: and detail sit side by side, but a JSON consumer gets them as separate
#: fields, where a leading ``: `` is only noise.
_DETAIL_SEPARATORS = " :;,-\u2013\u2014\u00b7|"


def _to_question(card: _Card) -> Question:
    """Return the structured, render-free form of one parsed card."""
    return Question(
        kind=card.kind,
        question=_collapsed_text(card.question),
        options=tuple(
            QuestionOption(
                label=_collapsed_text(option.label),
                detail=_collapsed_text(option.detail).lstrip(_DETAIL_SEPARATORS),
                recommended=option.recommended,
            )
            for option in card.options
        ),
    )


def _question_card_html(
    card: _Card, position: int, env: EnvType, anchor: str | None
) -> str:
    """Build the card HTML for one parsed question.

    ``position`` is the card's zero-based place among the questions of the
    reply; the browser reads it, with the kind and the one-line plain-text
    forms, from the data attributes.  The plain-text forms are the escaped
    forms a later stage copies into a human comment, so they are derived here
    from the same tokens the visible HTML is rendered from.  ``anchor`` is the
    reply's id; the card's own id adds the index so the question list can link
    to it.  Without an anchor the card keeps its previous, id-less HTML.
    """
    card_id = f' id="{html.escape(anchor)}-q{position}"' if anchor else ""
    parts = [
        f'<div class="qcard"{card_id} data-kind="{card.kind}" data-question-index="{position}">',
        f'<p class="qcard-q" data-text="{html.escape(_question_plain_text(_inline_text(card.question)))}">'
        f"{_MD.renderer.renderInline(card.question, _MD.options, env)}</p>",
    ]
    if card.prose:
        parts.append(
            '<div class="qcard-prose">'
            + _MD.renderer.render(card.prose, _MD.options, env)
            + "</div>"
        )
    if card.options:
        parts.append('<ul class="qcard-opts">')
        for option in card.options:
            label_html = _MD.renderer.renderInline(option.label, _MD.options, env)
            detail_html = (
                _MD.renderer.renderInline(option.detail, _MD.options, env)
                if option.detail
                else ""
            )
            detail = (
                f'<span class="qcard-d">{detail_html}</span>' if detail_html else ""
            )
            tag = (
                ' <span class="qcard-rec">recommended</span>'
                if option.recommended
                else ""
            )
            mark = " data-recommended" if option.recommended else ""
            parts.append(
                f'<li class="qcard-opt" data-text="{html.escape(_question_plain_text(_inline_text(option.label)))}"{mark}>'
                '<span class="qcard-t">'
                f'<span class="qcard-l">{label_html}</span>{tag}{detail}'
                "</span></li>"
            )
        parts.append("</ul>")
    if card.prose_after:
        parts.append(
            '<div class="qcard-prose">'
            + _MD.renderer.render(card.prose_after, _MD.options, env)
            + "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _blockquote_close_index(tokens: Sequence[Token], start: int) -> int | None:
    """Return the index of the ``blockquote_close`` matching ``start``.

    ``None`` means the token stream is unbalanced, which cannot happen for a
    parse of a text, but failing closed is cheaper than indexing past the end.
    """
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].type == "blockquote_open":
            depth += 1
        elif tokens[index].type == "blockquote_close":
            depth -= 1
            if depth == 0:
                return index
    return None


def _replace_question_cards(
    tokens: list[Token], env: EnvType, anchor: str | None
) -> None:
    """Replace every well-formed top-level question callout with a card token.

    Only a top-level blockquote can become a card; one nested in a list or in
    another quote keeps rendering as a quote.  Replacing whole token runs, not
    the rendered string, keeps escaping in the renderer and lets the section
    pass notice a card without searching HTML.  The token also carries what
    the question list needs, so it is not parsed a second time.
    """
    position = 0
    index = 0
    while index < len(tokens):
        if tokens[index].type != "blockquote_open" or tokens[index].level != 0:
            index += 1
            continue
        close = _blockquote_close_index(tokens, index)
        if close is None:
            return
        card = _parse_question_card(tokens, index, close + 1)
        if card is None:
            index += 1
            continue
        card_token = Token("question_card", "", 0)
        card_token.content = _question_card_html(card, position, env, anchor)
        card_token.meta["words"] = card.words
        card_token.meta["question_index"] = position
        card_token.meta["question_text"] = _collapsed_text(card.question)
        tokens[index : close + 1] = [card_token]
        position += 1
        index += 1


def _question_groups(tokens: Sequence[Token]) -> list[list[Token]]:
    """Split the reply's cards into the groups the page's tabs use.

    A group is a run of ``question_card`` tokens with no other token between
    them.  That is exactly ``buildGroups`` in ``static/questions.js``: it joins
    cards that are direct element siblings, and the renderer emits one element
    per card without wrapping the run.  A section heading, a paragraph or any
    other block therefore starts a new group on both sides.  The rule must stay
    identical, or the question list would link into a group the tabs do not
    have; change both together.
    """
    groups: list[list[Token]] = []
    previous: int | None = None
    for index, token in enumerate(tokens):
        if token.type != "question_card" or token.level != 0:
            continue
        if previous is not None and index == previous + 1:
            groups[-1].append(token)
        else:
            groups.append([token])
        previous = index
    return groups


def _question_list_html(tokens: Sequence[Token], anchor: str) -> str:
    """Build the index of a reply's questions, or ``""`` for one group.

    One group already reads as one block of questions, and a lone card has no
    strip to point at, so the list would only repeat it.  Two or more groups
    are spread over the reply, where an index earns its place.  Every item is
    the visible question as escaped plain text, linking to the card's id.
    """
    groups = _question_groups(tokens)
    if len(groups) < 2:
        return ""
    items: list[str] = []
    for group in groups:
        for token in group:
            index = int(token.meta["question_index"])
            text = html.escape(str(token.meta["question_text"]))
            target = html.escape(f"{anchor}-q{index}")
            items.append(
                f'<li data-question-index="{index}">'
                f'<a href="#{target}"><span class="qlist-t">{text}</span></a></li>'
            )
    return (
        '<nav class="qlist" aria-label="Questions"><ol>'
        + "".join(items)
        + "</ol></nav>"
    )


def parse_questions(text: str) -> list[Question]:
    """Return the well-formed question cards of *text*, in callout order.

    Only top-level callouts count, because those are the ones the ticket page
    turns into cards; a callout nested in a list or in another quote is left to
    the normal renderer there and is absent here too.  Malformed callouts and
    every block shape the cards do not understand are absent as well, so the
    JSON API never advertises a question the page would show as a quote.

    The bot footer is a plain paragraph, not a blockquote, so it does not need
    stripping first.
    """
    env: EnvType = {}
    tokens = _MD.parse(text, env)
    questions: list[Question] = []
    index = 0
    while index < len(tokens):
        if tokens[index].type != "blockquote_open" or tokens[index].level != 0:
            index += 1
            continue
        close = _blockquote_close_index(tokens, index)
        if close is None:
            break
        card = _parse_question_card(tokens, index, close + 1)
        if card is not None:
            questions.append(_to_question(card))
        # Everything inside the quote has a level above zero, so jumping past
        # it is the same scan the renderer's loop makes one token at a time.
        index = close + 1
    return questions


_MD.renderer.rules["question_card"] = _question_card_render


def render_markdown_sections(text: str, anchor: str | None = None) -> str:
    """Render *text* as safe HTML, one collapsible section per heading.

    The shallowest heading level present at the top level of the document
    defines the sections, and they are closed by default, except that a
    section holding a question card renders open.  Deeper headings inside a
    section render as ordinary headings.  Text before the first section heading
    stays visible above the ``<div class="secs">`` wrapper.  A text with no
    top-level heading renders exactly like :func:`render_markdown`.

    Each summary shows the section title and word count on its first line, a
    question count when the body holds cards, and a two-line preview of the
    body's first paragraph when it starts with one.  Collapsing a long reply
    into titles alone hides which section answers what; the preview is plain
    escaped text, and CSS clamps it, so no text is cut here.

    ``anchor`` is the reply entry's id.  It gives every card a unique id and,
    when the reply's cards fall into two or more groups, puts a list of the
    questions after the reply, where a phone reader finishes next to the
    composer and the answered count; at the top it cost a scroll back.  Without
    an anchor the output keeps its previous HTML, links and list absent,
    because a link would have no target.
    """
    env: EnvType = {}
    tokens = _MD.parse(text, env)
    _replace_question_cards(tokens, env, anchor)
    question_list = _question_list_html(tokens, anchor) if anchor else ""
    levels = [level for token in tokens if (level := _heading_level(token)) is not None]
    if not levels:
        return _MD.renderer.render(tokens, _MD.options, env) + question_list

    shallowest = min(levels)
    starts = [
        index
        for index, token in enumerate(tokens)
        if _heading_level(token) == shallowest
    ]

    rendered: list[str] = [
        _MD.renderer.render(tokens[: starts[0]], _MD.options, env),
    ]
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
        cards = _question_count(body)
        opened = " open" if cards else ""
        preview = _section_preview(body)
        badge = (
            f'<span class="sec-q">{cards} '
            f"{'question' if cards == 1 else 'questions'}</span>"
            if cards
            else ""
        )
        preview_html = (
            f'<span class="sec-p">{html.escape(preview)}</span>' if preview else ""
        )
        rendered.append(
            f'<details class="sec"{opened}><summary>'
            f'<span class="sec-t">{html.escape(title)}</span>'
            f'<span class="sec-n">{count} {unit}</span>'
            f"{preview_html}"
            f"{badge}"
            "</summary>"
            f'<div class="sec-b">{_MD.renderer.render(body, _MD.options, env)}</div>'
            "</details>"
        )
    rendered.append("</div>")
    rendered.append(question_list)
    return "".join(rendered)
