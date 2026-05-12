"""Typed configuration loader for symphony-lite.

Reads YAML from ``<workspace_dir>/config.yaml``, validates with Pydantic v2,
and expands ``~`` / ``$VAR`` references in string values.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

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
        return os.environ.get(m.group(1), "")

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
    extra_rw_paths: list[str] = Field(
        default_factory=list,
        description="Additional host paths to bind read-write inside the sandbox",
    )


class _OpenCodeConfig(BaseModel):
    # Optional: if unset, OpenCode uses whatever model its own config selects.
    model: str | None = Field(
        None,
        description="OpenCode model identifier (e.g. anthropic/claude-sonnet-4); if unset, OpenCode's default is used",
    )


class AppConfig(BaseModel):
    """Top-level application configuration."""

    model_config = ConfigDict(extra="forbid")

    linear: _LinearConfig
    sandbox: _SandboxConfig = Field(default_factory=_SandboxConfig)
    opencode: _OpenCodeConfig = Field(default_factory=_OpenCodeConfig)
    poll_interval_seconds: int = Field(30, gt=0, description="Seconds between Linear poll cycles")
    turn_timeout_seconds: int = Field(1800, gt=0, description="Max seconds per AI turn")

    @model_validator(mode="before")
    @classmethod
    def _drop_null_subconfigs(cls, data: Any) -> Any:
        # YAML keys with empty values (e.g. `opencode:` on its own line) parse
        # as ``None``. For sub-config fields that have a default, treat ``None``
        # as "use the default" rather than failing validation.
        if isinstance(data, dict):
            for key in ("sandbox", "opencode"):
                if key in data and data[key] is None:
                    del data[key]
        return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(workspace_dir: Path) -> AppConfig:
    """Load, expand, and validate the application configuration.

    Args:
        workspace_dir: Path to the workspace directory containing
            ``config.yaml``.

    Returns:
        A fully-validated ``AppConfig`` instance.

    Raises:
        FileNotFoundError: The config file does not exist.
        ValueError: The config file contains invalid YAML or fails validation.
    """
    path = workspace_dir / "config.yaml"

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            f"Create a config.yaml file in {workspace_dir} with the required settings."
        )

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if raw is None:
        raise ValueError(f"Config file is empty: {path}")

    expanded = _expand_values(raw)

    # LINEAR_API_KEY environment variable fallback: if linear.api_key is
    # missing or empty in the YAML, fall back to the LINEAR_API_KEY env var.
    if isinstance(expanded, dict):
        linear = expanded.setdefault("linear", {})
        if isinstance(linear, dict) and not linear.get("api_key", ""):
            env_key = os.environ.get("LINEAR_API_KEY", "")
            if env_key:
                linear["api_key"] = env_key
            else:
                raise ValueError(
                    f"Linear API key not set. Provide it in {path} "
                    f"(linear.api_key) or via the LINEAR_API_KEY "
                    f"environment variable."
                )

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
