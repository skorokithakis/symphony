"""Web Push for the built-in tracker: subscription store and Needs Input sender.

Unlike ntfy, Web Push needs no third-party server: the browser registers a
``PushSubscription`` with its own push service, and the daemon posts an
encrypted message to that endpoint.  The protocol has two independent halves,
both implemented here:

* **VAPID** (RFC 8292) identifies this daemon to the push service with a
  long-lived P-256 key pair.  The browser's subscription is bound to the public
  half through ``applicationServerKey``, so the pair must survive restarts and
  lives in :class:`WebPushStore`, not in memory.
* **Message encryption** (RFC 8291, ``aes128gcm``) makes the payload readable
  only by the subscribing browser, using the per-subscription ``p256dh`` and
  ``auth`` values the browser supplied at subscribe time.

Web Push is always enabled for the web UI backend: it has no config block and
no on/off switch.  The store file lives in the workspace root so the
workspace-root sandbox masking already hides it from agents, and so there is
exactly one store per workspace.  Both the daemon (removing dead
subscriptions) and the web server (adding them) share one instance, which
``cli._create_tracker`` builds and exposes as ``LocalTracker.webpush``; the
web server's ``/api/push-subscriptions`` route adds subscriptions to that
instance.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: The file name in the workspace root.  The computed sandbox masking hides
#: every root entry except the current ticket's directory, so agents cannot
#: read the subscriptions or the VAPID signing key without a config entry.
STORE_FILE_NAME = "webpush.json"

#: Push services are allowed to sit on a slow network, but a notification must
#: not hold a turn for long: the ticket page and the reply are already updated
#: by the time this runs.
_PUSH_TIMEOUT_SECONDS = 5.0

#: RFC 8292 caps a VAPID token's lifetime at 24 hours.  Twelve hours is the
#: value used by every established implementation and survives a night's
#: sleep without the daemon needing to mint tokens eagerly.
_VAPID_TOKEN_LIFETIME_SECONDS = 12 * 60 * 60

#: How long the push service may keep a message for a device that is offline.
#: One day is the de facto default; the ticket remains waiting after that.
_TTL_SECONDS = 24 * 60 * 60

#: Auth secrets are 16 octets (RFC 8291 section 3.2).  p256dh values are one
#: uncompressed P-256 point (65 octets, 0x04 followed by x and y).
_AUTH_SECRET_LENGTH = 16
_UNCOMPRESSED_POINT_LENGTH = 65

#: A push service accepts one encrypted record of at most 4096 bytes, and the
#: aes128gcm header, the ephemeral key and the JSON envelope all come out of
#: that budget.  A ticket title is arbitrary user text, so the body is capped
#: well below the limit; 200 code points leave room for four-byte characters.
_MAX_BODY_CHARACTERS = 200


class WebPushSubscription(BaseModel):
    """One browser subscription, as stored on disk and returned to the web UI.

    The keys are the browser's, not ours: ``p256dh`` is the subscription's
    public key and ``auth`` its shared authentication secret.  Both are
    base64url without padding, exactly as ``PushSubscription.getKey()``
    serialises them.
    """

    endpoint: str
    p256dh: str
    auth: str


class WebPushStore:
    """The VAPID key pair and subscriptions, persisted as one JSON file.

    Reads and writes are guarded by one lock because the web server thread
    adds subscriptions while the orchestrator thread removes dead ones.  The
    store keeps them in memory after the initial load; the file is rewritten
    atomically on every change, so a crash cannot leave a half-written pair
    behind.

    The VAPID pair is generated lazily on first use rather than at
    construction: creating a store must not write to the workspace before the
    web UI has actually asked for the public key or a notification is due.

    Raises :class:`ValueError` when the file exists but cannot be trusted.
    That happens at startup and is deliberately fatal: silently replacing a
    corrupt key pair would invalidate every existing subscription.
    """

    def __init__(self, workspace: Path) -> None:
        self._path = workspace / STORE_FILE_NAME
        self._lock = threading.RLock()
        self._keys: tuple[ec.EllipticCurvePrivateKey, str] | None = None
        self._subscriptions: dict[str, WebPushSubscription] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API (the web server and the sender both use this)
    # ------------------------------------------------------------------

    def public_key(self) -> str:
        """Return the VAPID public key as base64url uncompressed point.

        This is the value the browser needs as ``applicationServerKey`` in
        ``pushManager.subscribe``.  It is generated and persisted on the first
        call, so the browser sees the same key after a daemon restart.
        """
        with self._lock:
            return self._ensure_keys()[1]

    def add_subscription(self, endpoint: object, p256dh: object, auth: object) -> None:
        """Store one subscription, replacing an existing one for *endpoint*.

        *endpoint* must be an ``https`` URL: a notification body is encrypted
        but the endpoint is a bearer capability, so sending it over plain HTTP
        would let anyone on the path learn the capability and inject messages.
        ``p256dh`` and ``auth`` are validated as real key material here, not
        at send time, so a malformed browser payload is a rejected request
        instead of a WARNING on every later transition.  The stored strings
        are re-encoded canonically, which also makes the endpoint deduplication
        reliable.

        The parameters are typed ``object`` on purpose: the web server forwards
        a browser's JSON payload verbatim, so this method is the validation
        boundary for it, not the caller.

        Raises :class:`ValueError` for an unacceptable endpoint or key.
        """
        subscription = _validated_subscription(endpoint, p256dh, auth)
        with self._lock:
            self._subscriptions[subscription.endpoint] = subscription
            self._save()

    def remove_subscription(self, endpoint: str) -> bool:
        """Forget *endpoint*; return whether it was there.

        Used by the sender when the push service reports the subscription as
        gone (HTTP 404 or 410).  Unknown endpoints are not an error: two
        transitions can race to remove the same dead subscription.
        """
        with self._lock:
            if self._subscriptions.pop(endpoint, None) is None:
                return False
            self._save()
            return True

    def subscriptions(self) -> list[WebPushSubscription]:
        """Return a snapshot, so a caller can send while others modify the store."""
        with self._lock:
            return [
                subscription.model_copy()
                for subscription in self._subscriptions.values()
            ]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # ValueError covers a non-UTF-8 file: read_text decodes eagerly.
            raise ValueError(f"Cannot read Web Push state {self._path}: {exc}") from exc
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ValueError(
                f"Web Push state {self._path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Web Push state {self._path} must be a JSON object")

        private_pem = raw.get("vapid_private_key")
        public_key = raw.get("vapid_public_key")
        if (private_pem is None) != (public_key is None):
            raise ValueError(
                f"Web Push state {self._path} has only half of the VAPID key pair"
            )
        if private_pem is not None:
            if not isinstance(private_pem, str) or not isinstance(public_key, str):
                raise ValueError(
                    f"Web Push state {self._path} has a non-string VAPID key"
                )
            loaded = _load_private_key(private_pem)
            derived = _public_key_of(loaded)
            if derived != public_key:
                # Signing with the private key while advertising a different
                # public key makes every push service answer 403, and the
                # browser's applicationServerKey no longer matches either.
                raise ValueError(
                    f"Web Push state {self._path} has a VAPID public key that "
                    "does not match its private key"
                )
            self._keys = (loaded, derived)

        entries = raw.get("subscriptions", [])
        if not isinstance(entries, list):
            raise ValueError(
                f"Web Push state {self._path} has a non-list 'subscriptions'"
            )
        for number, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Web Push subscription #{number} in {self._path} is not an object"
                )
            try:
                subscription = _validated_subscription(
                    entry.get("endpoint"), entry.get("p256dh"), entry.get("auth")
                )
            except ValueError as exc:
                raise ValueError(
                    f"Web Push subscription #{number} in {self._path} is invalid: {exc}"
                ) from exc
            self._subscriptions[subscription.endpoint] = subscription

    def _save(self) -> None:
        """Persist the current state.  The caller must hold ``self._lock``."""
        payload: dict[str, Any] = {
            "subscriptions": [
                subscription.model_dump(mode="json")
                for subscription in self._subscriptions.values()
            ]
        }
        if self._keys is not None:
            private_key, public_key = self._keys
            payload["vapid_private_key"] = private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii")
            payload["vapid_public_key"] = public_key
        _write_json(self._path, payload)

    def _ensure_keys(self) -> tuple[ec.EllipticCurvePrivateKey, str]:
        """Return the VAPID pair, generating and persisting it on first use."""
        with self._lock:
            if self._keys is None:
                private_key = ec.generate_private_key(ec.SECP256R1())
                self._keys = (private_key, _public_key_of(private_key))
                self._save()
            return self._keys


# ---------------------------------------------------------------------------
# Needs Input sender
# ---------------------------------------------------------------------------


def publish_needs_input_push(
    store: WebPushStore,
    *,
    ticket_id: str,
    body: str,
    public_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Send one Web Push to every stored subscription for a Needs Input ticket.

    The caller supplies *body* (``"<identifier>: <display title>"``) because
    the tracker already owns the display-title fallback; this module must not
    import the tracker, which imports it.

    This runs at the end of a turn, after the state change was persisted and
    the reply was posted, so it is one best-effort boundary like
    :func:`symphony_linear.local_tracker.publish_needs_input`: every failure is
    one WARNING and a return, nothing is retried, and no exception reaches the
    turn.  A push service that no longer knows a subscription (404 or 410) is
    not a failure, it is the signal to delete that subscription; that is the
    only way subscriptions ever leave the store.

    *public_url* is used only for the VAPID ``sub`` claim.  The payload's
    ``url`` stays relative, because the browser resolves it against the page
    that registered the service worker.

    *transport* exists for tests, which pass ``httpx.MockTransport`` so no
    request leaves the process.
    """
    try:
        subscriptions = store.subscriptions()
        if not subscriptions:
            return
        private_key, public_key = store._ensure_keys()
        payload = json.dumps(
            {
                "title": "Needs Input",
                "body": _capped_body(body),
                # The service worker turns this into the notification's click
                # target.
                "url": f"/tickets/{ticket_id}",
                # ``tag`` replaces an older notification for the same ticket
                # instead of stacking one per turn.
                "tag": ticket_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        with httpx.Client(timeout=_PUSH_TIMEOUT_SECONDS, transport=transport) as client:
            for subscription in subscriptions:
                _send_one(
                    client,
                    store,
                    subscription,
                    payload=payload,
                    ticket_id=ticket_id,
                    private_key=private_key,
                    public_key=public_key,
                    public_url=public_url,
                )
    except Exception as exc:
        # Deliberately broad, for the same reason as the ntfy publisher: this
        # runs after a turn has ended, httpx can raise several unrelated types,
        # and the exception message can echo the endpoint, which is a bearer
        # capability.  Only the type is logged.
        logger.warning("Web Push for %s failed: %s", ticket_id, type(exc).__name__)


def _send_one(
    client: httpx.Client,
    store: WebPushStore,
    subscription: WebPushSubscription,
    *,
    payload: bytes,
    ticket_id: str,
    private_key: ec.EllipticCurvePrivateKey,
    public_key: str,
    public_url: str | None,
) -> None:
    """Encrypt and POST one message.  Never raises."""
    try:
        encrypted = _encrypt(subscription, payload)
        origin = _origin(subscription.endpoint)
        token = _vapid_token(private_key, origin, public_url=public_url)
        response = client.post(
            subscription.endpoint,
            content=encrypted,
            headers={
                # The space after the commas is what RFC 8292's ABNF allows and
                # what every push service accepts; the token itself is opaque.
                "Authorization": f"vapid t={token}, k={public_key}",
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(_TTL_SECONDS),
            },
        )
    except Exception as exc:
        logger.warning("Web Push for %s failed: %s", ticket_id, type(exc).__name__)
        return

    if response.status_code in (404, 410):
        # The browser unsubscribed, or its push service expired the endpoint.
        # Removing it here is the only cleanup path; retrying would warn on
        # every future transition.
        store.remove_subscription(subscription.endpoint)
        logger.info(
            "Dropped an expired Web Push subscription for %s (HTTP %s)",
            ticket_id,
            response.status_code,
        )
        return
    if not response.is_success:
        # No endpoint in the message: it is a capability, and a failed one can
        # still be a live secret.  The status code is the actionable part.
        logger.warning(
            "Web Push for %s returned HTTP %s", ticket_id, response.status_code
        )


def _encrypt(subscription: WebPushSubscription, payload: bytes) -> bytes:
    """Encrypt *payload* for one subscriber, returning the aes128gcm body.

    ``http_ece`` adds the aes128gcm content header (salt, record size and the
    ephemeral public key) itself, and the ephemeral key is generated per
    message: reusing one across subscribers would let any of them read the
    others' messages.
    """
    subscriber_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), _b64url_decode(subscription.p256dh)
    )
    ephemeral_key = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(
        payload,
        private_key=ephemeral_key,
        dh=subscriber_key,
        auth_secret=_b64url_decode(subscription.auth),
        version="aes128gcm",
    )


def _vapid_token(
    private_key: ec.EllipticCurvePrivateKey,
    audience: str,
    *,
    public_url: str | None,
) -> str:
    """Build the ES256 JWT a push service accepts as VAPID authentication.

    ``aud`` must be the endpoint's origin, not the full endpoint URL, and
    ``exp`` must be in the future (12 hours here).  ``sub`` is optional in RFC
    8292; it is sent only when a public URL is configured, because it is meant
    to be a contact for the operator and an unset ``webui.public_url`` has
    nothing truthful to put there.
    """
    claims: dict[str, Any] = {
        "aud": audience,
        "exp": int(time.time()) + _VAPID_TOKEN_LIFETIME_SECONDS,
    }
    if public_url:
        claims["sub"] = public_url
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = b".".join(
        _b64url_encode(
            json.dumps(part, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).encode("ascii")
        for part in (header, claims)
    )
    der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    # ES256 signatures are raw R||S (two 32-byte integers), while
    # ``cryptography`` produces DER.  Push services reject DER.
    r, s = utils.decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input.decode('ascii')}.{_b64url_encode(raw_signature)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capped_body(body: str) -> str:
    """Return *body* with at most :data:`_MAX_BODY_CHARACTERS` code points.

    The cap is applied to the whole body rather than to the ticket title inside
    the caller: the payload size is what the push service limits, and this is
    the one place every caller goes through.  One code point is spent on the
    ellipsis, so the cap is a hard limit; ``str`` slicing is by code point, so
    multi-byte characters are not cut in half.
    """
    if len(body) <= _MAX_BODY_CHARACTERS:
        return body
    return body[: _MAX_BODY_CHARACTERS - 1] + "…"


def _validated_subscription(
    endpoint: object, p256dh: object, auth: object
) -> WebPushSubscription:
    """Validate and canonicalise one subscription's fields."""
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("endpoint must be a non-empty string")
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("endpoint must be an https URL")
    if not isinstance(p256dh, str) or not p256dh:
        raise ValueError("p256dh must be a non-empty string")
    if not isinstance(auth, str) or not auth:
        raise ValueError("auth must be a non-empty string")

    p256dh_bytes = _b64url_decode(p256dh)
    if len(p256dh_bytes) != _UNCOMPRESSED_POINT_LENGTH or p256dh_bytes[0] != 0x04:
        raise ValueError("p256dh must be an uncompressed P-256 public key")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh_bytes)
    except ValueError as exc:
        raise ValueError("p256dh must be a valid P-256 public key") from exc

    auth_bytes = _b64url_decode(auth)
    if len(auth_bytes) != _AUTH_SECRET_LENGTH:
        raise ValueError("auth must be a 16-byte authentication secret")

    return WebPushSubscription(
        endpoint=endpoint,
        p256dh=_b64url_encode(p256dh_bytes),
        auth=_b64url_encode(auth_bytes),
    )


def _load_private_key(pem: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid VAPID private key: {exc}") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("VAPID private key is not an EC key")
    if not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("VAPID private key is not on P-256")
    return key


def _public_key_of(private_key: ec.EllipticCurvePrivateKey) -> str:
    """Return *private_key*'s public point as base64url, the ``k=`` value."""
    return _b64url_encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )


def _origin(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """Decode unpadded base64url, rejecting characters outside the alphabet.

    ``urlsafe_b64decode`` ignores unknown characters by default, so a browser
    payload with a stray character would silently decode to a shorter value
    instead of being rejected at subscribe time.
    """
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("value is not valid base64url") from exc


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* to *path* atomically, in the style of ``StateManager``.

    This duplicates the helper in ``local_tracker`` on purpose: that module
    imports this one, so importing it back would be a cycle.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(payload, indent=2, ensure_ascii=False)

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        dir=str(path.parent),
        delete=False,
    )
    try:
        tmp.write(json_text)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()

    os.replace(tmp.name, str(path))
