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

The request shape follows the current Messages API reference: ``POST
https://api.anthropic.com/v1/messages`` with ``x-api-key`` and
``anthropic-version`` headers, and a body of ``model``, ``max_tokens`` and
``messages``.  The reply is a JSON object whose ``content`` is a list of
blocks; only ``{"type": "text"}`` blocks carry the title.

The key comes from ``config.yaml``, which the sandbox masks from the coding
agents.  It is never logged and never included in an exception message or a
repr: a log line carries at most the exception type, the HTTP status or the
ticket id.
"""

from __future__ import annotations

import logging
import threading

import httpx

from symphony_linear.config import AppConfig
from symphony_linear.local_tracker import LocalTicketStore

logger = logging.getLogger(__name__)

#: Product-owner's fixed model id.  ``claude-haiku-5-5`` is listed in the
#: current Messages API model reference ("fastest model for high-volume,
#: real-time tasks").
MODEL = "claude-haiku-5-5"

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2026-10-21"

#: A title needs a handful of tokens; the cap also bounds a runaway reply.
_MAX_TOKENS = 64

#: The request must not hold the thread for long: nobody is waiting on it.
_TIMEOUT_SECONDS = 20.0

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

_QUOTE_CHARS = "\"'`\u2018\u2019\u201c\u201d"


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
    line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    line = line.strip(_QUOTE_CHARS).strip()
    line = " ".join(line.split())
    if len(line) > _MAX_TITLE_CHARS:
        line = line[:_MAX_TITLE_CHARS].rstrip()
    return line


def _text_from_response(response: httpx.Response) -> str | None:
    """Return the concatenated text blocks of a Messages reply, or ``None``."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return None
    texts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    if not texts:
        return None
    # Newlines let ``clean_title`` keep only the first line of a chatty reply.
    return "\n".join(texts)


def generate_title(
    description: str,
    *,
    api_key: str,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Ask the Messages API for a title, returning ``None`` on any failure.

    *transport* exists for tests, which pass ``httpx.MockTransport`` so no
    request leaves the process.  The key is only ever placed in the request
    headers.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": MODEL,
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": _prompt(description)}],
    }
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(_API_URL, headers=headers, json=body)
    except httpx.HTTPError as exc:
        # The exception type only: a transport error's message can echo the
        # request, and the key must never reach a log.
        logger.warning("Automatic title request failed: %s", type(exc).__name__)
        return None

    if response.status_code != httpx.codes.OK:
        logger.warning("Automatic title request returned HTTP %s", response.status_code)
        return None

    text = _text_from_response(response)
    if text is None:
        logger.warning("Automatic title response was not in the expected shape")
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
    ticket = store.get(ticket_id)
    if ticket is None or ticket.title.strip():
        return

    thread = threading.Thread(
        target=_generate_and_store,
        args=(store, ticket_id, api_key, ticket.description),
        name=f"webui-title-{ticket_id}",
        daemon=True,
    )
    thread.start()


def _generate_and_store(
    store: LocalTicketStore, ticket_id: str, api_key: str, description: str
) -> None:
    """The daemon thread body.  It must never raise into the server."""
    try:
        title = generate_title(description, api_key=api_key)
        if title is not None:
            store.set_title_if_empty(ticket_id, title)
    except Exception:
        # Includes the ticket having been deleted while the request ran.
        # Nothing is retried and nothing is changed.
        logger.warning("Automatic title for %s failed unexpectedly", ticket_id)
