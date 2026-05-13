"""Unit tests for extra_rw_paths argv construction.

These are pure unit tests — they patch shutil.which (to bypass the bwrap
pre-flight check) and subprocess.Popen (to capture the bwrap command line).
They do NOT require bwrap or any external binary.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock


from symphony_lite.sandbox import run_in_sandbox


class TestExtraRWPathsArgv:
    """Verify argv construction for extra_rw_paths.

    - extra_rw_paths must use --bind, not --bind-try.
    - extra_rw_paths binds must appear before hide_paths in argv
      (so hide wins on collision — later bwrap mounts override earlier).
    """

    def test_bind_not_bind_try(self) -> None:
        """extra_rw_paths must use --bind, not --bind-try."""
        with mock.patch(
            "symphony_lite.sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.return_value.returncode = 0
                popen_mock.return_value.communicate.return_value = (b"", b"")

                run_in_sandbox(
                    cmd=["echo", "hi"],
                    workspace_path="/fake/workspace",
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
            "symphony_lite.sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.return_value.returncode = 0
                popen_mock.return_value.communicate.return_value = (b"", b"")

                run_in_sandbox(
                    cmd=["echo", "hi"],
                    workspace_path="/fake/workspace",
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

    def test_extra_rw_none_omitted(self) -> None:
        """When extra_rw_paths is None/empty, no extra --bind args added."""
        with mock.patch(
            "symphony_lite.sandbox.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.return_value.returncode = 0
                popen_mock.return_value.communicate.return_value = (b"", b"")

                run_in_sandbox(
                    cmd=["echo", "hi"],
                    workspace_path="/fake/workspace",
                    hide_paths=[],
                    env={"HOME": "/fake/home"},
                    extra_rw_paths=None,
                )

                args = popen_mock.call_args[0][0]
                # Only 2 --bind: workspace and /tmp (no extras)
                assert args.count("--bind") == 2
