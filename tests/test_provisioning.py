"""Tests for trigger-label provisioning logic."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock


from symphony_linear.linear import LinearError
from symphony_linear.provisioning import provision_trigger_label
from symphony_linear.state import StateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_linear(
    find_id: str | None = None,
    find_error: Exception | None = None,
    create_id: str | None = None,
    create_error: Exception | None = None,
) -> MagicMock:
    """Build a mock LinearClient with configurable find/create behaviour."""
    linear = MagicMock()
    if find_error:
        linear.find_workspace_label.side_effect = find_error
    else:
        linear.find_workspace_label.return_value = find_id
    if create_error:
        linear.create_workspace_label.side_effect = create_error
    else:
        linear.create_workspace_label.return_value = create_id
    return linear


def _state(tmp_path: Path) -> StateManager:
    """Build a fresh StateManager backed by a temp file."""
    mgr = StateManager(tmp_path / "state.json")
    mgr.load()
    return mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAlreadyProvisioned:
    def test_state_matches_config_no_api_calls(self, tmp_path: Path) -> None:
        """When state already holds the same label name, skip API calls entirely."""
        state = _state(tmp_path)
        state.set_provisioned_label_name("Agent")

        linear = _fake_linear()

        provision_trigger_label(linear, state, "Agent")

        linear.find_workspace_label.assert_not_called()
        linear.create_workspace_label.assert_not_called()
        assert state.provisioned_label_name == "Agent"


class TestFreshStateLabelDoesNotExist:
    def test_creates_and_updates_state(self, tmp_path: Path) -> None:
        """Fresh state, label doesn't exist – creates it, state updated."""
        state = _state(tmp_path)
        assert state.provisioned_label_name is None

        linear = _fake_linear(find_id=None, create_id="lbl-agent")

        provision_trigger_label(linear, state, "Agent")

        linear.find_workspace_label.assert_called_once_with("Agent")
        linear.create_workspace_label.assert_called_once_with("Agent")
        assert state.provisioned_label_name == "Agent"


class TestFreshStateLabelAlreadyExists:
    def test_skips_create_still_updates_state(self, tmp_path: Path) -> None:
        """Fresh state, label already exists – skips create, state still updated."""
        state = _state(tmp_path)
        assert state.provisioned_label_name is None

        linear = _fake_linear(find_id="lbl-existing")

        provision_trigger_label(linear, state, "Agent")

        linear.find_workspace_label.assert_called_once_with("Agent")
        linear.create_workspace_label.assert_not_called()
        assert state.provisioned_label_name == "Agent"


class TestConfigNameChanged:
    def test_reprovisions_when_name_changed(self, tmp_path: Path) -> None:
        """State has 'old_label', config now has 'new_label' – reprovisions."""
        state = _state(tmp_path)
        state.set_provisioned_label_name("old_label")

        linear = _fake_linear(find_id=None, create_id="lbl-new")

        provision_trigger_label(linear, state, "new_label")

        linear.find_workspace_label.assert_called_once_with("new_label")
        linear.create_workspace_label.assert_called_once_with("new_label")
        assert state.provisioned_label_name == "new_label"


class TestApiFailures:
    def test_find_error_logs_warning_no_state_change(
        self, tmp_path: Path, caplog
    ) -> None:
        """Find raises – warning logged, state unchanged, no exception."""
        state = _state(tmp_path)
        assert state.provisioned_label_name is None

        linear = _fake_linear(find_error=LinearError("Network down"))

        with caplog.at_level(logging.WARNING):
            provision_trigger_label(linear, state, "Agent")

        assert "Failed to auto-provision Linear label 'Agent'" in caplog.text
        assert "Network down" in caplog.text
        linear.create_workspace_label.assert_not_called()
        assert state.provisioned_label_name is None

    def test_create_error_no_race_logs_warning_no_state_change(
        self, tmp_path: Path, caplog
    ) -> None:
        """Create fails and retry-find also returns None – warning, state unchanged."""
        state = _state(tmp_path)
        assert state.provisioned_label_name is None

        # find returns None initially; create raises; retry-find returns None
        linear = _fake_linear(
            find_id=None,
            create_error=LinearError("Permission denied"),
        )

        with caplog.at_level(logging.WARNING):
            provision_trigger_label(linear, state, "Agent")

        assert "Failed to auto-provision Linear label 'Agent'" in caplog.text
        assert "Permission denied" in caplog.text
        assert state.provisioned_label_name is None

    def test_create_error_with_race_resolved_succeeds(self, tmp_path: Path) -> None:
        """Create fails (race), retry-find succeeds – state updated."""
        state = _state(tmp_path)
        assert state.provisioned_label_name is None

        # find returns None initially; create raises; retry-find finds it
        linear = MagicMock()
        linear.find_workspace_label.side_effect = [None, "lbl-race"]
        linear.create_workspace_label.side_effect = LinearError("Already exists")

        provision_trigger_label(linear, state, "Agent")

        assert linear.find_workspace_label.call_count == 2
        linear.create_workspace_label.assert_called_once_with("Agent")
        assert state.provisioned_label_name == "Agent"

    def test_create_error_retry_find_also_fails_logs_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """Create fails, retry-find also raises – warning, state unchanged."""
        state = _state(tmp_path)
        assert state.provisioned_label_name is None

        linear = MagicMock()
        linear.find_workspace_label.side_effect = [
            None,
            LinearError("Network timeout during retry"),
        ]
        linear.create_workspace_label.side_effect = LinearError("Already exists")

        with caplog.at_level(logging.WARNING):
            provision_trigger_label(linear, state, "Agent")

        assert "Failed to auto-provision Linear label 'Agent'" in caplog.text
        assert "Network timeout during retry" in caplog.text
        assert linear.find_workspace_label.call_count == 2
        assert state.provisioned_label_name is None

    def test_create_non_linear_error_logs_warning_no_state_change(
        self, tmp_path: Path, caplog
    ) -> None:
        """Non-LinearError from create is caught, warning logged, state unchanged."""
        state = _state(tmp_path)
        assert state.provisioned_label_name is None

        # find returns None; create raises RuntimeError; retry-find returns None
        linear = MagicMock()
        linear.find_workspace_label.side_effect = [None, None]
        linear.create_workspace_label.side_effect = RuntimeError("boom")

        with caplog.at_level(logging.WARNING):
            provision_trigger_label(linear, state, "Agent")

        assert "Failed to auto-provision Linear label 'Agent'" in caplog.text
        assert "boom" in caplog.text
        assert linear.find_workspace_label.call_count == 2
        assert state.provisioned_label_name is None


class TestExceptionDoesNotPropagate:
    def test_none_trigger_label_name_assume_ok(self, tmp_path: Path) -> None:
        """Provisioning with a currently-None state and new label should work."""
        state = _state(tmp_path)
        linear = _fake_linear(find_id="lbl-ok")

        provision_trigger_label(linear, state, "Agent")
        # Should not raise
        assert state.provisioned_label_name == "Agent"

    def test_set_provisioned_label_name_failure_no_propagate(self, caplog) -> None:
        """If state.save() raises OSError, log a warning, do not crash."""
        state = MagicMock(spec=StateManager)
        state.provisioned_label_name = None
        state.set_provisioned_label_name.side_effect = OSError("disk full")

        linear = _fake_linear(find_id="lbl-ok")

        with caplog.at_level(logging.WARNING):
            provision_trigger_label(linear, state, "Agent")

        assert "Failed to auto-provision Linear label 'Agent'" in caplog.text
        assert "disk full" in caplog.text
        state.set_provisioned_label_name.assert_called_once_with("Agent")
