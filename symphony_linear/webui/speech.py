"""The listening script and the audio cache behind the speak route.

``api_speech`` in :mod:`symphony_linear.webui.api` makes both upstream calls.
This module owns the parts that are fixed per reply: the prompt that turns an
agent reply into a script written for the ear, and the in-memory cache of the
mp3 that Soniox generated from it.

The prompt is not configurable.  The person who hears it is driving and hears
the script once, so the wording is the design, not a per-workspace preference;
the same reply must produce the same kind of script wherever the daemon runs.
The one-line summary in ``webui/summary.py`` is deliberately different: it is
read on screen, while this is spoken.

The cache lives only in memory and is never persisted.  Audio is the largest
thing the web UI ever holds, a restart can regenerate it, and nothing on disk
should be able to leak a spoken reply into the workspace the sandbox hides.
Keeping the newest 50 entries bounds it at a few tens of megabytes while
covering the replies a person is likely to replay.  An entry fills while the
listener already hears it: one producer thread appends what Soniox sends and
any number of readers follow the same chunks, so a second listen of a clip
that is still being made starts at the beginning instead of waiting for the
clip.  Two concurrent clicks for one comment may both reach the APIs before
either has created an entry; the loser closes its stream and its audio is
discarded.  That duplicate costs one request and is accepted rather than
serialised, because the alternative holds a handler thread under a lock while
an upstream call runs.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterator

import httpx

from symphony_linear.anthropic import complete
from symphony_linear.soniox import SpeechStream

logger = logging.getLogger(__name__)

#: The design's prompt, verbatim.  ``{reply}`` is replaced with the agent's
#: reply text and nothing else may contain braces: it is one ``str.format``.
_PROMPT = (
    "You turn a software agent's written reply into a script that is read "
    "aloud to the person who asked for the work. That person is driving. "
    "They cannot see the screen, they hear the script once, and they will "
    "answer by voice afterwards.\n"
    "\n"
    "Say what they need in order to understand the reply and to answer it:\n"
    "- Start with what the agent did or found, in one or two sentences.\n"
    "- Give the context needed to judge each decision: the problem, the "
    "options, their trade-offs, and the risks the agent raised.\n"
    "- State every question the agent asks, in full, and number them "
    '("Question one"). For each question, read the options in order and say '
    "which one the agent recommends.\n"
    "- If the agent asks for an approval, say exactly what is to be approved.\n"
    "\n"
    "Use as many words as this needs, and at most about 250 words. Leave out "
    "detail that does not help the listener decide.\n"
    "\n"
    "Write for the ear: plain spoken English, full sentences, no Markdown, no "
    "lists, no headings, no symbols. Do not read out code, commands, file "
    "paths, URLs or identifiers. Say what they are for instead, unless a name "
    "is necessary to answer a question. Always write in English. Reply with "
    "the script only.\n"
    "\n"
    "Reply:\n{reply}"
)

#: The prompt asks for at most about 250 words, which is roughly 330 tokens.
#: The cap sits above that and the reply is used whole: a script that ran out
#: of budget would stop mid-sentence and make the audio useless anyway.
_MAX_TOKENS = 500

#: The reply is sent whole unless it is enormous, exactly as the one-line
#: summary caps it: an agent reply can carry attachment references and long
#: tool output, and a script prompt that large would only cost tokens.
_MAX_REPLY_CHARS = 20_000

#: How many replies' audio the cache keeps before the oldest is dropped.
_CACHE_ENTRIES = 50


def script_for_reply(
    reply: str,
    *,
    api_key: str,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Ask the Messages API for the listening script of *reply*.

    Returns ``None`` on any failure or an unusable reply, exactly like an
    automatic title.  *base_url* is the optional ``webui.anthropic_base_url``.
    *transport* exists for tests, which pass ``httpx.MockTransport`` so no
    request leaves the process.
    """
    text = reply.strip()
    if len(text) > _MAX_REPLY_CHARS:
        text = text[:_MAX_REPLY_CHARS]
    script = complete(
        _PROMPT.format(reply=text),
        api_key=api_key,
        max_tokens=_MAX_TOKENS,
        base_url=base_url,
        transport=transport,
    )
    if script is None:
        return None

    # The prompt asks for the script only, but a chatty model can still add a
    # blank line or a closing remark.  Surrounding whitespace is all that is
    # removed: the design wants the rest of a model's prose read as written.
    text = script.strip()
    if not text:
        logger.warning("Speech script response was empty")
        return None
    return text


class SpeechEntry:
    """One reply's audio, either still arriving or complete.

    The producer thread appends chunks and any number of response threads read
    them from the start.  One condition guards the chunks and the two terminal
    flags and is notified on every change, so a reader waits exactly as long as
    the producer has nothing to add and is woken either way when it ends.
    """

    def __init__(self, body: str) -> None:
        self.body = body
        self.complete = False
        self.failed = False
        self._chunks: list[bytes] = []
        self._condition = threading.Condition()

    def append(self, chunk: bytes) -> None:
        with self._condition:
            self._chunks.append(chunk)
            self._condition.notify_all()

    def finish(self) -> None:
        with self._condition:
            self.complete = True
            self._condition.notify_all()

    def fail(self) -> None:
        with self._condition:
            self.failed = True
            self._condition.notify_all()

    def iter_chunks(self) -> Iterator[bytes]:
        """Yield the audio from the start, waiting for chunks not yet produced.

        Returns once the producer has finished or failed, so a reader can never
        outlive it and a failed producer ends every open response instead of
        leaving it hanging.
        """
        index = 0
        while True:
            with self._condition:
                while (
                    index >= len(self._chunks) and not self.complete and not self.failed
                ):
                    self._condition.wait()
                if index >= len(self._chunks):
                    return
                chunk = self._chunks[index]
                index += 1
            yield chunk


class SpeechCache:
    """Audio for one agent reply, keyed by comment id.

    An entry holds the body the audio was made from beside the chunks, so a
    reply whose body changed is generated again instead of playing stale
    speech.  The store never edits an agent comment today, but the key is a
    comment id rather than a body, and comparing the body costs nothing.

    One lock guards the dictionary; the chunk lists are owned by their entries
    and the audio itself is never copied.  A hit does not refresh an entry's
    position: "newest" means when the audio was generated, and a replay must
    not keep a large clip alive ahead of a newer one.
    """

    def __init__(self, limit: int = _CACHE_ENTRIES) -> None:
        self._limit = limit
        self._entries: OrderedDict[str, SpeechEntry] = OrderedDict()
        self._lock = threading.Lock()

    def entry(self, comment_id: str, body: str) -> SpeechEntry | None:
        """Return the entry for *comment_id* made from *body*, or ``None``."""
        with self._lock:
            entry = self._entries.get(comment_id)
        if entry is None or entry.body != body:
            return None
        return entry

    def start_filling(self, comment_id: str, body: str, stream: SpeechStream) -> bool:
        """Store a new entry and copy *stream* into it in a daemon thread.

        Returns ``False`` when an entry for the same comment and body appeared
        first; the caller then owns *stream* and must close it.  The thread is
        deliberately not joined anywhere: nobody waits for audio after the
        client that asked for it has gone, and the daemon flag keeps a
        half-generated clip from holding up a restart.
        """
        with self._lock:
            entry = self._entries.get(comment_id)
            if entry is not None and entry.body == body:
                return False
            entry = SpeechEntry(body)
            self._entries[comment_id] = entry
            # Re-assigning an existing key keeps its old position, while the
            # audio just made is the newest one and must be evicted last.
            self._entries.move_to_end(comment_id)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)
        threading.Thread(
            target=self._fill,
            args=(comment_id, entry, stream),
            name="speech-producer",
            daemon=True,
        ).start()
        return True

    def discard(self, comment_id: str, entry: SpeechEntry) -> None:
        """Remove *entry* if it is still the one stored for *comment_id*."""
        with self._lock:
            if self._entries.get(comment_id) is entry:
                del self._entries[comment_id]

    def _fill(self, comment_id: str, entry: SpeechEntry, stream: SpeechStream) -> None:
        delivered = 0
        try:
            for chunk in stream.iter_chunks():
                delivered += len(chunk)
                entry.append(chunk)
        except Exception as exc:
            # A dropped connection is the one failure a listener can see.  The
            # type only: httpx's message can echo the request, and the key must
            # never reach a log.  The entry is removed before the readers are
            # woken so the next click asks Soniox again rather than replaying a
            # truncated clip, and a reader that has just ended can rely on the
            # entry being gone.
            logger.warning("Soniox speech stream failed: %s", type(exc).__name__)
            self.discard(comment_id, entry)
            entry.fail()
            return
        if delivered == 0:
            # A success with no audio is not a clip: caching it would make
            # every later click replay silence until a restart.  This is rare
            # enough to be worth one line, and it is handled exactly like a
            # transport failure so the next click generates again.
            logger.warning("Soniox speech stream ended without audio")
            self.discard(comment_id, entry)
            entry.fail()
            return
        entry.finish()
