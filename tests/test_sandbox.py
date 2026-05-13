"""Integration tests for the bwrap sandbox wrapper.

These tests require ``bwrap`` to be installed and user namespaces to be
available.  They are marked ``pytest.mark.integration`` so they can be
skipped on CI environments where bwrap is not available:

.. code-block:: bash

    pytest -m "not integration"   # skip integration tests
    pytest -m integration         # run only integration tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from symphony_linear.sandbox import run_in_sandbox

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
SMOKE_SCRIPT = FIXTURE_DIR / "smoke_test.sh"


def _bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def _require_bwrap() -> None:
    if not _bwrap_available():
        pytest.skip("bwrap not available")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunInSandbox:
    """Test the basic sandbox launch and smoke-test fixture."""

    def test_smoke_fixture_passes(self, tmp_path: Path) -> None:
        """Run the smoke-test fixture inside the sandbox and verify it exits 0."""
        _require_bwrap()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        hide = [
            str(Path("~/.ssh").expanduser()),
            str(Path("~/.gnupg").expanduser()),
            "/run/docker.sock",  # real path after symlink resolution
            "/var/run/docker.sock",  # will be expanded to /run/docker.sock
        ]

        env = {
            "HOME": str(Path.home()),
            "SMOKE_WORKSPACE": str(workspace),
            "SMOKE_HIDE_PATHS": ":".join(hide),
        }

        proc = run_in_sandbox(
            cmd=["bash", str(SMOKE_SCRIPT)],
            workspace_path=str(workspace),
            hide_paths=hide,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = proc.communicate(timeout=60)
        exit_code = proc.returncode

        # Print for debugging on failure
        if exit_code != 0:
            print(f"--- STDOUT ---\n{stdout.decode(errors='replace')}")
            print(f"--- STDERR ---\n{stderr.decode(errors='replace')}")

        assert exit_code == 0, (
            f"Smoke test failed with exit code {exit_code}\n"
            f"stdout:\n{stdout.decode(errors='replace')}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )

    def test_workspace_isolation(self, tmp_path: Path) -> None:
        """Verify the workspace is writable but /etc is not."""
        _require_bwrap()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        proc = run_in_sandbox(
            cmd=[
                "bash",
                "-c",
                (
                    f'echo ok > "{workspace}/test" && '
                    f'echo fail > /etc/test 2>/dev/null && echo "FAILED" || echo "PASS"'
                ),
            ],
            workspace_path=str(workspace),
            hide_paths=[],
            env={"HOME": str(Path.home())},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = proc.communicate(timeout=30)
        output = stdout.decode(errors="replace").strip()

        assert proc.returncode == 0
        assert "PASS" in output, f"Expected PASS, got: {output}"
        assert (workspace / "test").read_text().strip() == "ok"

    def test_paths_are_masked(self, tmp_path: Path) -> None:
        """Verify that hide_paths are effectively masked inside the sandbox."""
        _require_bwrap()

        # Create a dummy directory and file on the host to test masking
        secret_dir = tmp_path / "secret_dir"
        secret_dir.mkdir()
        (secret_dir / "key.txt").write_text("super-secret")

        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("super-secret")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        hide = [str(secret_dir), str(secret_file)]

        proc = run_in_sandbox(
            cmd=[
                "bash",
                "-c",
                (
                    # Check directory is empty/doesn't contain our file
                    f'if [[ -f "{secret_dir}/key.txt" ]]; then echo "DIR_VISIBLE"; '
                    f'elif [[ -d "{secret_dir}" ]]; then '
                    f'  count=$(ls -A "{secret_dir}" 2>/dev/null | wc -l); '
                    f'  [[ $count -eq 0 ]] && echo "DIR_EMPTY" || echo "DIR_NONEMPTY"; '
                    f'else echo "DIR_MISSING"; fi; '
                    # Check file is replaced with /dev/null
                    f'if [[ -c "{secret_file}" ]]; then echo "FILE_DEVD"; '
                    f'elif [[ -f "{secret_file}" ]]; then echo "FILE_VISIBLE"; '
                    f'else echo "FILE_MISSING"; fi'
                ),
            ],
            workspace_path=str(workspace),
            hide_paths=hide,
            env={"HOME": str(Path.home())},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = proc.communicate(timeout=30)
        output = stdout.decode(errors="replace").strip()

        assert proc.returncode == 0, f"stderr: {stderr.decode(errors='replace')}"
        assert "DIR_VISIBLE" not in output, f"Secret dir contents visible: {output}"
        assert "FILE_VISIBLE" not in output, f"Secret file contents visible: {output}"
        # Directory should be empty (tmpfs overlaid)
        assert "DIR_EMPTY" in output, f"Expected DIR_EMPTY, got: {output}"
        # File should be a character device (/dev/null)
        assert "FILE_DEVD" in output, f"Expected FILE_DEVD, got: {output}"

    def test_popen_handle_is_usable(self, tmp_path: Path) -> None:
        """Verify the returned Popen handle can be polled, waited, and killed."""
        _require_bwrap()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        proc = run_in_sandbox(
            cmd=["sleep", "30"],
            workspace_path=str(workspace),
            hide_paths=[],
            env={"HOME": str(Path.home())},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Poll should return None while running
        assert proc.poll() is None

        # Kill should work
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("Process did not die after kill()")

        # After wait, returncode should be negative (killed by signal)
        assert proc.returncode is not None
        assert proc.returncode != 0

    def test_bwrap_not_found_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify a clear error when bwrap is not on PATH."""
        # Remove bwrap from PATH entirely
        monkeypatch.setenv("PATH", "/nonexistent")

        with pytest.raises(FileNotFoundError, match="bwrap.*required"):
            run_in_sandbox(
                cmd=["echo", "hello"],
                workspace_path="/tmp",
                hide_paths=[],
                env={},
            )

    def test_custom_env_passed(self, tmp_path: Path) -> None:
        """Verify environment variables are passed cleanly into the sandbox."""
        _require_bwrap()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        proc = run_in_sandbox(
            cmd=["bash", "-c", 'echo "MYVAR=$MYVAR"; echo "EXTRA=$EXTRA"'],
            workspace_path=str(workspace),
            hide_paths=[],
            env={
                "MYVAR": "hello-world",
                "EXTRA": "sandboxed",
                "HOME": str(Path.home()),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = proc.communicate(timeout=30)
        output = stdout.decode(errors="replace")

        assert proc.returncode == 0
        assert "MYVAR=hello-world" in output
        assert "EXTRA=sandboxed" in output

    def test_host_env_not_leaked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify host environment is NOT leaked into the sandbox."""
        _require_bwrap()

        # Set a unique env var on the host
        monkeypatch.setenv("HOST_ONLY_VAR", "should-not-leak")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        proc = run_in_sandbox(
            cmd=["bash", "-c", 'echo "HOST_ONLY_VAR=${HOST_ONLY_VAR:-<unset>}"'],
            workspace_path=str(workspace),
            hide_paths=[],
            env={
                "HOME": str(Path.home()),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = proc.communicate(timeout=30)
        output = stdout.decode(errors="replace")

        assert proc.returncode == 0
        assert "HOST_ONLY_VAR=<unset>" in output, (
            f"Host environment leaked into sandbox: {output}"
        )

    def test_tilde_expansion_in_hide_paths(self, tmp_path: Path) -> None:
        """Verify that tilde-prefixed hide paths are expanded correctly."""
        _require_bwrap()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        home = str(Path.home())
        # Make a test directory under home to hide
        test_dir = Path(home) / ".symphony_smoke_test_dir"
        test_dir.mkdir(exist_ok=True)
        (test_dir / "secret").write_text("hidden")
        try:
            proc = run_in_sandbox(
                cmd=[
                    "bash",
                    "-c",
                    f'if [[ -f "{test_dir}/secret" ]]; then echo "VISIBLE"; else echo "MASKED"; fi',
                ],
                workspace_path=str(workspace),
                hide_paths=["~/.symphony_smoke_test_dir"],
                env={"HOME": home},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, stderr = proc.communicate(timeout=30)
            output = stdout.decode(errors="replace").strip()

            assert proc.returncode == 0
            assert output == "MASKED", f"Expected MASKED, got: {output}"
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_bind_try_for_cache_dirs(self, tmp_path: Path) -> None:
        """Verify that --bind-try for .cache and .local/share does not fail
        when those directories are missing on the host."""
        _require_bwrap()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Run with a fake HOME pointing to a tmp dir that has no .cache
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        # Override HOME to point to the fake home that has no .cache
        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(fake_home)
            proc = run_in_sandbox(
                cmd=["bash", "-c", "echo 'sandbox-ok'"],
                workspace_path=str(workspace),
                hide_paths=[],
                env={"HOME": str(fake_home)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, stderr = proc.communicate(timeout=30)
            output = stdout.decode(errors="replace").strip()

            assert proc.returncode == 0, (
                f"Sandbox failed: stderr={stderr.decode(errors='replace')}"
            )
            assert output == "sandbox-ok"
        finally:
            if old_home:
                os.environ["HOME"] = old_home

    def test_opencode_dirs_writable(self, tmp_path: Path) -> None:
        """Verify that ~/.opencode and ~/.local/share/opencode are writable
        inside the sandbox when they exist on the host."""
        _require_bwrap()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        home = str(Path.home())

        # Ensure both directories exist on the host so --bind-try actually binds them.
        opencode_legacy = Path(home) / ".opencode"
        opencode_xdg = Path(home) / ".local" / "share" / "opencode"

        created: list[Path] = []
        for d in [opencode_legacy, opencode_xdg]:
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                created.append(d)

        try:
            proc = run_in_sandbox(
                cmd=[
                    "bash",
                    "-c",
                    (
                        f'touch "{opencode_legacy}/.writetest" 2>&1 && echo "LEGACY_OK" || echo "LEGACY_FAIL";'
                        f'touch "{opencode_xdg}/.writetest" 2>&1 && echo "XDG_OK" || echo "XDG_FAIL";'
                        f'rm -f "{opencode_legacy}/.writetest" "{opencode_xdg}/.writetest"'
                    ),
                ],
                workspace_path=str(workspace),
                hide_paths=[],
                env={"HOME": home},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, stderr = proc.communicate(timeout=30)
            output = stdout.decode(errors="replace").strip()

            assert proc.returncode == 0, (
                f"Sandbox failed: stderr={stderr.decode(errors='replace')}"
            )
            assert "LEGACY_OK" in output, f"~/.opencode not writable: {output}"
            assert "XDG_OK" in output, f"~/.local/share/opencode not writable: {output}"
        finally:
            for d in created:
                shutil.rmtree(d, ignore_errors=True)

    def test_opencode_dirs_missing_ok(self, tmp_path: Path) -> None:
        """Verify that --bind-try for ~/.opencode and ~/.local/share/opencode
        does not fail when those directories are missing on the host."""
        _require_bwrap()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        # Override HOME so path expansion points to the fake home.
        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(fake_home)
            proc = run_in_sandbox(
                cmd=["bash", "-c", "echo 'sandbox-ok'"],
                workspace_path=str(workspace),
                hide_paths=[],
                env={"HOME": str(fake_home)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, stderr = proc.communicate(timeout=30)
            output = stdout.decode(errors="replace").strip()

            assert proc.returncode == 0, (
                f"Sandbox failed when OpenCode dirs are missing: "
                f"stderr={stderr.decode(errors='replace')}"
            )
            assert output == "sandbox-ok"
        finally:
            if old_home:
                os.environ["HOME"] = old_home

    def test_extra_rw_paths_writable(self, tmp_path: Path) -> None:
        """Verify that extra_rw_paths are writable inside the sandbox."""
        _require_bwrap()

        extra_dir = tmp_path / "extra_rw"
        extra_dir.mkdir()
        test_file = extra_dir / "test.txt"

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        proc = run_in_sandbox(
            cmd=[
                "bash",
                "-c",
                f'touch "{test_file}" && echo "WRITABLE" || echo "NOT_WRITABLE"',
            ],
            workspace_path=str(workspace),
            hide_paths=[],
            env={"HOME": str(Path.home())},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            extra_rw_paths=[str(extra_dir)],
        )

        stdout, stderr = proc.communicate(timeout=30)
        output = stdout.decode(errors="replace").strip()

        assert proc.returncode == 0, (
            f"Sandbox failed: stderr={stderr.decode(errors='replace')}"
        )
        assert "WRITABLE" in output, f"Expected WRITABLE, got: {output}"
        assert test_file.exists(), f"File not created: {test_file}"
