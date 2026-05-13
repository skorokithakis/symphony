"""Tests for the workspace lifecycle module.

Unit tests cover sanitization, path containment, and typed exceptions.
Integration tests (marked ``@pytest.mark.integration``) exercise the full
``prepare`` / ``remove`` cycle against a real git repository and sandbox.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from symphony_lite.workspace import (
    BranchFailed,
    CloneFailed,
    PathContainmentError,
    ServeScriptMissing,
    SetupFailed,
    WorkspaceError,
    _check_containment,
    _sanitize_identifier,
    prepare,
    remove,
    start_serve,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def _git_available() -> bool:
    return shutil.which("git") is not None


def _require_git() -> None:
    if not _git_available():
        pytest.skip("git not available")


def _require_bwrap() -> None:
    if not _bwrap_available():
        pytest.skip("bwrap not available")


def _run_git(args: list[str], cwd: Path) -> None:
    """Run a git command, raising on failure."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr}"
        )


def _make_source_repo(path: Path, *, setup_script: str | None = None) -> None:
    """Create a minimal git repository at *path* with one commit.

    If *setup_script* is provided, it is written as ``.symphony/setup`` and
    made executable.
    """
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-b", "main"], cwd=path)
    _run_git(["config", "user.email", "test@symphony.local"], cwd=path)
    _run_git(["config", "user.name", "Test User"], cwd=path)
    (path / "README.md").write_text("# Test Repo\n")
    _run_git(["add", "README.md"], cwd=path)
    _run_git(["commit", "-m", "initial commit"], cwd=path)

    if setup_script is not None:
        setup_dir = path / ".symphony"
        setup_dir.mkdir(exist_ok=True)
        setup_file = setup_dir / "setup"
        setup_file.write_text(setup_script)
        setup_file.chmod(setup_file.stat().st_mode | stat.S_IEXEC)
        _run_git(["add", ".symphony/setup"], cwd=path)
        _run_git(["commit", "-m", "add setup script"], cwd=path)


# ---------------------------------------------------------------------------
# Unit: _sanitize_identifier
# ---------------------------------------------------------------------------


class TestSanitizeIdentifier:
    """Sanitization replaces unsafe characters with ``_``."""

    def test_alphanumeric_unchanged(self) -> None:
        assert _sanitize_identifier("ABC-123") == "ABC-123"

    def test_dots_and_underscores_unchanged(self) -> None:
        assert _sanitize_identifier("team_1.0-release") == "team_1.0-release"

    def test_spaces_replaced(self) -> None:
        assert _sanitize_identifier("team 42") == "team_42"

    def test_slashes_replaced(self) -> None:
        assert _sanitize_identifier("a/b/c") == "a_b_c"

    def test_special_chars_replaced(self) -> None:
        # @ is between hello and world; ! # $ % follow world — 5 total replacements
        assert _sanitize_identifier("hello@world!#$%") == "hello_world____"

    def test_path_traversal_replaced(self) -> None:
        # Dots are allowed; slashes become underscores.
        assert _sanitize_identifier("../../etc") == ".._.._etc"

    def test_empty_string(self) -> None:
        assert _sanitize_identifier("") == ""


# ---------------------------------------------------------------------------
# Unit: _check_containment
# ---------------------------------------------------------------------------


class TestCheckContainment:
    """Path containment rejects escapes; passes valid paths."""

    def test_valid_subdirectory(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        child = root / "ticket-1"
        result = _check_containment(str(child), str(root))
        assert result == os.path.realpath(child)

    def test_path_equals_root(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        result = _check_containment(str(root), str(root))
        assert result == os.path.realpath(root)

    def test_dot_dot_escape_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        escape = root / ".." / "etc"
        with pytest.raises(PathContainmentError):
            _check_containment(str(escape), str(root))

    def test_dot_dot_in_middle_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        escape = root / "sub" / ".." / ".." / "etc"
        with pytest.raises(PathContainmentError):
            _check_containment(str(escape), str(root))

    def test_absolute_escape_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        with pytest.raises(PathContainmentError):
            _check_containment("/etc/passwd", str(root))

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        escape_target = tmp_path / "outside"
        escape_target.mkdir()
        symlink = root / "link"
        symlink.symlink_to(escape_target)
        with pytest.raises(PathContainmentError):
            _check_containment(str(symlink), str(root))

    def test_path_does_not_exist_yet(self, tmp_path: Path) -> None:
        """Containment is checked before the directory is created."""
        root = tmp_path / "ws"
        root.mkdir()
        future = root / "future-dir"
        result = _check_containment(str(future), str(root))
        assert result == os.path.realpath(future)

    def test_normalised_path_passes(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        child = root / "sub" / ".." / "ticket"  # normalises to root/ticket
        result = _check_containment(str(child), str(root))
        assert result == os.path.realpath(root / "ticket")


# ---------------------------------------------------------------------------
# Unit: typed exceptions
# ---------------------------------------------------------------------------


class TestExceptions:
    """All typed exceptions inherit from WorkspaceError."""

    def test_clone_failed_is_workspace_error(self) -> None:
        assert issubclass(CloneFailed, WorkspaceError)

    def test_branch_failed_is_workspace_error(self) -> None:
        assert issubclass(BranchFailed, WorkspaceError)

    def test_setup_failed_is_workspace_error(self) -> None:
        assert issubclass(SetupFailed, WorkspaceError)

    def test_path_containment_error_is_workspace_error(self) -> None:
        assert issubclass(PathContainmentError, WorkspaceError)

    def test_clone_failed_message(self) -> None:
        exc = CloneFailed("clone error")
        assert "clone error" in str(exc)

    def test_setup_failed_message(self) -> None:
        exc = SetupFailed("setup error\nstderr tail:\nline1\nline2")
        assert "setup error" in str(exc)
        assert "line1" in str(exc)


# ---------------------------------------------------------------------------
# Unit: prepare / remove path containment
# ---------------------------------------------------------------------------


class TestPreparePathContainment:
    """prepare() rejects paths that escape workspace_root (defense in depth).

    The sanitization step already replaces ``/`` with ``_``, so a bare
    identifier like ``../../etc`` becomes ``.._.._etc`` — a safe directory
    name.  The containment check is an additional safety net validated by the
    ``TestCheckContainment`` unit tests above.
    """

    def test_sanitized_identifier_is_safe(self, tmp_path: Path) -> None:
        """Sanitization prevents traversal: ../../etc becomes .._.._etc (safe)."""
        root = tmp_path / "ws"
        root.mkdir()
        workspace_key = _sanitize_identifier("../../etc")
        assert workspace_key == ".._.._etc"
        # _check_containment should pass for this safe key.
        result = _check_containment(str(root / workspace_key), str(root))
        assert result == os.path.realpath(root / workspace_key)


class TestRemovePathContainment:
    """remove() uses the same sanitization + containment as prepare()."""

    def test_sanitized_identifier_is_safe(self, tmp_path: Path) -> None:
        """Sanitization in remove() prevents traversal just like prepare()."""
        root = tmp_path / "ws"
        root.mkdir()
        # /etc/passwd becomes _etc_passwd after sanitization — safe.
        remove(
            ticket_identifier="/etc/passwd",
            workspace_root=str(root),
        )

    def test_nonexistent_workspace_is_idempotent(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        # Should not raise — idempotent
        remove("NONEXISTENT-TICKET", str(root))


# ---------------------------------------------------------------------------
# Integration: full prepare / remove cycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPrepareRemoveIntegration:
    """End-to-end test: create a source repo, prepare, verify, re-prepare, remove."""

    def test_full_lifecycle(self, tmp_path: Path) -> None:
        """Full prepare → verify → re-prepare → remove cycle."""
        _require_git()
        _require_bwrap()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        # 1. Create a source repo with a .symphony/setup script.
        source_repo = tmp_path / "source"
        marker_file = tmp_path / "setup_marker.txt"

        _make_source_repo(
            source_repo,
            setup_script=(
                "#!/bin/bash\n"
                f"echo 'setup ran' > {marker_file}\n"
            ),
        )

        # 2. Prepare the workspace.
        ticket = "TEAM-42"
        repo_url = str(source_repo)

        result_path = prepare(
            ticket_identifier=ticket,
            repo_url=repo_url,
            branch_name=None,  # use default: symphony/team-42
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        # 3. Verify clone happened (directory + .git).
        assert os.path.isdir(result_path)
        assert os.path.isdir(os.path.join(result_path, ".git"))

        # 4. Verify we are on the right branch.
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=result_path,
            capture_output=True,
            text=True,
        )
        assert branch_result.returncode == 0
        assert branch_result.stdout.strip() == "symphony/team-42"

        # 5. Verify the setup script ran (marker file exists).
        assert marker_file.exists()
        assert marker_file.read_text().strip() == "setup ran"

        # 6. Re-prepare (idempotent).
        marker_file.unlink()  # remove so we can detect re-run
        result_path2 = prepare(
            ticket_identifier=ticket,
            repo_url=repo_url,
            branch_name=None,
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        # Same path returned.
        assert result_path2 == result_path

        # Setup script was re-run (marker re-created).
        assert marker_file.exists()
        assert marker_file.read_text().strip() == "setup ran"

        # 7. Remove the workspace.
        remove(ticket, str(workspace_root))
        assert not os.path.isdir(result_path)

        # 8. Remove is idempotent.
        remove(ticket, str(workspace_root))  # no error

    def test_prepare_without_setup_script(self, tmp_path: Path) -> None:
        """prepare() should succeed when .symphony/setup is absent."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)  # no setup script

        result_path = prepare(
            ticket_identifier="NO-SETUP",
            repo_url=str(source_repo),
            branch_name="feature/test",
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        assert os.path.isdir(result_path)
        assert os.path.isdir(os.path.join(result_path, ".git"))

        # Clean up
        remove("NO-SETUP", str(workspace_root))

    def test_setup_script_failure(self, tmp_path: Path) -> None:
        """prepare() raises SetupFailed when .symphony/setup exits non-zero."""
        _require_git()
        _require_bwrap()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(
            source_repo,
            setup_script=(
                "#!/bin/bash\n"
                "echo 'something went wrong' >&2\n"
                "exit 42\n"
            ),
        )

        with pytest.raises(SetupFailed) as exc_info:
            prepare(
                ticket_identifier="FAIL-SETUP",
                repo_url=str(source_repo),
                branch_name="main",
                workspace_root=str(workspace_root),
                sandbox_hide_paths=[],
            )

        assert "42" in str(exc_info.value)
        assert "something went wrong" in str(exc_info.value)

    def test_clone_invalid_url_fails(self, tmp_path: Path) -> None:
        """prepare() raises CloneFailed for a bogus repo URL."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        with pytest.raises(CloneFailed):
            prepare(
                ticket_identifier="DEAD",
                repo_url="/nonexistent/path/not-a-repo",
                branch_name="main",
                workspace_root=str(workspace_root),
                sandbox_hide_paths=[],
            )

    def test_reprepare_switches_branch(self, tmp_path: Path) -> None:
        """Re-preparing with a different branch should switch to it."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        ticket = "SWITCH-TEST"

        # First prepare on branch A.
        prepare(
            ticket_identifier=ticket,
            repo_url=str(source_repo),
            branch_name="branch-a",
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        # Create branch-b in the source repo.
        _run_git(["checkout", "-b", "branch-b"], cwd=source_repo)
        (source_repo / "file-b.txt").write_text("branch b content")
        _run_git(["add", "file-b.txt"], cwd=source_repo)
        _run_git(["commit", "-m", "commit on branch-b"], cwd=source_repo)

        # Now re-prepare with branch-b.
        result_path = prepare(
            ticket_identifier=ticket,
            repo_url=str(source_repo),
            branch_name="branch-b",
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        # Verify we are on branch-b.
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=result_path,
            capture_output=True,
            text=True,
        )
        assert branch_result.stdout.strip() == "branch-b"

        # Verify the file from branch-b is present.
        assert (Path(result_path) / "file-b.txt").read_text().strip() == "branch b content"

        # Clean up
        remove(ticket, str(workspace_root))

    def test_reprepare_with_new_branch(self, tmp_path: Path) -> None:
        """Re-preparing with a branch that doesn't exist yet creates it."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        ticket = "NEW-BRANCH"

        # First prepare on main.
        prepare(
            ticket_identifier=ticket,
            repo_url=str(source_repo),
            branch_name="main",
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        # Now re-prepare with a branch that doesn't exist anywhere.
        result_path = prepare(
            ticket_identifier=ticket,
            repo_url=str(source_repo),
            branch_name="totally-new-branch",
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        # Verify we are on the new branch.
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=result_path,
            capture_output=True,
            text=True,
        )
        assert branch_result.stdout.strip() == "totally-new-branch"

        # Clean up
        remove(ticket, str(workspace_root))

    def test_default_branch_naming(self, tmp_path: Path) -> None:
        """When branch_name is None, the default naming convention is used."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        result_path = prepare(
            ticket_identifier="My-Team.42",
            repo_url=str(source_repo),
            branch_name=None,
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=result_path,
            capture_output=True,
            text=True,
        )
        # Default: symphony/<lowercase_identifier>
        assert branch_result.stdout.strip() == "symphony/my-team.42"

        remove("My-Team.42", str(workspace_root))

    def test_sanitized_directory_name(self, tmp_path: Path) -> None:
        """The workspace directory uses the sanitized identifier."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        result_path = prepare(
            ticket_identifier="Team/With Spaces",
            repo_url=str(source_repo),
            branch_name="main",
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
        )

        # The directory should be named Team_With_Spaces
        expected_dir = workspace_root / "Team_With_Spaces"
        assert os.path.realpath(result_path) == os.path.realpath(expected_dir)

        remove("Team/With Spaces", str(workspace_root))


# ---------------------------------------------------------------------------
# Unit: start_serve — missing / non-executable script
# ---------------------------------------------------------------------------


class TestStartServeMissingScript:
    """start_serve raises ServeScriptMissing when the script is absent or not executable."""

    def test_raises_when_symphony_dir_absent(self, tmp_path: Path) -> None:
        """No .symphony directory at all → ServeScriptMissing."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(ServeScriptMissing, match=r"\.symphony/serve"):
            start_serve(str(workspace), hide_paths=[])

    def test_raises_when_serve_file_absent(self, tmp_path: Path) -> None:
        """Directory exists but serve file is missing → ServeScriptMissing."""
        workspace = tmp_path / "ws"
        symphony_dir = workspace / ".symphony"
        symphony_dir.mkdir(parents=True)
        with pytest.raises(ServeScriptMissing, match=r"\.symphony/serve"):
            start_serve(str(workspace), hide_paths=[])

    def test_raises_when_serve_not_executable(self, tmp_path: Path) -> None:
        """serve file exists but is not executable → ServeScriptMissing."""
        workspace = tmp_path / "ws"
        symphony_dir = workspace / ".symphony"
        symphony_dir.mkdir(parents=True)
        serve_file = symphony_dir / "serve"
        serve_file.write_text("#!/bin/bash\nsleep 60\n")
        # Explicitly remove execute bit
        serve_file.chmod(0o644)
        with pytest.raises(ServeScriptMissing, match=r"\.symphony/serve"):
            start_serve(str(workspace), hide_paths=[])

    def test_serve_script_missing_is_workspace_error(self) -> None:
        """ServeScriptMissing is a subclass of WorkspaceError."""
        assert issubclass(ServeScriptMissing, WorkspaceError)

    def test_serve_script_missing_message(self) -> None:
        exc = ServeScriptMissing("missing at /some/path")
        assert "missing at /some/path" in str(exc)


# ---------------------------------------------------------------------------
# Integration: start_serve — live Popen
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStartServeIntegration:
    """start_serve returns a live Popen when the script exists and is executable."""

    def test_returns_popen_for_executable_script(self, tmp_path: Path) -> None:
        """A trivial 'sleep 60' serve script → Popen is returned and running."""
        _require_bwrap()

        workspace = tmp_path / "ws"
        symphony_dir = workspace / ".symphony"
        symphony_dir.mkdir(parents=True)
        serve_file = symphony_dir / "serve"
        serve_file.write_text("#!/bin/bash\nsleep 60\n")
        serve_file.chmod(serve_file.stat().st_mode | stat.S_IEXEC)

        proc = start_serve(str(workspace), hide_paths=[])
        try:
            # Process should still be running (sleep 60).
            assert proc.poll() is None, "serve process exited prematurely"
            # Both pipes should be open.
            assert proc.stdout is not None
            assert proc.stderr is not None
        finally:
            proc.kill()
            proc.wait()

    def test_returns_popen_with_extra_rw_paths(self, tmp_path: Path) -> None:
        """extra_rw_paths is forwarded to the sandbox without error."""
        _require_bwrap()

        workspace = tmp_path / "ws"
        symphony_dir = workspace / ".symphony"
        symphony_dir.mkdir(parents=True)
        serve_file = symphony_dir / "serve"
        serve_file.write_text("#!/bin/bash\nsleep 60\n")
        serve_file.chmod(serve_file.stat().st_mode | stat.S_IEXEC)

        extra_dir = tmp_path / "extra"
        extra_dir.mkdir()

        proc = start_serve(str(workspace), hide_paths=[], extra_rw_paths=[str(extra_dir)])
        try:
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait()
