"""Shared sandbox process runner for agent adapters.

The runner launches an adapter-provided command inside the bwrap sandbox and
returns raw process output. Adapters own protocol parsing, validation, and any
adapter-specific error messages.

-------------------------------------------------------------------------------
Watchdog and process lifecycle
-------------------------------------------------------------------------------

Each run has two watchdog limits. One daemon thread per output stream drains
stdout and stderr incrementally with ``read1(65536)`` and records the most
recent activity timestamp. The main thread kills the process when neither
stream has produced output for ``idle_timeout_seconds`` or when the total run
time reaches ``timeout_seconds``. The returned ``timeout_reason`` identifies
which limit fired; it is ``None`` on a normal exit.

Chunks rather than lines are used for liveness, so output without newline
framing still resets the idle watchdog. After a timeout the process is killed
and reaped, drain threads get up to ten seconds to flush output, and the
raw decoded streams are returned for the adapter to handle. A final reap keeps
processes from escaping on other error paths.

The drain threads also invoke an optional ``on_output`` callback for every
chunk, on either stream. It exists so the web UI can show how long ago the
agent last wrote anything; it is deliberately the same signal as the idle
watchdog rather than parsed protocol events, so a tool call that streams no
JSON still counts as activity.
"""

from __future__ import annotations

import io
import logging
import subprocess
import threading
import time
from typing import Callable

from symphony_linear.sandbox import run_in_sandbox

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """An agent process exited with a non-zero exit code."""


class AgentTimeout(Exception):
    """An agent process timed out and was killed.

    ``reason`` distinguishes the two failure modes: an idle stall
    ("produced no output for Ns") versus the absolute cap
    ("exceeded Ns in total"). Every production raise site sets it.
    """

    def __init__(
        self,
        message: str,
        partial_message: str = "",
        session_id: str | None = None,
        *,
        reason: str,
    ) -> None:
        super().__init__(message)
        self.partial_message = partial_message
        self.session_id = session_id
        self.reason = reason


class AgentCancelled(Exception):
    """An agent process was killed externally."""

    def __init__(self, message: str, session_id: str | None = None) -> None:
        super().__init__(message)
        self.session_id = session_id


def run(
    cmd: list[str],
    workspace_path: str,
    tmp_path: str,
    timeout_seconds: int,
    idle_timeout_seconds: int,
    on_subprocess: Callable[[subprocess.Popen[bytes]], None],
    *,
    env: dict[str, str],
    on_output: Callable[[], None] | None = None,
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
    attachments_path: str | None = None,
    dir_map: list[tuple[str, str]] | None = None,
) -> tuple[int | None, str, str, str | None]:
    """Launch *cmd* inside the sandbox and return its raw result.

    Returns ``(returncode, stdout_text, stderr_text, timeout_reason)``.
    ``timeout_reason`` is ``None`` when the process exits before either
    watchdog limit. Callers are responsible for parsing output, validating
    the return code, and raising adapter-specific exceptions.

    ``on_output`` is called from the drain threads on every chunk received,
    on stdout and stderr alike. It must be cheap and thread-safe; an
    exception there would kill a drain thread and stall the turn.
    """
    proc = run_in_sandbox(
        cmd=cmd,
        workspace_path=workspace_path,
        tmp_path=tmp_path,
        hide_paths=hide_paths or [],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        extra_rw_paths=extra_rw_paths or [],
        attachments_path=attachments_path,
        dir_map=dir_map,
    )

    # Let the caller register the Popen handle immediately.
    on_subprocess(proc)

    assert proc.stdout is not None and proc.stderr is not None

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    activity_lock = threading.Lock()
    last_activity = time.monotonic()

    def _drain(stream: io.BufferedReader, chunks: list[bytes]) -> None:
        """Read *stream* until EOF in byte chunks, recording last activity.

        ``read1`` returns as soon as any byte is available, so a process that
        writes slowly or without newlines still resets the watchdog; ``read``
        would block until its buffer fills or EOF, making a trickling stream
        look stalled.
        """
        nonlocal last_activity
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if on_output is not None:
                on_output()
            with activity_lock:
                last_activity = time.monotonic()
        stream.close()

    stdout_thread = threading.Thread(
        target=_drain,
        args=(proc.stdout, stdout_chunks),
        name="agent-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain,
        args=(proc.stderr, stderr_chunks),
        name="agent-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    start = time.monotonic()
    timeout_reason: str | None = None

    try:
        # ------------------------------------------------------------------
        # Watchdog loop: exit when the process dies, or kill it when either
        # deadline fires.
        # ------------------------------------------------------------------
        while True:
            if proc.poll() is not None:
                break
            with activity_lock:
                elapsed_total = time.monotonic() - start
                elapsed_idle = time.monotonic() - last_activity
            if elapsed_total >= timeout_seconds:
                timeout_reason = f"exceeded {timeout_seconds}s in total"
                break
            if elapsed_idle >= idle_timeout_seconds:
                timeout_reason = f"produced no output for {elapsed_idle:.0f}s"
                break
            # Block until the process exits or the nearer of the two
            # deadlines arrives: wait() returns as soon as the process dies,
            # so a healthy turn is reaped promptly instead of being
            # discovered only after the idle window expires. On deadline it
            # raises TimeoutExpired and we re-evaluate both deadlines (fresh
            # output from the drain threads may have pushed the idle one).
            try:
                proc.wait(
                    timeout=max(
                        0.0,
                        min(
                            timeout_seconds - elapsed_total,
                            idle_timeout_seconds - elapsed_idle,
                        ),
                    )
                )
            except subprocess.TimeoutExpired:
                pass

        if timeout_reason is not None:
            # Kill and reap so the pipes close and the drain threads finish.
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Agent process did not exit after kill")

        # Give the drain threads a chance to flush buffered output; they
        # finish on EOF. A leftover grandchild holding a pipe open only
        # means we proceed with what we have.
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)

        stdout_text = b"".join(stdout_chunks).decode(errors="replace")
        stderr_text = b"".join(stderr_chunks).decode(errors="replace")
        return proc.returncode, stdout_text, stderr_text, timeout_reason

    finally:
        # Ensure the process is reaped if not already.
        if proc.returncode is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


def _tail(text: str, lines: int = 30) -> str:
    """Return the last *lines* lines of *text*."""
    all_lines = text.splitlines()
    return "\n".join(all_lines[-lines:])
