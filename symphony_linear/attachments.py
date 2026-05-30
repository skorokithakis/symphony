"""Image attachment processing: extraction, download, validation, rewriting."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from symphony_linear.tracker import (
    AttachmentDownloadError,
    AttachmentTooLargeError,
)

if TYPE_CHECKING:
    from symphony_linear.tracker import Tracker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------------

_MARKDOWN_IMAGE_RE = re.compile(
    r"!\s*\[(.*?)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)",
)

# Match a whole line whose sole content is a URL.
_BARE_URL_RE = re.compile(
    r"^\s*(https?://\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Check whether a URL's path ends with a recognised image extension.
_IMAGE_EXT_RE = re.compile(
    r"\.(?:png|jpg|jpeg|gif|webp)(?:\?|#|$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_image_refs(body: str) -> list[tuple[str, str]]:
    """Return a de-duplicated list of ``(url, alt_text)`` image references.

    Detection is performed in two passes:

    1. **Markdown images** – ``![alt](url)`` syntax.  Optional whitespace
       around the URL and an optional trailing title in double quotes are
       accepted.
    2. **Bare image URLs** – a line whose sole content is a URL ending in
       ``.png``, ``.jpg``, ``.jpeg``, ``.gif``, or ``.webp``
       (case-insensitive).  Bare URLs have an empty alt-text.

    Entries are returned in source order.  Duplicate URLs are dropped;
    the *first* occurrence wins — Markdown matches therefore take
    precedence over any subsequent bare-URL match of the same URL.

    This is a **pure** function — no I/O, no network.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []

    # Pass 1: Markdown image syntax .......................................
    for m in _MARKDOWN_IMAGE_RE.finditer(body):
        alt = m.group(1).strip()
        url = m.group(2)
        if url not in seen:
            seen.add(url)
            result.append((url, alt))

    # Pass 2: Bare image URLs (only those not already found) ..............
    for m in _BARE_URL_RE.finditer(body):
        url = m.group(1)
        if _IMAGE_EXT_RE.search(url) and url not in seen:
            seen.add(url)
            result.append((url, ""))

    return result


# ---------------------------------------------------------------------------
# Attachment result
# ---------------------------------------------------------------------------


@dataclass
class AttachmentResult:
    """The result of processing attachments for a turn.

    Attributes:
        rewritten_body: The body text with successfully-downloaded image
            URLs replaced by sandbox paths.
        file_paths: Paths as seen inside the sandbox (e.g.
            ``/tmp/symphony-attachments/img-0001.png``).
        skipped: ``(url, reason)`` tuples for every URL that was not
            successfully downloaded and persisted.
        next_index: The lowest un-consumed attachment index.  Equal to
            ``existing_count + len(refs)`` regardless of how many files
            were successfully written, so that skipped indices are never
            reused and filename collisions across turns are avoided.
    """

    rewritten_body: str
    file_paths: list[str]
    skipped: list[tuple[str, str]]
    next_index: int = 0


# ---------------------------------------------------------------------------
# Whitelist & extension helpers
# ---------------------------------------------------------------------------

_WHITELIST_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

_CONTENT_TYPE_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _ext_from_url(url: str) -> str | None:
    """Return a whitelisted extension (with leading dot) from the URL path,
    or ``None`` if the path has no recognised image extension."""
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    if ext and ext[1:] in _WHITELIST_EXTENSIONS:
        return ext
    return None


def _ext_from_content_type(content_type: str | None) -> str | None:
    """Map a normalised Content-Type to a file extension, or ``None``."""
    if content_type is None:
        return None
    return _CONTENT_TYPE_TO_EXT.get(content_type)


# ---------------------------------------------------------------------------
# Body rewriting
# ---------------------------------------------------------------------------


def _build_url_regex(url: str) -> re.Pattern[str]:
    """Build a regex that matches ``![<any alt>](url)`` with flexible whitespace.

    Capture group 1 captures the alt text so callers can preserve it during
    replacement.
    """
    escaped_url = re.escape(url)
    return re.compile(r"!\s*\[(.*?)\]\(\s*" + escaped_url + r"(?:\s+\"[^\"]*\")?\s*\)")


def _rewrite_body(body: str, url: str, alt: str, sandbox_path: str) -> str:
    """Replace every occurrence of *url* in *body* with *sandbox_path*.

    Both passes always run, so a URL that appears in multiple forms
    (e.g. Markdown image and bare URL) is rewritten everywhere:

    * Markdown images (``![alt](url)``) are replaced globally, preserving the
      original alt text of each occurrence (not just the first-seen alt).
    * Bare URLs are replaced globally with ``![](sandbox_path)``.

    The Markdown pass runs first; its output no longer contains the
    literal *url*, so the subsequent ``str.replace`` is safe.
    """
    # Pass 1: replace all Markdown-image occurrences of this URL (any alt).
    url_re = _build_url_regex(url)
    body = url_re.sub(rf"![\1]({sandbox_path})", body)

    # Pass 2: replace all bare-URL occurrences (harmless if pass 1
    # already handled everything — the rewritten form uses sandbox_path,
    # not the original url, so str.replace won't match it).
    return body.replace(url, f"![]({sandbox_path})")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def process_attachments(
    body: str,
    tracker: Tracker,
    host_attachments_dir: str,
    sandbox_mount: str = "/tmp/symphony-attachments",
    existing_count: int = 0,
    per_turn_byte_cap: int = 50 * 1024 * 1024,
) -> AttachmentResult:
    """Download, validate, and persist image attachments from *body*.

    Parameters
    ----------
    body:
        The Markdown text to scan for image references.
    tracker:
        Any :class:`Tracker` implementation providing
        :meth:`~Tracker.download_attachment`.
    host_attachments_dir:
        Host-side directory to write downloaded files into.  Created if
        it does not exist.
    sandbox_mount:
        Prefix used for sandbox-side paths (default
            ``"/tmp/symphony-attachments"``).
    existing_count:
        Number of files already in *host_attachments_dir* so that
        numbering does not collide (e.g. ``5`` → first new file is
        ``img-0006.png``).
    per_turn_byte_cap:
        Maximum total bytes to write in this call.  Once this cap would
        be exceeded the current image is skipped (reason ``"turn byte
        cap exceeded"``) and all remaining images are also checked
        against the cap.

    Returns
    -------
    AttachmentResult
        The rewritten body, the list of sandbox-side file paths, and any
        ``(url, reason)`` skip entries.
    """
    refs = extract_image_refs(body)
    if not refs:
        return AttachmentResult(
            rewritten_body=body,
            file_paths=[],
            skipped=[],
            next_index=existing_count,
        )

    os.makedirs(host_attachments_dir, exist_ok=True)

    rewritten_body = body
    file_paths: list[str] = []
    skipped: list[tuple[str, str]] = []
    total_bytes = 0

    for idx, (url, alt) in enumerate(refs, start=existing_count + 1):
        # 1. Download .......................................................
        try:
            data, content_type = tracker.download_attachment(url)
        except AttachmentTooLargeError:
            skipped.append((url, "attachment too large"))
            continue
        except AttachmentDownloadError:
            skipped.append((url, "download failed"))
            continue
        except Exception:
            logger.debug("Unexpected download error for %s", url, exc_info=True)
            skipped.append((url, "download failed"))
            continue

        # 2. Determine extension ...........................................
        ext = _ext_from_url(url)
        if ext is None:
            ext = _ext_from_content_type(content_type)
        if ext is None:
            detail = content_type or "unknown"
            skipped.append((url, f"unsupported type: {detail}"))
            continue

        # 3. Check per-turn byte cap .......................................
        if total_bytes + len(data) > per_turn_byte_cap:
            skipped.append((url, "turn byte cap exceeded"))
            continue

        total_bytes += len(data)

        # 4. Persist ........................................................
        filename = f"img-{idx:04d}{ext}"
        host_path = os.path.join(host_attachments_dir, filename)
        try:
            with open(host_path, "wb") as f:
                f.write(data)
        except OSError:
            logger.debug("Failed to write attachment %s", host_path, exc_info=True)
            skipped.append((url, "write failed"))
            total_bytes -= len(data)  # roll back the byte tally
            continue

        sandbox_path = f"{sandbox_mount}/{filename}"
        file_paths.append(sandbox_path)

        # 5. Rewrite body ..................................................
        rewritten_body = _rewrite_body(rewritten_body, url, alt, sandbox_path)

    return AttachmentResult(
        rewritten_body=rewritten_body,
        file_paths=file_paths,
        skipped=skipped,
        next_index=existing_count + len(refs),
    )
