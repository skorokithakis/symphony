"""Render untrusted Markdown (agent replies, user text) as safe HTML.

Two entry points:

* :func:`render_markdown` renders CommonMark plus tables; it is used for user
  messages and notices.
* :func:`render_markdown_sections` renders the same, but turns an agent reply
  into one collapsed ``<details class="sec">`` per heading of the shallowest
  heading level present, inside a ``<div class="secs">``.

The input is model output and is therefore untrusted.  Raw HTML is never
emitted: the parser runs with ``html=False``, so tag-like input is escaped as
text, and link destinations go through markdown-it's default ``validateLink``
(``javascript:``, ``vbscript:``, ``file:`` and non-image ``data:`` URLs are
rejected).  Section titles are escaped plain text, not HTML.

No syntax highlighting and no linkify: both are off in the CommonMark preset.
"""

from __future__ import annotations

import html
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
    """Count the whitespace-separated words of a block token stream."""
    parts: list[str] = []
    for token in tokens:
        if token.type == "inline":
            parts.append(_inline_text(token.children or []))
        elif token.type in ("fence", "code_block"):
            parts.append(token.content)
    return len(" ".join(parts).split())


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


def render_markdown_sections(text: str) -> str:
    """Render *text* as safe HTML, one collapsible section per heading.

    The shallowest heading level present at the top level of the document
    defines the sections, and they are closed by default.  Deeper headings
    inside a section render as ordinary headings.  Text before the first
    section heading stays visible above the ``<div class="secs">`` wrapper.  A
    text with no top-level heading renders exactly like :func:`render_markdown`.
    """
    env: EnvType = {}
    tokens = _MD.parse(text, env)
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
        rendered.append(
            '<details class="sec"><summary>'
            f'<span class="sec-t">{html.escape(title)}</span>'
            f'<span class="sec-n">{count} {unit}</span>'
            "</summary>"
            f'<div class="sec-b">{_MD.renderer.render(body, _MD.options, env)}</div>'
            "</details>"
        )
    rendered.append("</div>")
    return "".join(rendered)
