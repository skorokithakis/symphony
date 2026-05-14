"""Tracker-neutral protocol, errors, and enums.

This module defines the seam that orchestrator.py can depend on without
knowing whether it is talking to Linear, GitHub, or another issue tracker.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from symphony_linear.linear import Comment, Issue
    from symphony_linear.state import StateManager


# ---------------------------------------------------------------------------
# Tracker-neutral exception hierarchy
# ---------------------------------------------------------------------------


class TrackerError(Exception):
    """Base exception for all tracker API errors.

    Every backend-specific error class inherits from this (and possibly
    a more specific subclass below) so that callers can handle all tracker
    errors uniformly when the backend doesn't matter.
    """


class TrackerAuthError(TrackerError):
    """Authentication / authorisation failed (HTTP 401/403 or equivalent)."""


class TrackerRateLimitError(TrackerError):
    """The tracker API returned a rate-limit response (HTTP 429 or equivalent)."""


class TrackerTransientError(TrackerError):
    """Transient server or network error (HTTP 5xx, timeouts, connection errors)."""


class TrackerNotFoundError(TrackerError):
    """A requested resource (issue, project, comment, label) was not found."""


# ---------------------------------------------------------------------------
# Transition target enum
# ---------------------------------------------------------------------------


class TransitionTarget(str, Enum):
    """Workflow states the orchestrator can request a ticket to move to.

    Values are deliberately generic so that every backend can map them to
    its own workflow state names.
    """

    in_progress = "in_progress"
    needs_input = "needs_input"
    qa = "qa"


# ---------------------------------------------------------------------------
# Tracker protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Tracker(Protocol):
    """Interface that any issue-tracker backend must satisfy.

    The orchestrator only references this protocol; it never imports
    backend-specific types such as ``LinearClient``.
    """

    def current_user_id(self) -> str:
        """Return the tracker user id of the authenticated principal.

        This is the bot's own id — used to filter out its own comments
        when looking for new *human* comments.
        """
        ...

    def list_triggered_issues(self) -> list[Issue]:
        """Return all currently triggered issues.

        "Triggered" means an issue that carries the trigger label AND is
        in one of the active states (``in_progress``, ``needs_input``,
        ``qa``).  The tracker backend owns the definition of "triggered"
        internally; the orchestrator never passes in label or state names.
        """
        ...

    def get_issue(self, id: str) -> Issue:
        """Return a single issue by its tracker-native id.

        Must include description, state, labels, project, comments, and
        archive status.  Raises ``TrackerNotFoundError`` if the issue
        does not exist.
        """
        ...

    def list_comments_since(self, id: str, last_seen: str | None) -> list[Comment]:
        """Return comments on *id* posted after *last_seen* (chronological).

        *last_seen* is a comment id.  Comments are returned oldest-first.
        When *last_seen* is ``None``, all comments are returned.
        """
        ...

    def post_comment(self, id: str, body: str) -> Comment:
        """Post a new comment on issue *id* with the given Markdown *body*.

        Returns the created comment.
        """
        ...

    def edit_comment(self, id: str, body: str) -> None:
        """Replace the body of an existing comment."""
        ...

    def transition_to(self, id: str, target: TransitionTarget) -> None:
        """Move an issue to the workflow state represented by *target*.

        Raises ``ValueError`` if the backend does not have a mapping for
        the given target (e.g. ``TransitionTarget.qa`` when no QA state
        is configured).
        """
        ...

    def is_still_triggered(self, issue: Issue) -> bool:
        """Return ``True`` if *issue* should remain tracked by the daemon.

        An issue that is no longer triggered (label removed, state changed,
        or archived) gets cleaned up on the next poll tick.
        """
        ...

    def repo_url_for(self, issue: Issue) -> str:
        """Return the clone URL for the repository linked to *issue*.

        Raises ``TrackerError`` with a user-facing message when no
        repository can be determined (e.g. the issue has no project,
        the project has no repo link, or the issue has no associated
        repo in the tracker).
        """
        ...

    def is_in_qa(self, issue: Issue) -> bool:
        """Return ``True`` when QA is enabled and *issue* is in the QA state.

        Evaluates using the tracker's configured QA state name, so the
        caller never touches backend-specific config.
        """
        ...

    @property
    def qa_enabled(self) -> bool:
        """Return ``True`` when a QA state is configured for this tracker."""
        ...

    def transition_name_for(self, target: TransitionTarget) -> str:
        """Return the tracker-specific human-readable name for *target*.

        Example: ``TransitionTarget.needs_input`` → ``"Needs Input"``.
        Used for user-facing comments that mention workflow states.
        """
        ...

    def ensure_trigger_setup(self, state: StateManager) -> None:
        """Idempotently ensure the trigger label exists in the tracker.

        Called once on daemon startup.  Must not raise on transient
        failures — the daemon must tolerate missing labels.
        """
        ...

    def human_trigger_description(self) -> str:
        """Return a short user-facing phrase describing how to un-trigger.

        Example: ``"remove the `Agent` label"``.  Used in recovery
        messages so that humans know how to stop the bot.
        """
        ...
