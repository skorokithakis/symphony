"""Client for the Soniox text-to-speech REST API.

This is the only place that asks Soniox for speech, so the endpoint, the fixed
voice and format, and the key handling exist once.  The web UI's speak route
makes one request per agent reply and returns the mp3 bytes unchanged; there
is no streaming, no split of long text and no retry, because a second request
only costs money without changing the reply.

The request shape follows the current Generate speech reference:
``POST https://tts-rt.soniox.com/tts`` with a bearer key and a JSON body of
``model``, ``language``, ``voice``, ``audio_format``, ``bitrate`` and ``text``.
A successful response is raw audio.  A failure before the audio starts is a
JSON body with ``error_code``, ``error_type``, ``error_message``, ``more_info``
and ``request_id``; once streaming has begun an error cannot be delivered, so
a truncated mp3 is the only possible signal and is not detectable here.

The key comes from ``config.yaml``, which the sandbox masks from the coding
agents.  It is never logged and never included in an exception message: a log
line carries at most the exception type or the HTTP status.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

#: The endpoint and the generation options are fixed rather than configurable.
#: The speak button reads one reply aloud; a per-workspace voice would make the
#: same reply sound different depending on which daemon produced it, and the
#: model, format and bitrate are the ones the design picked for speech.
TTS_URL = "https://tts-rt.soniox.com/tts"
MODEL = "tts-rt-v2"
LANGUAGE = "en"
VOICE = "Adrian"
AUDIO_FORMAT = "mp3"
BITRATE = 64_000

#: A script is at most a couple of minutes of audio, so a request that has not
#: answered by now is not going to.  The handler thread blocks for the whole
#: generation, which ThreadingHTTPServer keeps away from every other request.
TIMEOUT_SECONDS = 60.0


class SonioxError(Exception):
    """The text-to-speech request produced no audio.

    The message is safe to show to the person who clicked: it is Soniox's own
    ``error_message`` with the API key redacted, or one short fallback.
    """


def _error_message(response: httpx.Response) -> str:
    """Return Soniox's own message for a refused request, or a short fallback.

    The documented error shape uses ``error_message``; a gateway that speaks a
    slightly older shape uses ``message``.  A proxy's HTML error page has
    neither, so the status stands in.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for name in ("error_message", "message"):
            message = payload.get(name)
            if isinstance(message, str) and message:
                return message
    return f"Soniox returned HTTP {response.status_code}."


def synthesise(
    text: str,
    *,
    api_key: str,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Generate mp3 speech for *text* in one request and return the bytes.

    Raises :class:`SonioxError` with a message the page may show.  *transport*
    exists for tests, which pass ``httpx.MockTransport`` so no request leaves
    the process.  The key is only ever placed in the request headers.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "model": MODEL,
        "language": LANGUAGE,
        "voice": VOICE,
        "audio_format": AUDIO_FORMAT,
        "bitrate": BITRATE,
        "text": text,
    }
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(TTS_URL, headers=headers, json=body)
    except httpx.HTTPError as exc:
        # The exception type only: a transport error's message can echo the
        # request, and the key must never reach a log.
        logger.warning("Soniox speech request failed: %s", type(exc).__name__)
        raise SonioxError("Could not reach Soniox. Try again.") from None

    if not response.is_success:
        logger.warning("Soniox speech request returned HTTP %s", response.status_code)
        # The upstream message is relayed, but a proxy that echoes the request
        # must not turn it into a way to read the key back.
        raise SonioxError(_error_message(response).replace(api_key, "[redacted]"))

    # Soniox documents audio/mpeg for mp3.  A 200 carrying JSON (a proxy's error
    # page, or a changed endpoint) would otherwise be handed back as audio and
    # cached as if it were a clip, and the page would play nothing.  Only the
    # status and the type are logged: the body may echo the request.
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("audio/"):
        logger.warning(
            "Soniox speech response had Content-Type %r (HTTP %s)",
            content_type,
            response.status_code,
        )
        raise SonioxError("Soniox returned no audio.")

    audio = response.content
    if not audio:
        logger.warning("Soniox speech response carried no audio")
        raise SonioxError("Soniox returned no audio.")
    return audio
