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
    ticket_tmp = tmp_path / "ticket-tmp"
    ticket_tmp.mkdir()

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
                tmp_path=str(ticket_tmp),
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

    @pytest.mark.parametrize(
        ("env", "symphony_sandbox_path", "daemon_path", "expected"),
        [
            # A caller-supplied PATH wins over both other tiers.
            (
                {"HOME": "/home/user", "PATH": "/caller/bin:/usr/bin"},
                "/override/bin",
                "/daemon/bin",
                "/caller/bin:/usr/bin",
            ),
            # SYMPHONY_SANDBOX_PATH wins over the daemon's own PATH.
            (
                {"HOME": "/home/user"},
                "/custom/sandbox/bin:/usr/bin",
                "/daemon/bin:/usr/local/bin",
                "/custom/sandbox/bin:/usr/bin",
            ),
            # With no override, the daemon's PATH is inherited.
            (
                {"HOME": "/home/user"},
                None,
                "/home/user/.local/bin:/usr/local/bin:/usr/bin:/bin",
                "/home/user/.local/bin:/usr/local/bin:/usr/bin:/bin",
            ),
            # With neither set, the hard-coded fallback is used.
            (
                {"HOME": "/home/user"},
                None,
                None,
                "/usr/local/bin:/usr/bin:/bin",
            ),
        ],
        ids=["caller", "symphony_sandbox_path", "daemon", "fallback"],
    )
    def test_path_resolution(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        env: dict[str, str],
        symphony_sandbox_path: str | None,
        daemon_path: str | None,
        expected: str,
    ) -> None:
        args = _capture_bwrap_args(
            env=env,
            monkeypatch=monkeypatch,
            symphony_sandbox_path=symphony_sandbox_path,
            daemon_path=daemon_path,
            tmp_path=tmp_path,
        )
        assert _extract_path_value(args) == expected
