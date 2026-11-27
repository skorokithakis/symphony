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


def _make_environment(template_dir: Path) -> Environment:
    """Build the environment with every template compiled once, up front.

    Templates must come from the same checkout revision as the Python code
    that renders them.  The process keeps the code it imported at startup,
    but Jinja's default ``auto_reload`` reads a changed template back from
    disk.  After a ``git pull`` and before the deferred auto-restart (which
    waits for every in-flight turn), the old views then rendered the new
    templates, and a field the old code lacks raised under ``StrictUndefined``
    and returned 500.  Compiling all templates now and turning the reload
    check off fixes the templates to the startup revision; Jinja's cache
    already held them all after the first render of each page, so this costs
    no extra memory.  The price is that a template edit by hand shows only
    after a restart.  Static files are deliberately left live (see
    :func:`static_url`): a mismatch there breaks behaviour, not the response.
    """
    environment = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
        undefined=StrictUndefined,
        auto_reload=False,
        # Unbounded, so no template is ever evicted and re-read from disk.
        cache_size=-1,
    )
    # Jinja documents that globals must be set before any template loads.
    environment.globals["static_url"] = static_url
    environment.globals["icon_url"] = icon_url
    for name in environment.list_templates():
        environment.get_template(name)
    return environment


_ENV = _make_environment(_TEMPLATE_DIR)


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
