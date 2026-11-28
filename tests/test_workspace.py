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
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import pytest

from symphony_linear.workspace import (
    BranchFailed,
    CloneFailed,
    DirMapError,
    PathContainmentError,
    SecretsError,
    ServeScriptMissing,
    SetupFailed,
    _ATTACHMENTS_DIR,
    _check_containment,
    _git_switch_branch,
    _MOUNTS_DIR,
    _redact_url,
    _REPO_DIR,
    _run_git as _workspace_run_git,
    _run_setup_script,
    _SandboxGitTimeout,
    _sanitize_identifier,
    _TMP_DIR,
    clone_workspace,
    compute_attachments_path,
    compute_ticket_dir,
    compute_tmp_path,
    dirty_summary,
    ensure_attachments_dir,
    ensure_dir_map,
    ensure_secrets_file,
    ensure_tmp_dir,
    finalize_workspace,
    prepare,
    remove,
    repo_secrets_path,
    resolve_dir_map,
    resolve_secrets_dir,
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


def _base_outside_tmp() -> Path:
    """Return a writable directory that is not under ``/tmp``.

    ``run_in_sandbox`` replaces ``/tmp`` with the ticket's private tmp dir, so
    anything under the host ``/tmp`` is invisible inside the sandbox whether or
    not it is mounted.  A test fixture there cannot prove anything about what
    the sandbox can reach (gnosis njnsrm).
    """
    for base in (Path(__file__).resolve().parent.parent, Path.home()):
        resolved = base.resolve()
        text = str(resolved)
        if text == "/tmp" or text.startswith("/tmp/"):
            continue
        if os.access(resolved, os.W_OK):
            return resolved
    return pytest.skip("no writable directory outside /tmp for the sandbox git test")


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

    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [
            ("ABC-123", "ABC-123"),
            ("team_1.0-release", "team_1.0-release"),
            ("team 42", "team_42"),
            ("a/b/c", "a_b_c"),
            # @ sits between hello and world; ! # $ % follow world.
            ("hello@world!#$%", "hello_world____"),
            # Dots are allowed; slashes become underscores.
            ("../../etc", ".._.._etc"),
            ("", ""),
        ],
    )
    def test_sanitize(self, identifier: str, expected: str) -> None:
        assert _sanitize_identifier(identifier) == expected


# ---------------------------------------------------------------------------
# Unit: _check_containment
# ---------------------------------------------------------------------------


class TestCheckContainment:
    """Path containment rejects escapes; passes valid paths."""

    @pytest.mark.parametrize(
        ("path_parts", "expected_parts"),
        [
            (("ticket-1",), ("ticket-1",)),
            # Containment is normalised and may be checked before the
            # directory is created.
            (("sub", "..", "ticket"), ("ticket",)),
            (("future-dir",), ("future-dir",)),
            # The root itself is contained in itself.
            ((), ()),
        ],
        ids=["subdirectory", "normalised", "not-created-yet", "root-itself"],
    )
    def test_contained_paths_pass(
        self,
        tmp_path: Path,
        path_parts: tuple[str, ...],
        expected_parts: tuple[str, ...],
    ) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        result = _check_containment(str(root.joinpath(*path_parts)), str(root))
        assert result == os.path.realpath(root.joinpath(*expected_parts))

    @pytest.mark.parametrize(
        "escape", ["../etc", "sub/../../etc"], ids=["dot-dot", "dot-dot-in-middle"]
    )
    def test_relative_escapes_rejected(self, tmp_path: Path, escape: str) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        with pytest.raises(PathContainmentError):
            _check_containment(str(root / escape), str(root))

    def test_absolute_escape_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        with pytest.raises(PathContainmentError):
            _check_containment("/etc/passwd", str(root))

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        """Containment resolves symlinks, so a link out of the root fails."""
        root = tmp_path / "ws"
        root.mkdir()
        escape_target = tmp_path / "outside"
        escape_target.mkdir()
        symlink = root / "link"
        symlink.symlink_to(escape_target)
        with pytest.raises(PathContainmentError):
            _check_containment(str(symlink), str(root))


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
# Unit: ensure_attachments_dir / ensure_tmp_dir
# ---------------------------------------------------------------------------


class TestEnsurePerTicketDirs:
    """The per-ticket dirs are created 0700 and contained in the root."""

    @pytest.mark.parametrize(
        ("ensure", "subdir"),
        [
            (ensure_attachments_dir, _ATTACHMENTS_DIR),
            (ensure_tmp_dir, _TMP_DIR),
        ],
        ids=["attachments", "tmp"],
    )
    def test_creates_validated_path_idempotently(
        self, tmp_path: Path, ensure, subdir: str
    ) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        result = ensure("TICKET-1", str(root))

        assert result == os.path.realpath(root / "TICKET-1" / subdir)
        assert os.path.isdir(result)
        # Check mode: directory should have 0700 permissions.
        st = os.stat(result)
        assert (st.st_mode & 0o777) == 0o700
        # Calling it twice returns the same path.
        assert ensure("TICKET-1", str(root)) == result

    @pytest.mark.parametrize(
        "ensure",
        [ensure_attachments_dir, ensure_tmp_dir],
        ids=["attachments", "tmp"],
    )
    def test_symlink_attack_rejected(self, tmp_path: Path, ensure) -> None:
        """If <workspace_root>/<id> is a symlink pointing outside,
        the ensure helper must raise PathContainmentError."""
        root = tmp_path / "ws"
        root.mkdir()
        escape_target = tmp_path / "outside"
        escape_target.mkdir()
        (root / "TICKET-2").symlink_to(escape_target)

        with pytest.raises(PathContainmentError):
            ensure("TICKET-2", str(root))


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

    @pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
    def test_creates_host_source_and_dest(self, tmp_path: Path, absolute: bool) -> None:
        source = str(tmp_path / "shared-cache") if absolute else "npm"
        dest = tmp_path / "dest-npm"
        pairs = ensure_dir_map({str(dest): source}, "TEAM-1", str(tmp_path))

        host_src = (
            tmp_path / "shared-cache"
            if absolute
            else tmp_path / "TEAM-1" / _MOUNTS_DIR / "npm"
        )
        assert pairs == [(str(host_src), str(dest))]
        assert host_src.is_dir()
        assert dest.is_dir()
        # Both sides are created with mode 0700.
        assert (os.stat(host_src).st_mode & 0o777) == 0o700
        assert (os.stat(dest).st_mode & 0o777) == 0o700

    @pytest.mark.parametrize("dangling_symlink", [False, True], ids=["file", "symlink"])
    def test_dest_not_a_directory_raises(
        self, tmp_path: Path, dangling_symlink: bool
    ) -> None:
        """A file or dangling symlink at the destination is caught, not passed
        to bwrap."""
        dest = tmp_path / "dest-npm"
        if dangling_symlink:
            dest.symlink_to(tmp_path / "nonexistent-target")
        else:
            dest.write_text("i am a file")

        with pytest.raises(DirMapError, match="not a directory"):
            ensure_dir_map({str(dest): "npm"}, "TEAM-1", str(tmp_path))

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


# ---------------------------------------------------------------------------
# Unit: per-repo secrets mapping and copying
# ---------------------------------------------------------------------------


class TestRepoSecretsPath:
    """Every remote form of one repo maps to ``<secrets>/<host>/<path>.env``."""

    @pytest.mark.parametrize(
        ("repo_url", "relative"),
        [
            ("git@github.com:org/repo.git", "github.com/org/repo.env"),
            ("ssh://git@github.com/org/repo.git", "github.com/org/repo.env"),
            ("https://github.com/org/repo.git", "github.com/org/repo.env"),
            # Credentials are discarded, not part of the key.
            ("https://user:token@github.com/org/repo.git", "github.com/org/repo.env"),
            # A trailing .git is optional.
            ("https://github.com/org/repo", "github.com/org/repo.env"),
            # GitLab subgroups become nested directories.
            ("git@gitlab.com:group/sub/repo.git", "gitlab.com/group/sub/repo.env"),
            (
                "ssh://git@gitlab.com/group/sub/repo.git",
                "gitlab.com/group/sub/repo.env",
            ),
        ],
        ids=[
            "scp-like",
            "ssh-scheme",
            "https",
            "credentials",
            "no-dot-git",
            "subgroups",
            "subgroups-ssh-scheme",
        ],
    )
    def test_remote_forms_map_to_one_path(
        self, tmp_path: Path, repo_url: str, relative: str
    ) -> None:
        secrets = tmp_path / "secrets"
        assert repo_secrets_path(repo_url, str(secrets)) == str(secrets / relative)

    def test_local_path_has_no_secrets_path(self, tmp_path: Path) -> None:
        assert repo_secrets_path(str(tmp_path / "local-repo"), str(tmp_path)) is None

    def test_leading_slash_path_stays_contained(self, tmp_path: Path) -> None:
        """An absolute-looking URL path is not taken as an absolute host path."""
        result = repo_secrets_path(
            "ssh://git@host/etc/passwd", str(tmp_path / "secrets")
        )
        assert result == str(tmp_path / "secrets" / "host" / "etc" / "passwd.env")

    @pytest.mark.parametrize(
        "repo_url",
        [
            "git@github.com:../../etc/passwd",
            "https://github.com/../../../etc/passwd",
            "git@..:repo.git",
            # ``github.com/../gitlab.com/...`` cancels the host component and
            # lands inside secrets_dir, selecting another repo's file;
            # containment alone would accept it, so the component is rejected
            # outright.
            "git@github.com:../gitlab.com/team/repo.git",
            "https://github.com/../gitlab.com/team/repo.git",
            "git@github.com:org/./repo.git",
        ],
        ids=[
            "dot-dot-path",
            "dot-dot-https-path",
            "dot-dot-host",
            "dot-dot-cancels-host",
            "dot-dot-cancels-host-https",
            "single-dot",
        ],
    )
    def test_dot_segment_components_refused(
        self, tmp_path: Path, repo_url: str
    ) -> None:
        with pytest.raises(SecretsError):
            repo_secrets_path(repo_url, str(tmp_path / "secrets"))

    def test_error_message_redacts_credentials(self, tmp_path: Path) -> None:
        """A token in an HTTPS URL must not reach the error comment."""
        with pytest.raises(SecretsError) as excinfo:
            repo_secrets_path(
                "https://user:sekret-token@github.com/../gitlab.com/repo.git",
                str(tmp_path / "secrets"),
            )
        assert "sekret-token" not in str(excinfo.value)
        assert "github.com" in str(excinfo.value)


class TestResolveSecretsDir:
    """None (or an empty string) means ``<workspace_root>/secrets``."""

    def test_default_and_explicit_dirs(self, tmp_path: Path) -> None:
        default = str(tmp_path / "secrets")
        assert resolve_secrets_dir(None, str(tmp_path)) == default
        assert resolve_secrets_dir("", str(tmp_path)) == default
        assert (
            resolve_secrets_dir("/custom/secrets", str(tmp_path)) == "/custom/secrets"
        )


class TestEnsureSecretsFile:
    """Copies the source into the ticket dir, or removes a stale copy."""

    @pytest.mark.parametrize("explicit_dir", [False, True], ids=["default", "explicit"])
    def test_existing_source_copied_with_0600(
        self, tmp_path: Path, explicit_dir: bool
    ) -> None:
        workspace_root = tmp_path / "ws"
        secrets_dir = (
            tmp_path / "custom-secrets"
            if explicit_dir
            else (workspace_root / "secrets")
        )
        source = secrets_dir / "github.com" / "org" / "repo.env"
        source.parent.mkdir(parents=True)
        source.write_text("TOKEN=abc\n")

        result = ensure_secrets_file(
            "https://github.com/org/repo.git",
            "TEAM-1",
            str(workspace_root),
            str(secrets_dir) if explicit_dir else None,
        )

        expected = workspace_root / "TEAM-1" / "secrets.env"
        assert result == str(expected)
        assert expected.read_text() == "TOKEN=abc\n"
        assert stat.S_IMODE(expected.stat().st_mode) == 0o600

    @pytest.mark.parametrize(
        ("repo_url", "stale"),
        [
            # A stale copy must go when the source is gone, so removing a
            # secrets file takes effect on the next launch.
            ("https://github.com/org/repo.git", True),
            # Nothing to install and nothing stale is a no-op.
            ("https://github.com/org/repo.git", False),
            # A local repo path has no secrets mapping at all.
            ("local", True),
        ],
        ids=["stale-copy", "nothing-to-do", "local-repo"],
    )
    def test_no_source_leaves_no_secrets(
        self, tmp_path: Path, repo_url: str, stale: bool
    ) -> None:
        workspace_root = tmp_path / "ws"
        if repo_url == "local":
            repo_url = str(tmp_path / "local-repo")
        if stale:
            stale_file = workspace_root / "TEAM-1" / "secrets.env"
            stale_file.parent.mkdir(parents=True)
            stale_file.write_text("OLD=1\n")

        result = ensure_secrets_file(
            repo_url, "TEAM-1", str(workspace_root), str(tmp_path / "secrets")
        )

        assert result is None
        assert not (workspace_root / "TEAM-1" / "secrets.env").exists()

    def test_edited_source_takes_effect_on_next_call(self, tmp_path: Path) -> None:
        """Secrets are copied, never bind-mounted, so an edited source (an
        editor saving by rename) is picked up by the next launch."""
        secrets_dir = tmp_path / "secrets"
        source = secrets_dir / "github.com" / "org" / "repo.env"
        source.parent.mkdir(parents=True)
        source.write_text("A=1\n")
        workspace_root = tmp_path / "ws"
        target = workspace_root / "TEAM-1" / "secrets.env"

        ensure_secrets_file(
            "https://github.com/org/repo.git",
            "TEAM-1",
            str(workspace_root),
            str(secrets_dir),
        )
        assert target.read_text() == "A=1\n"

        source.write_text("A=2\n")
        ensure_secrets_file(
            "https://github.com/org/repo.git",
            "TEAM-1",
            str(workspace_root),
            str(secrets_dir),
        )
        assert target.read_text() == "A=2\n"


class TestSecretsEnvForSetupAndServe:
    """The setup and serve launch points export SYMPHONY_SECRETS_FILE."""

    @staticmethod
    def _make_script(workspace: Path, name: str) -> None:
        script = workspace / ".symphony" / name
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\n")
        script.chmod(0o755)

    @pytest.mark.parametrize(
        ("secrets_file", "expected"),
        [("/ws/TEAM-1/secrets.env", "/ws/TEAM-1/secrets.env"), (None, None)],
        ids=["set", "unset"],
    )
    @pytest.mark.parametrize("script", ["setup", "serve"])
    def test_secrets_file_env(
        self,
        tmp_path: Path,
        script: str,
        secrets_file: str | None,
        expected: str | None,
    ) -> None:
        workspace = tmp_path / "ws"
        self._make_script(workspace, script)
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = (b"", b"")

        with mock.patch(
            "symphony_linear.workspace.run_in_sandbox", return_value=fake_proc
        ) as m:
            if script == "setup":
                _run_setup_script(
                    str(workspace),
                    hide_paths=[],
                    secrets_file=secrets_file,
                    tmp_path=str(tmp_path / "tmp"),
                )
            else:
                start_serve(
                    str(workspace),
                    hide_paths=[],
                    secrets_file=secrets_file,
                    tmp_path=str(tmp_path / "tmp"),
                )

        env = m.call_args.kwargs["env"]
        assert env.get("SYMPHONY_SECRETS_FILE") == expected


# ---------------------------------------------------------------------------


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
        # Removing again is idempotent.
        remove(ticket, str(root))


class TestRemoveReadOnlyTree:
    """remove() deletes read-only trees agents leave behind."""

    @staticmethod
    def _restore_permissions(path: Path) -> None:
        """Make *path* deletable again so pytest's tmp cleanup never fails."""
        if not path.exists():
            return
        for dirpath, _dirnames, _filenames in os.walk(path):
            try:
                os.chmod(dirpath, 0o755)
            except OSError:
                pass

    def test_removes_read_only_nested_trees(self, tmp_path: Path) -> None:
        """0555 dirs with files, under tmp/ and repo/, are removed."""
        root = tmp_path / "ws"
        root.mkdir()
        ticket = "GO-7"
        ticket_dir = root / _sanitize_identifier(ticket)

        # Go-style cache: a 0555 dir with files and a 0555 subdirectory.
        tmp_cache = ticket_dir / _TMP_DIR / "go-build" / "cache"
        (tmp_cache / "sub").mkdir(parents=True)
        (tmp_cache / "file.txt").write_text("cached")
        (tmp_cache / "sub" / "nested.txt").write_text("nested")
        os.chmod(tmp_cache / "sub", 0o555)
        os.chmod(tmp_cache, 0o555)

        # The same shape inside the repo checkout.
        repo_cache = ticket_dir / _REPO_DIR / "vendor" / "cache"
        repo_cache.mkdir(parents=True)
        (repo_cache / "module.txt").write_text("module")
        os.chmod(repo_cache, 0o555)

        try:
            remove(ticket, str(root))

            assert not ticket_dir.exists()
        finally:
            self._restore_permissions(ticket_dir)

    def test_does_not_chmod_symlink_targets_outside_ticket_dir(
        self, tmp_path: Path
    ) -> None:
        """A symlink inside the read-only tree is unlinked, not chased.

        Both the failing dir and the symlink target start read-only: a helper
        that chmodded through the symlink would make the target writable and
        could reach its contents.
        """
        root = tmp_path / "ws"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_file = outside / "keep.txt"
        outside_file.write_text("outside data")
        os.chmod(outside, 0o555)

        ticket = "GO-8"
        ticket_dir = root / _sanitize_identifier(ticket)
        ro_dir = ticket_dir / _REPO_DIR / "cache"
        (ro_dir / "sub").mkdir(parents=True)
        (ro_dir / "sub" / "file.txt").write_text("cached")
        os.symlink(outside, ro_dir / "link")
        os.chmod(ro_dir, 0o555)

        try:
            remove(ticket, str(root))

            assert not ticket_dir.exists()
            assert stat.S_IMODE(outside.stat().st_mode) == 0o555
            assert sorted(entry.name for entry in outside.iterdir()) == ["keep.txt"]
            assert outside_file.read_text() == "outside data"
        finally:
            self._restore_permissions(ticket_dir)
            os.chmod(outside, 0o755)


class TestComputePaths:
    """The computed paths are <root>/<sanitized id>/<subdir>."""

    @pytest.mark.parametrize(
        ("compute", "parts"),
        [
            (compute_ticket_dir, ()),
            (compute_attachments_path, (_ATTACHMENTS_DIR,)),
            (compute_tmp_path, (_TMP_DIR,)),
        ],
        ids=["ticket", "attachments", "tmp"],
    )
    @pytest.mark.parametrize(
        ("identifier", "expected_key"),
        [("TEAM-42", "TEAM-42"), ("Team/With Spaces", "Team_With_Spaces")],
        ids=["plain", "sanitized"],
    )
    def test_compute_path(
        self,
        tmp_path: Path,
        compute,
        parts: tuple[str, ...],
        identifier: str,
        expected_key: str,
    ) -> None:
        root = tmp_path / "ws"
        assert compute(identifier, str(root)) == os.path.join(
            str(root), expected_key, *parts
        )


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
        _require_bwrap()

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
        _require_bwrap()

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

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/org/repo",
            "/tmp/some path/repo",
            "git@github.com:org/repo.git",
        ],
        ids=["plain-url", "local-path", "scp-like-remote"],
    )
    def test_leaves_non_credential_strings_unchanged(self, url: str) -> None:
        assert _redact_url(url) == url

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
                    sandbox_hide_paths=[],
                    tmp_path=str(tmp_path),
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
            path = clone_workspace(
                ticket_identifier="T-1",
                repo_url="https://github.com/org/repo.git",
                workspace_root=str(root),
                sandbox_hide_paths=[],
                tmp_path=str(tmp_path),
            )

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
                    sandbox_hide_paths=[],
                    tmp_path=str(tmp_path),
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
            "symphony_linear.workspace._run_git_in_sandbox",
            side_effect=[failed, succeeded],
        ) as run:
            _git_switch_branch("-weird", str(tmp_path), [], tmp_path=str(tmp_path))

        assert run.call_args_list[0].args[1] == ["switch", "--", "-weird"]
        assert run.call_args_list[1].args[1] == [
            "switch",
            "-c",
            "-weird",
            "--",
        ]

    def test_switch_branch_timeout_does_not_retry(self, tmp_path: Path) -> None:
        """A timed-out first switch raises and never starts `switch -c`."""
        with mock.patch(
            "symphony_linear.workspace._run_git_in_sandbox",
            side_effect=_SandboxGitTimeout("timed out after 60s"),
        ) as run:
            with pytest.raises(BranchFailed, match="timed out"):
                _git_switch_branch("branch", str(tmp_path), [], tmp_path=str(tmp_path))

        run.assert_called_once()


# ---------------------------------------------------------------------------
# Unit: clone_workspace — standalone clone step
# ---------------------------------------------------------------------------


class TestCloneWorkspace:
    """clone_workspace handles sanitization, containment, and cloning."""

    def test_clones_fresh_repo(self, tmp_path: Path) -> None:
        """A brand-new workspace clones the repo and returns the real path."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        result = clone_workspace(
            ticket_identifier="CLONE-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=str(tmp_path),
        )

        assert os.path.isdir(result)
        assert os.path.isdir(os.path.join(result, ".git"))
        # The clone lives in <root>/<sanitized_id>/repo.
        assert os.path.basename(os.path.realpath(result)) == "repo"
        assert os.path.basename(os.path.dirname(os.path.realpath(result))) == "CLONE-1"

        remove("CLONE-1", str(workspace_root))

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
                sandbox_hide_paths=[],
                tmp_path=str(tmp_path),
            )

    def test_sanitized_directory_name(self, tmp_path: Path) -> None:
        """The workspace directory uses the sanitized identifier."""
        _require_git()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        result = clone_workspace(
            ticket_identifier="Team/With Spaces",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=str(tmp_path),
        )

        expected_dir = workspace_root / "Team_With_Spaces" / "repo"
        assert os.path.realpath(result) == os.path.realpath(expected_dir)

        remove("Team/With Spaces", str(workspace_root))


# ---------------------------------------------------------------------------
# Unit: dirty_summary
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDirtySummary:
    """dirty_summary returns None when there is nothing to protect, and a
    short Markdown summary otherwise.

    The git commands run inside the sandbox since the ticket task, so this
    class needs bwrap as well as git.
    """

    @pytest.fixture(autouse=True)
    def _require_sandbox(self) -> None:
        _require_bwrap()

    @staticmethod
    def _sandbox_tmp(tmp_path: Path) -> str:
        """Create and return the tmp dir the sandbox mounts at /tmp."""
        sandbox_tmp = tmp_path / "sandbox-tmp"
        sandbox_tmp.mkdir(exist_ok=True)
        return str(sandbox_tmp)

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

        assert (
            dirty_summary(str(workspace), [], tmp_path=self._sandbox_tmp(tmp_path))
            is None
        )

    @pytest.mark.parametrize(
        ("filename", "contents"),
        [("untracked.txt", "hello"), ("README.md", "modified content")],
        ids=["untracked", "modified"],
    )
    def test_dirty_file_returns_summary(
        self, tmp_path: Path, filename: str, contents: str
    ) -> None:
        """A new or modified file yields a summary with a count and the name."""
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

        (workspace / filename).write_text(contents)

        summary = dirty_summary(
            str(workspace), [], tmp_path=self._sandbox_tmp(tmp_path)
        )
        assert summary is not None
        assert "1 uncommitted file," in summary
        assert filename in summary

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

        summary = dirty_summary(
            str(workspace), [], tmp_path=self._sandbox_tmp(tmp_path)
        )
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

        summary = dirty_summary(
            str(workspace), [], tmp_path=self._sandbox_tmp(tmp_path)
        )
        assert summary is not None
        assert "1 commit not on any remote" in summary

    def test_detached_head_commit_returns_summary(self, tmp_path: Path) -> None:
        """A commit made on a detached HEAD is unsaved work even though no
        branch points at it."""
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

        _run_git(["checkout", "--detach"], cwd=workspace)
        (workspace / "detached.txt").write_text("detached work")
        _run_git(["add", "detached.txt"], cwd=workspace)
        _run_git(
            [
                "-c",
                "user.email=test@test.com",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "detached commit",
            ],
            cwd=workspace,
        )

        summary = dirty_summary(
            str(workspace), [], tmp_path=self._sandbox_tmp(tmp_path)
        )
        assert summary is not None
        assert "1 commit not on any remote" in summary

    def test_stash_only_returns_summary(self, tmp_path: Path) -> None:
        """A stash entry is unsaved work even when the tree and branches are
        clean, and the summary names it."""
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

        (workspace / "README.md").write_text("stashed content")
        _run_git(["stash", "push", "-m", "unsaved work"], cwd=workspace)

        summary = dirty_summary(
            str(workspace), [], tmp_path=self._sandbox_tmp(tmp_path)
        )
        assert summary is not None
        assert "0 uncommitted files" in summary
        assert "0 commits not on any remote" in summary
        assert "1 stash entry" in summary

    def test_repository_without_commit_returns_none(self, tmp_path: Path) -> None:
        """An empty repository has no HEAD, which must not fail the check and
        must not read as unsaved work."""
        _require_git()

        workspace = tmp_path / "empty"
        workspace.mkdir()
        _run_git(["init", "-b", "main"], cwd=workspace)

        assert (
            dirty_summary(str(workspace), [], tmp_path=self._sandbox_tmp(tmp_path))
            is None
        )

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

        summary = dirty_summary(
            str(workspace), [], tmp_path=self._sandbox_tmp(tmp_path)
        )
        assert summary is not None
        assert "untracked.txt" in summary

    def test_non_repo_directory_returns_summary(self, tmp_path: Path) -> None:
        """A path that is not a git repo is conservatively dirty, so it is
        never deleted as "clean"."""
        plain = tmp_path / "plain"
        plain.mkdir()

        sandbox_tmp = self._sandbox_tmp(tmp_path)
        summary = dirty_summary(str(plain), [], tmp_path=sandbox_tmp)
        assert summary is not None
        assert "Could not verify workspace state" in summary

        # A path that does not exist has nothing to protect.
        assert (
            dirty_summary(str(tmp_path / "does-not-exist"), [], tmp_path=sandbox_tmp)
            is None
        )


# ---------------------------------------------------------------------------
# Integration: git commands in a checkout run inside the sandbox
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSandboxedGitCommands:
    """The dirty check and branch switch run the checkout's git programs in bwrap.

    A ticket checkout is writable by the agent, so ``core.fsmonitor`` (started
    by ``git status``) and the ``post-checkout`` hook (started by
    ``git switch``) are agent-controlled programs.  Each fixture program writes
    one marker inside the checkout — the positive control that proves it ran —
    and one into a host directory the sandbox mounts read-only.  A host
    invocation would create both; a sandboxed one can create only the inside
    marker, which is exactly what fails if the commands move back out of bwrap.
    """

    def test_checkout_hooks_run_inside_the_sandbox(self) -> None:
        _require_git()
        _require_bwrap()

        # The fixture lives outside /tmp: the sandbox replaces /tmp with the
        # per-ticket tmp dir, so an outside marker there would be invisible for
        # the wrong reason and the test would pass vacuously (gnosis njnsrm).
        with tempfile.TemporaryDirectory(
            prefix=".symphony-git-", dir=str(_base_outside_tmp())
        ) as fixture_root:
            root = Path(fixture_root)
            repo = root / "workspaces" / "SANDBOX-GIT" / "repo"
            sandbox_tmp = root / "workspaces" / "SANDBOX-GIT" / "tmp"
            outside = root / "outside"
            sandbox_tmp.mkdir(parents=True)
            outside.mkdir()

            _make_source_repo(repo)

            fsmonitor_script = repo / "fsmonitor.sh"
            fsmonitor_script.write_text(
                "#!/bin/bash\n"
                "echo ran >> ./fsmonitor-inside.txt\n"
                f"echo ran >> {outside / 'fsmonitor-outside.txt'}\n"
                "exit 0\n"
            )
            fsmonitor_script.chmod(0o755)
            _run_git(["config", "core.fsmonitor", str(fsmonitor_script)], cwd=repo)

            post_checkout = repo / ".git" / "hooks" / "post-checkout"
            post_checkout.write_text(
                "#!/bin/bash\n"
                "echo ran >> ./post-checkout-inside.txt\n"
                f"echo ran >> {outside / 'post-checkout-outside.txt'}\n"
                "exit 0\n"
            )
            post_checkout.chmod(0o755)

            # The fsmonitor program runs during the dirty check.
            summary = dirty_summary(str(repo), [], tmp_path=str(sandbox_tmp))
            assert summary is not None

            # The post-checkout hook runs when the branch is created.
            _git_switch_branch(
                "symphony/sandbox-git", str(repo), [], tmp_path=str(sandbox_tmp)
            )

            assert (repo / "fsmonitor-inside.txt").exists()
            assert (repo / "post-checkout-inside.txt").exists()
            assert not (outside / "fsmonitor-outside.txt").exists()
            assert not (outside / "post-checkout-outside.txt").exists()

            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            assert branch.stdout.strip() == "symphony/sandbox-git"


# ---------------------------------------------------------------------------
# Integration: clone_workspace decision for an existing checkout
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCloneWorkspaceExistingCheckout:
    """An existing checkout is replaced when clean, kept when dirty."""

    @staticmethod
    def _agent_sentinel(path: str) -> Path:
        """Return a marker inside .git, invisible to git status.

        A file in the working tree would make the checkout dirty and defeat
        the clean case; a file under .git survives only if the directory is
        not deleted.
        """
        sentinel = Path(path) / ".git" / "agent-sentinel"
        sentinel.write_text("agent was here")
        return sentinel

    def test_clean_checkout_is_cloned_again(self, tmp_path: Path) -> None:
        """No unsaved work → the checkout is deleted and re-cloned."""
        _require_git()
        _require_bwrap()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        path1 = clone_workspace(
            ticket_identifier="CLEAN-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("CLEAN-1", str(workspace_root)),
        )
        sentinel = self._agent_sentinel(path1)

        path2 = clone_workspace(
            ticket_identifier="CLEAN-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("CLEAN-1", str(workspace_root)),
        )

        assert path2 == path1
        # The old checkout, including its agent-written .git, is gone.
        assert not sentinel.exists()
        assert (Path(path2) / "README.md").is_file()

        remove("CLEAN-1", str(workspace_root))

    def test_dirty_checkout_is_kept(self, tmp_path: Path) -> None:
        """Unsaved work → the checkout is returned unchanged."""
        _require_git()
        _require_bwrap()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        path1 = clone_workspace(
            ticket_identifier="DIRTY-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("DIRTY-1", str(workspace_root)),
        )
        (Path(path1) / "dirty.txt").write_text("please preserve me")
        sentinel = self._agent_sentinel(path1)

        path2 = clone_workspace(
            ticket_identifier="DIRTY-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("DIRTY-1", str(workspace_root)),
        )

        assert path2 == path1
        assert (Path(path2) / "dirty.txt").read_text() == "please preserve me"
        assert sentinel.exists()

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
        ws_path = clone_workspace(
            ticket_identifier="FINALIZE-1",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("FINALIZE-1", str(workspace_root)),
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

        ws_path = clone_workspace(
            ticket_identifier="NO-BRANCH",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("NO-BRANCH", str(workspace_root)),
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
        _require_bwrap()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)  # no setup script

        ws_path = clone_workspace(
            ticket_identifier="NO-SETUP-F",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("NO-SETUP-F", str(workspace_root)),
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

        ws_path = clone_workspace(
            ticket_identifier="FAIL-FIN",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("FAIL-FIN", str(workspace_root)),
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
        _require_bwrap()

        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir()

        source_repo = tmp_path / "source"
        _make_source_repo(source_repo)

        ws_path = clone_workspace(
            ticket_identifier="My-Team.42",
            repo_url=str(source_repo),
            workspace_root=str(workspace_root),
            sandbox_hide_paths=[],
            tmp_path=ensure_tmp_dir("My-Team.42", str(workspace_root)),
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
    """start_serve raises ServeScriptMissing when the script is unusable."""

    @pytest.mark.parametrize("layout", ["no-dir", "no-file", "not-executable"])
    def test_raises_when_script_unusable(self, tmp_path: Path, layout: str) -> None:
        workspace = tmp_path / "ws"
        if layout == "no-dir":
            workspace.mkdir()
        else:
            symphony_dir = workspace / ".symphony"
            symphony_dir.mkdir(parents=True)
            if layout == "not-executable":
                serve_file = symphony_dir / "serve"
                serve_file.write_text("#!/bin/bash\nsleep 60\n")
                serve_file.chmod(0o644)

        with pytest.raises(ServeScriptMissing, match=r"\.symphony/serve"):
            start_serve(str(workspace), hide_paths=[], tmp_path=str(tmp_path / "t"))


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
