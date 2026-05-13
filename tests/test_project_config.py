"""Tests for project config loading and validation."""

from __future__ import annotations

import os
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


def test_file_missing_returns_empty_config(tmp_path: Path) -> None:
    """When .symphony/config.yaml doesn't exist, return empty ProjectConfig."""
    result = load_project_config(str(tmp_path))
    assert isinstance(result, ProjectConfig)
    assert result.auto_branch is None
    assert result.turn_timeout_seconds is None


def test_file_empty_returns_empty_config(tmp_path: Path) -> None:
    """When the file exists but is empty, return empty ProjectConfig."""
    cfg_dir = tmp_path / ".symphony"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("")

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is None
    assert result.turn_timeout_seconds is None


def test_file_whitespace_only_returns_empty_config(tmp_path: Path) -> None:
    """Whitespace-only file returns empty ProjectConfig."""
    cfg_dir = tmp_path / ".symphony"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("   \n  \n")

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is None
    assert result.turn_timeout_seconds is None


def test_file_comments_only_returns_empty_config(tmp_path: Path) -> None:
    """Comments-only YAML file returns empty ProjectConfig."""
    _write_project_config(tmp_path, "# just a comment\n# another one\n")

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is None
    assert result.turn_timeout_seconds is None


def test_null_document_returns_empty_config(tmp_path: Path) -> None:
    """Explicit null YAML document returns empty ProjectConfig."""
    _write_project_config(tmp_path, "null")

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is None
    assert result.turn_timeout_seconds is None


def test_empty_dict_returns_empty_config(tmp_path: Path) -> None:
    """Explicit empty dict returns empty ProjectConfig."""
    _write_project_config(tmp_path, {})

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is None
    assert result.turn_timeout_seconds is None


# ---------------------------------------------------------------------------
# Valid overrides
# ---------------------------------------------------------------------------


def test_valid_auto_branch_override(tmp_path: Path) -> None:
    """auto_branch can be set to True or False."""
    _write_project_config(tmp_path, {"auto_branch": False})

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is False
    assert result.turn_timeout_seconds is None


def test_valid_turn_timeout_override(tmp_path: Path) -> None:
    """turn_timeout_seconds can be set to a positive integer."""
    _write_project_config(tmp_path, {"turn_timeout_seconds": 600})

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is None
    assert result.turn_timeout_seconds == 600


def test_both_keys_set(tmp_path: Path) -> None:
    """Both keys can be set at once."""
    _write_project_config(tmp_path, {"auto_branch": True, "turn_timeout_seconds": 900})

    result = load_project_config(str(tmp_path))
    assert result.auto_branch is True
    assert result.turn_timeout_seconds == 900


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


def test_wrong_type_auto_branch_raises(tmp_path: Path) -> None:
    """auto_branch must be a bool, not a number."""
    _write_project_config(tmp_path, {"auto_branch": 42})

    with pytest.raises(ProjectConfigError, match="auto_branch"):
        load_project_config(str(tmp_path))


def test_turn_timeout_zero_raises(tmp_path: Path) -> None:
    """turn_timeout_seconds must be > 0."""
    _write_project_config(tmp_path, {"turn_timeout_seconds": 0})

    with pytest.raises(ProjectConfigError, match="turn_timeout_seconds"):
        load_project_config(str(tmp_path))


def test_turn_timeout_negative_raises(tmp_path: Path) -> None:
    """turn_timeout_seconds must be positive, negative raises."""
    _write_project_config(tmp_path, {"turn_timeout_seconds": -1})

    with pytest.raises(ProjectConfigError, match="turn_timeout_seconds"):
        load_project_config(str(tmp_path))


def test_turn_timeout_float_raises(tmp_path: Path) -> None:
    """turn_timeout_seconds must be an int, not a float."""
    _write_project_config(tmp_path, {"turn_timeout_seconds": 3.14})

    with pytest.raises(ProjectConfigError, match="turn_timeout_seconds"):
        load_project_config(str(tmp_path))


def test_not_a_mapping_raises(tmp_path: Path) -> None:
    """Root YAML node must be a mapping."""
    _write_project_config(tmp_path, ["item1", "item2"])

    with pytest.raises(ProjectConfigError, match="mapping"):
        load_project_config(str(tmp_path))


def test_error_message_includes_file_path(tmp_path: Path) -> None:
    """ProjectConfigError messages include the path to the config file."""
    _write_project_config(tmp_path, {"auto_branch": 42})

    with pytest.raises(ProjectConfigError, match=".symphony/config.yaml"):
        load_project_config(str(tmp_path))


# ---------------------------------------------------------------------------
# Git-based tests: load_project_config reads from origin/HEAD
# ---------------------------------------------------------------------------


class TestLoadProjectConfigFromOrigin:
    """load_project_config reads from origin/HEAD via git show."""

    def test_origin_differs_from_disk_origin_wins(
        self, tmp_path: Path
    ) -> None:
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

    def test_file_missing_in_origin_returns_empty(
        self, tmp_path: Path
    ) -> None:
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

    def test_origin_head_unresolvable_falls_back_to_disk(
        self, tmp_path: Path
    ) -> None:
        """When origin/HEAD is not resolvable, falls back to working-tree file."""
        _require_git()

        # 1. Create source repo with a config.
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

        # 3. Remove the origin remote so origin/HEAD becomes unresolvable.
        subprocess.run(
            ["git", "remote", "remove", "origin"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=True,
        )

        # 4. Modify the local file on disk (it's the only source of truth now).
        ws_config = workspace / ".symphony" / "config.yaml"
        ws_config.write_text("auto_branch: true\n")

        # 5. Should fall back to disk, reading the local version.
        result = load_project_config(str(workspace))
        assert result.auto_branch is True

    def test_not_a_git_repo_falls_back_to_disk(
        self, tmp_path: Path
    ) -> None:
        """When workspace is not a git repo, falls back to working-tree file."""
        # Just a plain directory with a config file.
        workspace = tmp_path / "not-a-repo"
        workspace.mkdir()
        _write_project_config(workspace, {"turn_timeout_seconds": 900})

        result = load_project_config(str(workspace))
        assert result.turn_timeout_seconds == 900
        assert result.auto_branch is None

    def test_no_config_anywhere_returns_empty(self, tmp_path: Path) -> None:
        """When neither origin/HEAD nor disk has a config, returns empty."""
        _require_git()

        # 1. Create source repo without config.
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

        # No config file anywhere.
        result = load_project_config(str(workspace))
        assert result.auto_branch is None
        assert result.turn_timeout_seconds is None

    def test_origin_fetch_failure_falls_back_to_disk(
        self, tmp_path: Path
    ) -> None:
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
