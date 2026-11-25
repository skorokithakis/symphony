"""One-line summaries of agent replies, shown on the home page's Needs Input
rows.

The feature is optional: ``cli.py`` builds the tracker's hook with
:func:`agent_reply_hook` only when ``webui.anthropic_api_key`` is set, and
passes it to ``LocalTracker``.  The trigger is ``LocalTracker.post_comment``,
which calls the hook for agent-reply kinds only (``tracker.is_agent_kind``).
The hook starts a daemon thread and returns immediately, so posting a reply
never waits for the API.  Nothing is retried and a failure stores nothing; the
row simply has no summary line.

The result goes to ``LocalComment.summary`` through
``LocalTicketStore.set_comment_summary``, which takes the store lock and treats
a deleted ticket or comment as a no-op.  ``updated_at`` is never touched, so a
summary landing cannot reorder the home list.  ``webui/views.py`` shows the
summary under the title of a Needs Input row that is not failed, and only for
the ticket's latest agent reply; it never reaches the ticket page, the JSON API
or ntfy.

The HTTP request itself lives in ``symphony_linear/anthropic.py``, shared with
automatic titles: the request shape, timeout and model id are the same, and the
key comes from ``config.yaml``, which the sandbox masks.  The key is never
logged and is only placed in the request headers.
"""

from __future__ import annotations

import logging
import threading

import httpx

from symphony_linear.anthropic import clean_line, complete
from symphony_linear.config import _WebUIConfig
from symphony_linear.local_tracker import (
    AgentReplyCallback,
    LocalTicketStore,
)

logger = logging.getLogger(__name__)

#: The home page shows one line under a Needs Input row's title.  The prompt is
#: fixed rather than configurable (unlike model labels): the row has room for a
#: sentence, and a per-workspace prompt would make the pages of two workspaces
#: describe the same reply differently.
_PROMPT = (
    "Summarise the software-agent reply below for the person who asked for the "
    "work. Reply with the summary only: one line, at most about 120 "
    "characters, saying what the agent did and what it needs from the user "
    "next, no surrounding quotes, no trailing period and no preamble.\n\n"
    "Reply:\n{reply}"
)

#: A summary needs a few more tokens than a title; the cap also bounds a
#: runaway reply.
_MAX_TOKENS = 128

#: Longest summary kept, in characters, after the reply is cleaned.  It is
#: deliberately above the "about 120" the prompt asks for: the model treats
#: that as approximate, and a cap at 120 cut ordinary summaries mid-sentence.
#: This only stops a runaway reply; about five wrapped lines on a phone.
_MAX_SUMMARY_CHARS = 200

#: The reply is sent whole unless it is enormous.  Agent replies can include
#: attachment references and long tool output; a summary prompt that large
#: would only cost tokens.
_MAX_REPLY_CHARS = 20_000


def _prompt(reply: str) -> str:
    """Wrap the agent's reply in the fixed summary prompt, capped to a sane size."""
    text = reply.strip()
    if len(text) > _MAX_REPLY_CHARS:
        text = text[:_MAX_REPLY_CHARS]
    return _PROMPT.format(reply=text)


def summarise_reply(
    reply: str,
    *,
    api_key: str,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Ask the Messages API for a one-line summary of *reply*.

    Returns ``None`` on any failure or an unusable reply, exactly like an
    automatic title.  *transport* exists for tests, which pass
    ``httpx.MockTransport`` so no request leaves the process.
    """
    text = complete(
        _prompt(reply),
        api_key=api_key,
        max_tokens=_MAX_TOKENS,
        base_url=base_url,
        transport=transport,
    )
    if text is None:
        return None

    summary = clean_line(text, max_chars=_MAX_SUMMARY_CHARS)
    if not summary:
        logger.warning("Reply summary response was empty")
        return None
    return summary


def agent_reply_hook(webui: _WebUIConfig) -> AgentReplyCallback | None:
    """Return the tracker's reply-summary hook, or ``None`` without a key.

    The hook starts one daemon thread per agent reply and returns immediately,
    so posting the reply never waits for the API.  Nothing is retried, and a
    failure stores nothing: the row simply has no summary line.
    """
    api_key = (webui.anthropic_api_key or "").strip()
    if not api_key:
        return None
    base_url = webui.anthropic_base_url

    def on_agent_reply(
        store: LocalTicketStore, ticket_id: str, comment_id: str, reply: str
    ) -> None:
        thread = threading.Thread(
            target=_summarise_and_store,
            args=(store, ticket_id, comment_id, reply, api_key, base_url),
            name=f"webui-summary-{ticket_id}",
            daemon=True,
        )
        thread.start()

    return on_agent_reply


def _summarise_and_store(
    store: LocalTicketStore,
    ticket_id: str,
    comment_id: str,
    reply: str,
    api_key: str,
    base_url: str | None,
) -> None:
    """The daemon thread body.  A failure changes nothing and is not retried."""
    try:
        summary = summarise_reply(reply, api_key=api_key, base_url=base_url)
        if summary is not None:
            store.set_comment_summary(ticket_id, comment_id, summary)
    except Exception:
        # A failed store write (a full disk, an unwritable workspace) or a bug
        # here must not surface only as an unhandled thread traceback: one
        # WARNING with the ticket id is the whole diagnostic.  Nothing is
        # retried and nothing is changed.
        logger.warning("Reply summary for %s failed unexpectedly", ticket_id)
