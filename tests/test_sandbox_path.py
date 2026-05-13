"""Unit tests for PATH resolution in run_in_sandbox().

These tests do not require bwrap to be installed — they mock subprocess.Popen
and inspect the bwrap argument list that would be passed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from symphony_linear.sandbox import run_in_sandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_bwrap_args(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    symphony_sandbox_path: str | None = None,
    daemon_path: str | None = None,
    tmp_path: Path,
) -> list[str]:
    """Call run_in_sandbox() with a mocked Popen and return the bwrap argv.

    Args:
        env: The ``env`` dict passed to ``run_in_sandbox``.
        monkeypatch: pytest monkeypatch fixture.
        symphony_sandbox_path: Value to set for ``SYMPHONY_SANDBOX_PATH``, or
            ``None`` to leave it unset.
        daemon_path: Value to set for the daemon's ``PATH`` env var, or
            ``None`` to leave it unset.
        tmp_path: A temporary directory used as the workspace.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Control the environment variables seen by sandbox.py.
    if symphony_sandbox_path is not None:
        monkeypatch.setenv("SYMPHONY_SANDBOX_PATH", symphony_sandbox_path)
    else:
        monkeypatch.delenv("SYMPHONY_SANDBOX_PATH", raising=False)

    if daemon_path is not None:
        monkeypatch.setenv("PATH", daemon_path)
    else:
        monkeypatch.delenv("PATH", raising=False)

    captured: list[list[str]] = []

    def fake_popen(args: list[str], **kwargs: object) -> MagicMock:
        captured.append(list(args))
        return MagicMock()

    with patch("symphony_linear.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
        with patch("symphony_linear.sandbox.subprocess.Popen", side_effect=fake_popen):
            run_in_sandbox(
                cmd=["echo", "hello"],
                workspace_path=str(workspace),
                hide_paths=[],
                env=env,
            )

    assert captured, "Popen was never called"
    return captured[0]


def _extract_path_value(bwrap_args: list[str]) -> str | None:
    """Return the value of ``--setenv PATH <value>`` from *bwrap_args*, or
    ``None`` if no such flag is present."""
    for i, arg in enumerate(bwrap_args):
        if (
            arg == "--setenv"
            and i + 2 < len(bwrap_args)
            and bwrap_args[i + 1] == "PATH"
        ):
            return bwrap_args[i + 2]
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPathResolution:
    """Verify the three-tier PATH resolution in run_in_sandbox()."""

    def test_caller_supplied_path_is_used_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When the caller passes PATH in env, it is used as-is regardless of
        SYMPHONY_SANDBOX_PATH or the daemon's own PATH."""
        caller_path = "/caller/bin:/usr/bin"
        args = _capture_bwrap_args(
            env={"HOME": "/home/user", "PATH": caller_path},
            monkeypatch=monkeypatch,
            symphony_sandbox_path="/override/bin",
            daemon_path="/daemon/bin",
            tmp_path=tmp_path,
        )
        assert _extract_path_value(args) == caller_path

    def test_symphony_sandbox_path_wins_over_daemon_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When SYMPHONY_SANDBOX_PATH is set and the caller does not supply
        PATH, SYMPHONY_SANDBOX_PATH is used."""
        override_path = "/custom/sandbox/bin:/usr/bin"
        args = _capture_bwrap_args(
            env={"HOME": "/home/user"},
            monkeypatch=monkeypatch,
            symphony_sandbox_path=override_path,
            daemon_path="/daemon/bin:/usr/local/bin",
            tmp_path=tmp_path,
        )
        assert _extract_path_value(args) == override_path

    def test_daemon_path_used_when_symphony_sandbox_path_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When SYMPHONY_SANDBOX_PATH is not set and the caller does not supply
        PATH, the daemon's own os.environ['PATH'] is inherited."""
        daemon_path = "/home/user/.local/bin:/usr/local/bin:/usr/bin:/bin"
        args = _capture_bwrap_args(
            env={"HOME": "/home/user"},
            monkeypatch=monkeypatch,
            symphony_sandbox_path=None,
            daemon_path=daemon_path,
            tmp_path=tmp_path,
        )
        assert _extract_path_value(args) == daemon_path

    def test_fallback_path_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When neither SYMPHONY_SANDBOX_PATH nor the daemon's PATH is set,
        the hard-coded fallback '/usr/local/bin:/usr/bin:/bin' is used."""
        args = _capture_bwrap_args(
            env={"HOME": "/home/user"},
            monkeypatch=monkeypatch,
            symphony_sandbox_path=None,
            daemon_path=None,
            tmp_path=tmp_path,
        )
        assert _extract_path_value(args) == "/usr/local/bin:/usr/bin:/bin"
