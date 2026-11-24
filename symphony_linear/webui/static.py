"""Serve package static files without ever escaping the static directory.

Every path here comes from the request URL and is untrusted.  The real path is
resolved and checked against the real static root before a file is opened, so
``..`` segments and symlinks cannot read anything else.
"""

from __future__ import annotations

from urllib.parse import unquote

from symphony_linear.webui.http_types import Request, Response

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def _not_found() -> Response:
    return Response.text("Not Found\n", status=404)


def serve_static(request: Request) -> Response:
    """Return a file below ``request.app.static_root``, or 404."""
    match = request.match
    if match is None:
        return _not_found()

    raw_path = match.group("path")
    try:
        relative = unquote(raw_path, errors="strict")
    except UnicodeDecodeError:
        return _not_found()
    if "\x00" in relative:
        return _not_found()

    root = request.app.static_root.resolve()
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
