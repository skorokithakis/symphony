"""Jinja2 environment for the web UI.

Autoescaping is on for every template; do not use ``|safe`` on user data.  If
a page needs to render Markdown, use the helpers in
:mod:`symphony_linear.webui.markdown` and mark only their output safe.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from symphony_linear.local_tracker import ICON_PATH

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# ICON_PATH is the URL spelling the web UI has always served the icon at;
# static_url wants the file's name relative to the static directory.
_ICON_NAME = PurePosixPath(ICON_PATH).relative_to("/static").as_posix()


def static_url(name: str) -> str:
    """Return ``/static/<name>`` with a cache-busting ``?v=`` query.

    The version is the file's nanosecond mtime, and it is read on every call
    rather than cached: editing a script or stylesheet in place must change
    its URL without restarting the daemon.  The query is stripped before
    routing and is what :mod:`symphony_linear.webui.static` keys the long
    ``Cache-Control`` on.
    """
    version = (_STATIC_DIR / name).stat().st_mtime_ns
    return f"/static/{name}?v={version}"


def icon_url() -> str:
    """Return the versioned favicon and title-bar logo URL.

    The favicon and the wordmark both read this one global so they cannot
    point at different files.
    """
    return static_url(_ICON_NAME)


_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
    undefined=StrictUndefined,
)
_ENV.globals["static_url"] = static_url
_ENV.globals["icon_url"] = icon_url


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
