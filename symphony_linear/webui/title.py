"""Automatic ticket titles through the Anthropic Messages API.

The feature is optional.  When ``webui.anthropic_api_key`` is set the editor
hides its title field and Start stores an empty title; that empty value is the
marker that no title has been written yet, and both the web UI and the
orchestrator fall back to the first line of the description in the meantime
(``local_tracker.display_title``).

``schedule_generation`` is the one trigger: both Start paths (the plain form
post and the JSON endpoint) call it once the ticket has entered In Progress.
It returns immediately; the request and the write happen on a daemon thread so
Start never waits for the API.  Nothing is retried, and a failure changes
nothing: the fallback title stays in place and one WARNING is logged.

The HTTP request itself lives in ``symphony_linear/anthropic.py``, shared with
the reply summaries the home page shows.  This module keeps the title prompt,
the cleaning of the reply and the Start trigger.
"""

from __future__ import annotations

import logging
import threading

import httpx

from symphony_linear.anthropic import clean_line, complete
from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import LocalTicketStore

logger = logging.getLogger(__name__)

#: A title needs a handful of tokens; the cap also bounds a runaway reply.
_MAX_TOKENS = 64

#: Longest title kept, in characters, after the reply is cleaned.
_MAX_TITLE_CHARS = 80

#: The description is sent whole unless it is enormous; the body cap is 1 MiB
#: and a title prompt that large would only cost tokens.
_MAX_DESCRIPTION_CHARS = 20_000

_PROMPT = (
    "Write a short title for the software task below. Reply with the title "
    "only: one line, at most 70 characters, no surrounding quotes, no "
    "trailing period and no preamble.\n\nTask:\n{description}"
)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def _prompt(description: str) -> str:
    """Wrap the ticket text in the fixed title prompt, capped to a sane size."""
    text = description.strip()
    if len(text) > _MAX_DESCRIPTION_CHARS:
        text = text[:_MAX_DESCRIPTION_CHARS]
    return _PROMPT.format(description=text)


def clean_title(raw: str) -> str:
    """Reduce a model reply to one plain title line.

    Only the first non-empty line is kept, surrounding quotes are removed,
    runs of whitespace collapse to single spaces, and the result is capped to
    ``_MAX_TITLE_CHARS``.  Returns an empty string when nothing usable is
    left; the caller then treats the reply as a failure.
    """
    return clean_line(raw, max_chars=_MAX_TITLE_CHARS)


def generate_title(
    description: str,
    *,
    api_key: str,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Ask the Messages API for a title, returning ``None`` on any failure.

    *base_url* is the optional ``webui.anthropic_base_url``.  *transport*
    exists for tests, which pass ``httpx.MockTransport`` so no request leaves
    the process.
    """
    text = complete(
        _prompt(description),
        api_key=api_key,
        max_tokens=_MAX_TOKENS,
        base_url=base_url,
        transport=transport,
    )
    if text is None:
        return None

    title = clean_title(text)
    if not title:
        logger.warning("Automatic title response was empty")
        return None
    return title


# ---------------------------------------------------------------------------
# The background trigger
# ---------------------------------------------------------------------------


def schedule_generation(
    store: LocalTicketStore, ticket_id: str, config: AppConfig
) -> None:
    """Start a title request in the background when one is due.

    Does nothing without a key in ``config.yaml``, for an unknown ticket, or
    when the ticket already has a title of its own.  The final write goes
    through ``LocalTicketStore.set_title_if_empty``, which repeats the
    emptiness check under its lock.
    """
    webui = config.webui
    api_key = (webui.anthropic_api_key or "").strip() if webui is not None else ""
    if not api_key:
        return
    base_url = webui.anthropic_base_url if webui is not None else None
    ticket = store.get(ticket_id)
    if ticket is None or ticket.title.strip():
        return

    thread = threading.Thread(
        target=_generate_and_store,
        args=(store, ticket_id, api_key, base_url, ticket.description),
        name=f"webui-title-{ticket_id}",
        daemon=True,
    )
    thread.start()


def _generate_and_store(
    store: LocalTicketStore,
    ticket_id: str,
    api_key: str,
    base_url: str | None,
    description: str,
) -> None:
    """The daemon thread body.  It must never raise into the server."""
    try:
        title = generate_title(description, api_key=api_key, base_url=base_url)
        if title is not None:
            store.set_title_if_empty(ticket_id, title)
    except Exception:
        # Includes the ticket having been deleted while the request ran.
        # Nothing is retried and nothing is changed.
        logger.warning("Automatic title for %s failed unexpectedly", ticket_id)
