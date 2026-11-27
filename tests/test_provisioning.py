"""Tests for trigger-label provisioning logic."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from symphony_linear.linear import LinearError
from symphony_linear.provisioning import provision_model_labels, provision_trigger_label
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
    """Build a mock LinearClient with configurable find/create behaviour.

    The same behaviour is applied to both label namespaces (issue labels and
    project labels), since ``provision_model_labels`` ensures every name in
    both.
    """
    linear = MagicMock()
    for find, create in (
        ("find_workspace_label", "create_workspace_label"),
        ("find_project_label", "create_project_label"),
    ):
        if find_error:
            getattr(linear, find).side_effect = find_error
        else:
            getattr(linear, find).return_value = find_id
        if create_error:
            getattr(linear, create).side_effect = create_error
        else:
            getattr(linear, create).return_value = create_id
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

        assert "Failed to auto-provision Linear issue label 'Agent'" in caplog.text
        assert "Network down" in caplog.text
        linear.create_workspace_label.assert_not_called()
        assert state.provisioned_label_name is None

    @pytest.mark.parametrize(
        ("retry_find", "expected_message"),
        [
            (None, "Permission denied"),
            (
                LinearError("Network timeout during retry"),
                "Network timeout during retry",
            ),
        ],
    )
    def test_create_error_logs_warning_no_state_change(
        self,
        tmp_path: Path,
        caplog,
        retry_find: Exception | None,
        expected_message: str,
    ) -> None:
        """Create fails and the retry-find does not resolve it – warning, state kept."""
        state = _state(tmp_path)
        assert state.provisioned_label_name is None

        linear = MagicMock()
        linear.find_workspace_label.side_effect = [None, retry_find]
        linear.create_workspace_label.side_effect = LinearError("Permission denied")

        with caplog.at_level(logging.WARNING):
            provision_trigger_label(linear, state, "Agent")

        assert "Failed to auto-provision Linear issue label 'Agent'" in caplog.text
        assert expected_message in caplog.text
        assert linear.find_workspace_label.call_count == 2
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

        assert "Failed to auto-provision Linear issue label 'Agent'" in caplog.text
        assert "boom" in caplog.text
        assert linear.find_workspace_label.call_count == 2
        assert state.provisioned_label_name is None


class TestExceptionDoesNotPropagate:
    def test_set_provisioned_label_name_failure_no_propagate(self, caplog) -> None:
        """If state.save() raises OSError, log a warning, do not crash."""
        state = MagicMock(spec=StateManager)
        state.provisioned_label_name = None
        state.set_provisioned_label_name.side_effect = OSError("disk full")

        linear = _fake_linear(find_id="lbl-ok")

        with caplog.at_level(logging.WARNING):
            provision_trigger_label(linear, state, "Agent")

        assert "Failed to auto-provision Linear issue label 'Agent'" in caplog.text
        assert "disk full" in caplog.text
        state.set_provisioned_label_name.assert_called_once_with("Agent")


class TestProvisionModelLabels:
    def test_creates_missing_labels_in_both_namespaces(self) -> None:
        """Each missing name is created as an issue label and a project label."""
        linear = _fake_linear(find_id=None, create_id="lbl-new")

        provision_model_labels(linear, ["Model: Strong", "Model: Cheap"])

        both = [call("Model: Strong"), call("Model: Cheap")]
        linear.find_workspace_label.assert_has_calls(both)
        linear.create_workspace_label.assert_has_calls(both)
        linear.find_project_label.assert_has_calls(both)
        linear.create_project_label.assert_has_calls(both)

    def test_existing_labels_not_recreated(self) -> None:
        """Labels that already exist are found but never created."""
        linear = _fake_linear(find_id="lbl-existing")

        provision_model_labels(linear, ["Model: Strong"])

        linear.find_workspace_label.assert_called_once_with("Model: Strong")
        linear.find_project_label.assert_called_once_with("Model: Strong")
        linear.create_workspace_label.assert_not_called()
        linear.create_project_label.assert_not_called()

    def test_existing_project_label_with_missing_issue_label(self) -> None:
        """The two namespaces are resolved independently of each other."""
        linear = MagicMock()
        linear.find_workspace_label.return_value = None
        linear.create_workspace_label.return_value = "lbl-issue"
        linear.find_project_label.return_value = "lbl-project-existing"

        provision_model_labels(linear, ["Model: Strong"])

        linear.create_workspace_label.assert_called_once_with("Model: Strong")
        linear.create_project_label.assert_not_called()

    def test_find_error_warns_and_does_not_raise(self, caplog) -> None:
        """A failing find warns, naming the namespace, and attempts no create."""
        linear = _fake_linear(find_error=LinearError("Network down"))

        with caplog.at_level(logging.WARNING):
            provision_model_labels(linear, ["Model: Strong"])

        issue = "Failed to auto-provision Linear issue label 'Model: Strong'"
        project = "Failed to auto-provision Linear project label 'Model: Strong'"
        assert issue in caplog.text
        assert project in caplog.text
        assert "Network down" in caplog.text
        linear.create_workspace_label.assert_not_called()
        linear.create_project_label.assert_not_called()

    def test_create_error_warns_and_does_not_raise(self, caplog) -> None:
        """A failing issue-label create (and retry-find) warns, does not raise."""
        linear = MagicMock()
        linear.find_workspace_label.side_effect = [None, None]
        linear.create_workspace_label.side_effect = LinearError("Permission denied")
        linear.find_project_label.return_value = "lbl-project"

        with caplog.at_level(logging.WARNING):
            provision_model_labels(linear, ["Model: Strong"])

        issue = "Failed to auto-provision Linear issue label 'Model: Strong'"
        assert issue in caplog.text
        assert "Permission denied" in caplog.text
        assert linear.find_workspace_label.call_count == 2

    def test_create_error_race_resolved_succeeds(self, caplog) -> None:
        """Create fails (race), retry-find finds the label – no warning."""
        linear = MagicMock()
        linear.find_workspace_label.side_effect = [None, "lbl-race"]
        linear.create_workspace_label.side_effect = LinearError("Already exists")
        linear.find_project_label.return_value = "lbl-project"

        with caplog.at_level(logging.WARNING):
            provision_model_labels(linear, ["Model: Strong"])

        assert linear.find_workspace_label.call_count == 2
        assert not caplog.records

    def test_project_find_error_warns_and_continues(self, caplog) -> None:
        """A project-label failure warns, does not raise, does not stop the rest."""
        linear = MagicMock()
        linear.find_workspace_label.return_value = "lbl-issue"
        linear.find_project_label.side_effect = [
            LinearError("Project labels down"),
            "lbl-project",
        ]

        with caplog.at_level(logging.WARNING):
            provision_model_labels(linear, ["Model: Strong", "Model: Cheap"])

        project = "Failed to auto-provision Linear project label 'Model: Strong'"
        assert project in caplog.text
        assert "Project labels down" in caplog.text
        # The issue-label namespace was fine, so no issue-label warning.
        assert "issue label" not in caplog.text
        assert linear.find_project_label.call_count == 2
        linear.create_project_label.assert_not_called()
        linear.find_workspace_label.assert_has_calls(
            [call("Model: Strong"), call("Model: Cheap")]
        )

    def test_continues_after_one_failure(self, caplog) -> None:
        """One label failing to provision doesn't stop the remaining labels."""
        linear = MagicMock()
        linear.find_workspace_label.side_effect = [
            LinearError("Network down"),
            "lbl-ok",
        ]
        linear.find_project_label.return_value = "lbl-project"

        with caplog.at_level(logging.WARNING):
            provision_model_labels(linear, ["Model: Strong", "Model: Cheap"])

        issue = "Failed to auto-provision Linear issue label 'Model: Strong'"
        assert issue in caplog.text
        assert linear.find_workspace_label.call_count == 2
        linear.create_workspace_label.assert_not_called()
