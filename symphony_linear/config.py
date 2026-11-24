"""Typed configuration loader for symphony-lite.

Reads YAML from ``<workspace_dir>/config.yaml``, validates with Pydantic v2,
and expands ``~`` / ``$VAR`` references in string values.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


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
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(..., description="Linear API key (bearer token)")
    trigger_label: str | None = Field(
        "Agent",
        description=(
            "Optional label that triggers the bot; null uses active states and "
            "a project Repo link"
        ),
    )

    @field_validator("trigger_label")
    @classmethod
    def _validate_trigger_label(cls, v: str | None) -> str | None:
        if v == "":
            raise ValueError("trigger_label must be a non-empty string or null")
        return v

    in_progress_state: str = Field(
        "In Progress", description="Linear state for active work"
    )
    needs_input_state: str = Field(
        "Needs Input", description="Linear state when input is needed"
    )
    qa_state: str | None = Field(
        None,
        description="Optional Linear state for QA; polled in addition to in_progress and needs_input",
    )


# Compile once to share between config-level and tracker-level validation.
_GITHUB_PROJECT_REF_RE = re.compile(r"^(orgs|users)/([^/]+)/projects/(\d+)$")


class _GitHubConfig(BaseModel):
    """GitHub Projects v2 backend configuration."""

    token: str = Field(..., description="GitHub personal-access or installation token")
    project: str = Field(
        ...,
        description="Project reference: orgs/<org>/projects/<n> or users/<user>/projects/<n>",
    )
    trigger_field: str = Field(
        "Symphony", description="Single-select field name that triggers the bot"
    )
    status_field: str = Field(
        "Status", description="Single-select field name for workflow state"
    )
    in_progress_status: str = Field(
        "In Progress", description="Status option name for in-progress work"
    )
    needs_input_status: str = Field(
        "Needs Input", description="Status option name when input is needed"
    )
    qa_status: str | None = Field(
        None,
        description="Optional status option name for QA; polled in addition to in_progress and needs_input",
    )
    clone_protocol: str = Field(
        "ssh",
        description="Protocol for cloning repositories: 'ssh' (default) or 'https'",
    )

    @field_validator("clone_protocol")
    @classmethod
    def _validate_clone_protocol(cls, v: str) -> str:
        if v not in ("ssh", "https"):
            raise ValueError(f"clone_protocol must be 'ssh' or 'https', got {v!r}")
        return v

    @field_validator("project")
    @classmethod
    def _validate_project_ref(cls, v: str) -> str:
        if not _GITHUB_PROJECT_REF_RE.match(v):
            raise ValueError(
                f"Invalid project ref: {v!r}. "
                f"Expected format: orgs/<org>/projects/<number> or "
                f"users/<user>/projects/<number>"
            )
        return v


class _SandboxConfig(BaseModel):
    hide_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_HIDE_PATHS),
        description="Paths to conceal inside the sandbox",
    )
    extra_rw_paths: list[str] = Field(
        default_factory=list,
        description="Additional host paths to bind read-write inside the sandbox",
    )
    dir_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of sandbox-internal paths (keys) to host source directories "
            "(values). Relative values resolve under each ticket's mounts/ "
            "directory; absolute values are shared host directories."
        ),
    )
    secrets_dir: str | None = Field(
        None,
        description=(
            "Directory holding per-repo .env secrets files, laid out as "
            "<host>/<owner>/<repo>.env. None means <workspace>/secrets. "
            "Supports ~ and $VAR expansion."
        ),
    )

    @field_validator("dir_map", mode="before")
    @classmethod
    def _expand_dir_map_keys(cls, v: Any) -> Any:
        # _expand_values walks values only, so expand ~ in keys explicitly.
        # Values are already expanded by the loader.
        if not isinstance(v, dict):
            return v
        expanded: dict[str, str] = {}
        for key, val in v.items():
            if (
                isinstance(key, str)
                and key.startswith("~")
                and (len(key) == 1 or key[1] in ("/", os.sep))
            ):
                key = str(Path(key).expanduser())
            expanded[key] = val
        return expanded

    @field_validator("dir_map")
    @classmethod
    def _validate_dir_map(cls, v: dict[str, str]) -> dict[str, str]:
        for dest, source in v.items():
            if not isinstance(dest, str) or not os.path.isabs(dest):
                raise ValueError(
                    f"dir_map key {dest!r} must be an absolute path inside "
                    f"the sandbox (after ~ expansion)"
                )
            if not isinstance(source, str) or not source:
                raise ValueError(
                    f"dir_map value for {dest!r} must be a non-empty string"
                )
            if ".." in Path(source).parts:
                raise ValueError(f"dir_map value for {dest!r} must not contain '..'")
        return v


class _WebhookConfig(BaseModel):
    """Optional webhook server configuration (Linear only)."""

    port: int = Field(..., gt=0, le=65535, description="Port for the webhook server")
    linear_secret: str = Field(
        ...,
        description=(
            "Linear webhook secret for HMAC signature verification. "
            "Fall back to SYMPHONY_LINEAR_WEBHOOK_SECRET env var if empty/missing."
        ),
    )


def _split_ntfy_topic_url(url: str) -> tuple[str, str]:
    """Return ``(server_root, topic)`` for a full ntfy topic URL.

    ntfy's JSON publish targets the server root and names the topic in the
    request body, so a configured topic URL is split rather than posted to as
    is.  A path prefix stays on the root because a self-hosted server may live
    under one (``https://example.com/ntfy`` plus ``/mytopic``), and it is kept
    exactly as written.  Only the final segment is percent-decoded: the topic
    in the JSON body is the real name, while decoding the prefix could change
    the path the server is served from.  A query or fragment would be silently
    dropped by the split, so it is rejected instead.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("must be an absolute http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            "must not contain user information (user:pass@); "
            "use the 'token' field instead"
        )
    if parts.query or parts.fragment:
        raise ValueError("must not contain a query string or fragment")
    root_path, _, topic = parts.path.rstrip("/").rpartition("/")
    if not topic:
        raise ValueError("must include a topic, e.g. https://ntfy.sh/mytopic")
    return (
        urlunsplit((parts.scheme, parts.netloc, root_path + "/", "", "")),
        unquote(topic),
    )


class _NtfyConfig(BaseModel):
    """Optional ntfy push notifications for the built-in backend.

    Notifications are sent only when a ticket actually changes to Needs Input,
    and carry the identifier and title only: comment text does not exist yet at
    that point and must not be sent to a third-party server.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        ...,
        description=(
            "Full ntfy topic URL, e.g. https://ntfy.sh/mytopic. "
            "Notifications are published to the server root as JSON."
        ),
    )
    token: str | None = Field(
        None, description="Optional bearer token sent as 'Authorization: Bearer'"
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        _split_ntfy_topic_url(v)
        return v

    @field_validator("token")
    @classmethod
    def _validate_token(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("must be a non-empty string when set")
        return v

    def endpoint(self) -> tuple[str, str]:
        """Return the ``(server_root, topic)`` pair to publish to."""
        return _split_ntfy_topic_url(self.url)


class _WebUIConfig(BaseModel):
    """Built-in web UI and local tracker backend configuration."""

    model_config = ConfigDict(extra="forbid")

    port: int = Field(..., gt=0, le=65535, description="Port for the web UI server")
    host: str = Field("127.0.0.1", description="Address the web UI server binds to")
    username: str = Field(..., description="HTTP Basic auth username")
    password: str = Field(..., description="HTTP Basic auth password")
    anthropic_api_key: str | None = Field(
        None,
        description="Optional Anthropic API key for automatic ticket titles",
    )
    public_url: str | None = Field(
        None,
        description=(
            "Optional public base URL of this web UI; when set, a notification's "
            "Click opens <public_url>/tickets/<id> and it shows the Symphony icon"
        ),
    )
    ntfy: _NtfyConfig | None = Field(
        None, description="Optional ntfy notifications when a ticket needs input"
    )

    @field_validator("username", "password")
    @classmethod
    def _validate_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("public_url")
    @classmethod
    def _validate_public_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        # urlsplit reports an empty query or fragment for a URL ending in "?"
        # or "#", indistinguishable from no marker at all, so the raw string is
        # checked before splitting.
        if stripped.endswith(("?", "#")):
            raise ValueError("must not contain a query string or fragment")
        base = stripped.rstrip("/")
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an absolute http(s) URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("must not contain user information (user:pass@)")
        if parts.query or parts.fragment:
            raise ValueError("must not contain a query string or fragment")
        return base


class AppConfig(BaseModel):
    """Top-level application configuration.

    Exactly one of ``linear``, ``github`` or ``webui`` must be set.
    """

    model_config = ConfigDict(extra="forbid")

    agent: Literal["opencode", "omp", "pi"] = Field(
        "opencode",
        description="Coding-agent CLI to run: 'opencode' (default), 'omp', or 'pi'",
    )
    linear: _LinearConfig | None = Field(
        None, description="Linear backend configuration block"
    )
    github: _GitHubConfig | None = Field(
        None, description="GitHub Projects v2 backend configuration block"
    )
    webui: _WebUIConfig | None = Field(
        None, description="Built-in web UI and local ticket store configuration"
    )
    sandbox: _SandboxConfig = Field(default_factory=_SandboxConfig)
    webhook: _WebhookConfig | None = Field(
        None, description="Optional webhook server configuration"
    )
    models: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-issue model override aliases: alias -> full provider/model id. "
            "A 'Model: <alias or id>' label on an issue makes the daemon pass "
            "--model to opencode run for that ticket's primary-agent turns."
        ),
    )
    poll_interval_seconds: int = Field(
        30, gt=0, description="Seconds between poll cycles"
    )
    turn_timeout_seconds: int = Field(1800, gt=0, description="Max seconds per AI turn")
    turn_idle_timeout_seconds: int = Field(
        1200,
        gt=0,
        description=(
            "Max seconds an AI turn may go without producing output on "
            "stdout or stderr before the process is killed"
        ),
    )
    auto_branch: bool = Field(
        True,
        description=(
            "If true (default), switch the workspace to a per-ticket branch "
            "during prepare() — the tracker's branchName, or symphony/<id> as "
            "fallback. If false, no branch switch runs and the workspace "
            "stays on whatever git clone checked out (the remote default "
            "branch). Useful when the agent commits straight to the default "
            "branch and pushes are handled outside Symphony's scope."
        ),
    )

    @field_validator("models")
    @classmethod
    def _validate_model_ids(cls, v: dict[str, str]) -> dict[str, str]:
        """Model alias values must be full '<provider>/<model>' ids.

        Only the shape is checked here — whether the provider or model
        actually exists is something only OpenCode can answer, and label
        values that are not aliases never pass through this field.
        """
        for alias, value in v.items():
            provider, sep, model = value.partition("/")
            if not sep or not provider or not model:
                raise ValueError(
                    f"Invalid models entry for alias {alias!r}: {value!r}. "
                    f"Expected a full provider/model id like "
                    f"'anthropic/claude-opus-5'."
                )
        return v

    @model_validator(mode="before")
    @classmethod
    def _drop_null_subconfigs(cls, data: Any) -> Any:
        # YAML keys with empty values (e.g. `sandbox:` on its own line) parse
        # as ``None``. For sub-config fields that have a default, treat ``None``
        # as "use the default" rather than failing validation.
        if isinstance(data, dict):
            if "sandbox" in data and data["sandbox"] is None:
                del data["sandbox"]
            if "webhook" in data and data["webhook"] is None:
                del data["webhook"]
            if "webui" in data and data["webui"] is None:
                del data["webui"]
        return data

    @model_validator(mode="after")
    def _exactly_one_tracker(self) -> AppConfig:
        """Enforce that exactly one tracker backend block is configured."""
        configured = [
            name
            for name, block in (
                ("linear", self.linear),
                ("github", self.github),
                ("webui", self.webui),
            )
            if block is not None
        ]
        if len(configured) > 1:
            names = ", ".join(f"'{name}'" for name in configured)
            raise ValueError(
                f"More than one tracker backend is configured in config.yaml: "
                f"{names}. Configure exactly one of 'linear', 'github' or 'webui'."
            )
        if not configured:
            raise ValueError(
                "No tracker backend configured. "
                "Add one of a 'linear:', 'github:' or 'webui:' block to "
                "config.yaml."
            )
        if self.webhook is not None and self.linear is None:
            raise ValueError(
                "webhook block requires a 'linear:' backend; "
                "webhooks are only supported for Linear."
            )
        return self


# ---------------------------------------------------------------------------
# Env-var fallback helpers
# ---------------------------------------------------------------------------


def _apply_linear_env_fallback(expanded: dict[str, Any], path: Path) -> None:
    """If a linear block is present but has no api_key, fill it from LINEAR_API_KEY."""
    linear = expanded.get("linear")
    if linear is None:
        return
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


def _apply_github_env_fallback(expanded: dict[str, Any], path: Path) -> None:
    """If a github block is present but has no token, fill it from GITHUB_TOKEN."""
    github = expanded.get("github")
    if github is None:
        return
    if isinstance(github, dict) and not github.get("token", ""):
        env_key = os.environ.get("GITHUB_TOKEN", "")
        if env_key:
            github["token"] = env_key
        else:
            raise ValueError(
                f"GitHub token not set. Provide it in {path} "
                f"(github.token) or via the GITHUB_TOKEN "
                f"environment variable."
            )


def _apply_webhook_env_fallback(expanded: dict[str, Any], path: Path) -> None:
    """If a webhook block is present but has no linear_secret, fill it from env."""
    webhook = expanded.get("webhook")
    if webhook is None:
        return
    if isinstance(webhook, dict) and not webhook.get("linear_secret", ""):
        env_key = os.environ.get("SYMPHONY_LINEAR_WEBHOOK_SECRET", "")
        if env_key:
            webhook["linear_secret"] = env_key
        else:
            raise ValueError(
                f"Webhook secret not set. Provide it in {path} "
                f"(webhook.linear_secret) or via the SYMPHONY_LINEAR_WEBHOOK_SECRET "
                f"environment variable."
            )


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

    # Validate exactly-one-tracker constraint early, before credential
    # fallbacks, so that setting several blocks (or none) produces a clear
    # error instead of a confusing "missing token" message.
    if isinstance(expanded, dict):
        configured = [
            name
            for name, present in (
                ("linear", expanded.get("linear") is not None),
                ("github", expanded.get("github") is not None),
                ("webui", expanded.get("webui") is not None),
            )
            if present
        ]
        has_linear = "linear" in configured
        has_webhook = expanded.get("webhook") is not None
        if len(configured) > 1:
            names = ", ".join(f"'{name}'" for name in configured)
            raise ValueError(
                f"More than one tracker backend is configured in config.yaml: "
                f"{names}. Configure exactly one of 'linear', 'github' or 'webui'."
            )
        if not configured:
            raise ValueError(
                "No tracker backend configured. "
                "Add one of a 'linear:', 'github:' or 'webui:' block to "
                "config.yaml."
            )
        # Run the webhook+linear-only check BEFORE the secret-fallback so that
        # non-linear+webhook configs produce the scope error, not a misleading
        # "missing webhook secret" error from the env fallback.
        if has_webhook and not has_linear:
            raise ValueError(
                "webhook block requires a 'linear:' backend; "
                "webhooks are only supported for Linear."
            )

    # Environment variable fallbacks: if the config block is present but its
    # credential is missing or empty, fall back to the appropriate env var.
    # We only run the fallback for blocks that actually appear in the YAML so
    # that a github-only config does not demand a Linear API key.
    if isinstance(expanded, dict):
        _apply_linear_env_fallback(expanded, path)
        _apply_github_env_fallback(expanded, path)
        _apply_webhook_env_fallback(expanded, path)

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
