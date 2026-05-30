"""Linear backend adapter for the Tracker protocol.

Wraps ``LinearClient`` so that the orchestrator can operate against the
generic ``Tracker`` interface without importing Linear-specific types.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from symphony_linear.config import _LinearConfig
from symphony_linear.linear import Comment, Issue, LinearClient
from symphony_linear.provisioning import provision_trigger_label
from symphony_linear.state import StateManager
from symphony_linear.tracker import (
    AttachmentDownloadError,
    AttachmentTooLargeError,
    TrackerError,
    TransitionTarget,
    normalise_content_type,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth class for attachment downloads
# ---------------------------------------------------------------------------

_LINEAR_UPLOAD_HOSTS: frozenset[str] = frozenset(
    {"uploads.linear.app", "public.linear.app"}
)


class _LinearAuth(httpx.Auth):
    """Attach the Linear API key only to HTTPS requests whose host is in
    the Linear upload-URL allowlist.

    ``auth_flow`` is called **once** per logical request by httpx, *not*
    on every redirect hop.  Cross-origin redirect stripping of the
    ``Authorization`` header is handled by httpx itself (verified with
    httpx ≥ 0.20 — see ``_redirect_headers`` in httpx's ``_client.py``).
    This class provides defence-in-depth for the initial request.
    """

    requires_request_body = False

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def auth_flow(self, request: httpx.Request):
        if request.url.scheme == "https" and request.url.host in _LINEAR_UPLOAD_HOSTS:
            request.headers["Authorization"] = self._api_key
        yield request


# ---------------------------------------------------------------------------
# Helpers carried over from orchestrator (duplicated here to avoid
# touching orchestrator.py until the follow-up migration ticket).
# ---------------------------------------------------------------------------


def _maybe_rewrite_to_ssh(url: str) -> str:
    """Rewrite GitHub browser-style HTTPS URLs to their SSH equivalents.

    Passes through unchanged: HTTPS URLs ending in .git, SSH URLs
    (``git@...``, ``ssh://...``), non-GitHub URLs, and local paths.
    """
    parsed = urlparse(url)

    # Must be HTTPS (case-insensitive).
    if parsed.scheme.lower() != "https":
        return url

    # Must be github.com (case-insensitive), no userinfo, no custom port.
    if parsed.hostname is None or parsed.hostname.lower() != "github.com":
        return url
    if parsed.username is not None or parsed.password is not None:
        return url
    try:
        if parsed.port is not None:
            return url
    except ValueError:
        return url  # malformed/non-numeric/out-of-range port — pass through

    # Path: strip trailing slash, then split into segments.
    # Pass through .git URLs and paths that are not exactly <owner>/<repo>.
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        return url

    parts = path.lstrip("/").split("/")
    if len(parts) != 2:
        return url

    owner, repo = parts
    if not owner or not repo:
        return url

    return f"git@github.com:{owner}/{repo}.git"


# ---------------------------------------------------------------------------
# LinearTracker
# ---------------------------------------------------------------------------


class LinearTracker:
    """Issue-tracker adapter that delegates to the Linear GraphQL client.

    Construct with a ready-to-use ``LinearClient`` and the Linear subsection
    of the app config.  The config supplies the trigger label and state names
    that ``list_triggered_issues``, ``is_still_triggered``, and ``transition_to``
    compose internally, so the orchestrator never touches those names.
    """

    def __init__(self, linear: LinearClient, config: _LinearConfig) -> None:
        self._linear = linear
        self._config = config

    # ------------------------------------------------------------------
    # Tracker protocol methods
    # ------------------------------------------------------------------

    def list_triggered_issues(self) -> list[Issue]:
        active_states = [
            self._config.in_progress_state,
            self._config.needs_input_state,
        ]
        if self._config.qa_state is not None:
            active_states.append(self._config.qa_state)
        return self._linear.list_triggered_issues(
            label=self._config.trigger_label,
            active_states=active_states,
        )

    def get_issue(self, id: str) -> Issue:
        return self._linear.get_issue(id)

    def list_comments_since(self, id: str, last_seen: str | None) -> list[Comment]:
        return self._linear.list_comments_since(id, last_seen)

    def post_comment(self, id: str, body: str, kind: str) -> Comment:
        return self._linear.post_comment(id, body + f"\n\n*Symphony · {kind}*")

    def edit_comment(self, id: str, body: str, kind: str) -> None:
        self._linear.edit_comment(id, body + f"\n\n*Symphony · {kind}*")

    def transition_to(self, id: str, target: TransitionTarget) -> None:
        state_map: dict[TransitionTarget, str | None] = {
            TransitionTarget.in_progress: self._config.in_progress_state,
            TransitionTarget.needs_input: self._config.needs_input_state,
            TransitionTarget.qa: self._config.qa_state,
        }
        state_name = state_map.get(target)
        if state_name is None:
            raise ValueError(
                f"No state mapping configured for transition target '{target.value}'"
            )
        self._linear.transition_to_state(id, state_name)

    def is_still_triggered(self, issue: Issue) -> bool:
        active_states = {
            self._config.in_progress_state,
            self._config.needs_input_state,
        }
        if self._config.qa_state is not None:
            active_states.add(self._config.qa_state)
        return (
            self._config.trigger_label in issue.labels
            and issue.state in active_states
            and issue.archived_at is None
        )

    def repo_url_for(self, issue: Issue) -> str:
        if issue.project is None or not issue.project.id:
            raise TrackerError("No project linked to this ticket.")
        project = self._linear.get_project(issue.project.id)
        for link in project.links:
            if link.label.strip().lower() == "repo":
                return _maybe_rewrite_to_ssh(link.url)
        raise TrackerError(
            "No `Repo` link found on the project. Add one and re-trigger."
        )

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def download_attachment(self, url: str) -> tuple[bytes, str | None]:
        """Download an attachment using the Linear API key for auth.

        Auth is only sent to known Linear upload hosts on the initial
        request.  Cross-origin redirect stripping is handled by httpx
        itself; :class:`_LinearAuth` provides defence-in-depth.
        """
        _MAX_BYTES = 10 * 1024 * 1024  # 10 MB

        # --- SSRF defence: reject non-allowlisted hosts before any I/O ---
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in _LINEAR_UPLOAD_HOSTS:
            raise AttachmentDownloadError("host not on allowlist")

        try:
            with httpx.stream(
                "GET",
                url,
                auth=_LinearAuth(self._config.api_key),
                follow_redirects=True,
                timeout=30.0,
            ) as response:
                if response.status_code >= 400:
                    raise AttachmentDownloadError(
                        f"HTTP {response.status_code} downloading {url}"
                    )

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > _MAX_BYTES:
                            raise AttachmentTooLargeError(
                                f"Attachment at {url} is "
                                f"{int(content_length)} bytes (limit {_MAX_BYTES})"
                            )
                    except ValueError:
                        pass  # non-numeric content-length — proceed

                content_type = normalise_content_type(
                    response.headers.get("content-type")
                )

                data = bytearray()
                for chunk in response.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > _MAX_BYTES:
                        raise AttachmentTooLargeError(
                            f"Attachment at {url} exceeds {_MAX_BYTES} bytes"
                        )

                return bytes(data), content_type
        except (AttachmentDownloadError, AttachmentTooLargeError):
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AttachmentDownloadError(f"Download failed for {url}: {exc}") from exc

    # ------------------------------------------------------------------
    # QA helpers
    # ------------------------------------------------------------------

    def is_in_qa(self, issue: Issue) -> bool:
        if self._config.qa_state is None:
            return False
        return issue.state == self._config.qa_state

    @property
    def qa_enabled(self) -> bool:
        return self._config.qa_state is not None

    def transition_name_for(self, target: TransitionTarget) -> str:
        return _target_to_linear_state_name(target, self._config)

    def ensure_trigger_setup(self, state: StateManager) -> None:
        # Delegate to the existing provisioning logic so we don't duplicate
        # the race-tolerant find/create/retry flow.  Calls into provisioning.py
        # which uses the LinearClient directly.  This will be inlined or
        # restructured in the follow-up migration ticket if needed.
        provision_trigger_label(self._linear, state, self._config.trigger_label)

    def human_trigger_description(self) -> str:
        return f"remove the `{self._config.trigger_label}` label"


# ---------------------------------------------------------------------------
# Package-private helpers
# ---------------------------------------------------------------------------


def _target_to_linear_state_name(
    target: TransitionTarget,
    config: _LinearConfig,
) -> str:
    """Map a ``TransitionTarget`` to the configured Linear state name."""
    if target == TransitionTarget.in_progress:
        return config.in_progress_state
    if target == TransitionTarget.needs_input:
        return config.needs_input_state
    if target == TransitionTarget.qa:
        qa = config.qa_state
        if qa is None:
            raise ValueError("Cannot resolve QA state name: qa_state is not configured")
        return qa
    raise ValueError(f"Unknown transition target: {target}")
