"""Unit tests for the checkout watcher and restart validation."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from symphony_linear.checkout import CheckoutWatcher, validate_checkout


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# ---------------------------------------------------------------------------
# CheckoutWatcher
# ---------------------------------------------------------------------------


class TestCheckoutWatcher:
    def test_reads_head_at_construction(self, tmp_path: Path) -> None:
        with mock.patch(
            "symphony_linear.checkout.subprocess.run",
            return_value=_completed(0, "abc123\n"),
        ) as mock_run:
            watcher = CheckoutWatcher(package_dir=tmp_path)

        assert watcher.disabled is False
        assert watcher.head == "abc123"
        # The first git command resolves HEAD against the package directory;
        # the second confirms the package is a tracked part of the checkout.
        assert mock_run.call_args_list[0].args[0] == [
            "git",
            "-C",
            str(tmp_path),
            "rev-parse",
            "HEAD",
        ]
        assert mock_run.call_args_list[1].args[0] == [
            "git",
            "-C",
            str(tmp_path),
            "ls-files",
            "--error-unmatch",
            "__init__.py",
        ]

    @pytest.mark.parametrize(
        "run_kwargs",
        [
            # git is not installed at all.
            {"side_effect": FileNotFoundError("git not found")},
            # Not a git repository.
            {"return_value": _completed(128, stderr="fatal: not a git repository")},
            # git reports an empty HEAD.
            {"return_value": _completed(0, "   \n")},
            # A wheel installed into a repo's own .venv: rev-parse succeeds
            # because the package sits under the checkout, but its
            # __init__.py is not in the index, so it is not the checkout's
            # code.
            {
                "side_effect": [
                    _completed(0, "abc123\n"),
                    _completed(1, stderr="error: pathspec '__init__.py' did not match"),
                ]
            },
        ],
        ids=["git-missing", "not-a-checkout", "blank-head", "untracked-package"],
    )
    def test_disabled_when_not_a_tracked_checkout(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        run_kwargs: dict[str, Any],
    ) -> None:
        with mock.patch("symphony_linear.checkout.subprocess.run", **run_kwargs):
            with caplog.at_level(logging.INFO, logger="symphony_linear.checkout"):
                watcher = CheckoutWatcher(package_dir=tmp_path)

        assert watcher.disabled is True
        assert watcher.head is None
        assert "auto-restart on checkout change is off" in caplog.text


# ---------------------------------------------------------------------------
# validate_checkout
# ---------------------------------------------------------------------------


class TestValidateCheckout:
    def test_success_runs_validate_config(self, tmp_path: Path) -> None:
        with mock.patch(
            "symphony_linear.checkout.subprocess.run",
            return_value=_completed(0, "Config is valid.\n"),
        ) as mock_run:
            assert validate_checkout(tmp_path) is True

        cmd = mock_run.call_args.args[0]
        assert cmd == [
            sys.executable,
            "-m",
            "symphony_linear",
            "--validate-config",
            "--workspace",
            str(tmp_path),
        ]
        assert mock_run.call_args.kwargs["timeout"] == 60

    @pytest.mark.parametrize(
        ("run_kwargs", "log_parts"),
        [
            (
                {"return_value": _completed(1, stdout="boom", stderr="details")},
                ("Checkout validation failed (rc=1)", "boom", "details"),
            ),
            (
                {
                    "side_effect": subprocess.TimeoutExpired(
                        cmd="x", timeout=60, output="partial"
                    )
                },
                ("timed out", "partial"),
            ),
            (
                {"side_effect": OSError("cannot exec")},
                ("could not run",),
            ),
        ],
        ids=["non-zero-exit", "timeout", "oserror"],
    )
    def test_failure_is_reported(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        run_kwargs: dict[str, Any],
        log_parts: tuple[str, ...],
    ) -> None:
        with mock.patch("symphony_linear.checkout.subprocess.run", **run_kwargs):
            with caplog.at_level(logging.ERROR, logger="symphony_linear.checkout"):
                assert validate_checkout(tmp_path) is False

        for part in log_parts:
            assert part in caplog.text
