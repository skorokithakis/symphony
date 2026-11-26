"""Unit tests for the unsandboxed launch mode of ``run_in_sandbox``.

These need no bwrap: they exercise the fallback the CLI turns on for an
interactive web UI trial on a host without bubblewrap.  Only the environment,
the working directory and the process-group kill are asserted; the bwrap argv
is covered by the sandboxed tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from symphony_linear import sandbox
from symphony_linear.sandbox import run_in_sandbox


@pytest.fixture(autouse=True)
def _restore_sandbox_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """``disable_sandbox`` mutates a module global, so restore it afterwards."""
    monkeypatch.setattr("symphony_linear.sandbox._SANDBOX_ENABLED", True)


def _make_ticket_directories(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ticket_tmp = tmp_path / "ticket-tmp"
    ticket_tmp.mkdir()
    return workspace, ticket_tmp


def _process_is_gone(pid: int) -> bool:
    # A killed grandchild is reaped by init, not by this process, so it can be
    # a zombie for a moment.  A zombie has already received its signal, which
    # is what the test is about, so treat it as gone.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


class TestUnsandboxedLaunch:
    def test_runs_with_a_clean_environment_tmpdir_and_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, ticket_tmp = _make_ticket_directories(tmp_path)
        sandbox_bin = tmp_path / "sandbox-bin"
        sandbox_bin.mkdir()
        sh_path = shutil.which("sh")
        assert sh_path is not None
        (sandbox_bin / "sh").symlink_to(sh_path)
        monkeypatch.setenv("HOST_ONLY_VARIABLE", "should-not-leak")
        monkeypatch.setenv("SYMPHONY_SANDBOX_PATH", str(sandbox_bin))
        sandbox.disable_sandbox()

        proc = run_in_sandbox(
            cmd=[
                "sh",
                "-c",
                'printf "%s|%s|%s|%s" '
                '"${HOST_ONLY_VARIABLE-<unset>}" "$TMPDIR" "$PWD" "$PATH"',
            ],
            workspace_path=str(workspace),
            tmp_path=str(ticket_tmp),
            hide_paths=["~/.ssh"],
            env={"HOME": str(tmp_path / "home")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate(timeout=30)

        assert proc.returncode == 0, stderr.decode(errors="replace")
        assert stdout.decode() == "|".join(
            [
                "<unset>",
                os.path.realpath(ticket_tmp),
                os.path.realpath(workspace),
                str(sandbox_bin),
            ]
        )

    def test_kill_kills_the_whole_process_group(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A grandchild survives a leader-only kill, so the group must be hit."""
        workspace, ticket_tmp = _make_ticket_directories(tmp_path)
        sandbox.disable_sandbox()

        proc = run_in_sandbox(
            cmd=["sh", "-c", "sleep 30 & echo $!; wait"],
            workspace_path=str(workspace),
            tmp_path=str(ticket_tmp),
            hide_paths=[],
            env={"HOME": str(tmp_path / "home")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        grandchild = int(proc.stdout.readline())

        proc.kill()
        assert proc.wait(timeout=10) != 0

        deadline = time.monotonic() + 5
        while not _process_is_gone(grandchild):
            if time.monotonic() > deadline:
                pytest.fail(f"grandchild {grandchild} survived the group kill")
            time.sleep(0.05)
