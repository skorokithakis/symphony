"""The no-argument first run: a default workspace under ``~/symphony``.

``symphony-linear`` with no arguments used to be a dead end outside a
directory that already held a ``config.yaml``.  In an interactive terminal it
now falls back to ``~/symphony`` and writes a config there, so
``uvx symphony-linear`` starts the web UI with nothing else to set up.

``~/symphony`` rather than ``~/.local/share/symphony`` because the sandbox
binds ``~/.local/share`` read-write and users are meant to find
``<ticket>/repo`` and push from it.  Creating anything here requires a
terminal, so a supervised daemon with a wrong working directory keeps failing
loudly instead of quietly adopting a workspace of its own.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys

from pathlib import Path

import yaml

from symphony_linear.sandbox_path import resolve_sandbox_path

DEFAULT_WORKSPACE = "~/symphony"
# Stavros picked 27149: 8080 is too common to be free on a developer machine,
# and it stays below 32768 so it never collides with Linux ephemeral ports.
DEFAULT_WEBUI_PORT = 27149
# The order is the preference order: OpenCode is the documented default, and
# the other two are alternatives rather than fallbacks for a missing feature.
AGENTS = ("opencode", "omp", "pi")


class FirstRunError(Exception):
    """Raised when the default workspace cannot be created."""


def default_workspace() -> Path:
    """Return the resolved path of the default workspace."""
    return Path(DEFAULT_WORKSPACE).expanduser().resolve()


def find_agent(sandbox_path: str) -> str | None:
    """Return the first configured agent binary present on *sandbox_path*."""
    for agent in AGENTS:
        if shutil.which(agent, path=sandbox_path) is not None:
            return agent
    return None


def ensure_default_workspace() -> Path:
    """Return ``~/symphony``, writing a config there if it has none.

    Nothing is created when the config already exists, or when no coding agent
    is installed: a config naming an agent that cannot run would only turn the
    first turn into a startup failure.
    """
    workspace = default_workspace()
    config_path = workspace / "config.yaml"
    if config_path.exists():
        return workspace

    sandbox_path = resolve_sandbox_path()
    agent = find_agent(sandbox_path)
    if agent is None:
        raise FirstRunError(
            f"No default config was created: none of the coding agents "
            f"{', '.join(AGENTS)} was found on the sandbox PATH "
            f"{sandbox_path!r}. Install one of them, or create {config_path} "
            f"yourself."
        )

    password = secrets.token_urlsafe(24)
    workspace.mkdir(parents=True, exist_ok=True)
    _write_config(config_path, agent=agent, password=password)
    print(
        f"Open http://127.0.0.1:{DEFAULT_WEBUI_PORT}/ and log in with password "
        f"{password} (stored in {config_path}).",
        file=sys.stderr,
    )
    return workspace


def _write_config(path: Path, *, agent: str, password: str) -> None:
    body = yaml.safe_dump(
        {
            "agent": agent,
            "webui": {"port": DEFAULT_WEBUI_PORT, "password": password},
        },
        sort_keys=False,
    )
    # The mode is applied at creation, not via a later chmod, because the file
    # holds the web UI password from its very first byte.  O_EXCL rather than
    # O_TRUNC: the caller's exists() check is only advisory, so the file could
    # appear in between, or the name could be a dangling symlink that open()
    # would happily follow and fill in.  Refusing to create over either is
    # better than truncating a config someone else just wrote, and the
    # FileExistsError propagates rather than being caught here.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(body)
