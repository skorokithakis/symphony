"""Tests for reading an agent reply aloud.

The upstream calls are exercised with ``httpx.MockTransport`` so no request
leaves the process, and the route is exercised through the real server.  The
boundaries that matter are the gate (both keys, in the route table, the policy
and the view), the fixed Soniox request shape, the in-memory cache, and the
refusal of anything that is not an agent reply.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from symphony_linear import anthropic, soniox
from symphony_linear.local_tracker import STATE_NEEDS_INPUT, LocalTracker
from symphony_linear.webui import speech
from symphony_linear.webui.routes import SPEECH_ROUTE, routes_for
from tests.test_webui_server import (
    _config,
    _origin,
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


def _speech_request(
    port: int, ticket_id: str, comment_id: str
) -> tuple[int, dict[str, str], bytes]:
    return _request(
        port,
        "POST",
        f"/api/tickets/{ticket_id}/comments/{comment_id}/speech",
        headers={"Origin": _origin(port)},
        body=b"{}",
    )


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

    def test_the_view_refuses_without_both_keys(self, tmp_path: Path) -> None:
        # A route table that was bypassed (a direct construction, or a later
        # change that forgets the conditional) must still not reach either
        # paid API.
        with _running_server(tmp_path, routes=(SPEECH_ROUTE,)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            status, _headers, body = _speech_request(port, ticket_id, comment_id)

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

    def test_csp_allows_blob_and_data_media_with_both_keys(
        self, tmp_path: Path
    ) -> None:
        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            _tracker,
            _state,
        ):
            _status, headers, _body = _request(port, "GET", "/")

        policy = headers["Content-Security-Policy"]
        assert "media-src blob: data:;" in policy
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


class TestSonioxRequest:
    def test_sends_one_request_with_the_fixed_voice_and_format(self) -> None:
        transport, seen = _capture(
            lambda request: httpx.Response(
                200, content=b"ID3audio", headers={"Content-Type": "audio/mpeg"}
            )
        )

        audio = soniox.synthesise(
            "A spoken script.", api_key=_SONIOX_KEY, transport=transport
        )

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
            soniox.synthesise(
                "x",
                api_key=_SONIOX_KEY,
                transport=httpx.MockTransport(lambda request: refused),
            )

        assert str(error.value) == "Key [redacted] lacks text-to-speech."
        assert _SONIOX_KEY not in str(error.value)

    def test_an_unreachable_or_empty_response_has_a_short_message(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(soniox.SonioxError) as error:
            soniox.synthesise(
                "x", api_key=_SONIOX_KEY, transport=httpx.MockTransport(unreachable)
            )
        assert str(error.value) == "Could not reach Soniox. Try again."

        with pytest.raises(soniox.SonioxError) as error:
            soniox.synthesise(
                "x",
                api_key=_SONIOX_KEY,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, content=b"", headers={"Content-Type": "audio/mpeg"}
                    )
                ),
            )
        assert str(error.value) == "Soniox returned no audio."


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


class TestSpeechCache:
    def test_returns_audio_only_for_the_body_it_was_made_from(self) -> None:
        cache = speech.SpeechCache()
        cache.put("c-1", "I fixed it.", b"mp3")

        assert cache.get("c-1", "I fixed it.") == b"mp3"
        assert cache.get("c-1", "I fixed it, again.") is None
        assert cache.get("c-2", " I fixed it.") is None

    def test_drops_the_oldest_entry_past_its_limit(self) -> None:
        cache = speech.SpeechCache(limit=2)
        cache.put("c-1", "a", b"1")
        cache.put("c-2", "b", b"2")
        cache.put("c-3", "c", b"3")

        assert cache.get("c-1", "a") is None
        assert cache.get("c-2", "b") == b"2"
        assert cache.get("c-3", "c") == b"3"


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


class TestSpeechRoute:
    def test_returns_the_mp3_and_reuses_it_on_a_second_click(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def fake_script(
            reply: str, *, api_key: str, base_url: str | None = None
        ) -> str:
            calls.append(reply)
            assert api_key == _ANTHROPIC_KEY
            return "A spoken script."

        def fake_synthesise(text: str, *, api_key: str) -> bytes:
            calls.append(text)
            assert api_key == _SONIOX_KEY
            return b"mp3-bytes"

        monkeypatch.setattr("symphony_linear.webui.api.script_for_reply", fake_script)
        monkeypatch.setattr("symphony_linear.webui.api.synthesise", fake_synthesise)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)

            status, headers, body = _speech_request(port, ticket_id, comment_id)
            assert status == 200
            assert headers["Content-Type"] == "audio/mpeg"
            assert body == b"mp3-bytes"
            assert calls == ["I fixed the widget.", "A spoken script."]

            # The cache answers the second click without either call.
            status, _headers, body = _speech_request(port, ticket_id, comment_id)
            assert status == 200
            assert body == b"mp3-bytes"
            assert calls == ["I fixed the widget.", "A spoken script."]

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
                status, _headers, body = _speech_request(port, ticket_id, comment_id)
                assert status == 404
                assert json.loads(body) == {"error": "That reply cannot be read aloud."}

    def test_an_upstream_failure_answers_502_with_a_readable_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unreachable_script(
            reply: str, *, api_key: str, base_url: str | None = None
        ) -> None:
            return None

        def refused_speech(text: str, *, api_key: str) -> bytes:
            raise soniox.SonioxError("Key [redacted] lacks text-to-speech.")

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

            monkeypatch.setattr(
                "symphony_linear.webui.api.script_for_reply",
                lambda reply, *, api_key, base_url=None: "A spoken script.",
            )
            monkeypatch.setattr("symphony_linear.webui.api.synthesise", refused_speech)
            status, _headers, body = _speech_request(port, ticket_id, comment_id)
            assert status == 502
            assert json.loads(body) == {"error": "Key [redacted] lacks text-to-speech."}

    def test_a_json_success_is_refused_and_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A 200 that is not audio must not reach the cache: a second click has
        # to ask Soniox again rather than replay the stored bytes.
        synthesised: list[str] = []

        def json_speech(text: str, *, api_key: str) -> bytes:
            synthesised.append(text)
            return soniox.synthesise(
                text,
                api_key=api_key,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={"error_message": "not audio"},
                        headers={"Content-Type": "application/json"},
                    )
                ),
            )

        monkeypatch.setattr(
            "symphony_linear.webui.api.script_for_reply",
            lambda reply, *, api_key, base_url=None: "A spoken script.",
        )
        monkeypatch.setattr("symphony_linear.webui.api.synthesise", json_speech)

        with _running_server(tmp_path, **dict(_BOTH_KEYS)) as (
            port,
            tracker,
            _state,
        ):
            ticket_id, comment_id = _reply_ticket(tracker)
            for _click in range(2):
                status, _headers, body = _speech_request(port, ticket_id, comment_id)
                assert status == 502
                assert json.loads(body) == {"error": "Soniox returned no audio."}

        assert synthesised == ["A spoken script.", "A spoken script."]

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
                f"/api/tickets/{ticket_id}/comments/{comment_id}/speech",
                body=b"{}",
            )

        assert status == 403
