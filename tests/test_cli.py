"""Unit tests for the CLI entry-point (webhook wiring)."""

from __future__ import annotations

import sys
from pathlib import Path
from shutil import which as shutil_which
from unittest import mock

import pytest

from symphony_linear.cli import _create_tracker, _reexec, main
from symphony_linear.config import load_config
from symphony_linear.local_tracker import STATE_IN_PROGRESS, LocalTracker
from symphony_linear.presence import TicketPresence
from symphony_linear.tracker import TransitionTarget


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

_CONFIG_WITH_WEBUI = """\
webui:
  port: 8080
  username: admin
  password: secret
poll_interval_seconds: 10
"""

_CONFIG_WITH_WEBUI_NTFY = """\
webui:
  port: 8080
  username: admin
  password: secret
  public_url: https://symphony.example.com
  ntfy:
    url: https://ntfy.sh/mytopic
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


class TestCliWebUIWiring:
    def test_webui_block_starts_and_stops_server_around_run(
        self, tmp_path: Path
    ) -> None:
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)
        events: list[str] = []

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.WebUIServer") as mock_webui_class,
        ):
            state = mock.MagicMock()
            mock_load_state.return_value = state
            tracker = LocalTracker(tmp_path)
            mock_create_tracker.return_value = tracker
            orch = mock.MagicMock()
            orch.run.side_effect = lambda: events.append("run")
            mock_orch_class.return_value = orch
            server = mock.MagicMock()
            server.start.side_effect = lambda: events.append("start")
            server.stop.side_effect = lambda: events.append("stop")
            mock_webui_class.return_value = server

            main(["--workspace", str(tmp_path)])

        assert events == ["start", "run", "stop"]
        kwargs = mock_webui_class.call_args.kwargs
        assert kwargs["store"] is tracker.store
        assert kwargs["state"] is state
        assert kwargs["config"].webui.port == 8080
        assert kwargs["on_wake"] == orch.wake

    def test_webui_server_stops_when_run_raises(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)

        with (
            mock.patch("symphony_linear.cli.load_state"),
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.WebUIServer") as mock_webui_class,
        ):
            mock_create_tracker.return_value = LocalTracker(tmp_path)
            orch = mock.MagicMock()
            orch.run.side_effect = RuntimeError("boom")
            mock_orch_class.return_value = orch
            server = mock.MagicMock()
            mock_webui_class.return_value = server

            with pytest.raises(RuntimeError, match="boom"):
                main(["--workspace", str(tmp_path)])

        server.stop.assert_called_once()

    def test_webui_server_stops_before_reexec(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)
        events: list[str] = []

        with (
            mock.patch("symphony_linear.cli.load_state"),
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.WebUIServer") as mock_webui_class,
            mock.patch("symphony_linear.cli.os.execv") as mock_execv,
        ):
            mock_create_tracker.return_value = LocalTracker(tmp_path)
            orch = mock.MagicMock()
            orch.run.return_value = True
            mock_orch_class.return_value = orch
            server = mock.MagicMock()
            server.stop.side_effect = lambda: events.append("stop")
            mock_webui_class.return_value = server
            mock_execv.side_effect = lambda *_args: events.append("execv")

            main(["--workspace", str(tmp_path)])

        assert events == ["stop", "execv"]

    def test_webui_server_stops_when_start_fails(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)

        with (
            mock.patch("symphony_linear.cli.load_state"),
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.WebUIServer") as mock_webui_class,
        ):
            mock_create_tracker.return_value = LocalTracker(tmp_path)
            orch = mock.MagicMock()
            mock_orch_class.return_value = orch
            server = mock.MagicMock()
            server.start.side_effect = RuntimeError("bind failed")
            mock_webui_class.return_value = server

            with pytest.raises(RuntimeError, match="bind failed"):
                main(["--workspace", str(tmp_path)])

        server.stop.assert_called_once()
        orch.run.assert_not_called()

    def test_no_webui_block_no_server_instantiated(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _CONFIG_WITHOUT_WEBHOOK)

        with (
            mock.patch("symphony_linear.cli.load_state"),
            mock.patch("symphony_linear.cli.Orchestrator"),
            mock.patch("symphony_linear.cli.WebUIServer") as mock_webui_class,
        ):
            main(["--workspace", str(tmp_path)])

        mock_webui_class.assert_not_called()


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
# Checkout-change auto-restart re-exec
# ---------------------------------------------------------------------------


class TestCheckoutRestartReexec:
    def test_restart_reexecs_original_command_line(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _CONFIG_WITHOUT_WEBHOOK)
        orig_argv = ["/py", "/bin/symphony-linear", "--workspace", "x"]

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.os.execv") as mock_execv,
            mock.patch("symphony_linear.cli.sys.orig_argv", orig_argv),
        ):
            mock_load_state.return_value = mock.MagicMock()
            mock_create_tracker.return_value = mock.MagicMock()
            mock_orch = mock.MagicMock()
            mock_orch.run.return_value = True
            mock_orch_class.return_value = mock_orch

            main(["--workspace", str(tmp_path)])

        mock_execv.assert_called_once_with(sys.executable, orig_argv)

    def test_no_restart_does_not_execv(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _CONFIG_WITHOUT_WEBHOOK)

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.os.execv") as mock_execv,
        ):
            mock_load_state.return_value = mock.MagicMock()
            mock_create_tracker.return_value = mock.MagicMock()
            mock_orch = mock.MagicMock()
            mock_orch.run.return_value = False
            mock_orch_class.return_value = mock_orch

            main(["--workspace", str(tmp_path)])

        mock_execv.assert_not_called()

    def test_reexec_falls_back_to_sys_argv(self) -> None:
        with (
            mock.patch("symphony_linear.cli.sys.orig_argv", []),
            mock.patch(
                "symphony_linear.cli.sys.argv",
                ["/bin/symphony-linear", "--workspace", "/ws"],
            ),
            mock.patch("symphony_linear.cli.os.execv") as mock_execv,
        ):
            _reexec()

        mock_execv.assert_called_once_with(
            sys.executable,
            [sys.executable, "-m", "symphony_linear", "--workspace", "/ws"],
        )


# ---------------------------------------------------------------------------
# Tracker backend construction
# ---------------------------------------------------------------------------


class TestCreateTracker:
    def test_webui_backend_builds_local_tracker(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)
        config = load_config(tmp_path)

        tracker = _create_tracker(config, tmp_path)

        assert isinstance(tracker, LocalTracker)
        assert (tmp_path / "tickets").is_dir()

    def test_webui_backend_receives_the_ntfy_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cli must pass webui.ntfy through: it is what arms notifications."""
        _write_config(tmp_path, _CONFIG_WITH_WEBUI_NTFY)
        config = load_config(tmp_path)
        tracker = _create_tracker(config, tmp_path)
        assert isinstance(tracker, LocalTracker)
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        calls: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **kwargs: calls.append(
                (notified.id, kwargs.get("public_url"))
            ),
        )

        tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        # The notification payload's links need the web UI's base URL, so cli
        # must hand webui.public_url to the tracker alongside the ntfy block.
        assert calls == [(ticket.id, "https://symphony.example.com")]

    def test_webui_backend_receives_the_presence_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ticket whose page is watched must not publish its push."""
        _write_config(tmp_path, _CONFIG_WITH_WEBUI_NTFY)
        config = load_config(tmp_path)
        presence = TicketPresence()
        tracker = _create_tracker(config, tmp_path, presence=presence)
        assert isinstance(tracker, LocalTracker)
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        presence.mark_active(ticket.id)
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input",
            lambda _config, notified, **_kwargs: calls.append(notified.id),
        )

        tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        assert calls == []

    def test_main_passes_workspace_to_tracker_factory(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.WebUIServer") as mock_webui_class,
        ):
            mock_load_state.return_value = mock.MagicMock()
            mock_create_tracker.return_value = LocalTracker(tmp_path)
            mock_orch_class.return_value = mock.MagicMock()

            main(["--workspace", str(tmp_path)])

        mock_webui_class.assert_called_once()
        args, _ = mock_create_tracker.call_args
        assert args[1] == tmp_path

    def test_main_shares_one_presence_between_tracker_and_web_ui(
        self, tmp_path: Path
    ) -> None:
        """Both sides must see the same record, or the gate never matches."""
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orch_class,
            mock.patch("symphony_linear.cli.WebUIServer") as mock_webui_class,
        ):
            mock_load_state.return_value = mock.MagicMock()
            mock_create_tracker.return_value = LocalTracker(tmp_path)
            mock_orch_class.return_value = mock.MagicMock()

            main(["--workspace", str(tmp_path)])

        tracker_presence = mock_create_tracker.call_args.kwargs["presence"]
        server_presence = mock_webui_class.call_args.kwargs["presence"]
        assert isinstance(tracker_presence, TicketPresence)
        assert tracker_presence is server_presence
