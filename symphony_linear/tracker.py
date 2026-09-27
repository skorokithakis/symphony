"""Tracker-neutral protocol, errors, and enums.

This module defines the seam that orchestrator.py can depend on without
knowing whether it is talking to Linear, GitHub, or another issue tracker.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:
    from symphony_linear.linear import Comment, Issue
    from symphony_linear.state import StateManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bot comment detection
# ---------------------------------------------------------------------------

_BOT_MARKER = "*Symphony · "


def is_bot_comment(body: str) -> bool:
    """Return ``True`` if *body* contains the Symphony footer marker."""
    return _BOT_MARKER in body


# ---------------------------------------------------------------------------
# Per-issue model override resolution
# ---------------------------------------------------------------------------

_MODEL_LABEL_PREFIX = "model:"


def model_label_name(alias: str) -> str:
    """Return the workspace label name for a model *alias*: ``Model: <alias>``.

    Built from ``_MODEL_LABEL_PREFIX`` so the provisioning side and
    ``model_from_labels`` can never drift apart.
    """
    return f"{_MODEL_LABEL_PREFIX.capitalize()} {alias}"


def model_from_labels(
    labels: list[str],
    aliases: Mapping[str, str],
    source: str = "ticket",
) -> str | None:
    """Resolve a per-issue model override from ``Model: <value>`` labels.

    A label whose stripped name starts with ``model:`` (case-insensitive)
    names the model for the ticket's primary-agent turns.  The remainder of
    the label is the value: it is looked up in *aliases*
    case-insensitively (mapping short names to full ``provider/model`` ids),
    and returned verbatim on a miss so a raw provider/model id works with
    no config entry.  Empty values are ignored (with a warning).  When
    several labels match, the first in sorted order with a non-empty
    value wins (with a warning).

    Returns ``None`` when no label matches.  Config-agnostic: *aliases* is
    a plain mapping, not an ``AppConfig``.

    *source* names the object the labels were read off — ``"ticket"`` or
    ``"project"`` — and appears in both warnings so an operator knows
    which one in Linear to go and fix.
    """
    matches: list[str] = []
    for label in labels:
        stripped = label.strip()
        if stripped.lower().startswith(_MODEL_LABEL_PREFIX):
            matches.append(stripped)

    if not matches:
        return None

    matches.sort()
    lowered_aliases = {name.lower(): model_id for name, model_id in aliases.items()}
    for candidate in matches:
        value = candidate[len(_MODEL_LABEL_PREFIX) :].strip()
        if not value:
            logger.warning(
                "Ignoring Model: label %r on %s — empty value", candidate, source
            )
            continue
        if len(matches) > 1:
            logger.warning(
                "Multiple Model: labels on %s (%s) — using %r",
                source,
                ", ".join(matches),
                candidate,
            )
        return lowered_aliases.get(value.lower(), value)

    return None


def model_for_issue(issue: Issue, aliases: Mapping[str, str]) -> str | None:
    """Resolve the effective model override for *issue*, issue tier first.

    A ``Model: <value>`` label on the issue itself wins.  Failing that, the
    same labels are read off the issue's project, so a project-wide label
    acts as a default for every issue in it.  Returns ``None`` when neither
    tier carries one (the agent's own default applies).  Both tiers go
    through ``model_from_labels``, so alias lookup, verbatim pass-through
    and the empty/multiple-label handling behave identically either way;
    only the warning text differs, naming the tier ("ticket" or
    "project") that carries the offending label.

    ``issue.project`` is ``None`` on trackers without a project concept
    (and on issues that simply have no project), which leaves only the
    issue tier.
    """
    from_issue = model_from_labels(issue.labels, aliases)
    if from_issue is not None:
        return from_issue
    if issue.project is None:
        return None
    return model_from_labels(issue.project.labels, aliases, source="project")


def normalise_content_type(value: str | None) -> str | None:
    """Return the lowercased media type from *value*, stripping parameters.

    ``"text/plain; charset=utf-8"`` → ``"text/plain"``.
    Returns ``None`` when *value* is falsy or cannot be parsed.
    """
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


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


class AttachmentDownloadError(TrackerError):
    """Failed to download an attachment (HTTP error, network error, etc.)."""


class AttachmentTooLargeError(AttachmentDownloadError):
    """The attachment exceeds the configured size limit (10 MB)."""


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

    def post_comment(self, id: str, body: str, kind: str) -> Comment:
        """Post a new comment on issue *id* with the given Markdown *body*.

        *kind* is a user-facing label (e.g. ``"update"``, ``"serve"``)
        that is composed into a visible footer appended to the comment.
        Returns the created comment.
        """
        ...

    def edit_comment(self, id: str, body: str, kind: str) -> None:
        """Replace the body of an existing comment.

        *kind* is a user-facing label composed into the visible footer.
        """
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

    def download_attachment(self, url: str) -> tuple[bytes, str | None]:
        """Download an attachment from *url* using tracker credentials.

        Returns ``(content_bytes, content_type_or_None)`` where
        *content_type* is the lowercased media type without parameters.

        Raises ``AttachmentDownloadError`` on HTTP / network errors and
        ``AttachmentTooLargeError`` when the response body exceeds 10 MB.
        """
        ...

    def is_upload_url(self, url: str) -> bool:
        """Return ``True`` if *url* points at an uploaded-file host.

        Per-backend because what counts as a "downloadable upload" differs:
        Linear serves uploads from dedicated hosts, while GitHub's download
        allowlist also contains all of ``github.com`` — ordinary repo and
        PR links must not be downloaded.

        Used by attachment extraction to decide whether a plain Markdown
        link or a bare URL is an uploaded file worth fetching.
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

    def ensure_trigger_setup(
        self, state: StateManager, model_labels: list[str]
    ) -> None:
        """Idempotently ensure the trigger label exists in the tracker.

        Called once on daemon startup.  Must not raise on transient
        failures — the daemon must tolerate missing labels.

        *model_labels* are the workspace label names for the configured
        model aliases (see ``model_label_name``), to be provisioned
        alongside the trigger label.  Backends that cannot provision them
        (e.g. no workspace-wide label concept) may ignore them.
        """
        ...

    def human_trigger_description(self) -> str:
        """Return a short user-facing phrase describing how to un-trigger.

        Example: ``"remove the `Agent` label"``.  Used in recovery
        messages so that humans know how to stop the bot.
        """
        ...
