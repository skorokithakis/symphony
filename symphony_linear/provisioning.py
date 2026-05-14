"""Trigger-label provisioning on daemon startup.

Ensures the configured ``linear.trigger_label`` exists as a workspace-wide
label in Linear, creating it if necessary.
"""

from __future__ import annotations

import logging

from symphony_linear.linear import LinearClient
from symphony_linear.state import StateManager

logger = logging.getLogger(__name__)


def provision_trigger_label(
    linear: LinearClient,
    state: StateManager,
    trigger_label: str,
) -> None:
    """Ensure *trigger_label* exists as a workspace-wide label in Linear.

    Compares against the last provisioned label name in state to avoid
    redundant API calls.  On any failure, logs a warning and continues
    startup – the daemon must not crash because of a label provisioning
    error.
    """
    # Already provisioned for this exact label name – nothing to do.
    if state.provisioned_label_name == trigger_label:
        return

    # Try to find an existing workspace-wide label with this name.
    try:
        label_id = linear.find_workspace_label(trigger_label)
    except Exception as exc:
        _warn(trigger_label, exc)
        return

    if label_id is None:
        # Label doesn't exist yet – create it.
        try:
            label_id = linear.create_workspace_label(trigger_label)
        except Exception as exc:
            # Race: another caller may have created the label between our
            # find and create.  Try one more lookup.
            try:
                label_id = linear.find_workspace_label(trigger_label)
            except Exception as lookup_exc:
                _warn(trigger_label, lookup_exc)
                return
            if label_id is None:
                _warn(trigger_label, exc)
                return

    # Success – record that this label name has been provisioned.
    try:
        state.set_provisioned_label_name(trigger_label)
    except Exception as exc:
        _warn(trigger_label, exc)
        return

    logger.info("Trigger label '%s' is provisioned.", trigger_label)


def _warn(trigger_label: str, exc: Exception) -> None:
    """Log an actionable warning when provisioning fails."""
    logger.warning(
        "Failed to auto-provision Linear label '%s': %s. "
        "Create it manually in Linear if you want auto-trigger to work.",
        trigger_label,
        exc,
    )
