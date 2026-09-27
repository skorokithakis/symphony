"""Unit tests for the checkout watcher and restart validation."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
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
    def test_reads_head_at_construction(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
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
        assert "auto-restart" not in caplog.text

    def test_not_a_checkout_is_disabled(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with mock.patch(
            "symphony_linear.checkout.subprocess.run",
            return_value=_completed(128, stderr="fatal: not a git repository"),
        ):
            with caplog.at_level(logging.INFO, logger="symphony_linear.checkout"):
                watcher = CheckoutWatcher(package_dir=tmp_path)

        assert watcher.disabled is True
        assert watcher.head is None
        assert "auto-restart on checkout change is off" in caplog.text

    def test_untracked_package_in_checkout_is_disabled(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A wheel installed into a repo's own .venv resolves HEAD but is off.

        ``rev-parse`` succeeds because the package sits under the checkout, but
        its ``__init__.py`` is not in the index, so it is not the checkout's
        code.
        """
        with mock.patch(
            "symphony_linear.checkout.subprocess.run",
            side_effect=[
                _completed(0, "abc123\n"),
                _completed(1, stderr="error: pathspec '__init__.py' did not match"),
            ],
        ):
            with caplog.at_level(logging.INFO, logger="symphony_linear.checkout"):
                watcher = CheckoutWatcher(package_dir=tmp_path)

        assert watcher.disabled is True
        assert watcher.head is None
        assert "auto-restart on checkout change is off" in caplog.text

    def test_git_missing_is_disabled(self, tmp_path: Path) -> None:
        with mock.patch(
            "symphony_linear.checkout.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            watcher = CheckoutWatcher(package_dir=tmp_path)

        assert watcher.disabled is True
        assert watcher.head is None

    def test_blank_head_is_disabled(self, tmp_path: Path) -> None:
        with mock.patch(
            "symphony_linear.checkout.subprocess.run",
            return_value=_completed(0, "   \n"),
        ):
            watcher = CheckoutWatcher(package_dir=tmp_path)

        assert watcher.disabled is True
        assert watcher.head is None


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

    def test_failure_logs_output_tail(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with mock.patch(
            "symphony_linear.checkout.subprocess.run",
            return_value=_completed(1, stdout="boom", stderr="details"),
        ):
            with caplog.at_level(logging.ERROR, logger="symphony_linear.checkout"):
                assert validate_checkout(tmp_path) is False

        assert "Checkout validation failed (rc=1)" in caplog.text
        assert "boom" in caplog.text
        assert "details" in caplog.text

    def test_timeout_is_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        exc = subprocess.TimeoutExpired(cmd="x", timeout=60, output="partial")
        with mock.patch("symphony_linear.checkout.subprocess.run", side_effect=exc):
            with caplog.at_level(logging.ERROR, logger="symphony_linear.checkout"):
                assert validate_checkout(tmp_path) is False

        assert "timed out" in caplog.text
        assert "partial" in caplog.text

    def test_oserror_is_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with mock.patch(
            "symphony_linear.checkout.subprocess.run",
            side_effect=OSError("cannot exec"),
        ):
            with caplog.at_level(logging.ERROR, logger="symphony_linear.checkout"):
                assert validate_checkout(tmp_path) is False

        assert "could not run" in caplog.text
