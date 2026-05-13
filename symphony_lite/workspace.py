"""Per-ticket workspace lifecycle: clone, branch, setup, serve, remove.

Git operations run outside the sandbox using the daemon's credentials.
The ``.symphony/setup`` and ``.symphony/serve`` scripts run inside the sandbox
via the sandbox wrapper.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from symphony_lite.sandbox import run_in_sandbox

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Character class for valid ticket identifier characters.
_VALID_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]")

# Default timeout for the setup script (5 minutes).
SETUP_TIMEOUT_SECONDS = 300

# Default branch name is derived from the ticket identifier (lowercased).
_DEFAULT_BRANCH_PREFIX = "symphony/"


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class WorkspaceError(Exception):
    """Base exception for all workspace lifecycle errors."""


class CloneFailed(WorkspaceError):
    """Git clone operation failed."""


class BranchFailed(WorkspaceError):
    """Git branch switch / creation failed."""


class SetupFailed(WorkspaceError):
    """The ``.symphony/setup`` script exited with a non-zero code or timed out."""


class PathContainmentError(WorkspaceError):
    """Computed workspace path escapes the workspace root (security invariant)."""


class ServeScriptMissing(WorkspaceError):
    """The ``.symphony/serve`` script is absent or not executable."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize_identifier(identifier: str) -> str:
    """Replace any character not in ``[A-Za-z0-9._-]`` with ``_``."""
    return _VALID_CHARS_RE.sub("_", identifier)


def _check_containment(workspace_path: str, workspace_root: str) -> str:
    """Verify *workspace_path* resides within *workspace_root* after symlink
    resolution.

    Returns the real path of *workspace_path* on success.

    Raises:
        PathContainmentError: If the resolved workspace path is not a child of
            the resolved workspace root.
    """
    real_root = os.path.realpath(workspace_root)
    real_path = os.path.realpath(workspace_path)

    # Normalise to avoid trailing-slash mismatches.
    if not real_path.startswith(real_root + os.sep) and real_path != real_root:
        raise PathContainmentError(
            f"Workspace path '{workspace_path}' (resolved to '{real_path}') "
            f"is not contained within workspace root '{workspace_root}' "
            f"(resolved to '{real_root}')"
        )

    return real_path


def _run_git(
    args: list[str],
    cwd: str | None = None,
    *,
    description: str = "git operation",
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the completed process.

    Args:
        args: Git command arguments (without the leading ``git``).
        cwd: Working directory for the command.
        description: Human-readable label used in error messages.

    Returns:
        The completed process.

    Raises:
        CloneFailed / BranchFailed: Depending on context, if the process exits
            with a non-zero code.
    """
    cmd = ["git"] + args
    logger.debug("Running git: %s (cwd=%s)", " ".join(cmd), cwd)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr_tail = result.stderr.strip().splitlines()
        tail = "\n".join(stderr_tail[-5:]) if stderr_tail else "(no stderr)"
        logger.error("Git %s failed (rc=%d): %s", description, result.returncode, tail)

        # Distinguish clone from branch by looking at the sub-command.
        if args[0] == "clone":
            raise CloneFailed(f"git clone failed (rc={result.returncode}): {tail}")
        raise BranchFailed(f"git {args[0]} failed (rc={result.returncode}): {tail}")

    return result


def _run_setup_script(
    workspace_path: str,
    hide_paths: list[str],
    on_subprocess: Callable[[subprocess.Popen[bytes]], None] | None = None,
    extra_rw_paths: list[str] | None = None,
) -> None:
    """Run ``.symphony/setup`` inside the sandbox.

    Args:
        workspace_path: Host path to the workspace directory.
        hide_paths: Paths to conceal inside the sandbox.
        on_subprocess: Optional callback invoked with the Popen handle
            immediately after launch, for external cancellation.
        extra_rw_paths: Additional host paths to bind read-write inside the
            sandbox.

    Raises:
        SetupFailed: If the script exits with a non-zero code or times out.
    """
    setup_path = os.path.join(workspace_path, ".symphony", "setup")
    if not os.path.isfile(setup_path) or not os.access(setup_path, os.X_OK):
        logger.debug("No executable .symphony/setup found at %s – skipping", setup_path)
        return

    logger.info("Running .symphony/setup for workspace %s", workspace_path)

    proc = run_in_sandbox(
        cmd=["./.symphony/setup"],
        workspace_path=workspace_path,
        hide_paths=hide_paths,
        env={
            "HOME": os.environ.get("HOME", str(Path.home())),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        extra_rw_paths=extra_rw_paths or [],
    )

    if on_subprocess is not None:
        on_subprocess(proc)

    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=SETUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            _, stderr_bytes = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stderr_bytes = b"(timed out collecting stderr)"
        stderr_text = (
            stderr_bytes.decode(errors="replace")
            if isinstance(stderr_bytes, bytes)
            else str(stderr_bytes)
        )
        raise SetupFailed(
            f".symphony/setup timed out after {SETUP_TIMEOUT_SECONDS}s\n"
            f"stderr tail:\n{_tail(stderr_text)}"
        )

    stderr_text = stderr_bytes.decode(errors="replace") if stderr_bytes else ""

    if proc.returncode != 0:
        logger.error(
            ".symphony/setup failed (rc=%d) for workspace %s",
            proc.returncode,
            workspace_path,
        )
        raise SetupFailed(
            f".symphony/setup exited with code {proc.returncode}\n"
            f"stderr tail:\n{_tail(stderr_text)}"
        )

    logger.info(
        ".symphony/setup completed successfully for workspace %s", workspace_path
    )


def start_serve(
    workspace_path: str,
    hide_paths: list[str],
    extra_rw_paths: list[str] | None = None,
) -> subprocess.Popen[bytes]:
    """Launch ``.symphony/serve`` inside the sandbox and return the Popen handle.

    Unlike :func:`_run_setup_script`, this function does **not** wait for the
    process to finish — ``.symphony/serve`` is expected to be a long-running
    process.  The caller is responsible for managing the process lifetime
    (draining/closing pipes, killing, waiting).

    Args:
        workspace_path: Host path to the workspace directory.
        hide_paths: Paths to conceal inside the sandbox.
        extra_rw_paths: Additional host paths to bind read-write inside the
            sandbox.

    Returns:
        A :class:`~subprocess.Popen` instance for the sandboxed serve process.
        Both ``stdout`` and ``stderr`` are :data:`subprocess.PIPE` so the
        caller can capture stderr tail on early failure.

    Raises:
        ServeScriptMissing: If ``.symphony/serve`` is absent or not executable.
        FileNotFoundError: If ``bwrap`` is not available on ``$PATH``.
    """
    serve_path = os.path.join(workspace_path, ".symphony", "serve")
    if not os.path.isfile(serve_path) or not os.access(serve_path, os.X_OK):
        raise ServeScriptMissing(
            f".symphony/serve is missing or not executable at {serve_path}"
        )

    logger.info("Launching .symphony/serve for workspace %s", workspace_path)

    return run_in_sandbox(
        cmd=["./.symphony/serve"],
        workspace_path=workspace_path,
        hide_paths=hide_paths,
        env={
            "HOME": os.environ.get("HOME", str(Path.home())),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        extra_rw_paths=extra_rw_paths or [],
    )


def _tail(text: str, lines: int = 20) -> str:
    """Return the last *lines* lines of *text*."""
    if not text:
        return "(no stderr)"
    all_lines = text.strip().splitlines()
    return "\n".join(all_lines[-lines:]) if all_lines else "(no stderr)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _git_switch_branch(
    branch_name: str,
    workspace_path: str,
) -> None:
    """Switch to *branch_name*, creating it if it does not exist.

    Tries ``git switch <branch>`` first (which auto-creates from a matching
    remote).  If that fails, falls back to ``git switch -c <branch>``.

    Raises:
        BranchFailed: If both attempts fail.
    """
    # Attempt 1: plain switch (works if branch exists locally or on remote).
    result = subprocess.run(
        ["git", "switch", branch_name],
        cwd=workspace_path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.debug("Switched to existing branch '%s'", branch_name)
        return

    logger.debug(
        "git switch '%s' failed (rc=%d), trying -c: %s",
        branch_name,
        result.returncode,
        result.stderr.strip().splitlines()[-1]
        if result.stderr.strip()
        else "(no stderr)",
    )

    # Attempt 2: create a new branch from HEAD.
    result = subprocess.run(
        ["git", "switch", "-c", branch_name],
        cwd=workspace_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr_tail = result.stderr.strip().splitlines()
        tail = "\n".join(stderr_tail[-5:]) if stderr_tail else "(no stderr)"
        raise BranchFailed(
            f"git switch -c {branch_name} failed (rc={result.returncode}): {tail}"
        )

    logger.debug("Created and switched to new branch '%s'", branch_name)


def prepare(
    ticket_identifier: str,
    repo_url: str,
    branch_name: str | None,
    workspace_root: str,
    sandbox_hide_paths: list[str],
    on_subprocess: Callable[[subprocess.Popen[bytes]], None] | None = None,
    sandbox_extra_rw_paths: list[str] | None = None,
    auto_branch: bool = True,
) -> str:
    """Prepare a workspace for *ticket_identifier*.

    1. Sanitize the identifier to a safe directory name.
    2. Compute the workspace path and verify it is within *workspace_root*.
    3. Clone the repository if the directory does not already exist.
    4. Switch to (or create) the target branch (skipped when ``auto_branch``
       is false — the workspace stays on whatever ``git clone`` checked out).
    5. Run ``.symphony/setup`` inside the sandbox if present and executable.

    This function is idempotent: re-calling it on an existing workspace will
    switch to the right branch (when ``auto_branch`` is true) and re-run setup.

    Args:
        ticket_identifier: Human-readable ticket ID (e.g. ``TEAM-42``).
        repo_url: Git clone URL (supports local paths for testing).
        branch_name: Target branch name.  If ``None``, defaults to
            ``symphony/<identifier_lower>``. Ignored when ``auto_branch`` is
            false.
        workspace_root: Root directory under which all workspaces live.
        sandbox_hide_paths: Paths to conceal inside the sandbox when running
            the setup script.
        on_subprocess: Optional callback invoked with the Popen handle of the
            setup script (if any), for external cancellation.
        sandbox_extra_rw_paths: Additional host paths to bind read-write inside
            the sandbox when running the setup script.
        auto_branch: If true (default), switch to a per-ticket branch after
            clone/fetch. If false, skip the branch switch entirely and leave
            the workspace on the cloned default branch.

    Returns:
        The real path to the prepared workspace.

    Raises:
        PathContainmentError: If the computed workspace path escapes
            *workspace_root*.
        CloneFailed: If ``git clone`` fails.
        BranchFailed: If ``git switch`` fails.
        SetupFailed: If ``.symphony/setup`` fails or times out.
    """
    # 1. Sanitize identifier → workspace_key
    workspace_key = _sanitize_identifier(ticket_identifier)

    # 2. Compute and validate workspace path
    workspace_path = os.path.join(workspace_root, workspace_key)
    real_path = _check_containment(workspace_path, workspace_root)

    # Determine the target branch name (only relevant when auto_branch is on).
    if branch_name is None:
        branch_name = f"{_DEFAULT_BRANCH_PREFIX}{ticket_identifier.lower()}"

    # 3. Clone if the workspace does not exist; fetch if it does.
    # Note: clone has no -b — it checks out the remote's default branch.
    # The target branch is selected in step 4.
    if not os.path.isdir(real_path):
        logger.info("Cloning %s into %s", repo_url, real_path)
        _run_git(
            ["clone", repo_url, real_path],
            description="clone",
        )
    else:
        logger.info("Workspace %s already exists – reusing", real_path)
        # Fetch to pick up any new remote branches.
        _run_git(
            ["fetch", "origin"],
            cwd=real_path,
            description="fetch",
        )

    # 4. Switch to (or create) the target branch — unless disabled.
    if auto_branch:
        logger.info("Switching to branch '%s' in %s", branch_name, real_path)
        _git_switch_branch(branch_name, real_path)
    else:
        logger.info(
            "auto_branch disabled — staying on cloned default branch in %s",
            real_path,
        )

    # 5. Run setup script if present
    _run_setup_script(
        real_path,
        sandbox_hide_paths,
        on_subprocess=on_subprocess,
        extra_rw_paths=sandbox_extra_rw_paths,
    )

    return real_path


def remove(
    ticket_identifier: str,
    workspace_root: str,
) -> None:
    """Delete the workspace for *ticket_identifier*.

    Idempotent — no error if the workspace is already gone.

    Args:
        ticket_identifier: Human-readable ticket ID.
        workspace_root: Root directory for workspaces.

    Raises:
        PathContainmentError: If the computed path escapes *workspace_root*.
    """
    workspace_key = _sanitize_identifier(ticket_identifier)
    workspace_path = os.path.join(workspace_root, workspace_key)

    # Verify containment before removing anything.
    _check_containment(workspace_path, workspace_root)

    if not os.path.isdir(workspace_path):
        logger.debug("Workspace %s does not exist – nothing to remove", workspace_path)
        return

    logger.info("Removing workspace %s", workspace_path)
    shutil.rmtree(workspace_path, ignore_errors=False)
    logger.info("Workspace %s removed", workspace_path)
