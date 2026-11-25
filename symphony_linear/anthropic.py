"""Client for the Anthropic Messages API, shared by the web UI's two optional
Anthropic features.

Both callers supply a prompt and a token cap and get back the reply text, or
``None`` on any failure: ``webui/title.py`` turns it into an automatic ticket
title and ``cli.py`` into a one-line summary of an agent reply.  This module is
the only place that speaks to the API, so the request shape, the timeout, the
model id and the key handling exist once.  Nothing is retried; a failure is one
WARNING and ``None``, and the caller leaves whatever it had in place.

The request shape follows the current Messages API reference: ``POST
https://api.anthropic.com/v1/messages`` with ``x-api-key`` and
``anthropic-version`` headers, and a body of ``model``, ``max_tokens`` and
``messages``.  The base URL comes from ``webui.anthropic_base_url`` when it is
set, so a gateway can front the API; only the ``/v1/messages`` path is fixed.
The reply is a JSON object whose ``content`` is a list of blocks; only
``{"type": "text"}`` blocks carry text.

The key comes from ``config.yaml``, which the sandbox masks from the coding
agents.  It is never logged and never included in an exception message or a
repr: a log line carries at most the exception type or the HTTP status.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

#: Product-owner's fixed model id.  ``claude-haiku-5-5`` is listed in the
#: current Messages API model reference ("fastest model for high-volume,
#: real-time tasks"), which suits both features: they run per ticket and the
#: human only reads the result.
MODEL = "claude-haiku-5-5"

#: Default Messages API base URL.  ``webui.anthropic_base_url`` replaces it
#: for a gateway or proxy; the ``/v1/messages`` path is always appended.
DEFAULT_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2026-10-21"

#: The request must not hold the thread for long: nobody is waiting on it.
TIMEOUT_SECONDS = 20.0

_QUOTE_CHARS = "\"'`\u2018\u2019\u201c\u201d"


def complete(
    prompt: str,
    *,
    api_key: str,
    max_tokens: int,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Send one user *prompt* and return the reply text, or ``None``.

    *max_tokens* bounds a runaway reply and is the caller's decision: a title
    needs a handful of tokens, a short summary a few more.  *base_url* is the
    optional ``webui.anthropic_base_url``; the fixed ``/v1/messages`` path is
    appended to it, so a gateway only has to speak the same Messages API at
    that path.  *transport* exists for tests, which pass
    ``httpx.MockTransport`` so no request leaves the process.  The key is only
    ever placed in the request headers.

    Every text block of the reply is joined with newlines, which lets a caller
    that only wants one line discard the rest with :func:`clean_line`.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        # Haiku 5.5 thinks by default when this field is absent, and thinking
        # tokens count toward max_tokens.  With the small caps both callers use,
        # a long input made the model spend the whole budget on a thinking
        # block and stop before writing any text, so summaries silently failed.
        # Raising max_tokens was rejected: it only makes the failure rarer and
        # pays for thinking we discard.  Disabled is valid at the default
        # (medium) effort; it returns a 400 only at xhigh or max.
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
    }
    url = f"{base_url or DEFAULT_BASE_URL}/v1/messages"
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        # The exception type only: a transport error's message can echo the
        # request, and the key must never reach a log.
        logger.warning("Anthropic Messages request failed: %s", type(exc).__name__)
        return None

    if response.status_code != httpx.codes.OK:
        logger.warning(
            "Anthropic Messages request returned HTTP %s", response.status_code
        )
        return None

    text = _text_from_response(response)
    if text is None:
        logger.warning(
            "Anthropic Messages response was not in the expected shape"
            " (stop_reason: %s)",
            _stop_reason(response),
        )
    return text


def clean_line(raw: str, *, max_chars: int) -> str:
    """Reduce a model reply to one plain line of at most *max_chars*.

    Only the first non-empty line is kept, surrounding quotes are removed,
    runs of whitespace collapse to single spaces, and the result is capped.
    A capped line is cut at the last word boundary that fits and ends in an
    ellipsis, so the reader can see the text goes on instead of meeting a
    sentence that stops mid-word.  Returns an empty string when nothing usable
    is left; the caller then treats the reply as a failure.
    """
    line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    line = line.strip(_QUOTE_CHARS).strip()
    line = " ".join(line.split())
    if len(line) > max_chars:
        cut = line[: max_chars - 1]
        # A single word longer than the cap has no boundary; cut it mid-word
        # rather than return an empty line.
        if " " in cut:
            cut = cut[: cut.rindex(" ")]
        line = cut.rstrip() + "\u2026"
    return line


def _stop_reason(response: httpx.Response) -> str | None:
    """Return the reply's ``stop_reason`` for the log, or ``None``.

    Only this one field is logged, never the body: it says whether a textless
    reply ran out of tokens (``max_tokens``) or ended normally.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    reason = payload.get("stop_reason")
    return reason if isinstance(reason, str) else None


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
    # Newlines let ``clean_line`` keep only the first line of a chatty reply.
    return "\n".join(texts)
