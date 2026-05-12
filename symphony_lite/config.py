"""Typed configuration loader for symphony-lite.

Reads YAML from ``~/.config/symphony-lite/config.yaml`` (overridable via
``$SYMPHONY_CONFIG``), validates with Pydantic v2, and expands ``~`` / ``$VAR``
references in string values.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "symphony-lite" / "config.yaml"

DEFAULT_HIDE_PATHS: list[str] = [
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.config/gcloud",
    "~/.netrc",
    "~/.docker",
    # Docker socket (real path; /var/run is a symlink to /run on most systems)
    "/run/docker.sock",
]


# ---------------------------------------------------------------------------
# Helpers: env-var / tilde expansion
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{?(\w+)\}?")


def _expand(value: str) -> str:
    """Expand ``~`` and ``$VAR`` / ``${VAR}`` references in *value*."""
    # Tilde expansion
    if value.startswith("~") and (len(value) == 1 or value[1] in ("/", os.sep)):
        value = str(Path(value).expanduser())
    # Env-var expansion
    def _sub(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(0))

    return _VAR_RE.sub(_sub, value)


def _expand_values(obj: Any) -> Any:
    """Recursively expand env vars in every string inside *obj*."""
    if isinstance(obj, str):
        return _expand(obj)
    if isinstance(obj, dict):
        return {k: _expand_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_values(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Pydantic sub-models
# ---------------------------------------------------------------------------


class _LinearConfig(BaseModel):
    api_key: str = Field(..., description="Linear API key (bearer token)")
    trigger_label: str = Field("agent", description="Label that triggers the bot")
    in_progress_state: str = Field("In Progress", description="Linear state for active work")
    needs_input_state: str = Field("Needs Input", description="Linear state when input is needed")
    bot_user_email: str = Field(..., description="Email of the bot user in Linear")


class _SandboxConfig(BaseModel):
    hide_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_HIDE_PATHS),
        description="Paths to conceal inside the sandbox",
    )


class _OpenCodeConfig(BaseModel):
    # Optional: if unset, OpenCode uses whatever model its own config selects.
    model: str | None = Field(
        None,
        description="OpenCode model identifier (e.g. anthropic/claude-sonnet-4); if unset, OpenCode's default is used",
    )


class AppConfig(BaseModel):
    """Top-level application configuration."""

    linear: _LinearConfig
    sandbox: _SandboxConfig = Field(default_factory=_SandboxConfig)
    opencode: _OpenCodeConfig
    workspace_root: Path = Field(
        default_factory=lambda: Path("~/symphony/ws").expanduser(),
        description="Root directory for per-ticket workspaces",
    )
    poll_interval_seconds: int = Field(30, gt=0, description="Seconds between Linear poll cycles")
    turn_timeout_seconds: int = Field(1800, gt=0, description="Max seconds per AI turn")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _resolve_config_path() -> Path:
    env = os.environ.get("SYMPHONY_CONFIG")
    return Path(env).expanduser() if env else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> AppConfig:
    """Load, expand, and validate the application configuration.

    Args:
        path: Optional explicit path to the config file.  When ``None`` the
            default path is used (``~/.config/symphony-lite/config.yaml``),
            which can be overridden via ``$SYMPHONY_CONFIG``.

    Returns:
        A fully-validated ``AppConfig`` instance.

    Raises:
        FileNotFoundError: The config file does not exist.
        ValueError: The config file contains invalid YAML or fails validation.
    """
    if path is None:
        path = _resolve_config_path()

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if raw is None:
        raise ValueError(f"Config file is empty: {path}")

    expanded = _expand_values(raw)

    try:
        return AppConfig.model_validate(expanded)
    except ValidationError as exc:
        # Re-raise with a friendlier message that includes the file path.
        msg = f"Config validation failed for {path}:\n{_format_errors(exc)}"
        raise ValueError(msg) from exc


def _format_errors(exc: ValidationError) -> str:
    """Format Pydantic validation errors into a readable multi-line string."""
    lines: list[str] = []
    for err in exc.errors():
        loc = " -> ".join(str(part) for part in err["loc"])
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)
