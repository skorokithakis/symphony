"""Linear backend adapter for the Tracker protocol.

Wraps ``LinearClient`` so that the orchestrator can operate against the
generic ``Tracker`` interface without importing Linear-specific types.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from symphony_linear.config import _LinearConfig
from symphony_linear.linear import Comment, Issue, LinearClient
from symphony_linear.provisioning import provision_trigger_label
from symphony_linear.state import StateManager
from symphony_linear.tracker import (
    TrackerError,
    TransitionTarget,
)

logger = logging.getLogger(__name__)


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

    def current_user_id(self) -> str:
        return self._linear.current_user_id()

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

    def post_comment(self, id: str, body: str) -> Comment:
        return self._linear.post_comment(id, body)

    def edit_comment(self, id: str, body: str) -> None:
        self._linear.edit_comment(id, body)

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
