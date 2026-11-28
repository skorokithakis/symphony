"""Tests for reading an agent reply aloud.

The upstream calls are exercised with ``httpx.MockTransport`` so no request
leaves the process, and the routes are exercised through the real server.  The
boundaries that matter are the gate (both keys, in the route table, the policy
and the views), the fixed Soniox request shape, the in-memory cache that fills
while the audio is streamed, and the refusal of anything that is not an agent
reply.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from symphony_linear import anthropic, soniox
from symphony_linear.local_tracker import STATE_NEEDS_INPUT, LocalTracker
from symphony_linear.webui import speech
from symphony_linear.webui.routes import SPEECH_AUDIO_ROUTE, SPEECH_ROUTE, routes_for
from tests.test_webui_server import (
    _basic,
    _config,
    _origin,
    _raw_socket_request,
    _request,
    _running_server,
)

_ANTHROPIC_KEY = "sk-ant-test"
_SONIOX_KEY = "soniox-test"
_BOTH_KEYS = {
    "anthropic_api_key": _ANTHROPIC_KEY,
    "soniox_api_key": _SONIOX_KEY,
}

#: The policy without speech, hard-coded on purpose: comparing with the
#: module's own constant would not notice a change to it.
_BASE_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "worker-src 'self'; "
    "style-src 'self'; "
    "img-src 'self'; "
    "manifest-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reply_ticket(tracker: LocalTracker) -> tuple[str, str]:
    """Create a Needs Input ticket with one agent reply, and return both ids."""
    ticket = tracker.store.create_started_draft(title="A ticket", description="Body")
    tracker.store.set_state(ticket.id, STATE_NEEDS_INPUT)
    comment = tracker.post_comment(ticket.id, "I fixed the widget.", "final")
    return ticket.id, comment.id


def _speech_path(ticket_id: str, comment_id: str) -> str:
    return f"/api/tickets/{ticket_id}/comments/{comment_id}/speech"


def _speech_request(
    port: int, ticket_id: str, comment_id: str
) -> tuple[int, dict[str, str], bytes]:
    return _request(
        port,
        "POST",
        _speech_path(ticket_id, comment_id),
        headers={"Origin": _origin(port)},
        body=b"{}",
    )


def _open_audio(
    port: int, ticket_id: str, comment_id: str
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Start a GET for the audio and return before its body is complete."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        "GET",
        _speech_path(ticket_id, comment_id),
        headers={"Authorization": _basic()},
    )
    return connection, connection.getresponse()


class _ControlledAudio:
    """A Soniox body whose second chunk waits for the test to release it.

    Waiting on an event rather than sleeping keeps the timing exact; the
    timeout means a bug fails the test instead of hanging the suite.
    """

    def __init__(self, *, fail_late: bool = False) -> None:
        self.late = threading.Event()
        self.fail_late = fail_late

    def __iter__(self) -> Iterator[bytes]:
        yield b"early"
        if not self.late.wait(timeout=5):
            raise TimeoutError("the test never released the late audio")
        if self.fail_late:
            raise httpx.ReadError("the connection dropped")
        yield b"late"


class _FakeStream:
    """A stand-in for an open Soniox stream, with no network at all."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def iter_chunks(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def _serve_stream(
    monkeypatch: pytest.MonkeyPatch,
    body: Iterator[bytes],
) -> list[httpx.Request]:
    """Make the route use a real ``stream_speech`` over a mock transport."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/mpeg"},
            content=body,
        )

    def fake_script(reply: str, *, api_key: str, base_url: str | None = None) -> str:
        return "A spoken script."

    def fake_stream(
        text: str, *, api_key: str, region: str = "us"
    ) -> soniox.SpeechStream:
        return soniox.stream_speech(
            text,
            api_key=api_key,
            region=region,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("symphony_linear.webui.api.script_for_reply", fake_script)
    monkeypatch.setattr("symphony_linear.webui.api.stream_speech", fake_stream)
    return seen


def _message(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": anthropic.MODEL,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
    )


def _capture(handler: Any) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(recording), seen


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestGate:
    @pytest.mark.parametrize(
        ("anthropic_api_key", "soniox_api_key", "enabled"),
        [
            (None, None, False),
            (_ANTHROPIC_KEY, None, False),
            (None, _SONIOX_KEY, False),
            (_ANTHROPIC_KEY, _SONIOX_KEY, True),
        ],
    )
    def test_the_route_needs_both_keys(
        self,
        anthropic_api_key: str | None,
        soniox_api_key: str | None,
        enabled: bool,
    ) -> None:
        config = _config(
            anthropic_api_key=anthropic_api_key,
            soniox_api_key=soniox_api_key,
        )
        assert (SPEECH_ROUTE in routes_for(config)) is enabled
        assert (SPEECH_AUDIO_ROUTE in routes_for(config)) is enabled

    def test_the_view_refuses_without_both_keys(self, tmp_path: Path) -> None:
        # A route table that was bypassed (a direct construction, or a later
        # change that forgets the conditional) must still not reach either
        # paid API, and must not stream an entry either.
        with _running_server(tmp_path, routes=(SPEECH_ROUTE, SPEECH_AUDIO_ROUTE)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            status, _headers, body = _speech_request(port, ticket_id, comment_id)
            assert status == 404
            assert json.loads(body) == {"error": "Speech is not configured."}

            status, _headers, body = _request(
                port, "GET", _speech_path(ticket_id, comment_id)
            )
            assert status == 404
            assert json.loads(body) == {"error": "Speech is not configured."}

    def test_csp_is_unchanged_without_speech(self, tmp_path: Path) -> None:
        with _running_server(tmp_path) as (port, _tracker, _state):
            status, headers, _body = _request(port, "GET", "/")

        assert status == 200
        assert headers["Content-Security-Policy"] == _BASE_CSP

    @pytest.mark.parametrize(
        "keys",
        [
            {},
            {"anthropic_api_key": _ANTHROPIC_KEY},
            {"soniox_api_key": _SONIOX_KEY},
        ],
    )
    def test_csp_gains_no_media_sources_without_both_keys(
        self, tmp_path: Path, keys: dict[str, str]
    ) -> None:
        with _running_server(tmp_path, **keys) as (port, _tracker, _state):
            _status, headers, _body = _request(port, "GET", "/")

        assert "media-src" not in headers["Content-Security-Policy"]

    def test_csp_allows_self_blob_and_data_media_with_both_keys(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            _tracker,
            _state,
        ):
            _status, headers, _body = _request(port, "GET", "/")

        policy = headers["Content-Security-Policy"]
        assert "media-src 'self' blob: data:;" in policy
        # The rest of the policy is untouched.
        assert "script-src 'self';" in policy
        assert "frame-ancestors 'none'" in policy


# ---------------------------------------------------------------------------
# The button
# ---------------------------------------------------------------------------


class TestSpeakButton:
    def test_the_page_offers_it_on_an_agent_reply_only(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            tracker.store.add_human_comment(ticket_id, "Thank you.")
            _status, _headers, body = _request(port, "GET", f"/tickets/{ticket_id}")

        text = body.decode()
        assert (
            f'data-speech="/api/tickets/{ticket_id}/comments/{comment_id}/speech"'
            in text
        )
        # The human comment and the ticket text get no button.
        assert text.count("data-speech=") == 1
        # The script that drives it is loaded only here.
        assert "speech.js" in text

    @pytest.mark.parametrize(
        "keys",
        [
            {},
            {"anthropic_api_key": _ANTHROPIC_KEY},
            {"soniox_api_key": _SONIOX_KEY},
        ],
    )
    def test_it_is_absent_without_both_keys(
        self, tmp_path: Path, keys: dict[str, str]
    ) -> None:
        with _running_server(tmp_path, **keys) as (port, tracker, _state):
            ticket_id, _comment_id = _reply_ticket(tracker)
            _status, _headers, body = _request(port, "GET", f"/tickets/{ticket_id}")

        text = body.decode()
        assert "data-speech=" not in text
        assert "speech.js" not in text

    def test_a_reply_appended_by_the_poll_gets_the_button(self, tmp_path: Path) -> None:
        # The page script delegates the click to the thread, so what the poll
        # must supply is a button carrying the new comment's URL.
        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, first_id = _reply_ticket(tracker)
            second = tracker.post_comment(
                ticket_id, "And then I fixed the rest.", "final"
            )
            status, _headers, body = _request(
                port, "GET", f"/api/tickets/{ticket_id}?after={first_id}"
            )

        assert status == 200
        payload = json.loads(body)
        assert (
            f'data-speech="/api/tickets/{ticket_id}/comments/{second.id}/speech"'
            in payload["entries_html"]
        )


# ---------------------------------------------------------------------------
# The Soniox request
# ---------------------------------------------------------------------------


class TestSonioxStream:
    def test_sends_one_request_with_the_fixed_voice_and_format(self) -> None:
        transport, seen = _capture(
            lambda request: httpx.Response(
                200,
                content=iter([b"ID3", b"audio"]),
                headers={"Content-Type": "audio/mpeg"},
            )
        )

        stream = soniox.stream_speech(
            "A spoken script.", api_key=_SONIOX_KEY, transport=transport
        )
        audio = b"".join(stream.iter_chunks())

        assert audio == b"ID3audio"
        assert len(seen) == 1
        request = seen[0]
        assert request.method == "POST"
        assert str(request.url) == "https://tts-rt.soniox.com/tts"
        assert request.headers["Authorization"] == f"Bearer {_SONIOX_KEY}"
        assert json.loads(request.content) == {
            "model": "tts-rt-v2",
            "language": "en",
            "voice": "Adrian",
            "audio_format": "mp3",
            "bitrate": 64000,
            "text": "A spoken script.",
        }

    def test_an_error_relays_the_message_with_the_key_redacted(self) -> None:
        refused = httpx.Response(
            403,
            json={
                "error_code": 403,
                "error_type": "permission_denied",
                "error_message": f"Key {_SONIOX_KEY} lacks text-to-speech.",
                "request_id": "rid",
            },
        )
        with pytest.raises(soniox.SonioxError) as error:
            soniox.stream_speech(
                "x",
                api_key=_SONIOX_KEY,
                transport=httpx.MockTransport(lambda request: refused),
            )

        assert str(error.value) == "Key [redacted] lacks text-to-speech."
        assert _SONIOX_KEY not in str(error.value)

    def test_a_refused_streamed_response_relays_its_message(self) -> None:
        """Soniox's real refusal is a streamed body that is not read yet.

        The other tests build their responses with ``content=``, which
        ``httpx.MockTransport`` hands over already read, so they cannot catch
        this: reading the JSON of a real streamed refusal raises
        ``ResponseNotRead`` until the body is pulled.  The error is only a
        ``SonioxError`` carrying Soniox's own ``error_message`` after that.
        """

        class RefusalBody(httpx.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                yield json.dumps(
                    {
                        "error_code": 401,
                        "error_type": "invalid_request",
                        "error_message": "Incorrect API key provided",
                    }
                ).encode()

        refused = httpx.Response(
            401,
            headers={"Content-Type": "application/json"},
            stream=RefusalBody(),
        )

        with pytest.raises(soniox.SonioxError) as error:
            soniox.stream_speech(
                "x",
                api_key=_SONIOX_KEY,
                transport=httpx.MockTransport(lambda request: refused),
            )

        assert str(error.value) == "Incorrect API key provided"

    def test_a_refused_body_that_cannot_be_read_is_a_short_message(self) -> None:
        """A refusal's body can fail while it is being read.

        The connection must still be released, and the answer is the same
        short status message a non-JSON body gets; the body's own error must
        not escape and become a 500.
        """

        class FailingBody(httpx.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                raise httpx.ReadTimeout("the error body timed out")
                yield b""  # Unreachable; this is what makes the method a generator.

        refused = httpx.Response(
            503,
            headers={"Content-Type": "application/json"},
            stream=FailingBody(),
        )

        with pytest.raises(soniox.SonioxError) as error:
            soniox.stream_speech(
                "x",
                api_key=_SONIOX_KEY,
                transport=httpx.MockTransport(lambda request: refused),
            )

        assert str(error.value) == "Soniox returned HTTP 503."

    def test_an_unreachable_response_has_a_short_message(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(soniox.SonioxError) as error:
            soniox.stream_speech(
                "x", api_key=_SONIOX_KEY, transport=httpx.MockTransport(unreachable)
            )
        assert str(error.value) == "Could not reach Soniox. Try again."

    def test_a_non_audio_success_is_refused(self) -> None:
        with pytest.raises(soniox.SonioxError) as error:
            soniox.stream_speech(
                "x",
                api_key=_SONIOX_KEY,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={"error_message": "not audio"},
                        headers={"Content-Type": "application/json"},
                    )
                ),
            )
        assert str(error.value) == "Soniox returned no audio."


# ---------------------------------------------------------------------------
# The region
# ---------------------------------------------------------------------------


class TestRegion:
    """One setting has to move every Soniox URL together.

    A key works only on the hosts of its own project region.  One host left
    behind would make dictation or speech answer "Incorrect API key provided"
    with no hint that the region is the problem.
    """

    @pytest.mark.parametrize("label", ["eu", "jp", "in"])
    def test_each_region_inserts_its_label_in_all_three_hosts(self, label: str) -> None:
        assert soniox.speech_url(label) == f"https://tts-rt.{label}.soniox.com/tts"
        assert soniox.temporary_key_url(label) == (
            f"https://api.{label}.soniox.com/v1/auth/temporary-api-key"
        )
        assert soniox.transcription_websocket_origin(label) == (
            f"wss://stt-rt.{label}.soniox.com"
        )
        assert soniox.transcription_websocket_url(label) == (
            f"wss://stt-rt.{label}.soniox.com/transcribe-websocket"
        )

    def test_a_non_default_region_moves_the_key_the_csp_and_the_speech(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        minted = httpx.Response(201, json={"api_key": "snx_temp"})
        key_urls: list[str] = []
        regions: list[str] = []

        def mint(request_url: str, **kwargs: Any) -> httpx.Response:
            key_urls.append(request_url)
            return minted

        def fake_script(
            reply: str, *, api_key: str, base_url: str | None = None
        ) -> str:
            return "A spoken script."

        def fake_stream(text: str, *, api_key: str, region: str = "us") -> _FakeStream:
            regions.append(region)
            return _FakeStream([b"mp3-bytes"])

        monkeypatch.setattr("symphony_linear.webui.api.httpx.post", mint)
        monkeypatch.setattr("symphony_linear.webui.api.script_for_reply", fake_script)
        monkeypatch.setattr("symphony_linear.webui.api.stream_speech", fake_stream)

        with _running_server(tmp_path, soniox_region="eu", **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            _status, _headers, key_body = _request(
                port,
                "POST",
                "/api/soniox-key",
                headers={"Origin": _origin(port)},
                body=b"{}",
            )
            _status, page_headers, _page = _request(port, "GET", "/")
            ticket_id, comment_id = _reply_ticket(tracker)
            status, _headers, _body = _speech_request(port, ticket_id, comment_id)

        assert key_urls == ["https://api.eu.soniox.com/v1/auth/temporary-api-key"]
        assert json.loads(key_body)["websocket_url"] == (
            "wss://stt-rt.eu.soniox.com/transcribe-websocket"
        )
        assert (
            "connect-src 'self' wss://stt-rt.eu.soniox.com;"
            in page_headers["Content-Security-Policy"]
        )
        assert status == 200
        assert regions == ["eu"]


# ---------------------------------------------------------------------------
# The listening script
# ---------------------------------------------------------------------------


class TestScript:
    def test_asks_for_the_fixed_prompt_with_the_reply_tail(self) -> None:
        transport, seen = _capture(lambda request: _message("  A spoken script.\n"))

        script = speech.script_for_reply(
            "I fixed the widget.", api_key=_ANTHROPIC_KEY, transport=transport
        )

        assert script == "A spoken script."
        body = json.loads(seen[0].content)
        assert body["model"] == anthropic.MODEL
        assert body["max_tokens"] == 500
        prompt = body["messages"][0]["content"]
        assert prompt.endswith("Reply:\nI fixed the widget.")
        # The wording is the contract the listener's experience rests on.
        assert 'number them ("Question one")' in prompt
        assert "no Markdown" in prompt

    def test_caps_the_reply_and_returns_none_on_failure(self) -> None:
        transport, seen = _capture(lambda request: _message("A script"))
        speech.script_for_reply(
            "x" * (speech._MAX_REPLY_CHARS + 500),
            api_key=_ANTHROPIC_KEY,
            transport=transport,
        )
        prompt = json.loads(seen[0].content)["messages"][0]["content"]
        assert len(prompt) < speech._MAX_REPLY_CHARS + len(speech._PROMPT) + 100

        for response in (
            httpx.Response(529, json={"error": "overloaded"}),
            _message("   \n "),
        ):
            assert (
                speech.script_for_reply(
                    "text",
                    api_key=_ANTHROPIC_KEY,
                    transport=httpx.MockTransport(lambda request: response),
                )
                is None
            )


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


class TestSpeechEntry:
    def test_a_reader_waits_for_later_chunks_and_ends_with_the_producer(self) -> None:
        entry = speech.SpeechEntry("I fixed it.")
        entry.append(b"first")
        received: list[bytes] = []
        finished = threading.Event()

        def read() -> None:
            received.extend(entry.iter_chunks())
            finished.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        entry.append(b"second")
        entry.finish()

        assert finished.wait(timeout=5)
        assert received == [b"first", b"second"]

    def test_a_failure_ends_the_readers(self) -> None:
        entry = speech.SpeechEntry("I fixed it.")
        entry.append(b"first")
        received: list[bytes] = []
        finished = threading.Event()

        def read() -> None:
            received.extend(entry.iter_chunks())
            finished.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        entry.fail()

        assert finished.wait(timeout=5)
        assert received == [b"first"]


class TestSpeechCache:
    def test_returns_an_entry_only_for_the_body_it_was_made_from(self) -> None:
        cache = speech.SpeechCache()
        assert cache.start_filling("c-1", "I fixed it.", _FakeStream([b"audio"]))
        entry = cache.entry("c-1", "I fixed it.")
        assert entry is not None

        assert cache.entry("c-1", "I fixed it, again.") is None
        assert cache.entry("c-2", " I fixed it.") is None

    def test_drops_the_oldest_entry_past_its_limit(self) -> None:
        cache = speech.SpeechCache(limit=2)
        for comment_id, body in (("c-1", "a"), ("c-2", "b"), ("c-3", "c")):
            assert cache.start_filling(comment_id, body, _FakeStream([b"audio"]))

        assert cache.entry("c-1", "a") is None
        assert cache.entry("c-2", "b") is not None
        assert cache.entry("c-3", "c") is not None

    def test_a_second_start_for_the_same_body_is_refused(self) -> None:
        cache = speech.SpeechCache()
        assert cache.start_filling("c-1", "body", _FakeStream([b"audio"]))
        duplicate = _FakeStream([b"audio"])

        assert cache.start_filling("c-1", "body", duplicate) is False
        # The caller closes the unused stream; the cache does not.
        assert duplicate.closed is False


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


class TestSpeechRoute:
    def test_the_post_answers_before_the_audio_is_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = _ControlledAudio()
        seen = _serve_stream(monkeypatch, audio)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)

            status, _headers, body = _speech_request(port, ticket_id, comment_id)
            assert status == 200
            assert json.loads(body) == {"status": "generating"}
            assert len(seen) == 1

            connection, response = _open_audio(port, ticket_id, comment_id)
            try:
                assert response.status == 200
                assert response.getheader("Content-Type") == "audio/mpeg"
                # The media element cannot set a cache mode, so a stored
                # partial body would be replayed on the next listen.
                assert response.getheader("Cache-Control") == "no-store"
                # A filling entry has no known length, so the connection
                # closing is what ends the body.
                assert response.getheader("Content-Length") is None
                assert response.read(5) == b"early"
                audio.late.set()
                assert response.read() == b"late"
            finally:
                audio.late.set()
                connection.close()

    def test_a_second_click_calls_neither_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def fake_script(
            reply: str, *, api_key: str, base_url: str | None = None
        ) -> str:
            calls.append("script")
            return "A spoken script."

        def fake_stream(text: str, *, api_key: str, region: str = "us") -> _FakeStream:
            calls.append("stream")
            return _FakeStream([b"mp3-bytes"])

        monkeypatch.setattr("symphony_linear.webui.api.script_for_reply", fake_script)
        monkeypatch.setattr("symphony_linear.webui.api.stream_speech", fake_stream)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)

            status, _headers, _body = _speech_request(port, ticket_id, comment_id)
            assert status == 200

            status, headers, body = _request(
                port, "GET", _speech_path(ticket_id, comment_id)
            )
            assert status == 200
            assert headers["Content-Type"] == "audio/mpeg"
            assert body == b"mp3-bytes"

            # The complete entry answers the next POST without either call.
            status, _headers, _body = _speech_request(port, ticket_id, comment_id)
            assert status == 200

        assert calls == ["script", "stream"]

    def test_get_without_an_entry_is_a_404_and_calls_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def fake_script(
            reply: str, *, api_key: str, base_url: str | None = None
        ) -> str:
            calls.append("script")
            return "A spoken script."

        def fake_stream(text: str, *, api_key: str, region: str = "us") -> _FakeStream:
            calls.append("stream")
            return _FakeStream([b"mp3-bytes"])

        monkeypatch.setattr("symphony_linear.webui.api.script_for_reply", fake_script)
        monkeypatch.setattr("symphony_linear.webui.api.stream_speech", fake_stream)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            status, _headers, body = _request(
                port, "GET", _speech_path(ticket_id, comment_id)
            )

        assert status == 404
        assert json.loads(body) == {"error": "No audio for that reply."}
        assert calls == []

    def test_a_human_comment_a_notice_and_an_unknown_comment_are_refused(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, _reply_id = _reply_ticket(tracker)
            human = tracker.store.add_human_comment(ticket_id, "Hello")
            notice = tracker.store.add_comment(
                ticket_id, "Workspace ready", kind="workspace"
            )

            for comment_id in (human.id, notice.id, "c-missing"):
                for method in ("POST", "GET"):
                    headers = {"Origin": _origin(port)} if method == "POST" else {}
                    status, _headers, body = _request(
                        port,
                        method,
                        _speech_path(ticket_id, comment_id),
                        headers=headers,
                        body=b"{}" if method == "POST" else None,
                    )
                    assert status == 404
                    assert json.loads(body) == {
                        "error": "That reply cannot be read aloud."
                    }

    def test_an_upstream_failure_answers_502_with_a_readable_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unreachable_script(
            reply: str, *, api_key: str, base_url: str | None = None
        ) -> None:
            return None

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)

            monkeypatch.setattr(
                "symphony_linear.webui.api.script_for_reply", unreachable_script
            )
            status, _headers, body = _speech_request(port, ticket_id, comment_id)
            assert status == 502
            assert json.loads(body) == {
                "error": "Could not summarise the reply. Try again."
            }

    def test_an_upstream_refusal_leaves_no_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both kinds of refusal happen before any audio exists: a non-success
        # status and a 200 that is not audio.  Neither may leave an entry, so
        # the next click asks Soniox again rather than replaying bytes.
        responses = [
            httpx.Response(
                403,
                json={"error_message": "Key [redacted] lacks text-to-speech."},
            ),
            httpx.Response(
                200,
                json={"error_message": "not audio"},
                headers={"Content-Type": "application/json"},
            ),
        ]
        attempts: list[str] = []

        def fake_script(
            reply: str, *, api_key: str, base_url: str | None = None
        ) -> str:
            return "A spoken script."

        def fake_stream(
            text: str, *, api_key: str, region: str = "us"
        ) -> soniox.SpeechStream:
            attempts.append(text)
            response = responses[len(attempts) - 1]
            return soniox.stream_speech(
                text,
                api_key=api_key,
                region=region,
                transport=httpx.MockTransport(lambda request: response),
            )

        monkeypatch.setattr("symphony_linear.webui.api.script_for_reply", fake_script)
        monkeypatch.setattr("symphony_linear.webui.api.stream_speech", fake_stream)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)

            for message in (
                "Key [redacted] lacks text-to-speech.",
                "Soniox returned no audio.",
            ):
                status, _headers, body = _speech_request(port, ticket_id, comment_id)
                assert status == 502
                assert json.loads(body) == {"error": message}

                # No entry was left, so the GET has nothing to serve.
                status, _headers, _body = _request(
                    port, "GET", _speech_path(ticket_id, comment_id)
                )
                assert status == 404

        assert attempts == ["A spoken script.", "A spoken script."]

    def test_a_producer_failure_ends_the_readers_and_removes_the_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = _ControlledAudio(fail_late=True)
        _serve_stream(monkeypatch, audio)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            status, _headers, _body = _speech_request(port, ticket_id, comment_id)
            assert status == 200

            connection, response = _open_audio(port, ticket_id, comment_id)
            try:
                assert response.read(5) == b"early"
                audio.late.set()
                # The reader ends instead of waiting for a chunk that will
                # never come.
                assert response.read() == b""
            finally:
                audio.late.set()
                connection.close()

            status, _headers, _body = _request(
                port, "GET", _speech_path(ticket_id, comment_id)
            )
            assert status == 404

    def test_an_empty_stream_is_dropped_so_the_next_click_generates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A success with no bytes is not a clip.  Caching it would replay
        # silence on every click until the daemon restarts.
        bodies = [[], [b"mp3-bytes"]]
        streams: list[str] = []

        def fake_script(
            reply: str, *, api_key: str, base_url: str | None = None
        ) -> str:
            return "A spoken script."

        def fake_stream(text: str, *, api_key: str, region: str = "us") -> _FakeStream:
            streams.append(text)
            return _FakeStream(bodies[len(streams) - 1])

        monkeypatch.setattr("symphony_linear.webui.api.script_for_reply", fake_script)
        monkeypatch.setattr("symphony_linear.webui.api.stream_speech", fake_stream)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)

            status, _headers, _body = _speech_request(port, ticket_id, comment_id)
            assert status == 200

            # A GET of a filling entry ends only when its producer has, so
            # after this request the empty clip is gone either way.
            _status, _headers, _body = _request(
                port, "GET", _speech_path(ticket_id, comment_id)
            )

            status, _headers, _body = _speech_request(port, ticket_id, comment_id)
            assert status == 200
            status, _headers, body = _request(
                port, "GET", _speech_path(ticket_id, comment_id)
            )
            assert status == 200
            assert body == b"mp3-bytes"

        assert streams == ["A spoken script.", "A spoken script."]

    def test_a_client_that_leaves_does_not_stop_the_producer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = _ControlledAudio()
        _serve_stream(monkeypatch, audio)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            status, _headers, _body = _speech_request(port, ticket_id, comment_id)
            assert status == 200

            connection, response = _open_audio(port, ticket_id, comment_id)
            assert response.read(5) == b"early"
            # http.client hands the socket to the response once the request is
            # sent, so closing the connection alone leaves the reader attached
            # and does not exercise an abandoned listener.
            response.close()
            connection.close()

            # Nobody is listening any more, but the entry still completes, so
            # the next listen starts from the beginning and is immediate.
            audio.late.set()
            status, _headers, body = _request(
                port, "GET", _speech_path(ticket_id, comment_id)
            )
            assert status == 200
            assert body == b"earlylate"

    def test_a_head_returns_without_the_audio(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = _ControlledAudio()
        _serve_stream(monkeypatch, audio)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            status, _headers, _body = _speech_request(port, ticket_id, comment_id)
            assert status == 200

            # A raw socket sees body bytes the HTTP client would discard for a
            # HEAD, and the five-second read timeout catches an implementation
            # that waits for the producer instead of returning at once.
            raw = _raw_socket_request(
                port,
                (
                    f"HEAD {_speech_path(ticket_id, comment_id)} HTTP/1.0\r\n"
                    f"Authorization: {_basic()}\r\n"
                    "Host: 127.0.0.1\r\n"
                    "\r\n"
                ).encode("ascii"),
            )
            head, _separator, raw_body = raw.partition(b"\r\n\r\n")
            assert b" 200 " in head.split(b"\r\n", 1)[0]
            assert b"Content-Type: audio/mpeg" in head
            assert raw_body == b""

            # The entry was untouched by the HEAD and still completes.
            audio.late.set()
            status, _headers, body = _request(
                port, "GET", _speech_path(ticket_id, comment_id)
            )
            assert status == 200
            assert body == b"earlylate"

    def test_the_route_is_same_origin_guarded(self, tmp_path: Path) -> None:
        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            status, _headers, _body = _request(
                port,
                "POST",
                _speech_path(ticket_id, comment_id),
                body=b"{}",
            )

        assert status == 403
