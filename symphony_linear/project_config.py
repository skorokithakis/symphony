"""Per-project .symphony/config.yaml loader.

Reads an optional YAML config from ``<workspace_path>/.symphony/config.yaml``
that can override select global settings on a per-project basis.  The file is
read from the checkout's working tree, never from git, and no symbolic link is
followed.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

# A settings file this large is a mistake, not a configuration; refusing it
# also caps how much the daemon reads from an agent-writable checkout.
MAX_PROJECT_CONFIG_BYTES = 64 * 1024


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
        msg = f"Project config validation failed for {source}:\n" + "\n".join(lines)
        raise ProjectConfigError(msg) from exc


def _read_limited(fd: int, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes from *fd*.

    The extra byte is how a file that grew after the size check is still
    detected as oversized rather than silently truncated.
    """
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def load_project_config(workspace_path: str) -> ProjectConfig:
    """Load and validate the optional per-project config file.

    The file is read from ``<workspace_path>/.symphony/config.yaml`` on the
    filesystem, once per workspace preparation.  It is deliberately not read
    on resume: the checkout is writable by the agent, so a later read would
    let the agent change its own turn limit, and a file the agent broke would
    stop the ticket before the agent could repair it.

    The read is descriptor based: the checkout is opened, ``.symphony`` is
    opened relative to it with ``O_DIRECTORY | O_NOFOLLOW``, and the file is
    opened relative to that with ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``.  No
    symbolic link is followed.  A process that still runs in the checkout (a
    QA serve can) could otherwise swap a path component between a check and
    the open, and a FIFO there would block the worker thread indefinitely.

    Args:
        workspace_path: Path to the checkout. The config file is expected at
            ``<workspace_path>/.symphony/config.yaml``.

    Returns:
        A ``ProjectConfig`` instance. Returns an empty config (all fields
        ``None``) when ``.symphony`` or the file does not exist.

    Raises:
        ProjectConfigError: If either path component is a symbolic link, the
            file is not a regular file, larger than
            :data:`MAX_PROJECT_CONFIG_BYTES`, not valid UTF-8, or its YAML is
            malformed or fails validation.
    """
    config_file = Path(workspace_path) / ".symphony" / "config.yaml"

    try:
        checkout_fd = os.open(workspace_path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise ProjectConfigError(
            f"Cannot open workspace checkout {workspace_path} to read its "
            f"project config: {exc}."
        ) from exc

    try:
        try:
            symphony_fd = os.open(
                ".symphony",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=checkout_fd,
            )
        except FileNotFoundError:
            logger.debug("No .symphony directory at %s", config_file)
            return ProjectConfig()
        except OSError as exc:
            raise ProjectConfigError(
                f"Cannot read project config from {config_file}: .symphony is "
                f"not a real directory ({exc}). A symbolic link is not "
                f"allowed; replace it with a directory inside the checkout."
            ) from exc

        try:
            try:
                config_fd = os.open(
                    "config.yaml",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=symphony_fd,
                )
            except FileNotFoundError:
                logger.debug("No project config found at %s", config_file)
                return ProjectConfig()
            except OSError as exc:
                raise ProjectConfigError(
                    f"Cannot open project config {config_file}: {exc}. A "
                    f"symbolic link is not allowed; replace it with a regular "
                    f"file inside .symphony."
                ) from exc

            try:
                file_stat = os.fstat(config_fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ProjectConfigError(
                        f"Project config {config_file} is not a regular file. "
                        f"Replace it with a normal file."
                    )
                if file_stat.st_size > MAX_PROJECT_CONFIG_BYTES:
                    raise ProjectConfigError(
                        f"Project config {config_file} is larger than "
                        f"{MAX_PROJECT_CONFIG_BYTES} bytes. Remove the settings "
                        f"you do not use."
                    )
                raw_bytes = _read_limited(config_fd, MAX_PROJECT_CONFIG_BYTES)
            finally:
                os.close(config_fd)
        finally:
            os.close(symphony_fd)
    finally:
        os.close(checkout_fd)

    if len(raw_bytes) > MAX_PROJECT_CONFIG_BYTES:
        raise ProjectConfigError(
            f"Project config {config_file} is larger than "
            f"{MAX_PROJECT_CONFIG_BYTES} bytes. Remove the settings you do not "
            f"use."
        )

    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectConfigError(
            f"Project config {config_file} is not valid UTF-8: {exc}. Save it as UTF-8."
        ) from exc

    return _parse_yaml_config(raw, str(config_file))
