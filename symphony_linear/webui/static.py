"""Serve package static files without ever escaping the static directory.

Every path here comes from the request URL and is untrusted.  The real path is
resolved and checked against the real static root before a file is opened, so
``..`` segments and symlinks cannot read anything else.

There are two roots under the package's ``static/`` directory: the ordinary
``static/`` assets behind Basic auth, and ``static/public/``, whose single icon
the public route serves without it.  Both go through :func:`_serve_file`, so
the containment check is shared rather than duplicated.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from symphony_linear.local_tracker import PUBLIC_ICON_PATH
from symphony_linear.webui.http_types import Request, Response

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

#: Both derived from the one URL constant so the file on disk, the route and
#: the favicon cannot drift apart.
_PUBLIC_ROOT = PurePosixPath(PUBLIC_ICON_PATH).parent.name
_PUBLIC_FILE = PurePosixPath(PUBLIC_ICON_PATH).name


def _not_found() -> Response:
    return Response.text("Not Found\n", status=404)


def serve_static(request: Request) -> Response:
    """Return a file below ``request.app.static_root``, or 404."""
    match = request.match
    if match is None:
        return _not_found()
    return _serve_file(request.app.static_root, match.group("path"))


def serve_public(request: Request) -> Response:
    """Return the icon file below ``<static_root>/<public root>``, or 404.

    The route matches the one path in ``PUBLIC_ICON_PATH``, so no part of the
    request reaches the filesystem: this serves a fixed package asset, never
    user-supplied content.  Only this route is marked ``public``, and only
    GET/HEAD requests to it skip Basic auth in the server.
    """
    return _serve_file(request.app.static_root / _PUBLIC_ROOT, _PUBLIC_FILE)


def _serve_file(static_root: Path, raw_path: str) -> Response:
    try:
        relative = unquote(raw_path, errors="strict")
    except UnicodeDecodeError:
        return _not_found()
    if "\x00" in relative:
        return _not_found()

    root = static_root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return _not_found()

    if not candidate.is_file():
        return _not_found()
    try:
        data = candidate.read_bytes()
    except OSError:
        return _not_found()

    content_type = _CONTENT_TYPES.get(
        candidate.suffix.lower(), "application/octet-stream"
    )
    return Response(body=data, content_type=content_type)
