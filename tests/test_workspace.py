"""Tests for the workspace lifecycle module.

Unit tests cover sanitization, path containment, and typed exceptions.
Integration tests (marked ``@pytest.mark.integration``) exercise the full
``prepare`` / ``remove`` cycle against a real git repository and sandbox.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import pytest

from symphony_linear.workspace import (
    BranchFailed,
    CloneFailed,
    DirMapError,
    PathContainmentError,
    ServeScriptMissing,
    SetupFailed,
    WorkspaceError,
    _ATTACHMENTS_DIR,
    _check_containment,
    _git_switch_branch,
    _MOUNTS_DIR,
    _redact_url,
    _REPO_DIR,
    _run_git as _workspace_run_git,
    _sanitize_identifier,
    _TMP_DIR,
    clone_workspace,
    compute_attachments_path,
    compute_ticket_dir,
    compute_tmp_path,
    dirty_summary,
    ensure_attachments_dir,
    ensure_dir_map,
    ensure_tmp_dir,
    finalize_workspace,
    prepare,
    remove,
    resolve_dir_map,
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
# Unit: degenerate ticket identifiers
# ---------------------------------------------------------------------------


class TestDegenerateIdentifier:
    """Identifiers that sanitize to "", "." or ".." resolve to the workspace
    root (or its parent), so they must be rejected before a path is computed.
    Otherwise ``remove()`` would ``rmtree`` the root."""

    @pytest.mark.parametrize("identifier", ["", ".", ".."])
    def test_compute_ticket_dir_rejects_degenerate(
        self, tmp_path: Path, identifier: str
    ) -> None:
        with pytest.raises(PathContainmentError):
            compute_ticket_dir(identifier, str(tmp_path))

    def test_remove_rejects_degenerate_without_deleting_root(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        sentinel = root / "config.yaml"
        sentinel.write_text("secret")

        with pytest.raises(PathContainmentError):
            remove("..", str(root))

        assert sentinel.exists()
        assert root.exists()


# ---------------------------------------------------------------------------
# Unit: ensure_attachments_dir
# ---------------------------------------------------------------------------


class TestEnsureAttachmentsDir:
    """Tests for :func:`ensure_attachments_dir`."""

    def test_creates_and_returns_validated_path(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        result = ensure_attachments_dir("TICKET-1", str(root))

        expected = os.path.realpath(root / "TICKET-1" / _ATTACHMENTS_DIR)
        assert result == expected
        assert os.path.isdir(result)
        # Check mode: directory should have 0700 permissions.
        st = os.stat(result)
        assert (st.st_mode & 0o777) == 0o700

    def test_symlink_attack_rejected(self, tmp_path: Path) -> None:
        """If <workspace_root>/<id> is a symlink pointing outside,
        ensure_attachments_dir must raise PathContainmentError."""
        root = tmp_path / "ws"
        root.mkdir()
        # Create a symlink at <root>/TICKET-2 → /tmp/elsewhere
        escape_target = tmp_path / "outside"
        escape_target.mkdir()
        (root / "TICKET-2").symlink_to(escape_target)

        with pytest.raises(PathContainmentError):
            ensure_attachments_dir("TICKET-2", str(root))

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling ensure_attachments_dir twice returns the same path."""
        root = tmp_path / "ws"
        root.mkdir()
        first = ensure_attachments_dir("T-42", str(root))
        second = ensure_attachments_dir("T-42", str(root))
        assert first == second
        assert os.path.isdir(first)


class TestEnsureTmpDir:
    """Tests for :func:`ensure_tmp_dir`."""

    def test_creates_and_returns_validated_path(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        result = ensure_tmp_dir("TICKET-1", str(root))

        expected = os.path.realpath(root / "TICKET-1" / _TMP_DIR)
        assert result == expected
        assert os.path.isdir(result)
        # Check mode: directory should have 0700 permissions.
        st = os.stat(result)
        assert (st.st_mode & 0o777) == 0o700

    def test_symlink_attack_rejected(self, tmp_path: Path) -> None:
        """If <workspace_root>/<id> is a symlink pointing outside,
        ensure_tmp_dir must raise PathContainmentError."""
        root = tmp_path / "ws"
        root.mkdir()
        escape_target = tmp_path / "outside"
        escape_target.mkdir()
        (root / "TICKET-2").symlink_to(escape_target)

        with pytest.raises(PathContainmentError):
            ensure_tmp_dir("TICKET-2", str(root))

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling ensure_tmp_dir twice returns the same path."""
        root = tmp_path / "ws"
        root.mkdir()
        first = ensure_tmp_dir("T-42", str(root))
        second = ensure_tmp_dir("T-42", str(root))
        assert first == second
        assert os.path.isdir(first)


# ---------------------------------------------------------------------------
# Unit: sandbox.dir_map resolution and preparation
# ---------------------------------------------------------------------------


class TestResolveDirMap:
    """resolve_dir_map: relative vs absolute values, containment, '..'."""

    def test_relative_value_resolves_under_ticket_mounts_dir(
        self, tmp_path: Path
    ) -> None:
        pairs = resolve_dir_map({"~/dest": "npm"}, "TEAM-1", str(tmp_path))
        assert pairs == [
            (os.path.join(str(tmp_path), "TEAM-1", _MOUNTS_DIR, "npm"), "~/dest")
        ]

    def test_absolute_value_used_verbatim(self, tmp_path: Path) -> None:
        pairs = resolve_dir_map(
            {"/sandbox/npm": "/host/shared-cache"}, "TEAM-1", str(tmp_path)
        )
        assert pairs == [("/host/shared-cache", "/sandbox/npm")]

    def test_empty_dir_map_resolves_to_nothing(self, tmp_path: Path) -> None:
        assert resolve_dir_map({}, "TEAM-1", str(tmp_path)) == []

    def test_relative_value_with_dotdot_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PathContainmentError, match=r"\.\."):
            resolve_dir_map({"~/dest": "../escape"}, "TEAM-1", str(tmp_path))

    def test_relative_value_symlink_escape_rejected(self, tmp_path: Path) -> None:
        """A mounts/ symlink pointing outside must fail containment."""
        root = tmp_path / "ws"
        root.mkdir()
        ticket_dir = root / "TEAM-1"
        ticket_dir.mkdir()
        # Escape target lives outside the workspace root.
        escape_target = tmp_path / "outside"
        escape_target.mkdir()
        (ticket_dir / _MOUNTS_DIR).symlink_to(escape_target)

        with pytest.raises(PathContainmentError):
            resolve_dir_map({"~/dest": "npm"}, "TEAM-1", str(root))


class TestEnsureDirMap:
    """ensure_dir_map: creates both sides on the host, dest-is-a-file error."""

    def test_creates_host_source_and_dest(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest-npm"
        pairs = ensure_dir_map({str(dest): "npm"}, "TEAM-1", str(tmp_path))

        host_src = tmp_path / "TEAM-1" / _MOUNTS_DIR / "npm"
        assert pairs == [(str(host_src), str(dest))]
        assert host_src.is_dir()
        assert dest.is_dir()
        # Both sides are created with mode 0700.
        assert (os.stat(host_src).st_mode & 0o777) == 0o700
        assert (os.stat(dest).st_mode & 0o777) == 0o700

    def test_absolute_source_created(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared-cache"
        dest = tmp_path / "dest-npm"
        pairs = ensure_dir_map({str(dest): str(shared)}, "TEAM-1", str(tmp_path))

        assert pairs == [(str(shared), str(dest))]
        assert shared.is_dir()
        assert dest.is_dir()

    def test_dest_existing_file_raises(self, tmp_path: Path) -> None:
        dest_file = tmp_path / "dest-npm"
        dest_file.write_text("i am a file")

        with pytest.raises(DirMapError, match="not a directory"):
            ensure_dir_map({str(dest_file): "npm"}, "TEAM-1", str(tmp_path))

    def test_dest_dangling_symlink_raises(self, tmp_path: Path) -> None:
        """A dangling symlink at the destination is caught (not passed to bwrap)."""
        dest_link = tmp_path / "dest-link"
        dest_link.symlink_to(tmp_path / "nonexistent-target")

        with pytest.raises(DirMapError, match="not a directory"):
            ensure_dir_map({str(dest_link): "npm"}, "TEAM-1", str(tmp_path))

    def test_source_existing_file_raises(self, tmp_path: Path) -> None:
        src_file = tmp_path / "TEAM-1" / _MOUNTS_DIR / "npm"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("i am a file")

        with pytest.raises(DirMapError, match=r"dir_map source"):
            ensure_dir_map({str(tmp_path / "dest-npm"): "npm"}, "TEAM-1", str(tmp_path))

    def test_mkdir_permission_failure_wrapped(self, tmp_path: Path) -> None:
        """An OSError during preparation surfaces as DirMapError, not raw OSError."""
        with mock.patch(
            "symphony_linear.workspace.os.makedirs",
            side_effect=PermissionError("permission denied"),
        ):
            with pytest.raises(DirMapError, match="could not be created"):
                ensure_dir_map(
                    {str(tmp_path / "dest-npm"): "npm"}, "TEAM-1", str(tmp_path)
                )

    def test_empty_dir_map_creates_nothing(self, tmp_path: Path) -> None:
        assert ensure_dir_map({}, "TEAM-1", str(tmp_path)) == []
        # No mounts/ dir (and no ticket dir at all) for an empty map.
        assert not (tmp_path / "TEAM-1").exists()


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


class TestRemoveTicketDir:
    """remove() deletes the whole per-ticket directory (repo, attachments, tmp)."""

    def test_remove_deletes_ticket_dir(self, tmp_path: Path) -> None:
        """remove() deletes the ticket dir with repo/, attachments/, and tmp/."""
        root = tmp_path / "ws"
        root.mkdir()

        ticket = "TEAM-42"
        ticket_dir = root / _sanitize_identifier(ticket)
        repo_dir = ticket_dir / _REPO_DIR
        attachments_dir = ticket_dir / _ATTACHMENTS_DIR
        tmp_dir = ticket_dir / _TMP_DIR

        # Lay out the directory tree the same way prepare() would.
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")
        os.makedirs(attachments_dir, mode=0o700, exist_ok=True)
        (attachments_dir / "screenshot.png").write_text("fake image")
        os.makedirs(tmp_dir, mode=0o700, exist_ok=True)
        (tmp_dir / "scratch.txt").write_text("scratch")

        remove(ticket, str(root))

        assert not ticket_dir.exists()
        assert not repo_dir.exists()
        assert not attachments_dir.exists()
        assert not tmp_dir.exists()

    def test_remove_idempotent_when_attachments_missing(self, tmp_path: Path) -> None:
        """remove() is idempotent even when only the repo dir exists."""
        root = tmp_path / "ws"
        root.mkdir()

        ticket = "TEAM-42"
        workspace_key = _sanitize_identifier(ticket)
        ticket_dir = root / workspace_key
        repo_dir = ticket_dir / _REPO_DIR
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")

        remove(ticket, str(root))

        assert not ticket_dir.exists()
        # Calling again is idempotent.
        remove(ticket, str(root))  # no exception

    def test_remove_idempotent_when_repo_missing(self, tmp_path: Path) -> None:
        """remove() is idempotent even when only the attachments dir exists."""
        root = tmp_path / "ws"
        root.mkdir()

        ticket = "TEAM-42"
        workspace_key = _sanitize_identifier(ticket)

        # Only the attachments dir exists.
        attachments_dir = root / workspace_key / _ATTACHMENTS_DIR
        os.makedirs(attachments_dir, mode=0o700)
        (attachments_dir / "file.txt").write_text("data")

        remove(ticket, str(root))

        assert not (root / workspace_key).exists()
        # Calling again is idempotent.
        remove(ticket, str(root))  # no exception

    def test_remove_both_missing_is_idempotent(self, tmp_path: Path) -> None:
        """remove() is idempotent when neither repo nor attachments exist."""
        root = tmp_path / "ws"
        root.mkdir()

        remove("NO-SUCH-TICKET", str(root))  # no exception
        remove("NO-SUCH-TICKET", str(root))  # still no exception


class TestComputeTicketDir:
    """compute_ticket_dir returns <root>/<sanitized_identifier>."""

    def test_basic(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        result = compute_ticket_dir("TEAM-42", str(root))
        assert result == os.path.join(str(root), "TEAM-42")

    def test_sanitized_identifier(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        result = compute_ticket_dir("Team/With Spaces", str(root))
        assert result == os.path.join(str(root), "Team_With_Spaces")


class TestComputeAttachmentsPath:
    """compute_attachments_path returns the expected path."""

    def test_basic(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        result = compute_attachments_path("TEAM-42", str(root))
        assert result == os.path.join(str(root), "TEAM-42", "attachments")

    def test_sanitized_identifier(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        result = compute_attachments_path("Team/With Spaces", str(root))
        assert result == os.path.join(str(root), "Team_With_Spaces", "attachments")


class TestComputeTmpPath:
    """compute_tmp_path returns the expected path."""

    def test_basic(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        result = compute_tmp_path("TEAM-42", str(root))
        assert result == os.path.join(str(root), "TEAM-42", "tmp")

    def test_sanitized_identifier(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        result = compute_tmp_path("Team/With Spaces", str(root))
        assert result == os.path.join(str(root), "Team_With_Spaces", "tmp")


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

        # The setup script writes its marker via a relative path: the sandbox
        # binds the workspace read-write and chdirs into it, while /tmp is the
        # per-ticket tmp dir — an absolute host /tmp path would land there.
        marker_name = "setup-marker.txt"
        _make_source_repo(
            source_repo,
            setup_script=(f"#!/bin/bash\necho 'setup ran' > ./{marker_name}\n"),
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
        # The clone lives in the repo/ subdir of the per-ticket directory.
        assert os.path.realpath(result_path) == os.path.realpath(
            workspace_root / "TEAM-42" / "repo"
        )

        # 3a. Verify the attachments directory was created.
        attachments_dir = compute_attachments_path(ticket, str(workspace_root))
        assert os.path.isdir(attachments_dir)
        # Check mode 0700 (allow user rwx only).
        assert (os.stat(attachments_dir).st_mode & 0o777) == 0o700

        # 3b. Verify the tmp directory was created.
        tmp_dir = compute_tmp_path(ticket, str(workspace_root))
        assert os.path.isdir(tmp_dir)
        # Check mode 0700 (allow user rwx only).
        assert (os.stat(tmp_dir).st_mode & 0o777) == 0o700

        # 4. Verify we are on the right branch.
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=result_path,
            capture_output=True,
            text=True,
        )
        assert branch_result.returncode == 0
        assert branch_result.stdout.strip() == "symphony/team-42"

        # 5. Verify the setup script ran (marker file exists in the workspace).
        marker_file = Path(result_path) / marker_name
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
        assert not os.path.isdir(attachments_dir)
        assert not os.path.isdir(tmp_dir)
        # The whole per-ticket directory is gone.
        assert not (workspace_root / "TEAM-42").exists()

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
            setup_script=("#!/bin/bash\necho 'something went wrong' >&2\nexit 42\n"),
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
        assert (
            Path(result_path) / "file-b.txt"
        ).read_text().strip() == "branch b content"

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

    def test_auto_branch_disabled_skips_switch(self, tmp_path: Path) -> None:
        """When auto_branch=False, no branch switch runs and HEAD stays on
        whatever git clone checked out (the source repo's default branch)."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)  # default branch: main

        result_path = prepare(
            ticket_identifier="TEAM-99",
            repo_url=str(source_repo),
            branch_name="symphony/team-99",  # supplied but should be ignored
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            auto_branch=False,
        )

        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=result_path,
            capture_output=True,
            text=True,
        )
        # HEAD stays on the clone default ("main"), not the supplied name.
        assert branch_result.stdout.strip() == "main"

        # And no symphony/team-99 branch was created.
        list_result = subprocess.run(
            ["git", "branch", "--list", "symphony/team-99"],
            cwd=result_path,
            capture_output=True,
            text=True,
        )
        assert list_result.stdout.strip() == ""

        remove("TEAM-99", str(workspace_root))

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

        # The directory should be named Team_With_Spaces/repo
        expected_dir = workspace_root / "Team_With_Spaces" / "repo"
        assert os.path.realpath(result_path) == os.path.realpath(expected_dir)

        remove("Team/With Spaces", str(workspace_root))


# ---------------------------------------------------------------------------
# Unit: URL redaction in logs
# ---------------------------------------------------------------------------


class TestRedactUrl:
    """_redact_url strips userinfo so credentials never reach the log."""

    def test_strips_token_userinfo(self) -> None:
        assert (
            _redact_url("https://sekrit-token@github.com/org/repo.git")
            == "https://github.com/org/repo.git"
        )

    def test_strips_user_and_password_and_keeps_port(self) -> None:
        assert (
            _redact_url("https://user:secret@host:8443/repo")
            == "https://host:8443/repo"
        )

    def test_leaves_plain_url_unchanged(self) -> None:
        url = "https://github.com/org/repo"
        assert _redact_url(url) == url

    def test_leaves_local_path_unchanged(self) -> None:
        path = "/tmp/some path/repo"
        assert _redact_url(path) == path

    def test_leaves_scp_like_remote_unchanged(self) -> None:
        remote = "git@github.com:org/repo.git"
        assert _redact_url(remote) == remote

    def test_malformed_schemed_url_fails_closed(self) -> None:
        """A URL urlsplit cannot parse (invalid IPv6 host) must still have its
        userinfo stripped rather than being logged verbatim."""
        url = "https://fake-secret@[broken/repo"
        with pytest.raises(ValueError):
            urlsplit(url)
        redacted = _redact_url(url)
        assert "fake-secret" not in redacted
        assert redacted == "https://[broken/repo"


class TestGitLogRedaction:
    """Credentials are redacted from the git debug log, but the credential is
    still passed to git itself."""

    def test_run_git_debug_log_redacts_userinfo(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "symphony_linear.workspace.subprocess.run", return_value=completed
        ) as run:
            with caplog.at_level(logging.DEBUG, logger="symphony_linear.workspace"):
                _workspace_run_git(
                    ["clone", "https://sekrit-token@github.com/org/repo.git", "/tmp/x"]
                )

        assert "sekrit-token" not in caplog.text
        assert "github.com/org/repo.git" in caplog.text
        # The real argv passed to git still carries the credential.
        assert run.call_args[0][0] == [
            "git",
            "clone",
            "https://sekrit-token@github.com/org/repo.git",
            "/tmp/x",
        ]

    def test_clone_info_log_redacts_userinfo(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "ws"
        root.mkdir()

        with mock.patch("symphony_linear.workspace._run_git"):
            with caplog.at_level(logging.INFO, logger="symphony_linear.workspace"):
                clone_workspace(
                    ticket_identifier="T-1",
                    repo_url="https://sekrit-token@github.com/org/repo.git",
                    workspace_root=str(root),
                )

        assert "sekrit-token" not in caplog.text
        assert "github.com/org/repo.git" in caplog.text


# ---------------------------------------------------------------------------
# Unit: git option termination
# ---------------------------------------------------------------------------


class TestGitOptionTermination:
    """Untrusted repo URLs and branch names must not reach git as options."""

    def test_clone_passes_double_dash_before_repo_url(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()

        with mock.patch("symphony_linear.workspace._run_git") as run_git:
            path, recovered = clone_workspace(
                ticket_identifier="T-1",
                repo_url="https://github.com/org/repo.git",
                workspace_root=str(root),
            )

        assert not recovered
        run_git.assert_called_once_with(
            ["clone", "--", "https://github.com/org/repo.git", path],
            description="clone",
        )

    def test_repo_url_leading_dash_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()

        with mock.patch("symphony_linear.workspace._run_git") as run_git:
            with pytest.raises(CloneFailed, match="must not begin with"):
                clone_workspace(
                    ticket_identifier="T-1",
                    repo_url="--upload-pack=/bin/sh",
                    workspace_root=str(root),
                )
        run_git.assert_not_called()

    def test_switch_branch_terminates_options(self, tmp_path: Path) -> None:
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no such branch"
        )
        succeeded = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "symphony_linear.workspace.subprocess.run",
            side_effect=[failed, succeeded],
        ) as run:
            _git_switch_branch("-weird", str(tmp_path))

        assert run.call_args_list[0][0][0] == ["git", "switch", "--", "-weird"]
        assert run.call_args_list[1][0][0] == [
            "git",
            "switch",
            "-c",
            "-weird",
            "--",
        ]


# ---------------------------------------------------------------------------
# Unit: clone_workspace — standalone clone/fetch step
# ---------------------------------------------------------------------------


class TestCloneWorkspace:
    """clone_workspace handles sanitization, containment, and clone/fetch."""

    def test_clones_fresh_repo(self, tmp_path: Path) -> None:
        """A brand-new workspace clones the repo and returns the real path."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        result, recovered = clone_workspace(
            ticket_identifier="CLONE-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        assert not recovered
        assert os.path.isdir(result)
        assert os.path.isdir(os.path.join(result, ".git"))
        # The clone lives in <root>/<sanitized_id>/repo.
        assert os.path.basename(os.path.realpath(result)) == "repo"
        assert os.path.basename(os.path.dirname(os.path.realpath(result))) == "CLONE-1"

        remove("CLONE-1", str(workspace_root))

    def test_reuses_existing_workspace(self, tmp_path: Path) -> None:
        """Re-calling clone_workspace on an existing directory fetches, not clones."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        # First call: clone.
        result1, recovered1 = clone_workspace(
            ticket_identifier="REUSE-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        assert not recovered1

        # Second call: reuse (fetch).
        result2, recovered2 = clone_workspace(
            ticket_identifier="REUSE-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        assert not recovered2
        assert result2 == result1

        remove("REUSE-1", str(workspace_root))

    def test_raises_clone_failed_for_bogus_url(self, tmp_path: Path) -> None:
        """clone_workspace raises CloneFailed for a non-existent repo URL."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        with pytest.raises(CloneFailed):
            clone_workspace(
                ticket_identifier="DEAD",
                repo_url="/nonexistent/path/not-a-repo",
                workspace_root=str(workspace_root),
            )

    def test_sanitized_directory_name(self, tmp_path: Path) -> None:
        """The workspace directory uses the sanitized identifier."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        result, recovered = clone_workspace(
            ticket_identifier="Team/With Spaces",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        assert not recovered
        expected_dir = workspace_root / "Team_With_Spaces" / "repo"
        assert os.path.realpath(result) == os.path.realpath(expected_dir)

        remove("Team/With Spaces", str(workspace_root))


# ---------------------------------------------------------------------------
# Unit: dirty_summary
# ---------------------------------------------------------------------------


class TestDirtySummary:
    """dirty_summary returns None when there is nothing to protect, and a
    short Markdown summary otherwise."""

    def test_clean_clone_returns_none(self, tmp_path: Path) -> None:
        """A freshly-cloned repo with no modifications has nothing to protect."""
        _require_git()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        workspace = tmp_path / "ws"
        subprocess.run(
            ["git", "clone", str(source_repo), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        assert dirty_summary(str(workspace)) is None

    def test_untracked_file_returns_summary(self, tmp_path: Path) -> None:
        """An untracked file yields a summary with a count and the file name."""
        _require_git()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        workspace = tmp_path / "ws"
        subprocess.run(
            ["git", "clone", str(source_repo), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        (workspace / "untracked.txt").write_text("hello")

        summary = dirty_summary(str(workspace))
        assert summary is not None
        assert "1 uncommitted file," in summary
        assert "untracked.txt" in summary

    def test_modified_tracked_file_returns_summary(self, tmp_path: Path) -> None:
        """A modified tracked file makes the workspace dirty."""
        _require_git()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        workspace = tmp_path / "ws"
        subprocess.run(
            ["git", "clone", str(source_repo), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        (workspace / "README.md").write_text("modified content")

        summary = dirty_summary(str(workspace))
        assert summary is not None
        assert "1 uncommitted file," in summary
        assert "README.md" in summary

    def test_local_only_commit_returns_summary(self, tmp_path: Path) -> None:
        """A local commit that is not on any remote makes the workspace dirty."""
        _require_git()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        workspace = tmp_path / "ws"
        subprocess.run(
            ["git", "clone", str(source_repo), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        # Make a local commit without pushing.
        (workspace / "local.txt").write_text("local only")
        _run_git(["add", "local.txt"], cwd=workspace)
        _run_git(
            [
                "-c",
                "user.email=test@test.com",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "local commit",
            ],
            cwd=workspace,
        )

        summary = dirty_summary(str(workspace))
        assert summary is not None
        assert "1 commit not on any remote" in summary

    def test_side_branch_commit_returns_summary(self, tmp_path: Path) -> None:
        """A commit on a local side branch is dirty even when HEAD itself
        sits clean on a remote-backed branch."""
        _require_git()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        workspace = tmp_path / "ws"
        subprocess.run(
            ["git", "clone", str(source_repo), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        # Commit on a side branch, then return HEAD to the remote-backed main.
        _run_git(["checkout", "-b", "side-branch"], cwd=workspace)
        (workspace / "side.txt").write_text("side work")
        _run_git(["add", "side.txt"], cwd=workspace)
        _run_git(
            [
                "-c",
                "user.email=test@test.com",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "side commit",
            ],
            cwd=workspace,
        )
        _run_git(["checkout", "main"], cwd=workspace)

        summary = dirty_summary(str(workspace))
        assert summary is not None
        assert "1 commit not on any remote" in summary

    def test_untracked_file_dirty_with_showuntrackedfiles_no(
        self, tmp_path: Path
    ) -> None:
        """status.showUntrackedFiles=no in the repo config must not hide
        untracked work."""
        _require_git()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        workspace = tmp_path / "ws"
        subprocess.run(
            ["git", "clone", str(source_repo), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        # A repo-local config that would hide untracked files from a plain
        # `git status --porcelain` unless explicitly overridden.
        _run_git(["config", "status.showUntrackedFiles", "no"], cwd=workspace)

        (workspace / "untracked.txt").write_text("hello")

        summary = dirty_summary(str(workspace))
        assert summary is not None
        assert "untracked.txt" in summary

    def test_missing_directory_returns_none(self, tmp_path: Path) -> None:
        """A path that is not a directory has nothing to protect."""
        assert dirty_summary(str(tmp_path / "does-not-exist")) is None

    def test_non_repo_directory_returns_summary(self, tmp_path: Path) -> None:
        """A directory that is not a git repo is conservatively dirty."""
        plain = tmp_path / "plain"
        plain.mkdir()

        summary = dirty_summary(str(plain))
        assert summary is not None
        assert "Could not verify workspace state" in summary


# ---------------------------------------------------------------------------
# Integration: clone_workspace recovery path
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCloneWorkspaceRecovery:
    """clone_workspace auto-recovers from fetch failures on clean workspaces."""

    def test_fetch_failure_on_clean_workspace_reclones(self, tmp_path: Path) -> None:
        """When fetch fails on a clean workspace, it is nuked and re-cloned."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        # First call: normal clone.
        path1, rec1 = clone_workspace(
            ticket_identifier="RECOVER-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )
        assert not rec1
        assert os.path.isdir(path1)

        # Sabotage the remote URL in the workspace so fetch fails.
        _run_git(
            ["remote", "set-url", "origin", "/nonexistent/path"],
            cwd=Path(path1),
        )

        # Second call: fetch fails, workspace is clean → nuke + re-clone.
        path2, rec2 = clone_workspace(
            ticket_identifier="RECOVER-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )
        assert rec2
        assert os.path.isdir(path2)
        # The re-cloned workspace should have a working .git and valid remote.
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path2,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == str(source_repo)

        remove("RECOVER-1", str(workspace_root))

    def test_fetch_failure_on_dirty_workspace_preserves(self, tmp_path: Path) -> None:
        """When fetch fails on a dirty workspace, it is preserved (no exception)."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        # First call: normal clone.
        path1, rec1 = clone_workspace(
            ticket_identifier="DIRTY-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )
        assert not rec1

        # Make the workspace dirty (untracked file).
        (Path(path1) / "dirty.txt").write_text("please preserve me")

        # Sabotage the remote URL so fetch fails.
        _run_git(
            ["remote", "set-url", "origin", "/nonexistent/path"],
            cwd=Path(path1),
        )

        # Second call: fetch fails, workspace is dirty → preserve (no exception).
        path2, rec2 = clone_workspace(
            ticket_identifier="DIRTY-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )
        assert not rec2
        assert path2 == path1
        # The dirty file should still exist.
        assert (Path(path2) / "dirty.txt").read_text() == "please preserve me"
        # The remote URL is still sabotaged (we didn't touch the workspace).
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path2,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "/nonexistent/path"

        remove("DIRTY-1", str(workspace_root))


# ---------------------------------------------------------------------------
# Integration: finalize_workspace — branch switch + setup
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFinalizeWorkspace:
    """finalize_workspace handles branch switching and setup script execution."""

    def test_branch_switch_and_setup(self, tmp_path: Path) -> None:
        """finalize_workspace switches branch and runs .symphony/setup."""
        _require_git()
        _require_bwrap()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        # The setup script writes its marker via a relative path (cwd is the
        # workspace inside the sandbox); /tmp is the per-ticket tmp dir, so an
        # absolute host /tmp path would land there instead.
        marker_name = "finalize-marker.txt"
        _make_source_repo(
            source_repo,
            setup_script=(f"#!/bin/bash\necho 'finalize ran' > ./{marker_name}\n"),
        )

        # Clone first.
        ws_path, _ = clone_workspace(
            ticket_identifier="FINALIZE-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        # Then finalize.
        finalize_workspace(
            workspace_path=ws_path,
            ticket_identifier="FINALIZE-1",
            branch_name=None,  # default: symphony/finalize-1
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("FINALIZE-1", str(workspace_root)),
        )

        # Verify branch.
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ws_path,
            capture_output=True,
            text=True,
        )
        assert branch_result.stdout.strip() == "symphony/finalize-1"

        # Verify setup ran (marker file exists in the workspace).
        marker_file = Path(ws_path) / marker_name
        assert marker_file.exists()
        assert marker_file.read_text().strip() == "finalize ran"

        remove("FINALIZE-1", str(workspace_root))

    def test_auto_branch_disabled_skips_switch(self, tmp_path: Path) -> None:
        """When auto_branch=False, the branch is not switched."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        ws_path, _ = clone_workspace(
            ticket_identifier="NO-BRANCH",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        finalize_workspace(
            workspace_path=ws_path,
            ticket_identifier="NO-BRANCH",
            branch_name="symphony/no-branch",  # supplied but ignored
            sandbox_hide_paths=[],
            auto_branch=False,
            tmp_path=ensure_tmp_dir("NO-BRANCH", str(workspace_root)),
        )

        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ws_path,
            capture_output=True,
            text=True,
        )
        assert branch_result.stdout.strip() == "main"

        # Clean branch was NOT created.
        list_result = subprocess.run(
            ["git", "branch", "--list", "symphony/no-branch"],
            cwd=ws_path,
            capture_output=True,
            text=True,
        )
        assert list_result.stdout.strip() == ""

        remove("NO-BRANCH", str(workspace_root))

    def test_no_setup_script_succeeds(self, tmp_path: Path) -> None:
        """finalize_workspace succeeds when .symphony/setup is absent."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)  # no setup script

        ws_path, _ = clone_workspace(
            ticket_identifier="NO-SETUP-F",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        # Should not raise.
        finalize_workspace(
            workspace_path=ws_path,
            ticket_identifier="NO-SETUP-F",
            branch_name="main",
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("NO-SETUP-F", str(workspace_root)),
        )

        remove("NO-SETUP-F", str(workspace_root))

    def test_setup_script_failure_raises(self, tmp_path: Path) -> None:
        """finalize_workspace raises SetupFailed when setup script fails."""
        _require_git()
        _require_bwrap()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(
            source_repo,
            setup_script=("#!/bin/bash\necho 'fail' >&2\nexit 13\n"),
        )

        ws_path, _ = clone_workspace(
            ticket_identifier="FAIL-FIN",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        with pytest.raises(SetupFailed) as exc_info:
            finalize_workspace(
                workspace_path=ws_path,
                ticket_identifier="FAIL-FIN",
                branch_name="main",
                sandbox_hide_paths=[],
                tmp_path=ensure_tmp_dir("FAIL-FIN", str(workspace_root)),
            )

        assert "13" in str(exc_info.value)

        remove("FAIL-FIN", str(workspace_root))

    def test_default_branch_naming(self, tmp_path: Path) -> None:
        """When branch_name is None, default naming is used."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        ws_path, _ = clone_workspace(
            ticket_identifier="My-Team.42",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
        )

        finalize_workspace(
            workspace_path=ws_path,
            ticket_identifier="My-Team.42",
            branch_name=None,
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("My-Team.42", str(workspace_root)),
        )

        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ws_path,
            capture_output=True,
            text=True,
        )
        assert branch_result.stdout.strip() == "symphony/my-team.42"

        remove("My-Team.42", str(workspace_root))


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
            start_serve(str(workspace), hide_paths=[], tmp_path=str(tmp_path / "t"))

    def test_raises_when_serve_file_absent(self, tmp_path: Path) -> None:
        """Directory exists but serve file is missing → ServeScriptMissing."""
        workspace = tmp_path / "ws"
        symphony_dir = workspace / ".symphony"
        symphony_dir.mkdir(parents=True)
        with pytest.raises(ServeScriptMissing, match=r"\.symphony/serve"):
            start_serve(str(workspace), hide_paths=[], tmp_path=str(tmp_path / "t"))

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
            start_serve(str(workspace), hide_paths=[], tmp_path=str(tmp_path / "t"))

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

        ticket_tmp = tmp_path / "ticket-tmp"
        ticket_tmp.mkdir()

        proc = start_serve(str(workspace), hide_paths=[], tmp_path=str(ticket_tmp))
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

        ticket_tmp = tmp_path / "ticket-tmp"
        ticket_tmp.mkdir()

        proc = start_serve(
            str(workspace),
            hide_paths=[],
            extra_rw_paths=[str(extra_dir)],
            tmp_path=str(ticket_tmp),
        )
        try:
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait()
