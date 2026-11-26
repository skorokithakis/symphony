"""Unit tests for the CLI entry-point (webhook wiring)."""

from __future__ import annotations

import sys
from pathlib import Path
from shutil import which as shutil_which
from unittest import mock

import pytest

from symphony_linear import sandbox
from symphony_linear.cli import _create_tracker, _reexec, main
from symphony_linear.config import load_config
from symphony_linear.local_tracker import STATE_IN_PROGRESS, LocalTracker
from symphony_linear.presence import TicketPresence
from symphony_linear.tracker import TransitionTarget
from symphony_linear.turn_activity import TurnActivity
from symphony_linear.webpush import WebPushStore


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
  password: secret
poll_interval_seconds: 10
"""

_CONFIG_WITH_WEBUI_PUBLIC_URL = """\
webui:
  port: 8080
  password: secret
  public_url: https://symphony.example.com
poll_interval_seconds: 10
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content)
    return config_path


def _which_with_bwrap(binary: str, path: str | None = None) -> str | None:
    """Real PATH lookup with bwrap faked, so a test exercises the agent check."""
    if binary == "bwrap":
        return "/usr/bin/bwrap"
    return shutil_which(binary, path=path)


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

        mock_which.assert_any_call("bwrap")
        mock_which.assert_any_call("omp", path=sandbox_path)

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

        mock_which.assert_any_call("bwrap")
        mock_which.assert_any_call("omp", path=sandbox_path)

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
        monkeypatch.setattr("symphony_linear.cli.shutil.which", _which_with_bwrap)

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
        monkeypatch.setattr("symphony_linear.cli.shutil.which", _which_with_bwrap)

        with pytest.raises(SystemExit) as excinfo:
            main(["--workspace", str(tmp_path), "--validate-config"])

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "omp" in stderr
        assert "PATH" in stderr
        assert str(sandbox_bin) in stderr
        assert "SYMPHONY_SANDBOX_PATH" in stderr


class TestBwrapValidation:
    @pytest.fixture(autouse=True)
    def _restore_sandbox_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The switch is a module global, so restore it after every test."""
        monkeypatch.setattr("symphony_linear.sandbox._SANDBOX_ENABLED", True)

    @staticmethod
    def _bwrap_missing(binary: str, path: str | None = None) -> str | None:
        return None if binary == "bwrap" else "/usr/bin/agent"

    @pytest.mark.parametrize("extra_args", [[], ["--validate-config"]])
    def test_non_webui_config_without_bwrap_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        extra_args: list[str],
    ) -> None:
        """A Linear config holds live tokens, so it may never run unsandboxed."""
        _write_config(tmp_path, _CONFIG_WITHOUT_WEBHOOK)
        monkeypatch.setattr("symphony_linear.cli.shutil.which", self._bwrap_missing)

        with pytest.raises(SystemExit) as excinfo:
            main(["--workspace", str(tmp_path), *extra_args])

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "bwrap" in stderr
        assert "apt install bubblewrap" in stderr
        assert "dnf install bubblewrap" in stderr
        assert "pacman -S bubblewrap" in stderr
        assert sandbox.is_sandboxed() is True

    def test_webui_without_a_terminal_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A supervised daemon must fail loudly rather than drop the sandbox."""
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)
        monkeypatch.setattr("symphony_linear.cli.shutil.which", self._bwrap_missing)
        monkeypatch.setattr(
            "symphony_linear.cli.sys.stdin", mock.Mock(isatty=lambda: False)
        )

        with pytest.raises(SystemExit) as excinfo:
            main(["--workspace", str(tmp_path)])

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "bwrap" in stderr
        assert sandbox.is_sandboxed() is True

    def test_webui_with_a_terminal_and_dir_map_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A directory mapping only exists inside bwrap, so it cannot fall back."""
        _write_config(
            tmp_path,
            """\
webui:
  port: 8080
  password: secret
poll_interval_seconds: 10
sandbox:
  dir_map:
    /sandbox/repos: /host/repos
""",
        )
        monkeypatch.setattr("symphony_linear.cli.shutil.which", self._bwrap_missing)
        monkeypatch.setattr(
            "symphony_linear.cli.sys.stdin", mock.Mock(isatty=lambda: True)
        )

        with pytest.raises(SystemExit) as excinfo:
            main(["--workspace", str(tmp_path)])

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "sandbox.dir_map is set, but bwrap (bubblewrap) was not found" in stderr
        assert "remove sandbox.dir_map from config.yaml" in stderr
        assert sandbox.is_sandboxed() is True

    def test_webui_with_a_terminal_runs_unsandboxed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The attended web UI trial is the one case that drops the sandbox."""
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)
        monkeypatch.setattr("symphony_linear.cli.shutil.which", self._bwrap_missing)
        monkeypatch.setattr(
            "symphony_linear.cli.sys.stdin", mock.Mock(isatty=lambda: True)
        )

        with (
            mock.patch("symphony_linear.cli.load_state") as mock_load_state,
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orchestrator_class,
            mock.patch("symphony_linear.cli.WebUIServer"),
        ):
            mock_load_state.return_value = mock.MagicMock()
            mock_create_tracker.return_value = LocalTracker(tmp_path)
            mock_orchestrator_class.return_value = mock.MagicMock()

            main(["--workspace", str(tmp_path)])

        assert sandbox.is_sandboxed() is False
        stderr = capsys.readouterr().err
        assert "so this run has NO sandbox" in stderr
        assert "sandbox.hide_paths and sandbox.extra_rw_paths are ignored" in stderr
        assert "pacman -S bubblewrap' and restart." in stderr


# ---------------------------------------------------------------------------
# First run: the ~/symphony default workspace
# ---------------------------------------------------------------------------


class TestFirstRunWorkspace:
    """The default workspace is only ever created by an interactive run.

    Every test here runs with a temporary ``HOME`` and working directory, so a
    regression that creates the workspace would create it inside the temporary
    directory rather than the developer's real home.
    """

    @pytest.fixture(autouse=True)
    def isolated_home_and_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path]:
        home = tmp_path / "home"
        home.mkdir()
        current_directory = tmp_path / "cwd"
        current_directory.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(current_directory)
        return home, current_directory

    @pytest.fixture(autouse=True)
    def _restore_sandbox_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The switch is a module global, so restore it after every test."""
        monkeypatch.setattr("symphony_linear.sandbox._SANDBOX_ENABLED", True)

    @pytest.fixture
    def terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "symphony_linear.cli.sys.stdin", mock.Mock(isatty=lambda: True)
        )

    @pytest.fixture
    def all_agents_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "symphony_linear.cli.shutil.which",
            lambda binary, path=None: f"/usr/bin/{binary}",
        )

    def _run_with_mocks(self) -> mock.Mock:
        """Run main() with the daemon parts mocked, and return the Orchestrator mock."""
        with (
            mock.patch("symphony_linear.cli.load_state"),
            mock.patch("symphony_linear.cli._create_tracker") as mock_create_tracker,
            mock.patch("symphony_linear.cli.Orchestrator") as mock_orchestrator_class,
            mock.patch("symphony_linear.cli.WebUIServer"),
        ):
            mock_create_tracker.side_effect = lambda _config, workspace, **_kwargs: (
                LocalTracker(workspace)
            )
            main([])
        return mock_orchestrator_class

    def test_validate_config_never_creates_the_default_workspace(
        self,
        isolated_home_and_cwd: tuple[Path, Path],
        terminal: None,
        all_agents_available: None,
    ) -> None:
        """The checkout watcher runs this unattended with an explicit workspace."""
        home, _ = isolated_home_and_cwd

        with pytest.raises(SystemExit) as excinfo:
            main(["--validate-config"])

        assert excinfo.value.code == 1
        assert not (home / "symphony").exists()

    def test_non_terminal_run_never_creates_the_default_workspace(
        self,
        isolated_home_and_cwd: tuple[Path, Path],
        all_agents_available: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A supervised daemon with a wrong cwd must fail, not adopt ~/symphony."""
        home, _ = isolated_home_and_cwd
        # Explicit rather than relying on pytest's own stdin, so the test still
        # means what it says under `pytest -s` in a real terminal.
        monkeypatch.setattr(
            "symphony_linear.cli.sys.stdin", mock.Mock(isatty=lambda: False)
        )

        with pytest.raises(SystemExit) as excinfo:
            main([])

        assert excinfo.value.code == 1
        assert not (home / "symphony").exists()

    def test_terminal_first_run_creates_a_config_and_starts_the_web_ui(
        self,
        isolated_home_and_cwd: tuple[Path, Path],
        terminal: None,
        all_agents_available: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        home, _ = isolated_home_and_cwd

        orchestrator_class = self._run_with_mocks()

        workspace = (home / "symphony").resolve()
        config_path = workspace / "config.yaml"
        assert orchestrator_class.call_args.kwargs["workspace"] == workspace
        assert config_path.stat().st_mode & 0o777 == 0o600
        config = load_config(workspace)
        assert config.agent == "opencode"
        assert config.webui is not None
        assert config.webui.port == 27149

        stderr = capsys.readouterr().err
        password = config.webui.password
        assert len(password) >= 24
        assert password in config_path.read_text()
        # One line, with the password outside any URL: it is not a credential
        # in a query string, it is the Basic-auth password for the login form.
        line = next(
            line for line in stderr.splitlines() if line.startswith("Open http")
        )
        assert line.startswith(
            f"Open http://127.0.0.1:27149/ and log in with password {password}"
        )
        assert "config.yaml" in line

    def test_first_run_without_bwrap_creates_the_config_and_warns(
        self,
        isolated_home_and_cwd: tuple[Path, Path],
        terminal: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The trial fallback applies after the workspace is resolved.

        The old check ran before workspace resolution, so a host without bwrap
        could not even get as far as writing its first config.
        """
        home, _ = isolated_home_and_cwd
        monkeypatch.setattr(
            "symphony_linear.cli.shutil.which",
            lambda binary, path=None: (
                None if binary == "bwrap" else f"/usr/bin/{binary}"
            ),
        )

        self._run_with_mocks()

        workspace = (home / "symphony").resolve()
        assert (workspace / "config.yaml").is_file()
        assert sandbox.is_sandboxed() is False
        assert "so this run has NO sandbox" in capsys.readouterr().err

    def test_first_run_prefers_opencode_then_omp_then_pi(
        self,
        isolated_home_and_cwd: tuple[Path, Path],
        terminal: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home, _ = isolated_home_and_cwd
        monkeypatch.setenv("SYMPHONY_SANDBOX_PATH", "/sandbox/bin")
        probed: list[tuple[str, str | None]] = []

        def _which(binary: str, path: str | None = None) -> str | None:
            if binary == "bwrap":
                return "/usr/bin/bwrap"
            probed.append((binary, path))
            return f"/sandbox/bin/{binary}" if binary in ("omp", "pi") else None

        monkeypatch.setattr(
            "symphony_linear.cli.shutil.which",
            lambda binary, path=None: _which(binary, path),
        )

        self._run_with_mocks()

        config = load_config((home / "symphony").resolve())
        assert config.agent == "omp"
        # The lookup must be the sandbox PATH, the same one the startup agent
        # check uses, and it must stop at the first hit rather than probe pi.
        # The startup check itself probes the resolved agent again.
        assert probed[:2] == [("opencode", "/sandbox/bin"), ("omp", "/sandbox/bin")]

    def test_no_agent_creates_nothing_and_names_all_three(
        self,
        isolated_home_and_cwd: tuple[Path, Path],
        terminal: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        home, _ = isolated_home_and_cwd
        monkeypatch.setattr(
            "symphony_linear.cli.shutil.which",
            lambda binary, path=None: "/usr/bin/bwrap" if binary == "bwrap" else None,
        )

        with pytest.raises(SystemExit) as excinfo:
            main([])

        assert excinfo.value.code == 1
        assert not (home / "symphony").exists()
        stderr = capsys.readouterr().err
        for agent in ("opencode", "omp", "pi"):
            assert agent in stderr

    def test_existing_default_config_is_reused_unchanged(
        self,
        isolated_home_and_cwd: tuple[Path, Path],
        terminal: None,
        all_agents_available: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        home, _ = isolated_home_and_cwd
        workspace = home / "symphony"
        workspace.mkdir()
        _write_config(workspace, _CONFIG_WITH_WEBUI)
        before = (workspace / "config.yaml").read_text()

        orchestrator_class = self._run_with_mocks()

        assert (workspace / "config.yaml").read_text() == before
        assert orchestrator_class.call_args.kwargs["workspace"] == workspace.resolve()
        assert "Open http" not in capsys.readouterr().err

    def test_current_directory_config_wins_over_the_default(
        self,
        isolated_home_and_cwd: tuple[Path, Path],
        terminal: None,
        all_agents_available: None,
    ) -> None:
        home, current_directory = isolated_home_and_cwd
        _write_config(current_directory, _CONFIG_WITH_WEBUI)

        orchestrator_class = self._run_with_mocks()

        assert (
            orchestrator_class.call_args.kwargs["workspace"]
            == current_directory.resolve()
        )
        assert not (home / "symphony").exists()


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

    def test_webui_backend_receives_a_web_push_store(self, tmp_path: Path) -> None:
        """Web Push is always on: the CLI builds the one store to share.

        The same instance must reach the web server, so it is exposed on the
        tracker rather than created twice.
        """
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)
        config = load_config(tmp_path)

        tracker = _create_tracker(config, tmp_path)

        assert isinstance(tracker, LocalTracker)
        assert isinstance(tracker.webpush, WebPushStore)
        assert tracker.webpush.public_key()
        # The file deliberately lives in the workspace root, where the
        # computed sandbox masking hides it from agents.
        assert (tmp_path / "webpush.json").is_file()

    def test_webui_backend_passes_public_url_to_web_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The push's VAPID contact needs webui.public_url, so cli must pass it."""
        _write_config(tmp_path, _CONFIG_WITH_WEBUI_PUBLIC_URL)
        config = load_config(tmp_path)
        tracker = _create_tracker(config, tmp_path)
        assert isinstance(tracker, LocalTracker)
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        calls: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input_push",
            lambda _store, **kwargs: calls.append(
                (kwargs["ticket_id"], kwargs["public_url"])
            ),
        )

        tracker.transition_to(ticket.id, TransitionTarget.needs_input)

        assert calls == [(ticket.id, "https://symphony.example.com")]

    def test_webui_backend_receives_the_presence_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ticket whose page is watched must not send its push."""
        _write_config(tmp_path, _CONFIG_WITH_WEBUI)
        config = load_config(tmp_path)
        presence = TicketPresence()
        tracker = _create_tracker(config, tmp_path, presence=presence)
        assert isinstance(tracker, LocalTracker)
        ticket = tracker.store.create_draft(title="t", description="x")
        tracker.store.set_state(ticket.id, STATE_IN_PROGRESS)
        presence.mark_active(ticket.id)
        calls: list[str] = []
        monkeypatch.setattr(
            "symphony_linear.local_tracker.publish_needs_input_push",
            lambda _store, **kwargs: calls.append(kwargs["ticket_id"]),
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

    def test_main_shares_one_turn_activity_between_orchestrator_and_web_ui(
        self, tmp_path: Path
    ) -> None:
        """The web UI can only show ages the orchestrator actually records."""
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

        orchestrator_activity = mock_orch_class.call_args.kwargs["turn_activity"]
        server_activity = mock_webui_class.call_args.kwargs["turn_activity"]
        assert isinstance(orchestrator_activity, TurnActivity)
        assert orchestrator_activity is server_activity
