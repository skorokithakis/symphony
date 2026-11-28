"""Tests for project config loading and validation."""

from __future__ import annotations

import os
import threading
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
# Symlinks and special files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shape", ["file", "directory"], ids=["symlinked-file", "symlinked-dir"]
)
def test_config_symlink_raises(tmp_path: Path, shape: str) -> None:
    """A config path with a symlink in it is refused, even inside the checkout.

    The agent can write the checkout, so no symbolic link at either component
    is followed by this host-side read.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.yaml").write_text("turn_timeout_seconds: 60\n")

    if shape == "file":
        (checkout / ".symphony").mkdir()
        (checkout / ".symphony" / "config.yaml").symlink_to(outside / "config.yaml")
    else:
        (checkout / ".symphony").symlink_to(outside)

    with pytest.raises(ProjectConfigError, match="symbolic link is not allowed"):
        load_project_config(str(checkout))


def test_config_fifo_returns_error_promptly(tmp_path: Path) -> None:
    """A FIFO is refused without blocking the worker thread.

    A read-only open of a FIFO blocks until a writer appears, so the
    descriptor is opened ``O_NONBLOCK`` and its type is checked with fstat.  A
    daemon thread turns a regression into a prompt failure instead of a hung
    test run.
    """
    checkout = tmp_path / "checkout"
    (checkout / ".symphony").mkdir(parents=True)
    os.mkfifo(checkout / ".symphony" / "config.yaml")

    errors: list[BaseException] = []

    def load() -> None:
        try:
            load_project_config(str(checkout))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=load, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "load_project_config blocked on a FIFO"
    assert len(errors) == 1
    assert isinstance(errors[0], ProjectConfigError)
    assert "not a regular file" in str(errors[0])
