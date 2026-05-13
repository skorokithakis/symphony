"""bwrap sandbox wrapper for OpenCode execution.

Wraps arbitrary commands in a bubblewrap_ sandbox that isolates the process
from the host filesystem while providing controlled access to the workspace
and essential system resources.

.. _bubblewrap: https://github.com/containers/bubblewrap
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _expand(path: str) -> str:
    """Expand ``~`` and resolve symlinks in *path*."""
    # Tilde expansion
    expanded = str(Path(path).expanduser())
    # Resolve symlinks (critical for e.g. /var/run → /run)
    return os.path.realpath(expanded)


def run_in_sandbox(
    cmd: list[str],
    workspace_path: str,
    hide_paths: list[str],
    env: dict[str, str],
    stdin: Any = None,
    stdout: int = subprocess.PIPE,
    stderr: int = subprocess.PIPE,
    extra_rw_paths: list[str] | None = None,
) -> subprocess.Popen[bytes]:
    """Run *cmd* inside a bwrap sandbox and return the :class:`~subprocess.Popen` handle.

    The caller is responsible for managing the process lifetime (wait, kill,
    timeout, etc.).

    Args:
        cmd: The command and arguments to execute inside the sandbox.
        workspace_path: Host path to the workspace directory, which will be
            mounted **read-write** at the same location inside the sandbox.
        hide_paths: List of host paths to conceal inside the sandbox.
            Directories are overlaid with an empty ``tmpfs``; files (including
            sockets) are replaced with ``/dev/null``.  ``~`` and symlinks are
            expanded.
        env: Environment variables to set for the sandboxed process.  The
            host environment is **not** inherited (``--clearenv`` is used).
            If *env* does not contain ``"PATH"``, the PATH is resolved in
            this order: (1) the ``SYMPHONY_SANDBOX_PATH`` environment
            variable if set; (2) the daemon's own ``os.environ["PATH"]``
            if set; (3) the hard-coded fallback
            ``"/usr/local/bin:/usr/bin:/bin"``.  Pass ``"PATH"`` explicitly
            in *env* to override this resolution entirely.
        stdin: Passed through to :class:`subprocess.Popen` (default ``None``).
        stdout: Passed through to :class:`subprocess.Popen` (default
            :data:`subprocess.PIPE`).
        stderr: Passed through to :class:`subprocess.Popen` (default
            :data:`subprocess.PIPE`).
        extra_rw_paths: Additional host paths to bind read-write inside the
            sandbox using ``--bind`` (not ``--bind-try``).  Applied before
            *hide_paths* so hide wins on collision.  ``~`` is expanded.

    Returns:
        A :class:`subprocess.Popen` instance for the bwrap process.  The
        actual sandboxed command is a child of bwrap.

    Raises:
        FileNotFoundError: If ``bwrap`` is not available on ``$PATH``.
    """
    # ------------------------------------------------------------------
    # Pre-flight: ensure bwrap exists
    # ------------------------------------------------------------------
    if shutil.which("bwrap") is None:
        raise FileNotFoundError(
            "bwrap is required for sandbox execution but was not found on $PATH"
        )

    # ------------------------------------------------------------------
    # Expand paths on the host side
    # ------------------------------------------------------------------
    home = str(Path.home())
    cache_dir = str(Path("~/.cache").expanduser())
    local_share_dir = str(Path("~/.local/share").expanduser())
    expanded_workspace = _expand(workspace_path)

    # ------------------------------------------------------------------
    # Build bwrap argument list
    # ------------------------------------------------------------------
    bwrap_args: list[str] = ["bwrap"]

    # -- Filesystem construction --------------------------------------------
    # The root is an empty tmpfs by default; we layer mounts on top.
    # Order matters: earlier mounts act as lower layers.

    # 1. Read-only bind of the entire host filesystem
    bwrap_args.extend(["--ro-bind", "/", "/"])

    # 2. Read-write bind for the workspace
    bwrap_args.extend(["--bind", expanded_workspace, expanded_workspace])

    # 3. Read-write bind for /tmp
    bwrap_args.extend(["--bind", "/tmp", "/tmp"])

    # 4. Read-write binds for tool caches and state (OpenCode, pip, etc.).
    #    Using --bind-try so missing dirs don't cause a fatal error.
    bwrap_args.extend(["--bind-try", cache_dir, cache_dir])
    bwrap_args.extend(["--bind-try", local_share_dir, local_share_dir])
    # OpenCode state directories (legacy ~/.opencode and XDG ~/.local/share/opencode).
    # Both are needed so `--session <id>` resume works inside the sandbox.
    opencode_legacy = str(Path("~/.opencode").expanduser())
    opencode_xdg = str(Path("~/.local/share/opencode").expanduser())
    bwrap_args.extend(["--bind-try", opencode_legacy, opencode_legacy])
    bwrap_args.extend(["--bind-try", opencode_xdg, opencode_xdg])

    # 5. Extra read-write paths (applied before hide_paths so hide wins on
    #    collision — later bwrap mounts override earlier ones).
    if extra_rw_paths:
        for raw_path in extra_rw_paths:
            path = _expand(raw_path)
            bwrap_args.extend(["--bind", path, path])

    # 6. Conceal sensitive paths
    for raw_path in hide_paths:
        path = _expand(raw_path)
        if os.path.isdir(path):
            # --tmpfs successfully overlays an existing directory, even under
            # a read-only parent mount (bwrap mounts a fresh tmpfs on top).
            bwrap_args.extend(["--tmpfs", path])
        elif os.path.exists(path):
            # --tmpfs cannot replace individual files under a read-only parent
            # because it must create the mount-point directory first, so we
            # overlay /dev/null instead.  This handles sockets, regular files,
            # and symlinks alike.
            bwrap_args.extend(["--ro-bind", "/dev/null", path])
        # else: path does not exist on the host → nothing to conceal.

    # 7. Essential pseudo-filesystems
    bwrap_args.extend(["--dev", "/dev"])
    bwrap_args.extend(["--proc", "/proc"])

    # -- Namespace isolation ------------------------------------------------
    # Share the network namespace (tools need outbound connectivity).
    # Unshare everything else for strong isolation.
    bwrap_args.extend(
        [
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
        ]
    )

    # -- Process lifecycle --------------------------------------------------
    bwrap_args.append("--die-with-parent")
    bwrap_args.append("--new-session")

    # -- Working directory --------------------------------------------------
    bwrap_args.extend(["--chdir", expanded_workspace])

    # -- Environment --------------------------------------------------------
    # Start with a clean slate so nothing leaks from the daemon.
    bwrap_args.append("--clearenv")

    # Resolve PATH if the caller did not supply one explicitly.
    # Priority: caller-supplied PATH > SYMPHONY_SANDBOX_PATH env var >
    # daemon's own os.environ["PATH"] > hard-coded fallback.
    env_to_set = dict(env)
    if "PATH" not in env_to_set:
        if "SYMPHONY_SANDBOX_PATH" in os.environ:
            env_to_set["PATH"] = os.environ["SYMPHONY_SANDBOX_PATH"]
        elif "PATH" in os.environ:
            env_to_set["PATH"] = os.environ["PATH"]
        else:
            env_to_set["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    for key, value in env_to_set.items():
        bwrap_args.extend(["--setenv", key, value])

    # -- Command ------------------------------------------------------------
    bwrap_args.extend(cmd)

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------
    return subprocess.Popen(
        bwrap_args,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
