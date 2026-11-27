"""Unit + integration tests for sandbox dir_map argv construction.

dir_map is the mirror image of extra_rw_paths: its binds are emitted AFTER
the hide_paths block (unlike extra_rw_paths, which goes before), so an
explicit mapping can punch through a broad hide — later bwrap mounts win.

The unit tests patch shutil.which (to bypass the bwrap pre-flight check) and
subprocess.Popen (to capture the bwrap command line). They do NOT require
bwrap. The integration tests at the bottom actually launch bwrap.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from symphony_linear.sandbox import run_in_sandbox


class TestDirMapArgv:
    """Verify argv construction for dir_map.

    - dir_map pairs must use --bind <host_source> <sandbox_dest>, with the
      two sides distinct (unlike extra_rw_paths, source and destination
      differ).
    - dir_map binds must appear after hide_paths args in argv
      (the inverse of extra_rw_paths, so the mapping wins on collision).
    - When dir_map is None/empty, no extra --bind args are added.
    """

    def test_bind_pairs_emitted_with_distinct_sides(self) -> None:
        """Each (host_source, sandbox_dest) pair is emitted as a --bind."""
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
                    dir_map=[("/host/src/npm", "/home/user/.config/npm")],
                )

                args = popen_mock.call_args[0][0]
                triples = list(zip(args, args[1:], args[2:]))
                assert (
                    "--bind",
                    "/host/src/npm",
                    "/home/user/.config/npm",
                ) in triples, f"dir_map --bind triple not found in args: {args}"
                # --bind-try must not be used for dir_map pairs.
                for i, a in enumerate(args):
                    if a == "--bind-try":
                        assert args[i + 1] != "/host/src/npm"

    def test_binds_after_hide(self, tmp_path: Path) -> None:
        """dir_map binds must appear after hide_paths args so the mapping
        wins on collision (the mirror image of extra_rw_paths)."""
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
                    dir_map=[("/host/src/npm", "/sandbox/npm")],
                )

                args = popen_mock.call_args[0][0]
                triples = list(zip(args, args[1:], args[2:]))
                dir_map_target = ("--bind", "/host/src/npm", "/sandbox/npm")
                assert dir_map_target in triples, (
                    f"dir_map --bind triple not found in args: {args}"
                )
                dir_map_idx = triples.index(dir_map_target)
                # Locate the --tmpfs for our hide path.
                hide_target = ("--tmpfs", str(hide_dir))
                assert hide_target in zip(args, args[1:]), (
                    f"Hide --tmpfs pair not found in args: {args}"
                )
                hide_idx = list(zip(args, args[1:])).index(hide_target)
                assert hide_idx < dir_map_idx, (
                    f"dir_map --bind at index {dir_map_idx} should be after "
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
class TestDirMapSandboxIntegration:
    """Verify dir_map mounts end-to-end with the real bwrap binary."""

    def test_dir_map_punches_through_hide(self, tmp_path: Path) -> None:
        """A dir_map entry under a hidden parent dir is visible and writable,
        while the rest of the hidden parent stays concealed."""
        _require_bwrap()

        # Parent dir that gets hidden, containing a decoy file and the
        # pre-created mount-point dir (run_in_sandbox never creates dirs).
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "decoy.txt").write_text("secret")
        (config_dir / "npm").mkdir()

        # Host source with a marker file.
        host_src = tmp_path / "mounts-npm"
        host_src.mkdir()
        (host_src / "marker.txt").write_text("mapped-content")

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        ticket_tmp = tmp_path / "ticket-tmp"
        ticket_tmp.mkdir()

        proc = run_in_sandbox(
            cmd=[
                "bash",
                "-c",
                f'cat "{config_dir}/npm/marker.txt" && '
                f'ls "{config_dir}" && '
                f'touch "{config_dir}/npm/wrote.txt" && echo "WRITE_OK"',
            ],
            workspace_path=str(workspace),
            tmp_path=str(ticket_tmp),
            hide_paths=[str(config_dir)],
            env={"HOME": str(Path.home())},
            dir_map=[(str(host_src), str(config_dir / "npm"))],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = proc.communicate(timeout=30)
        output = stdout.decode(errors="replace")

        assert proc.returncode == 0, (
            f"Sandbox failed with exit code {proc.returncode}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )
        # The mapped dir shows the host source contents.
        assert "mapped-content" in output, f"Expected mapped content, got: {output}"
        # The mapped dir is writable inside the sandbox...
        assert "WRITE_OK" in output, f"Expected WRITE_OK, got: {output}"
        # ...and the write landed in the host source.
        assert (host_src / "wrote.txt").exists()
        # The decoy file was hidden by the tmpfs over the parent.
        assert "decoy.txt" not in output, f"Decoy leaked through the hide: {output}"
