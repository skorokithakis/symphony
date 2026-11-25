"""The browser half of Web Push: the service worker and the subscription route.

The worker is served from the root path, because a worker only controls the
pages at or below its own URL and its notification click handler opens
``/tickets/<id>``.  Serving it from ``/static/``, where the pages' other
scripts live, would give it ``/static/`` scope and no control over the ticket
page.  The script's bytes still live beside the other static files, so a
packaging change cannot forget it.

Both views are ordinary routes and go through the server's single dispatch
choke point: Basic auth, the same-origin check on the POST, the body cap and
the security headers all apply.  Web Push is always on for this backend, so
neither route is gated on config; ``webpush`` is None only for a server built
without a tracker (tests), and then the subscribe route answers 404 exactly
like an unknown path.
"""

from __future__ import annotations

from symphony_linear.local_tracker import NTFY_ICON_URL
from symphony_linear.webui.api import _body, _error, _json
from symphony_linear.webui.http_types import Request, Response

#: The page policy's image directive.  The worker's policy is that same string
#: with the notification icon's origin appended, so no other directive and no
#: other response changes.
_IMG_SRC_SELF = "img-src 'self';"


def _service_worker_csp(base: str) -> str:
    """Return the server's policy with the notification icon's origin allowed.

    The worker shows the notification, and its icon is the public GitHub copy
    (``NTFY_ICON_URL``) because the notification service fetches it without
    Basic credentials.  The page policy allows images from ``'self'`` only, so
    the worker would block its own icon.  Appending to the existing directive
    rather than restating the policy keeps every other directive, and every
    other response, byte for byte identical.
    """
    return base.replace(_IMG_SRC_SELF, f"img-src 'self' {NTFY_ICON_URL};", 1)


def serve_service_worker(request: Request) -> Response:
    """Return the push service worker, with a type workers accept.

    ``Cache-Control: no-cache`` rather than the long-lived header the rest of
    ``/static/`` gets: a worker is re-checked on every page load, and a proxy
    in front of the daemon must not pin an old one.
    """
    script = (request.app.static_root / "sw.js").read_bytes()
    return Response(
        body=script,
        content_type="text/javascript; charset=utf-8",
        headers=(("Cache-Control", "no-cache"),),
        csp=_service_worker_csp(request.app.csp),
    )


def api_push_subscription(request: Request) -> Response:
    """Store the ``PushSubscription.toJSON()`` payload the bell posts.

    The browser sends an object with ``endpoint``, ``expirationTime`` and a
    ``keys`` object holding ``p256dh`` and ``auth``.  ``expirationTime`` is
    ignored: the push service enforces it, and the store needs only the
    endpoint and the two keys.  The store is the same instance the tracker
    sends from, so the subscription is live the moment this view returns.
    """
    store = request.app.webpush
    if store is None:
        return Response.text("Not Found\n", status=404)

    payload = _body(request)
    if payload is None:
        return _error("Send the subscription as a JSON object.", status=400)
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        return _error("The subscription has no keys.", status=400)
    try:
        store.add_subscription(
            payload.get("endpoint"), keys.get("p256dh"), keys.get("auth")
        )
    except ValueError as exc:
        # The store's message names the offending field and never echoes its
        # value: an endpoint is a bearer capability.
        return _error(str(exc), status=400)
    return _json({"ok": True}, status=201)
