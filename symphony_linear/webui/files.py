"""Serving uploaded files and describing them to the templates.

Serving lives here rather than in ``static.py`` because these files are not
package assets: they live under the ticket store and are named by untrusted
URL parameters.  The store does the realpath containment check; this module
only decides the response's content type and disposition.

Only the four allowlisted image types are served inline.  Every other type,
notably HTML and SVG, is served as ``application/octet-stream`` with
``Content-Disposition: attachment``: an HTML file served inline from this
origin would run script with the user's Basic-auth credentials.
"""

from __future__ import annotations

from dataclasses import dataclass

from symphony_linear.local_tracker import (
    LocalFile,
    image_content_type,
    upload_path,
)
from symphony_linear.tracker import TrackerError
from symphony_linear.webui.http_types import Request, Response

#: Types that may be rendered by the browser.  Everything else downloads.
_CONTENT_DISPOSITION = 'attachment; filename="{}"'


@dataclass(frozen=True)
class FileChip:
    """One uploaded file, formatted for a template."""

    ticket_id: str
    stored_name: str
    name: str
    url: str
    size: str
    image: bool


def format_size(size: int) -> str:
    """Return a short human-readable size, as the design's file chip shows."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def chip_for(ticket_id: str, record: LocalFile) -> FileChip:
    """Build the display chip for one stored file."""
    return FileChip(
        ticket_id=ticket_id,
        stored_name=record.name,
        name=record.original_name,
        url=f"/files/{ticket_id}/{record.name}",
        size=format_size(record.size),
        image=image_content_type(record.name) is not None,
    )


def chips_for(ticket_id: str, files: list[LocalFile]) -> tuple[FileChip, ...]:
    """Build the display chips for a ticket's or comment's files."""
    return tuple(chip_for(ticket_id, record) for record in files)


def _not_found() -> Response:
    return Response.text("Not Found\n", status=404)


def serve_file(request: Request) -> Response:
    """Serve ``GET /files/<ticket>/<stored-name>``, or 404.

    The ticket id and stored name are untrusted.  ``upload_path`` matches the
    raw request path against the one canonical form, so a percent-encoded
    alias is not served; the store then checks containment and that the name
    is actually registered on the ticket.
    """
    match = upload_path(request.path)
    if match is None:
        return _not_found()
    ticket_id, stored_name = match

    try:
        data = request.app.store.read_file(ticket_id, stored_name)
    except (FileNotFoundError, ValueError, OSError, TrackerError):
        return _not_found()

    image_type = image_content_type(stored_name)
    if image_type is not None:
        return Response(
            body=data,
            content_type=image_type,
            headers=(("Cache-Control", "private, max-age=3600"),),
        )
    return Response(
        body=data,
        content_type="application/octet-stream",
        headers=(
            ("Content-Disposition", _CONTENT_DISPOSITION.format(stored_name)),
            ("Cache-Control", "private, no-store"),
        ),
    )
