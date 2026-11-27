"""Tests for project config loading and validation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from symphony_linear.project_config import (
    ProjectConfig,
    ProjectConfigError,
    load_project_config,
)


def _write_project_config(workspace: Path, data: object) -> Path:
    """Write a .symphony/config.yaml file and return its path."""
    cfg_dir = workspace / ".symphony"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.yaml"
    path.write_text(yaml.dump(data) if not isinstance(data, str) else data)
    return path


def _require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")


def _make_source_repo(path: Path, *, config_data: str | None = None) -> None:
    """Create a minimal git repo at *path* with one commit.

    If *config_data* is provided, it is committed as ``.symphony/config.yaml``.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@symphony.local"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )
    (path / "README.md").write_text("# Test Repo\n")

    if config_data is not None:
        cfg_dir = path / ".symphony"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.yaml").write_text(config_data)
        subprocess.run(
            ["git", "add", ".symphony/config.yaml"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=True,
        )

    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# File absent / empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contents",
    [None, "", "   \n  \n", "# just a comment\n# another one\n", "null", "{}"],
    ids=["missing", "empty", "whitespace", "comments", "null-document", "empty-dict"],
)
def test_no_settings_returns_empty_config(tmp_path: Path, contents: str | None) -> None:
    """A missing, empty or settings-free file yields an empty ProjectConfig."""
    if contents is not None:
        cfg_dir = tmp_path / ".symphony"
        cfg_dir.mkdir()
        (cfg_dir / "config.yaml").write_text(contents)

    result = load_project_config(str(tmp_path))
    assert isinstance(result, ProjectConfig)
    assert result.auto_branch is None
    assert result.turn_timeout_seconds is None


# ---------------------------------------------------------------------------
# Valid overrides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "auto_branch", "turn_timeout_seconds"),
    [
        ({"auto_branch": False}, False, None),
        ({"turn_timeout_seconds": 600}, None, 600),
        ({"auto_branch": True, "turn_timeout_seconds": 900}, True, 900),
    ],
    ids=["auto-branch", "turn-timeout", "both"],
)
def test_overrides_are_read(
    tmp_path: Path,
    data: dict[str, object],
    auto_branch: bool | None,
    turn_timeout_seconds: int | None,
) -> None:
    _write_project_config(tmp_path, data)

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is auto_branch
    assert result.turn_timeout_seconds == turn_timeout_seconds


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_unknown_key_raises_project_config_error(tmp_path: Path) -> None:
    """An unknown key triggers ProjectConfigError (extra='forbid')."""
    _write_project_config(tmp_path, {"auto_branch": True, "foo": "bar"})

    with pytest.raises(ProjectConfigError, match="foo"):
        load_project_config(str(tmp_path))


def test_malformed_yaml_raises_project_config_error(tmp_path: Path) -> None:
    """Malformed YAML triggers ProjectConfigError."""
    cfg_dir = tmp_path / ".symphony"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("key: [unclosed\n")

    with pytest.raises(ProjectConfigError, match="Invalid YAML"):
        load_project_config(str(tmp_path))


def test_not_a_mapping_raises(tmp_path: Path) -> None:
    """Root YAML node must be a mapping."""
    _write_project_config(tmp_path, ["item1", "item2"])

    with pytest.raises(ProjectConfigError, match="mapping"):
        load_project_config(str(tmp_path))


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"auto_branch": 42}, "auto_branch"),
        ({"turn_timeout_seconds": 0}, "turn_timeout_seconds"),
        ({"turn_timeout_seconds": -1}, "turn_timeout_seconds"),
        ({"turn_timeout_seconds": 3.14}, "turn_timeout_seconds"),
    ],
    ids=["auto-branch-not-bool", "zero", "negative", "float"],
)
def test_invalid_value_raises_project_config_error(
    tmp_path: Path, data: dict[str, object], field: str
) -> None:
    _write_project_config(tmp_path, data)

    with pytest.raises(ProjectConfigError, match=field):
        load_project_config(str(tmp_path))


# ---------------------------------------------------------------------------
# Git-based tests: load_project_config reads from origin/HEAD
# ---------------------------------------------------------------------------


class TestLoadProjectConfigFromOrigin:
    """load_project_config reads from origin/HEAD via git show."""

    def test_origin_differs_from_disk_origin_wins(self, tmp_path: Path) -> None:
        """When the origin version differs from disk, origin is used."""
        _require_git()

        # 1. Create source repo with config version A.
        source = tmp_path / "source"
        _make_source_repo(source, config_data="auto_branch: false\n")
        source_config = source / ".symphony" / "config.yaml"
        assert source_config.is_file()

        # 2. Clone into workspace.
        workspace = tmp_path / "workspace"
        subprocess.run(
            ["git", "clone", str(source), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )
        # Workspace starts with version A.
        ws_config = workspace / ".symphony" / "config.yaml"
        assert ws_config.read_text() == "auto_branch: false\n"

        # 3. Update the config on origin (version B).
        source_config.write_text("auto_branch: true\n")
        subprocess.run(
            ["git", "add", ".symphony/config.yaml"],
            cwd=str(source),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "update config to B"],
            cwd=str(source),
            capture_output=True,
            text=True,
            check=True,
        )

        # 4. Also modify the working-tree file locally (version C).
        ws_config.write_text("turn_timeout_seconds: 999\n")

        # 5. load_project_config should do git fetch + git show, getting version B.
        result = load_project_config(str(workspace))
        assert result.auto_branch is True
        assert result.turn_timeout_seconds is None  # not version C from disk

    def test_file_missing_in_origin_returns_empty(self, tmp_path: Path) -> None:
        """When origin/HEAD exists but file is missing there, return empty config."""
        _require_git()

        # 1. Create source repo WITHOUT config.yaml.
        source = tmp_path / "source"
        _make_source_repo(source)

        # 2. Clone into workspace.
        workspace = tmp_path / "workspace"
        subprocess.run(
            ["git", "clone", str(source), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        # 3. Write a local config file on disk (not committed, not on origin).
        _write_project_config(workspace, {"auto_branch": True})

        # 4. load_project_config: origin/HEAD is reachable, but no config there.
        result = load_project_config(str(workspace))
        assert result.auto_branch is None  # empty — local file ignored

    def test_not_a_git_repo_falls_back_to_disk(self, tmp_path: Path) -> None:
        """When workspace is not a git repo, falls back to working-tree file."""
        # Just a plain directory with a config file.
        workspace = tmp_path / "not-a-repo"
        workspace.mkdir()
        _write_project_config(workspace, {"turn_timeout_seconds": 900})

        result = load_project_config(str(workspace))
        assert result.turn_timeout_seconds == 900
        assert result.auto_branch is None

    def test_origin_fetch_failure_falls_back_to_disk(self, tmp_path: Path) -> None:
        """When git fetch fails (e.g. offline), falls back to working-tree file."""
        _require_git()

        # 1. Create source repo with config.
        source = tmp_path / "source"
        _make_source_repo(source, config_data="auto_branch: false\n")

        # 2. Clone into workspace.
        workspace = tmp_path / "workspace"
        subprocess.run(
            ["git", "clone", str(source), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        # 3. Point origin to a non-existent URL so fetch fails.
        subprocess.run(
            ["git", "remote", "set-url", "origin", "/nonexistent/path"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=True,
        )

        # 4. Modify the local file on disk.
        ws_config = workspace / ".symphony" / "config.yaml"
        ws_config.write_text("auto_branch: true\nturn_timeout_seconds: 600\n")

        # 5. Should fall back to disk after fetch failure.
        result = load_project_config(str(workspace))
        assert result.auto_branch is True
        assert result.turn_timeout_seconds == 600

    def test_malformed_origin_config_raises(self, tmp_path: Path) -> None:
        """Malformed YAML on origin raises ProjectConfigError."""
        _require_git()

        # 1. Create source repo with malformed config.
        source = tmp_path / "source"
        _make_source_repo(source, config_data="key: [unclosed\n")

        # 2. Clone into workspace.
        workspace = tmp_path / "workspace"
        subprocess.run(
            ["git", "clone", str(source), str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        )

        # Should raise because origin/config is malformed.
        with pytest.raises(ProjectConfigError, match="Invalid YAML"):
            load_project_config(str(workspace))
