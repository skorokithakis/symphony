"""Per-project .symphony/config.yaml loader.

Reads an optional YAML config from ``<workspace_path>/.symphony/config.yaml``
that can override select global settings on a per-project basis.

On each call, attempts to read the config from ``origin/HEAD`` via
``git show`` so that repo-side fixes to the file are picked up regardless
of which branch the workspace is on.  Falls back to the working-tree file
when the workspace is not a git clone or ``origin/HEAD`` is unavailable.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class ProjectConfigError(Exception):
    """Raised when a project config file is malformed or contains invalid values."""


class ProjectConfig(BaseModel):
    """Per-project overrides that can be optionally set in .symphony/config.yaml."""

    model_config = ConfigDict(extra="forbid")

    auto_branch: bool | None = Field(
        None, description="Override the global auto_branch setting"
    )
    turn_timeout_seconds: int | None = Field(
        None, gt=0, description="Override the global turn_timeout_seconds setting"
    )


def _parse_yaml_config(raw: str, source: str) -> ProjectConfig:
    """Parse raw YAML text and return a validated ``ProjectConfig``.

    Args:
        raw: Raw YAML string.
        source: Human-readable label for the config source (used in error messages).

    Returns:
        A ``ProjectConfig`` instance. Returns an empty config when the content
        is empty or a null document.

    Raises:
        ProjectConfigError: If the YAML is malformed or validation fails.
    """
    if not raw.strip():
        logger.debug("Project config from %s is empty", source)
        return ProjectConfig()

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ProjectConfigError(
            f"Invalid YAML in project config from {source}: {exc}"
        ) from exc

    # None from yaml.safe_load means an empty document (e.g. comments-only).
    if data is None:
        return ProjectConfig()

    if not isinstance(data, dict):
        raise ProjectConfigError(
            f"Project config from {source} must be a mapping, got {type(data).__name__}"
        )

    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        lines: list[str] = []
        for err in exc.errors():
            loc = " -> ".join(str(p) for p in err["loc"])
            lines.append(f"  {loc}: {err['msg']}")
        msg = (
            f"Project config validation failed for {source}:\n"
            + "\n".join(lines)
        )
        raise ProjectConfigError(msg) from exc


def _try_read_from_origin(workspace_path: str) -> str | None:
    """Try to read ``.symphony/config.yaml`` from ``origin/HEAD``.

    Args:
        workspace_path: Path to the workspace directory (a git clone).

    Returns:
        - The raw file content if the file exists in ``origin/HEAD``.
        - ``""`` if ``origin/HEAD`` exists but the file does not exist there.
        - ``None`` if ``origin/HEAD`` is unresolvable, ``git fetch`` failed,
          the workspace is not a git repo, or the remote is unreachable
          (caller should fall back to the working-tree file).
    """
    # Best-effort fetch so origin refs are fresh.
    fetch_ok = True
    try:
        result = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug(
                "git fetch origin failed in %s (rc=%d) — will fall back to working tree",
                workspace_path,
                result.returncode,
            )
            fetch_ok = False
    except Exception:
        logger.debug(
            "git fetch origin failed in %s — will fall back to working tree",
            workspace_path,
        )
        fetch_ok = False

    if not fetch_ok:
        return None

    # Check if origin/HEAD is resolvable.
    rev_parse = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/HEAD"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
    )
    if rev_parse.returncode != 0:
        logger.debug(
            "origin/HEAD not resolvable in %s — falling back to working tree",
            workspace_path,
        )
        return None

    # Try to read the file via git show.
    show = subprocess.run(
        ["git", "show", "origin/HEAD:.symphony/config.yaml"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
    )
    if show.returncode == 0:
        logger.debug("Read project config from origin/HEAD in %s", workspace_path)
        return show.stdout

    # origin/HEAD exists but the file does not — treat as no config.
    logger.debug(
        ".symphony/config.yaml not found in origin/HEAD for %s — returning empty config",
        workspace_path,
    )
    return ""


def load_project_config(workspace_path: str) -> ProjectConfig:
    """Load and validate an optional per-project config file.

    Attempts to read ``.symphony/config.yaml`` from ``origin/HEAD`` first,
    falling back to the working-tree file when the remote copy is unavailable.

    Args:
        workspace_path: Path to the workspace directory. The config file is
            expected at ``<workspace_path>/.symphony/config.yaml``.

    Returns:
        A ``ProjectConfig`` instance. Returns an empty config (all fields
        ``None``) when no config file exists.

    Raises:
        ProjectConfigError: If the YAML is malformed or validation fails.
    """
    # Try to read from origin/HEAD first.
    remote_content = _try_read_from_origin(workspace_path)
    if remote_content is not None:
        if remote_content == "":
            # File missing on origin — return empty config.
            return ProjectConfig()
        return _parse_yaml_config(remote_content, f"origin/HEAD in {workspace_path}")

    # Fall back to working-tree file.
    config_file = Path(workspace_path) / ".symphony" / "config.yaml"

    if not config_file.is_file():
        logger.debug("No project config found at %s", config_file)
        return ProjectConfig()

    raw = config_file.read_text()
    return _parse_yaml_config(raw, str(config_file))
