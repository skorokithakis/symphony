"""Tests for the Web Push store and the Needs Input sender."""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from symphony_linear.webpush import (
    WebPushStore,
    publish_needs_input_push,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class _Subscriber:
    """A browser side of one subscription: its private key and auth secret."""

    def __init__(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.auth_secret = bytes(range(16))

    @property
    def p256dh(self) -> str:
        return _b64url(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )

    @property
    def auth(self) -> str:
        return _b64url(self.auth_secret)

    def add_to(self, store: WebPushStore, endpoint: str) -> None:
        store.add_subscription(endpoint, self.p256dh, self.auth)


def _recording_transport(
    seen: list[httpx.Request], *, status: int = 201
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return httpx.MockTransport(handle)


def _vapid_parts(request: httpx.Request) -> tuple[str, str]:
    """Split the Authorization header into its JWT and public key."""
    scheme, _, params = request.headers["Authorization"].partition(" ")
    assert scheme == "vapid"
    fields = dict(part.strip().split("=", 1) for part in params.split(","))
    return fields["t"], fields["k"]


class TestStore:
    def test_the_public_key_is_stable_and_persisted(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)

        key = store.public_key()

        # applicationServerKey must be the uncompressed point (0x04 + x + y),
        # not a DER SubjectPublicKeyInfo.
        raw = _decode_b64url(key)
        assert len(raw) == 65
        assert raw[0] == 0x04
        assert (tmp_path / "webpush.json").is_file()

        # A daemon restart must not silently mint a new key: every browser
        # subscription is bound to this one.
        assert WebPushStore(tmp_path).public_key() == key

    def test_keys_are_not_generated_until_first_use(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)

        assert not (tmp_path / "webpush.json").exists()
        assert store.subscriptions() == []

    @pytest.mark.parametrize(
        "endpoint",
        ["http://push.example.com/1", "push.example.com/1", "https://"],
    )
    def test_a_non_https_endpoint_is_rejected(
        self, tmp_path: Path, endpoint: str
    ) -> None:
        store = WebPushStore(tmp_path)
        subscriber = _Subscriber()

        with pytest.raises(ValueError, match="https"):
            store.add_subscription(endpoint, subscriber.p256dh, subscriber.auth)

        assert store.subscriptions() == []
        assert not (tmp_path / "webpush.json").exists()

    def test_invalid_key_material_is_rejected(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)
        subscriber = _Subscriber()

        # A DER key, a P-256 key with the wrong length, and an auth secret of
        # the wrong size are all browser bugs a 400 should surface, not a
        # WARNING on every later transition.
        der_p256dh = _b64url(
            subscriber.private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        with pytest.raises(ValueError, match="p256dh"):
            store.add_subscription(
                "https://push.example.com/1", der_p256dh, subscriber.auth
            )
        with pytest.raises(ValueError, match="auth"):
            store.add_subscription(
                "https://push.example.com/1", subscriber.p256dh, _b64url(b"short")
            )
        with pytest.raises(ValueError, match="base64url"):
            store.add_subscription(
                "https://push.example.com/1", "not base64!", subscriber.auth
            )

        assert store.subscriptions() == []

    def test_one_subscription_per_endpoint(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)
        first = _Subscriber()
        second = _Subscriber()

        first.add_to(store, "https://push.example.com/1")
        second.add_to(store, "https://push.example.com/1")

        subscriptions = store.subscriptions()
        assert len(subscriptions) == 1
        # Re-subscribing is how a browser rotates its keys, so the newest keys
        # must win rather than the first registration sticking forever.
        assert subscriptions[0].p256dh == second.p256dh

    def test_remove_reports_whether_the_endpoint_was_there(
        self, tmp_path: Path
    ) -> None:
        store = WebPushStore(tmp_path)
        _Subscriber().add_to(store, "https://push.example.com/1")

        assert store.remove_subscription("https://push.example.com/1") is True
        assert store.remove_subscription("https://push.example.com/1") is False
        assert store.subscriptions() == []

    def test_a_mismatched_key_pair_is_fatal(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)
        store.public_key()
        data = json.loads((tmp_path / "webpush.json").read_text())
        data["vapid_public_key"] = _b64url(b"\x04" + bytes(64))
        (tmp_path / "webpush.json").write_text(json.dumps(data))

        # Silently regenerating here would invalidate every subscription and
        # every browser's applicationServerKey.
        with pytest.raises(ValueError, match="does not match"):
            WebPushStore(tmp_path)


class TestSender:
    def test_the_vapid_jwt_verifies_with_the_stored_public_key(
        self, tmp_path: Path
    ) -> None:
        store = WebPushStore(tmp_path)
        _Subscriber().add_to(store, "https://push.example.com/subscription/1")
        seen: list[httpx.Request] = []

        publish_needs_input_push(
            store,
            ticket_id="SYM-1",
            body="SYM-1: Fix the frobnicator",
            public_url="https://symphony.example.com",
            transport=_recording_transport(seen),
        )

        token, advertised_key = _vapid_parts(seen[0])
        assert advertised_key == store.public_key()
        header_b64, claims_b64, signature_b64 = token.split(".")
        header = json.loads(_decode_b64url(header_b64))
        claims = json.loads(_decode_b64url(claims_b64))
        assert header == {"typ": "JWT", "alg": "ES256"}
        # aud is the endpoint origin, not the full subscription URL.
        assert claims["aud"] == "https://push.example.com"
        assert claims["sub"] == "https://symphony.example.com"
        now = int(time.time())
        assert now < claims["exp"] <= now + 12 * 60 * 60

        signature = _decode_b64url(signature_b64)
        assert len(signature) == 64
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), _decode_b64url(advertised_key)
        )
        # Push services reject DER, and an invalid signature is a 401, so this
        # conversion is part of the protocol, not a formality.
        public_key.verify(
            utils.encode_dss_signature(
                int.from_bytes(signature[:32], "big"),
                int.from_bytes(signature[32:], "big"),
            ),
            f"{header_b64}.{claims_b64}".encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )

    def test_the_subscribed_browser_can_decrypt_the_payload(
        self, tmp_path: Path
    ) -> None:
        store = WebPushStore(tmp_path)
        subscriber = _Subscriber()
        subscriber.add_to(store, "https://push.example.com/subscription/1")
        seen: list[httpx.Request] = []

        publish_needs_input_push(
            store,
            ticket_id="SYM-7",
            body="SYM-7: Déployer — 日本語",
            transport=_recording_transport(seen),
        )

        request = seen[0]
        assert request.method == "POST"
        assert request.headers["Content-Encoding"] == "aes128gcm"
        assert request.headers["TTL"] == "86400"
        encrypted = http_ece.decrypt(
            request.content,
            private_key=subscriber.private_key,
            auth_secret=subscriber.auth_secret,
            version="aes128gcm",
        )
        assert json.loads(encrypted) == {
            "title": "Needs Input",
            "body": "SYM-7: Déployer — 日本語",
            "url": "/tickets/SYM-7",
            "tag": "SYM-7",
        }

    def test_a_long_body_is_capped_by_code_point(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)
        subscriber = _Subscriber()
        subscriber.add_to(store, "https://push.example.com/subscription/1")
        seen: list[httpx.Request] = []

        publish_needs_input_push(
            store,
            ticket_id="SYM-9",
            body="SYM-9: " + "日" * 500,
            transport=_recording_transport(seen),
        )

        encrypted = http_ece.decrypt(
            seen[0].content,
            private_key=subscriber.private_key,
            auth_secret=subscriber.auth_secret,
            version="aes128gcm",
        )
        body = json.loads(encrypted)["body"]
        # The cap keeps the encrypted record under the 4096-byte single-record
        # limit, and slicing by code point leaves no broken character.
        assert len(seen[0].content) < 4096
        assert len(body) == 200
        assert body.startswith("SYM-9: 日")
        assert body.endswith("…")

    def test_an_expired_subscription_is_removed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = WebPushStore(tmp_path)
        _Subscriber().add_to(store, "https://push.example.com/gone")
        _Subscriber().add_to(store, "https://push.example.com/live")

        def handle(request: httpx.Request) -> httpx.Response:
            status = 410 if request.url.path == "/gone" else 201
            return httpx.Response(status)

        with caplog.at_level(logging.INFO, logger="symphony_linear.webpush"):
            publish_needs_input_push(
                store,
                ticket_id="SYM-1",
                body="SYM-1: t",
                transport=httpx.MockTransport(handle),
            )

        assert [s.endpoint for s in store.subscriptions()] == [
            "https://push.example.com/live"
        ]
        assert "Dropped an expired Web Push subscription" in caplog.text
        # The endpoint is a bearer capability, so it must not reach the log.
        assert "push.example.com/gone" not in caplog.text

    def test_any_other_status_only_warns_and_keeps_the_subscription(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = WebPushStore(tmp_path)
        _Subscriber().add_to(store, "https://push.example.com/1")

        with caplog.at_level(logging.WARNING, logger="symphony_linear.webpush"):
            publish_needs_input_push(
                store,
                ticket_id="SYM-1",
                body="SYM-1: t",
                transport=_recording_transport([], status=500),
            )

        assert "HTTP 500" in caplog.text
        assert len(store.subscriptions()) == 1

    def test_a_transport_error_never_reaches_the_turn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = WebPushStore(tmp_path)
        _Subscriber().add_to(store, "https://push.example.com/1")

        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        with caplog.at_level(logging.WARNING, logger="symphony_linear.webpush"):
            publish_needs_input_push(
                store,
                ticket_id="SYM-1",
                body="SYM-1: t",
                transport=httpx.MockTransport(explode),
            )

        assert "ConnectError" in caplog.text
        assert len(store.subscriptions()) == 1

    def test_no_subscribers_means_no_request(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)
        seen: list[httpx.Request] = []

        publish_needs_input_push(
            store,
            ticket_id="SYM-1",
            body="SYM-1: t",
            transport=_recording_transport(seen),
        )

        assert seen == []
        # Sending is what generates the pair, so a workspace with no browser
        # subscription stays untouched.
        assert not (tmp_path / "webpush.json").exists()

    def test_sub_no_claim_without_public_url(self, tmp_path: Path) -> None:
        store = WebPushStore(tmp_path)
        _Subscriber().add_to(store, "https://push.example.com/1")
        seen: list[httpx.Request] = []

        publish_needs_input_push(
            store,
            ticket_id="SYM-1",
            body="SYM-1: t",
            transport=_recording_transport(seen),
        )

        token, _ = _vapid_parts(seen[0])
        claims = json.loads(_decode_b64url(token.split(".")[1]))
        assert "sub" not in claims
