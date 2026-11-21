"""Jinja2 environment for the web UI.

Autoescaping is on for every template; do not use ``|safe`` on user data.  If
a page needs to render Markdown, use the helpers in
:mod:`symphony_linear.webui.markdown` and mark only their output safe.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
    undefined=StrictUndefined,
)


def render(template_name: str, **context: object) -> str:
    """Render *template_name* with *context*, escaping every value."""
    return _ENV.get_template(template_name).render(**context)


def render_macro(name: str, *args: object) -> str:
    """Render one macro from ``_macros.html``, with autoescaping.

    A poll fragment and the full page must not drift apart, so both go through
    the same macro rather than through a second copy of the markup.
    """
    module = _ENV.get_template("_macros.html").module
    macro = getattr(module, name)
    return macro(*args)
