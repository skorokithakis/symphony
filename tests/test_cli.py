"""Unit tests for the CLI entry-point (webhook wiring)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

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
  bot_user_email: bot@example.com
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
  bot_user_email: bot@example.com
poll_interval_seconds: 10
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content)
    return config_path


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
