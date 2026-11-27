"""Unit tests for extra_rw_paths argv construction.

These are pure unit tests — they patch shutil.which (to bypass the bwrap
pre-flight check) and subprocess.Popen (to capture the bwrap command line).
They do NOT require bwrap or any external binary.

Integration tests (marked ``pytest.mark.integration``) at the bottom of
this file actually launch bwrap to verify end-to-end sandbox behaviour.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from symphony_linear.sandbox import run_in_sandbox


class TestAttachmentsArgv:
    """Verify argv construction for attachments_path.

    - When attachments_path is set, ``--ro-bind <path> /tmp/symphony-attachments`` appears.
    - When attachments_path is None, no ``/tmp/symphony-attachments`` mount appears.
    - The mount appears after the per-ticket /tmp bind.
    """

    TMP_DIR = "/fake/ws/TEAM-42/tmp"

    def test_attachments_path_included_after_tmp(self) -> None:
        """A set attachments_path becomes one --ro-bind at /tmp/symphony-attachments.

        It must come after the per-ticket /tmp bind: mounts are applied in
        order, so an earlier /tmp bind would hide the attachments mount.
        """
        with mock.patch(
            "symphony_linear.sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.return_value.returncode = 0
                popen_mock.return_value.communicate.return_value = (b"", b"")

                run_in_sandbox(
                    cmd=["echo", "hi"],
                    workspace_path="/fake/workspace",
                    tmp_path=self.TMP_DIR,
                    hide_paths=[],
                    env={"HOME": "/fake/home"},
                    attachments_path="/fake/ws/TEAM-42/attachments",
                )

                args = popen_mock.call_args[0][0]

                triples = list(zip(args, args[1:], args[2:]))
                attachments_mount = (
                    "--ro-bind",
                    "/fake/ws/TEAM-42/attachments",
                    "/tmp/symphony-attachments",
                )
                assert triples.count(attachments_mount) == 1, (
                    f"Expected exactly one {attachments_mount} in args: {args}"
                )
                assert triples.index(("--bind", self.TMP_DIR, "/tmp")) < (
                    triples.index(attachments_mount)
                ), f"Expected the /tmp bind before the attachments mount: {args}"

    def test_tmp_path_bound_at_tmp(self) -> None:
        """The per-ticket tmp dir is bound at /tmp, not the host /tmp."""
        with mock.patch(
            "symphony_linear.sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.return_value.returncode = 0
                popen_mock.return_value.communicate.return_value = (b"", b"")

                run_in_sandbox(
                    cmd=["echo", "hi"],
                    workspace_path="/fake/workspace",
                    tmp_path=self.TMP_DIR,
                    hide_paths=[],
                    env={"HOME": "/fake/home"},
                )

                args = popen_mock.call_args[0][0]

                # The per-ticket tmp dir must be bound read-write at /tmp.
                triples = list(zip(args, args[1:], args[2:]))
                assert ("--bind", self.TMP_DIR, "/tmp") in triples, (
                    f"Expected --bind {self.TMP_DIR} /tmp in args: {args}"
                )
                # The host /tmp must NOT be bound directly anymore.
                assert ("--bind", "/tmp", "/tmp") not in triples, (
                    f"Host /tmp must not be bound inside the sandbox: {args}"
                )

    def test_attachments_path_absent_when_none(self) -> None:
        """When attachments_path is None, no /tmp/symphony-attachments mount appears."""
        with mock.patch(
            "symphony_linear.sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.return_value.returncode = 0
                popen_mock.return_value.communicate.return_value = (b"", b"")

                run_in_sandbox(
                    cmd=["echo", "hi"],
                    workspace_path="/fake/workspace",
                    tmp_path=self.TMP_DIR,
                    hide_paths=[],
                    env={"HOME": "/fake/home"},
                    # attachments_path omitted (defaults to None)
                )

                args = popen_mock.call_args[0][0]

                # /tmp/symphony-attachments should not appear anywhere in the args
                assert "/tmp/symphony-attachments" not in args, (
                    f"Unexpected /tmp/symphony-attachments in args: {args}"
                )


# ---------------------------------------------------------------------------
# Existing tests
# ---------------------------------------------------------------------------


class TestExtraRWPathsArgv:
    """Verify argv construction for extra_rw_paths.

    - extra_rw_paths must use --bind, not --bind-try.
    - extra_rw_paths binds must appear before hide_paths in argv
      (so hide wins on collision — later bwrap mounts override earlier).
    """

    def test_bind_not_bind_try(self) -> None:
        """extra_rw_paths must use --bind, not --bind-try."""
        with mock.patch(
            "symphony_linear.sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.return_value.returncode = 0
                popen_mock.return_value.communicate.return_value = (b"", b"")

                run_in_sandbox(
                    cmd=["echo", "hi"],
                    workspace_path="/fake/workspace",
                    tmp_path="/fake/ws/TEAM-42/tmp",
                    hide_paths=[],
                    env={"HOME": "/fake/home"},
                    extra_rw_paths=["/extra/a", "/extra/b"],
                )

                args = popen_mock.call_args[0][0]  # bwrap_args list
                # Check that --bind appears for our paths (not --bind-try)
                assert ("--bind", "/extra/a", "/extra/a") in zip(
                    args, args[1:], args[2:]
                )
                assert ("--bind", "/extra/b", "/extra/b") in zip(
                    args, args[1:], args[2:]
                )
                # Verify --bind-try is NOT used for extra paths
                for i, a in enumerate(args):
                    if a == "--bind-try":
                        assert args[i + 1] not in ("/extra/a", "/extra/b")

    def test_binds_before_hide(self, tmp_path: Path) -> None:
        """extra_rw_paths binds must appear before hide_paths args so
        hide wins on collision."""
        # Use a real temp dir for hide_paths so --tmpfs actually appears in args.
        hide_dir = tmp_path / "hide_me"
        hide_dir.mkdir()

        with mock.patch(
            "symphony_linear.sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.return_value.returncode = 0
                popen_mock.return_value.communicate.return_value = (b"", b"")

                run_in_sandbox(
                    cmd=["echo", "hi"],
                    workspace_path="/fake/workspace",
                    tmp_path="/fake/ws/TEAM-42/tmp",
                    hide_paths=[str(hide_dir)],
                    env={"HOME": "/fake/home"},
                    extra_rw_paths=["/extra/collide"],
                )

                args = popen_mock.call_args[0][0]
                # Locate the exact --bind triple for our extra path.
                triples = list(zip(args, args[1:], args[2:]))
                extra_target = ("--bind", "/extra/collide", "/extra/collide")
                assert extra_target in triples, (
                    f"Extra RW --bind triple not found in args: {args}"
                )
                extra_idx = triples.index(extra_target)
                # Locate the --tmpfs for our hide path.
                hide_target = ("--tmpfs", str(hide_dir))
                assert hide_target in zip(args, args[1:]), (
                    f"Hide --tmpfs pair not found in args: {args}"
                )
                hide_idx = list(zip(args, args[1:])).index(hide_target)
                assert extra_idx < hide_idx, (
                    f"Extra RW --bind at index {extra_idx} should be before "
                    f"hide --tmpfs at index {hide_idx}: {args}"
                )


# ---------------------------------------------------------------------------
# Integration tests — actually launch bwrap
# ---------------------------------------------------------------------------


def _bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def _require_bwrap() -> None:
    if not _bwrap_available():
        pytest.skip("bwrap not available")


@pytest.mark.integration
class TestAttachmentsSandboxIntegration:
    """Verify that attachments_path is correctly mounted inside the sandbox
    using the real bwrap binary.
    """

    def test_attachments_mount_visible(self, tmp_path: Path) -> None:
        """A file in the attachments dir is readable at
        /tmp/symphony-attachments/<filename> inside the sandbox."""
        _require_bwrap()

        # Create a host-side attachments directory with a test file.
        attachments_dir = tmp_path / "attachments"
        attachments_dir.mkdir()
        test_file = attachments_dir / "hello.txt"
        test_file.write_text("hello from attachments")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        ticket_tmp = tmp_path / "ticket-tmp"
        ticket_tmp.mkdir()

        # Launch bwrap with attachments_path set and cat the file.
        proc = run_in_sandbox(
            cmd=["cat", "/tmp/symphony-attachments/hello.txt"],
            workspace_path=str(workspace),
            tmp_path=str(ticket_tmp),
            hide_paths=[],
            env={"HOME": str(Path.home())},
            attachments_path=str(attachments_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = proc.communicate(timeout=30)
        output = stdout.decode(errors="replace").strip()

        assert proc.returncode == 0, (
            f"Sandbox failed with exit code {proc.returncode}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )
        assert output == "hello from attachments", (
            f"Expected 'hello from attachments', got: {output}"
        )
