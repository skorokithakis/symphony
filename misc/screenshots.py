#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright",
#   "symphony-linear",
# ]
#
# [tool.uv.sources]
# symphony-linear = { path = "..", editable = true }
# ///
"""Capture the README screenshots of the built-in web UI with fake data.

The script builds a throwaway workspace in a temporary directory, seeds it
with tickets, comments, repositories and daemon state, and serves it with the
real :class:`WebUIServer`.  It never builds an Orchestrator: a fake In Progress
ticket must not be able to start a clone or an agent turn.  The store is seeded
through the normal tracker methods, so bot comments get their real footer.

The capture needs Google Chrome installed because it launches Playwright with
``channel="chrome"``, which is deliberate: Playwright then never downloads a
browser.

The images land in ``docs/images/``.  Only the four PNGs are written to the
repository; everything else is deleted with the temporary workspace.

Run it from the repository root:

    ./misc/screenshots.py
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from playwright.sync_api import Browser, Page, sync_playwright

from symphony_linear.config import AppConfig
from symphony_linear.linear import Comment
from symphony_linear.local_tracker import (
    STATE_DONE,
    STATE_IN_PROGRESS,
    STATE_NEEDS_INPUT,
    STATE_QA,
    LocalTicketStore,
    LocalTracker,
)
from symphony_linear.state import StateManager, TicketState, TicketStatus
from symphony_linear.webpush import WebPushStore
from symphony_linear.webui.server import WebUIServer

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMAGES = _REPO_ROOT / "docs" / "images"

#: Fake credentials for the throwaway server.  They never leave this process.
_USERNAME = "demo"
_PASSWORD = "demo-symphony"

#: The ticket page under the camera.
_TICKET_ID = "SYM-12"

_SHOP_API = "git@github.com:acme/shop-api.git"
_STOREFRONT = "git@github.com:acme/storefront.git"
_DOCS = "git@github.com:acme/docs.git"

#: The model aliases the ticket page's Details panel and composer select show.
_MODELS: dict[str, str] = {
    "Strong": "anthropic/claude-opus-4-6",
    "Fast": "anthropic/claude-haiku-4-5",
}


@dataclass(frozen=True)
class _Timeline:
    """How old one ticket's timestamps should look, in the fake store.

    The store's public API stamps every write with the current time, which
    would collapse the whole fake history into one second.  These offsets are
    applied to the written JSON files afterwards, so the thread and the home
    list read like an installation that has been used for a few days.
    """

    created: timedelta
    state_changed: timedelta
    updated: timedelta
    comments: tuple[timedelta, ...] = ()


def _minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


def _hours(value: float) -> timedelta:
    return timedelta(hours=value)


def _days(value: float) -> timedelta:
    return timedelta(days=value)


#: The fake history, keyed by the ticket id the counter allocates.  Ids run
#: from 8 to 18 with no gaps: the four Done tickets fill the holes and give
#: the archive some depth.
_TIMELINES: dict[str, _Timeline] = {
    "SYM-8": _Timeline(_days(20), _days(3), _days(3)),
    "SYM-9": _Timeline(_days(3), _hours(5), _hours(5), (_hours(5),)),
    "SYM-10": _Timeline(_days(16), _days(2), _days(2)),
    "SYM-11": _Timeline(_days(2), _minutes(40), _minutes(40), (_hours(3),)),
    "SYM-12": _Timeline(
        _hours(3),
        _minutes(12),
        _minutes(12),
        (_minutes(170), _minutes(70), _minutes(12)),
    ),
    "SYM-13": _Timeline(_days(12), _days(9), _days(9)),
    "SYM-14": _Timeline(_hours(26), _minutes(38), _minutes(38), (_minutes(38),)),
    "SYM-15": _Timeline(_hours(6), _minutes(4), _minutes(4), (_hours(3),)),
    "SYM-16": _Timeline(_days(3), _minutes(21), _minutes(21), (_hours(2),)),
    "SYM-17": _Timeline(_minutes(32), _minutes(32), _minutes(32)),
    "SYM-18": _Timeline(_days(22), _days(14), _days(14)),
}


# ---------------------------------------------------------------------------
# Fake data
# ---------------------------------------------------------------------------

_RATE_LIMIT_DESCRIPTION = """\
Credential-stuffing attempts against `POST /login` have climbed sharply over \
the last week. The endpoint currently accepts unlimited attempts, so one \
address can try passwords as fast as it can open connections.

Please add a limit of five attempts per minute for each client address, with \
a clear 429 response when the limit is exceeded. Keep it in the application \
rather than nginx: the API runs behind the load balancer, so the proxy sees \
the balancer's address and cannot tell clients apart."""

_RATE_LIMIT_REPLY = """\
I added per-address rate limiting to `POST /login` in \
`shop_api/auth/limits.py`, using the Redis client the session code already \
opens.

**What I changed**

- `shop_api/auth/limits.py`: a `SlidingWindowLimiter` keyed by client address.
- `shop_api/auth/routes.py`: the login view checks the limiter before it \
verifies the password.
- `tests/test_limits.py`: tests for the limit, the window reset and the \
headers.

```python
limiter = SlidingWindowLimiter(redis, limit=5, window_seconds=60)
if not limiter.allow(request.remote_addr):
    return too_many_requests(retry_after=limiter.retry_after(request.remote_addr))
```

1. Should a blocked login return 429 with a `Retry-After` header, or should \
it lock the account for a cooling-off period?
2. Should the same limit apply to password reset requests, or does that need \
a different budget?"""

_RATE_LIMIT_ANSWER = """\
Return 429 with a Retry-After header. Make the limit configurable."""

_RATE_LIMIT_FOLLOW_UP = """\
## Rate limit

Done. The limit now comes from `SHOP_LOGIN_RATE_LIMIT` (default 5) and the \
window from `SHOP_LOGIN_RATE_WINDOW_SECONDS` (default 60); both are documented \
in `.env.example`.

- `shop_api/auth/limits.py`: reads the settings from the config object rather \
than module constants.
- `tests/test_limits.py`: added tests for a custom limit and window.

The response now carries the header:

```python
# Refuse before reading the password.
retry = limiter.retry_after(address)
if retry > 0:
    # Retry-After says when to retry.
    return retry_later(retry)
```

All 14 tests pass.

## Lockout

> [!QUESTION] Should repeated blocks lock the account anyway?
> - No, keep returning 429. (recommended)
> - Yes, lock it after three blocks.

## Password reset

> [!QUESTION] Should the same limit cover the password reset endpoint?
> - No, a separate budget. (recommended)
> - Yes, the same budget."""

_SUMMARY_RATE_LIMIT = (
    "Added a limit of 5 attempts per minute per address, with tests. Asks if a "
    "blocked login returns 429 or locks the account."
)
_SUMMARY_ROUNDING = (
    "Found and fixed the rounding order in the discount code. Wants you to "
    "confirm the expected total for the example order."
)
_SUMMARY_WEBP = (
    "Converted 212 images and updated the templates. Asks if the old JPEG "
    "files can be deleted."
)


def _started(
    store: LocalTicketStore,
    *,
    state: str,
    title: str,
    repo: str,
    description: str = "",
    model: str | None = None,
) -> str:
    """Create a started ticket in *state* and return its id."""
    ticket = store.create_started_draft(
        title=title,
        description=description,
        repo=repo,
        model=model,
    )
    if state != STATE_IN_PROGRESS:
        store.set_state(ticket.id, state)
    return ticket.id


def _agent_reply(
    tracker: LocalTracker,
    ticket_id: str,
    body: str,
    *,
    # The daemon's final-comment kind is "context: N tokens" with an optional
    # " · model: <id>", and the ticket header parses the count out of the first
    # segment.  A kind with a "final · " prefix would silently drop the token
    # label from the screenshots.
    kind: str = "context: 21,408 tokens",
    summary: str | None = None,
) -> Comment:
    """Post an agent reply through the normal path, footer included."""
    comment = tracker.post_comment(ticket_id, body, kind)
    if summary is not None:
        tracker.store.set_comment_summary(ticket_id, comment.id, summary)
    return comment


def _seed(workspace: Path) -> None:
    """Write the fake tickets, comments and repositories."""
    tickets_dir = workspace / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    # The store allocates ids from the counter, and the fake history needs
    # specific numbers so the seeded comments and the home list line up.
    (tickets_dir / "counter.json").write_text(json.dumps({"next_id": 8}))

    tracker = LocalTracker(workspace)
    store = tracker.store
    for url, name in (
        (_SHOP_API, "shop-api"),
        (_STOREFRONT, "storefront"),
        (_DOCS, "docs"),
    ):
        store.add_repo(url, name)

    _started(
        store,
        state=STATE_DONE,
        title="Add a health check endpoint",
        repo=_SHOP_API,
        description="A `/healthz` route for the load balancer.",
    )

    webp_id = _started(
        store,
        state=STATE_NEEDS_INPUT,
        title="Convert product images to WebP",
        repo=_STOREFRONT,
        description=(
            "The product gallery still ships the original 1.5 MB JPEGs. "
            "Convert them to WebP and use the new files in the templates."
        ),
    )
    comment = _agent_reply(
        tracker,
        webp_id,
        """\
Converted the 212 product images under `assets/products/` to WebP at quality \
82, keeping their dimensions. The gallery and product templates now point at \
the `.webp` files.

- `scripts/convert_images.py`: added a `--quality` flag and a dry-run mode.
- `templates/catalog/_gallery.html`: switched the image sources.
- `tests/test_catalog.py`: updated the fixture paths.

The average image is 38% smaller. Can I delete the old JPEG files, or do you \
want to keep them until the next release?""",
        kind="context: 18,220 tokens",
        summary=_SUMMARY_WEBP,
    )
    store.set_seen_comment(webp_id, comment.id)

    _started(
        store,
        state=STATE_DONE,
        title="Set up the staging deploy pipeline",
        repo=_STOREFRONT,
        description="Deploy every merge to master to staging.",
    )

    onboarding_id = _started(
        store,
        state=STATE_QA,
        title="Onboarding checklist on the dashboard",
        repo=_STOREFRONT,
        description=(
            "New accounts land on an empty dashboard and do not know where to "
            "start. Add a short checklist that disappears once each step is done."
        ),
    )
    _agent_reply(
        tracker,
        onboarding_id,
        """\
The checklist is on the dashboard and remembers a dismissed state per account.

- `dashboard/onboarding.py`: the four steps and their completion checks.
- `templates/dashboard.html`: the checklist panel.

All 9 tests pass. The "Invite a teammate" step still needs a real email \
address to complete, so I left it as a follow-up.""",
        kind="context: 16,905 tokens",
    )

    rate_limit_id = _started(
        store,
        state=STATE_NEEDS_INPUT,
        title="Add rate limiting to the login endpoint",
        repo=_SHOP_API,
        description=_RATE_LIMIT_DESCRIPTION,
        model="Strong",
    )
    _agent_reply(
        tracker,
        rate_limit_id,
        _RATE_LIMIT_REPLY,
        kind="context: 24,816 tokens · model: anthropic/claude-opus-4-6",
    )
    answer = store.add_human_comment(rate_limit_id, _RATE_LIMIT_ANSWER)
    _agent_reply(
        tracker,
        rate_limit_id,
        _RATE_LIMIT_FOLLOW_UP,
        kind="context: 31,402 tokens · model: anthropic/claude-opus-4-6",
        summary=_SUMMARY_RATE_LIMIT,
    )
    store.set_seen_comment(rate_limit_id, answer.id)
    store.set_reply_draft(rate_limit_id, "Yes, same limit for password reset.")

    _started(
        store,
        state=STATE_DONE,
        title="Move the docs build to GitHub Actions",
        repo=_DOCS,
        description="Stop building the docs on the shared runner.",
    )

    rounding_id = _started(
        store,
        state=STATE_NEEDS_INPUT,
        title="Checkout total is wrong when a coupon meets free shipping",
        repo=_STOREFRONT,
        description=(
            "Order 4471 shows £48.78 instead of the expected total when a 10% "
            "coupon is combined with free shipping. It looks like a rounding "
            "order problem in the discount calculation."
        ),
    )
    _agent_reply(
        tracker,
        rounding_id,
        """\
The rounding happened before the free-shipping credit was applied, so the \
coupon was calculated on a total that still included the shipping charge.

- `checkout/pricing.py`: apply the discount first, recalculate shipping, then \
round once.
- `tests/test_pricing.py`: added the order from the ticket as a regression test.

Can you confirm the expected total for the example order? I make it £48.78 \
after the fix.""",
        kind="context: 19,640 tokens",
        summary=_SUMMARY_ROUNDING,
    )

    dark_mode_id = _started(
        store,
        state=STATE_IN_PROGRESS,
        title="Dark mode for the order history page",
        repo=_STOREFRONT,
        description=(
            "The order history page is the last page that ignores the dark "
            "theme. Follow the existing tokens and check both themes."
        ),
    )
    _agent_reply(
        tracker,
        dark_mode_id,
        "**Workspace ready.** Cloned `acme/storefront` and switched to "
        "`symphony/SYM-15`.",
        kind="workspace",
    )

    upgrade_id = _started(
        store,
        state=STATE_IN_PROGRESS,
        title="Upgrade to Python 3.13 and fix the deprecation warnings",
        repo=_SHOP_API,
        description=(
            "Bump the runtime to 3.13, replace the deprecated `datetime.utcnow` "
            "calls and make the test suite pass without warnings."
        ),
    )
    _agent_reply(
        tracker,
        upgrade_id,
        "**Workspace ready.** Cloned `acme/shop-api` and switched to "
        "`symphony/SYM-16`.",
        kind="workspace",
    )

    store.create_draft(
        title="Export invoices as CSV",
        description=(
            "Finance wants a CSV export of the invoices for a date range, "
            "with one row per line item."
        ),
        repo=_SHOP_API,
        model="Fast",
    )

    _started(
        store,
        state=STATE_DONE,
        title="Add a sitemap to the help centre",
        repo=_DOCS,
        description="Generate a sitemap so search engines can find the articles.",
    )


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _apply_timelines(workspace: Path) -> None:
    """Rewrite the seeded timestamps to the offsets in ``_TIMELINES``.

    The store has no clock injection and its public writers deliberately stamp
    "now"; a realistic thread needs older comments and a home list ordered by
    activity, so the temp store's JSON files are adjusted in place.  Nothing
    here is a package write: the store is reloaded from these files afterwards.
    """
    now = datetime.now(timezone.utc)
    for ticket_id, timeline in _TIMELINES.items():
        path = workspace / "tickets" / f"{ticket_id}.json"
        payload = json.loads(path.read_text())
        payload["created_at"] = _iso(now - timeline.created)
        payload["state_changed_at"] = _iso(now - timeline.state_changed)
        payload["updated_at"] = _iso(now - timeline.updated)
        comments = payload.get("comments", [])
        if len(comments) != len(timeline.comments):
            raise RuntimeError(
                f"{ticket_id} has {len(comments)} comments, "
                f"the timeline expects {len(timeline.comments)}"
            )
        for comment, age in zip(comments, timeline.comments):
            comment["created_at"] = _iso(now - age)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def _config() -> AppConfig:
    """The fake installation's configuration.

    The two API keys are dummy values that only switch UI features on: the
    microphone button (Soniox) and automatic titles plus reply summaries
    (Anthropic).  Neither is contacted while a page renders.  The summaries the
    home page shows are seeded as if a previous reply had already been
    summarised.
    """
    return AppConfig.model_validate(
        {
            "webui": {
                "port": 8765,
                "host": "127.0.0.1",
                "password": _PASSWORD,
                "soniox_api_key": "demo-soniox-key",
                "anthropic_api_key": "demo-anthropic-key",
            },
            "models": _MODELS,
        }
    )


def _build_server(workspace: Path) -> WebUIServer:
    store = LocalTicketStore(workspace / "tickets")
    ticket = store.get(_TICKET_ID)
    if ticket is None:
        raise RuntimeError(f"The fake store is missing {_TICKET_ID}")
    last_comment = ticket.comments[-1].id if ticket.comments else None

    state = StateManager(workspace / "state.json")
    state.load()
    state.upsert(
        TicketState(
            ticket_id=_TICKET_ID,
            ticket_identifier=_TICKET_ID,
            repo_url=_SHOP_API,
            workspace_path=f"/srv/symphony/{_TICKET_ID}/repo",
            branch=f"symphony/{_TICKET_ID.lower()}",
            session_id="ses_01j9x2k4m6q8r0t2v4w6",
            last_seen_comment_id=last_comment,
            status=TicketStatus.needs_input,
        )
    )
    state.save()

    return WebUIServer(
        store=store,
        state=state,
        config=_config(),
        on_wake=lambda: None,
        port=0,
        webpush=WebPushStore(workspace),
    )


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _settle(page: Page) -> None:
    """Wait for fonts and the deferred scripts that format times and reveal controls."""
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.wait_for_timeout(300)


def _scroll_ticket(page: Page, *, align_human: bool) -> None:
    """Frame the ticket page on its last human reply or on the end of the thread.

    The desktop layout has room to show the human reply, the agent's follow-up
    and the composer together, and anchoring the human reply keeps the clipped
    earlier messages out of the picture.  The phone is shorter, so it scrolls
    to the end: the latest agent reply then sits just above the composer.

    A small offset after ``scrollIntoView`` supplies the top margin the thread
    padding would otherwise lose, without pulling the previous entry back into
    view.
    """
    page.evaluate(
        """alignHuman => {
          const main = document.getElementById('ticket');
          const style = window.getComputedStyle(main);
          const scroller =
            style.overflowY === 'auto' || style.overflowY === 'scroll'
              ? main
              : document.scrollingElement || document.documentElement;
          if (alignHuman) {
            const users = document.querySelectorAll('.entry--user');
            users[users.length - 1].scrollIntoView({ block: 'start' });
            scroller.scrollTop = Math.max(0, scroller.scrollTop - 12);
          } else {
            scroller.scrollTop = scroller.scrollHeight;
          }
        }""",
        align_human,
    )
    if not align_human:
        # The phone layout reveals a fixed compact header on a scroll up.  A
        # late layout change (the composer settling after fonts load) can clamp
        # the page upward and look like one, leaving that bar over the agent's
        # header.  A two-pixel up-then-down scroll is below the reveal
        # threshold but makes the page's own handler hide the bar again.
        page.wait_for_timeout(100)
        page.evaluate("window.scrollTo(0, window.scrollY - 2)")
        page.wait_for_timeout(50)
        page.evaluate("window.scrollTo(0, window.scrollY + 2)")
    page.wait_for_timeout(250)


def _capture(
    browser: Browser,
    url: str,
    path: Path,
    *,
    width: int,
    height: int,
    color_scheme: Literal["light", "dark"],
    align_human: bool | None = None,
) -> None:
    context = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=2,
        color_scheme=color_scheme,
        http_credentials={"username": _USERNAME, "password": _PASSWORD},
    )
    try:
        page = context.new_page()
        page.goto(url, wait_until="load")
        _settle(page)
        if align_human is not None:
            _scroll_ticket(page, align_human=align_human)
        page.screenshot(path=str(path), type="png", animations="disabled")
    finally:
        context.close()


def main() -> None:
    _IMAGES.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="symphony-screenshots-") as tmp:
        workspace = Path(tmp)
        _seed(workspace)
        _apply_timelines(workspace)

        server = _build_server(workspace)
        server.start()
        try:
            port = server.bound_port
            if port is None:
                raise RuntimeError("The web UI server did not bind a port")
            base = f"http://127.0.0.1:{port}"
            ticket_url = f"{base}/tickets/{_TICKET_ID}"

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    channel="chrome",
                    # Chrome cannot start its own sandbox inside bwrap.  The
                    # flag is only for this throwaway capture process, never
                    # for the daemon's own sandboxing.
                    args=["--no-sandbox"],
                )
                try:
                    _capture(
                        browser,
                        base + "/",
                        _IMAGES / "home-desktop.png",
                        width=1280,
                        height=800,
                        color_scheme="light",
                    )
                    _capture(
                        browser,
                        base + "/",
                        _IMAGES / "home-mobile.png",
                        width=390,
                        # Taller than the 844 px phone viewport the ticket
                        # suggests: at 844 the sticky dock hides the QA row,
                        # leaving a group heading with no visible ticket.
                        height=900,
                        color_scheme="dark",
                    )
                    _capture(
                        browser,
                        ticket_url,
                        _IMAGES / "ticket-desktop.png",
                        width=1280,
                        height=800,
                        color_scheme="light",
                        align_human=True,
                    )
                    _capture(
                        browser,
                        ticket_url,
                        _IMAGES / "ticket-mobile.png",
                        width=390,
                        height=844,
                        color_scheme="dark",
                        align_human=False,
                    )
                finally:
                    browser.close()
        finally:
            server.stop()


if __name__ == "__main__":
    main()
