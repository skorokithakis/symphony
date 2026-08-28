"""Unit tests for the CLI entry-point (webhook wiring)."""

from __future__ import annotations

from pathlib import Path
from shutil import which as shutil_which
from unittest import mock

import pytest

from symphony_linear.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFIG_WITH_WEBHOOK = """\
linear:
  api_key: test-key
  trigger_label: Agent
  in_progress_state: In Progress
  needs_input_state: Needs Input
webhook:
  port: 8080
  linear_secret: my-secret
poll_interval_seconds: 10
"""

_CONFIG_WITHOUT_WEBHOOK = """\
linear:
  api_key: test-key
  trigger_label: Agent
  in_progress_state: In Progress
  needs_input_state: Needs Input
poll_interval_seconds: 10
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content)
    return config_path


@pytest.fixture(autouse=True)
def _agent_binary_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI tests independent from agent binaries installed on the host."""
    monkeypatch.setattr(
        "symphony_linear.cli.shutil.which",
        lambda _binary, path=None: "/usr/bin/agent",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCliWebhookWiring:
    def test_no_webhook_block_no_server_instantiated(self, tmp_path: Path) -> None:
        """Without `webhook:` in config, WebhookServer is never constructed."""
        _write_config(tmp_path, _CONFIG_WITHOUT_WEBHOOK)

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
        ):
            mock_load_state.return_value = mock.MagicMock()

            mock_orch = mock.MagicMock()
            mock_orch_class.return_value = mock_orch

            main(["--workspace", str(tmp_path)])

            # Orchestrator constructed without webhook_server.
            _, kwargs = mock_orch_class.call_args
            assert (
                "webhook_server" not in kwargs or kwargs.get("webhook_server") is None
            )

            # _webhook_server was never set on the orchestrator instance.
            # Since mock.MagicMock returns a mock for any attribute, we check
            # that no set was called that looks like setting _webhook_server.
            # Instead, check that WebhookServer was never instantiated.
            with mock.patch("symphony_linear.cli.WebhookServer") as mock_ws_class:
                main(["--workspace", str(tmp_path)])
            mock_ws_class.assert_not_called()

    def test_webhook_block_creates_and_wires_server(self, tmp_path: Path) -> None:
        """With a webhook config block, WebhookServer is constructed and set on orchestrator."""
        _write_config(tmp_path, _CONFIG_WITH_WEBHOOK)

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.WebhookServer") as mock_ws_class,
        ):
            mock_load_state.return_value = mock.MagicMock()
            mock_orch = mock.MagicMock()
            mock_orch_class.return_value = mock_orch

            main(["--workspace", str(tmp_path)])

            # WebhookServer was constructed with the right args.
            mock_ws_class.assert_called_once()
            args, kwargs = mock_ws_class.call_args
            assert kwargs["port"] == 8080
            assert kwargs["linear_secret"] == "my-secret"
            assert kwargs["on_wake"] is mock_orch.wake

            # Orchestrator.set_webhook_server was called with the server.
            mock_ws_instance = mock_ws_class.return_value
            mock_orch.set_webhook_server.assert_called_once_with(mock_ws_instance)


# ---------------------------------------------------------------------------
# Model-label provisioning wiring
# ---------------------------------------------------------------------------

_CONFIG_WITH_MODELS = """\
linear:
  api_key: test-key
  trigger_label: Agent
  in_progress_state: In Progress
  needs_input_state: Needs Input
models:
  Strong: anthropic/claude-opus-5
  Cheap: openai/gpt-5-mini
poll_interval_seconds: 10
"""


class TestModelLabelProvisioningWiring:
    def test_model_aliases_become_label_names(self, tmp_path: Path) -> None:
        """Configured model aliases are passed as Model: <alias> label names."""
        _write_config(tmp_path, _CONFIG_WITH_MODELS)

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
        ):
            mock_state = mock.MagicMock()
            mock_load_state.return_value = mock_state
            mock_orch_class.return_value = mock.MagicMock()
            mock_tracker = mock.MagicMock()
            mock_create_tracker.return_value = mock_tracker

            main(["--workspace", str(tmp_path)])

            mock_tracker.ensure_trigger_setup.assert_called_once_with(
                mock_state, ["Model: Strong", "Model: Cheap"]
            )

    def test_empty_models_passes_empty_list(self, tmp_path: Path) -> None:
        """No `models:` block means an empty list and no label work."""
        _write_config(tmp_path, _CONFIG_WITHOUT_WEBHOOK)

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
        ):
            mock_state = mock.MagicMock()
            mock_load_state.return_value = mock_state
            mock_orch_class.return_value = mock.MagicMock()
            mock_tracker = mock.MagicMock()
            mock_create_tracker.return_value = mock_tracker

            main(["--workspace", str(tmp_path)])

            mock_tracker.ensure_trigger_setup.assert_called_once_with(mock_state, [])


# ---------------------------------------------------------------------------
# Agent binary validation
# ---------------------------------------------------------------------------


class TestAgentBinaryValidation:
    def test_startup_checks_selected_agent_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path,
            """\
agent: omp
linear:
  api_key: test-key
""",
        )

        sandbox_path = "/sandbox/bin"
        monkeypatch.setenv("SYMPHONY_SANDBOX_PATH", sandbox_path)

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch(
                "symphony_linear.cli.shutil.which", return_value="/usr/bin/omp"
            ) as mock_which,
        ):
            mock_load_state.return_value = mock.MagicMock()
            mock_create_tracker.return_value = mock.MagicMock()
            mock_orch_class.return_value = mock.MagicMock()

            main(["--workspace", str(tmp_path)])

        mock_which.assert_called_once_with("omp", path=sandbox_path)

    def test_validate_config_checks_selected_agent_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path,
            """\
agent: omp
linear:
  api_key: test-key
""",
        )

        sandbox_path = "/sandbox/bin"
        monkeypatch.setenv("SYMPHONY_SANDBOX_PATH", sandbox_path)

        with mock.patch(
            "symphony_linear.cli.shutil.which", return_value="/usr/bin/omp"
        ) as mock_which:
            main(["--workspace", str(tmp_path), "--validate-config"])

        mock_which.assert_called_once_with("omp", path=sandbox_path)

    def test_agent_found_only_via_sandbox_path_passes_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path,
            """\
agent: omp
linear:
  api_key: test-key
""",
        )
        sandbox_bin = tmp_path / "sandbox-bin"
        sandbox_bin.mkdir()
        agent_binary = sandbox_bin / "omp"
        agent_binary.write_text("#!/bin/sh\n")
        agent_binary.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path / "daemon-bin"))
        monkeypatch.setenv("SYMPHONY_SANDBOX_PATH", str(sandbox_bin))
        monkeypatch.setattr("symphony_linear.cli.shutil.which", shutil_which)

        main(["--workspace", str(tmp_path), "--validate-config"])

    def test_missing_agent_binary_fails_with_clear_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_config(
            tmp_path,
            """\
agent: omp
linear:
  api_key: test-key
""",
        )
        sandbox_bin = tmp_path / "sandbox-bin"
        sandbox_bin.mkdir()
        monkeypatch.setenv("PATH", str(tmp_path / "daemon-bin"))
        monkeypatch.setenv("SYMPHONY_SANDBOX_PATH", str(sandbox_bin))
        monkeypatch.setattr("symphony_linear.cli.shutil.which", shutil_which)

        with pytest.raises(SystemExit) as excinfo:
            main(["--workspace", str(tmp_path), "--validate-config"])

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "omp" in stderr
        assert "PATH" in stderr
        assert str(sandbox_bin) in stderr
        assert "SYMPHONY_SANDBOX_PATH" in stderr


# ---------------------------------------------------------------------------
# Pi agent binary check
# ---------------------------------------------------------------------------


class TestPiAgentBinaryCheck:
    def test_pi_agent_present_on_sandbox_path_passes(self, tmp_path: Path) -> None:
        """agent: pi with `pi` present on sandbox PATH passes startup."""
        _write_config(
            tmp_path,
            """\
agent: pi
linear:
  api_key: test-key
""",
        )
        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch(
                "symphony_linear.cli.shutil.which", return_value="/usr/bin/pi"
            ) as mock_which,
        ):
            mock_load_state.return_value = mock.MagicMock()
            mock_create_tracker.return_value = mock.MagicMock()
            mock_orch_class.return_value = mock.MagicMock()

            main(["--workspace", str(tmp_path)])

        mock_which.assert_called_once_with("pi", path=mock.ANY)

    def test_pi_agent_absent_exits_1_naming_pi(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """agent: pi with `pi` absent on sandbox PATH exits 1 naming 'pi'."""
        _write_config(
            tmp_path,
            """\
agent: pi
linear:
  api_key: test-key
""",
        )
        sandbox_bin = tmp_path / "sandbox-bin"
        sandbox_bin.mkdir()
        monkeypatch.setenv("PATH", str(tmp_path / "daemon-bin"))
        monkeypatch.setenv("SYMPHONY_SANDBOX_PATH", str(sandbox_bin))
        monkeypatch.setattr("symphony_linear.cli.shutil.which", shutil_which)

        with pytest.raises(SystemExit) as excinfo:
            main(["--workspace", str(tmp_path), "--validate-config"])

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "pi" in stderr
        assert "SYMPHONY_SANDBOX_PATH" in stderr
