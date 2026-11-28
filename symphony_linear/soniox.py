"""Client for the Soniox text-to-speech REST API.

This is the only place that asks Soniox for speech, so the endpoint, the
request shape and the key handling exist once.  The web UI's speak route
makes one request per agent reply and hands the unread response body back;
the audio is pulled as it arrives, because Soniox produces it at about 1.2
times real time and waiting for the whole clip would leave the listener
silent for tens of seconds.  There is no split of long text and no retry,
because a second request only costs money without changing the reply.

It is also the only place that knows Soniox's hosts.  A Soniox key belongs to
one project region and works only on that region's hosts, so the hosts are
derived from ``webui.soniox_region`` here and every caller (dictation's
temporary key and WebSocket, and this module's speech request) asks for them
by region.  A second copy of a host string anywhere else is how dictation and
speech would silently end up in different regions.

The request shape follows the current Generate speech reference:
``POST https://tts-rt.soniox.com/tts`` with a bearer key and a JSON body of
``model``, ``language``, ``voice``, ``speed``, ``audio_format``, ``bitrate``
and ``text``.
A successful response is raw audio.  A failure before the audio starts is a
JSON body with ``error_code``, ``error_type``, ``error_message``, ``more_info``
and ``request_id``; once streaming has begun an error cannot be delivered, so
a truncated mp3 is the only possible signal and is not detectable here.

The key comes from ``config.yaml``, which the sandbox masks from the coding
agents.  It is never logged and never included in an exception message: a log
line carries at most the exception type or the HTTP status.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SonioxHosts:
    """The three Soniox hosts of one project region.

    ``api_host`` mints temporary keys, ``transcription_host`` carries the
    dictation WebSocket and ``speech_host`` the text-to-speech request.  The
    hosts are kept without a scheme or path here so one value can become both
    the URL a client opens and the CSP origin it is allowed to open.
    """

    api_host: str
    speech_host: str
    transcription_host: str


def _regional_hosts(region: str | None) -> SonioxHosts:
    """Build the hosts for *region*, or the United States ones when ``None``.

    The United States is the one region whose hosts have no region label, and
    those are the URLs every install used before the setting existed.  Every
    other region inserts its code as a label before ``soniox.com``; see
    https://soniox.com/docs/data-residency.
    """
    label = "" if region is None else f"{region}."
    return SonioxHosts(
        api_host=f"api.{label}soniox.com",
        speech_host=f"tts-rt.{label}soniox.com",
        transcription_host=f"stt-rt.{label}soniox.com",
    )


#: The regions Soniox offers today.  ``config.py`` accepts exactly these keys,
#: so an unknown region is a config error rather than a lookup failure at the
#: first click.
SONIOX_HOSTS: dict[str, SonioxHosts] = {
    "us": _regional_hosts(None),
    "eu": _regional_hosts("eu"),
    "jp": _regional_hosts("jp"),
    "in": _regional_hosts("in"),
}

DEFAULT_REGION = "us"


def hosts_for(region: str) -> SonioxHosts:
    """Return the hosts of *region*.

    The region has already been validated by the config model, so a missing
    key here is a programming error, not a bad config value.
    """
    return SONIOX_HOSTS[region]


def speech_url(region: str) -> str:
    """The text-to-speech endpoint for *region*."""
    return f"https://{hosts_for(region).speech_host}/tts"


def temporary_key_url(region: str) -> str:
    """The endpoint that mints a browser's temporary key in *region*."""
    return f"https://{hosts_for(region).api_host}/v1/auth/temporary-api-key"


def transcription_websocket_url(region: str) -> str:
    """The dictation WebSocket URL the browser opens in *region*."""
    return f"wss://{hosts_for(region).transcription_host}/transcribe-websocket"


def transcription_websocket_origin(region: str) -> str:
    """The dictation WebSocket origin, for the Content-Security-Policy."""
    return f"wss://{hosts_for(region).transcription_host}"


#: The model, language, audio format and bitrate stay fixed: they are the ones
#: the design picked for reading one reply aloud, and nothing about a
#: workspace should change them.  The voice and speed do come from
#: ``webui.tts_voice`` and ``webui.tts_speed``, because they are the two
#: things a listener may need to change without editing this module.  A speed
#: outside Soniox's own accepted range is left for Soniox to refuse; its
#: message is shown on the speak button.
MODEL = "tts-rt-v2"
LANGUAGE = "en"
AUDIO_FORMAT = "mp3"
BITRATE = 64_000

#: The timeout applies to each socket operation, not to the whole generation:
#: a stream that keeps delivering audio can run for the minutes a full script
#: needs, while a connection that goes silent for a minute is dead.
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


class SpeechStream:
    """One open Soniox response whose audio has not been read yet.

    The request has already answered with a success status and an audio
    content type, so this object exists only to pull the body.  ``close()`` is
    idempotent because both the consumer that exhausts the iterator and a
    caller that abandons it after a race may release the same connection.
    """

    def __init__(self, stack: contextlib.ExitStack, response: httpx.Response) -> None:
        self._stack = stack
        self._response = response

    def iter_chunks(self) -> Iterator[bytes]:
        """Yield the audio as Soniox sends it, releasing the connection at the end."""
        try:
            yield from self._response.iter_bytes()
        finally:
            self.close()

    def close(self) -> None:
        """Release the response and its client."""
        self._stack.close()


def stream_speech(
    text: str,
    *,
    api_key: str,
    voice: str,
    speed: float,
    region: str = DEFAULT_REGION,
    transport: httpx.BaseTransport | None = None,
) -> SpeechStream:
    """Open one speech request for *text* and return its unread body.

    Waits for the status line and the headers only, so a refusal is raised as
    :class:`SonioxError` before the caller has committed to an entry.
    *transport* exists for tests, which pass ``httpx.MockTransport`` so no
    request leaves the process.  The key is only ever placed in the request
    headers.  The region selects the host, because a key answers only on its
    own region's hosts; any other region's host refuses it as an incorrect
    key.  *voice* is a built-in voice name or a cloned-voice ID, and *speed*
    is passed as given: Soniox accepts its own range and refuses anything else
    with a message the caller can show.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "model": MODEL,
        "language": LANGUAGE,
        "voice": voice,
        "speed": speed,
        "audio_format": AUDIO_FORMAT,
        "bitrate": BITRATE,
        "text": text,
    }
    stack = contextlib.ExitStack()
    client = stack.enter_context(
        httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport)
    )
    try:
        response = stack.enter_context(
            client.stream("POST", speech_url(region), headers=headers, json=body)
        )
    except httpx.HTTPError as exc:
        # The exception type only: a transport error's message can echo the
        # request, and the key must never reach a log.
        stack.close()
        logger.warning("Soniox speech request failed: %s", type(exc).__name__)
        raise SonioxError("Could not reach Soniox. Try again.") from None
    except Exception:
        # Not a transport error, so it is not ours to translate, but the
        # client still owns a connection that must not outlive this call.
        stack.close()
        raise

    if not response.is_success:
        status = response.status_code
        # A streamed response has no body until it is read, and reading the
        # JSON of an unread one raises instead of returning the message.  The
        # mock transport hides this, because its responses arrive already
        # read; it was found with a real refused key.
        try:
            response.read()
        except httpx.HTTPError as exc:
            # The refusal's body can itself time out or drop.  That is no
            # message at all, the same answer as a proxy's HTML page, but the
            # connection still has to be released.  Only the type and the
            # status are logged: the exception's message can echo the request.
            stack.close()
            logger.warning(
                "Soniox speech request returned HTTP %s (%s)",
                status,
                type(exc).__name__,
            )
            raise SonioxError(f"Soniox returned HTTP {status}.") from None
        except Exception:
            # The same rule for anything else a custom transport can raise:
            # translate nothing, but release the connection.
            stack.close()
            raise
        message = _error_message(response)
        stack.close()
        logger.warning("Soniox speech request returned HTTP %s", status)
        # The upstream message is relayed, but a proxy that echoes the request
        # must not turn it into a way to read the key back.
        raise SonioxError(message.replace(api_key, "[redacted]"))

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
        stack.close()
        raise SonioxError("Soniox returned no audio.")

    return SpeechStream(stack, response)
