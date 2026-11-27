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

-------------------------------------------------------------------------------
Adapter-requested grace stops
-------------------------------------------------------------------------------

An adapter can pass ``on_stdout_line``, a callback invoked for every complete
stdout line (reassembled across chunk boundaries) that returns a grace delay
in seconds to arm, or ``None`` to disarm. A timer thread kills the process
when the delay expires while the process is still running, and the run
reports ``planned_stop=True`` so the adapter can treat the turn as a success
rather than a cancellation. This exists for OpenCode, whose CLI waits
indefinitely for background shell jobs; OMP and pi do not pass the hook.

The timer is a separate thread because the main loop blocks in
``proc.wait()``; it cannot observe a deadline that the stdout drain thread
arms after the wait started, but the timer's kill makes that wait return.
A process that has already exited, or one killed externally (the
orchestrator calls ``kill()`` on the registered Popen), is left alone: its
own outcome wins, so an external cancellation never becomes a success.
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
    on_stdout_line: Callable[[str], float | None] | None = None,
    hide_paths: list[str] | None = None,
    extra_rw_paths: list[str] | None = None,
    attachments_path: str | None = None,
    dir_map: list[tuple[str, str]] | None = None,
) -> tuple[int | None, str, str, str | None, bool]:
    """Launch *cmd* inside the sandbox and return its raw result.

    Returns
    ``(returncode, stdout_text, stderr_text, timeout_reason, planned_stop)``.
    ``timeout_reason`` is ``None`` when the process exits before either
    watchdog limit. ``planned_stop`` is ``True`` only when the process was
    killed by the grace timer armed through ``on_stdout_line``; an external
    kill, a watchdog timeout and a normal exit all leave it ``False``.
    Callers are responsible for parsing output, validating the return code,
    and raising adapter-specific exceptions.

    ``on_output`` is called from the drain threads on every chunk received,
    on stdout and stderr alike. It must be cheap and thread-safe; an
    exception there would kill a drain thread and stall the turn.

    ``on_stdout_line`` is called from the stdout drain thread for every
    complete stdout line, including empty ones, with lines reassembled across
    chunk boundaries (stderr is not framed). Its return value is a grace
    delay in seconds: a number arms a timer that kills the process when it
    expires, while ``None`` disarms an armed timer. The callback must be
    cheap and thread-safe, and must not raise, for the same reason as
    ``on_output``.
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

    # The grace timer runs on its own thread: the watchdog loop blocks in
    # proc.wait(), which cannot notice a deadline the stdout drain thread
    # arms after that wait has started. The timer's kill makes the wait
    # return.
    grace_lock = threading.Lock()
    grace_timer: threading.Timer | None = None
    grace_generation = 0
    planned_stop = False

    def _expire_grace(generation: int) -> None:
        nonlocal planned_stop
        with grace_lock:
            # Timer.cancel() cannot stop a callback that has already started
            # and is waiting for this lock, so a callback from a superseded
            # timer must not kill: a later stdout line may have disarmed it or
            # re-armed a new one.
            if generation != grace_generation:
                return
            # A process that already exited, or that the orchestrator killed
            # externally, keeps its own outcome; our kill is a planned stop
            # only when the process was still alive to receive it.
            if proc.poll() is not None:
                return
            planned_stop = True
        proc.kill()

    def _arm_grace(delay: float) -> None:
        nonlocal grace_timer, grace_generation
        with grace_lock:
            if grace_timer is not None:
                grace_timer.cancel()
            grace_generation += 1
            generation = grace_generation
            timer = threading.Timer(delay, _expire_grace, args=(generation,))
            timer.daemon = True
            grace_timer = timer
            timer.start()

    def _disarm_grace() -> None:
        nonlocal grace_timer, grace_generation
        with grace_lock:
            # Bump the generation before cancelling so a callback that has
            # already started sees the change as soon as it takes this lock.
            grace_generation += 1
            if grace_timer is not None:
                grace_timer.cancel()
                grace_timer = None

    def _dispatch_stdout_line(line: str) -> None:
        if on_stdout_line is None:
            return
        delay = on_stdout_line(line)
        if delay is None:
            _disarm_grace()
        else:
            _arm_grace(delay)

    def _drain(
        stream: io.BufferedReader,
        chunks: list[bytes],
        *,
        frame_lines: bool = False,
    ) -> None:
        """Read *stream* until EOF in byte chunks, recording last activity.

        ``read1`` returns as soon as any byte is available, so a process that
        writes slowly or without newlines still resets the watchdog; ``read``
        would block until its buffer fills or EOF, making a trickling stream
        look stalled.
        """
        nonlocal last_activity
        pending = b""
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if on_output is not None:
                on_output()
            if frame_lines:
                lines, pending = _split_stdout_lines(pending, chunk)
                for line in lines:
                    _dispatch_stdout_line(line)
            with activity_lock:
                last_activity = time.monotonic()
        if frame_lines and pending:
            # A killed process can leave a last line without its newline;
            # frame it at EOF so a final event is still seen.
            _dispatch_stdout_line(pending.decode(errors="replace").rstrip("\r"))
        stream.close()

    stdout_thread = threading.Thread(
        target=_drain,
        args=(proc.stdout, stdout_chunks),
        kwargs={"frame_lines": on_stdout_line is not None},
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

        with grace_lock:
            # A watchdog timeout always wins: it is never a planned stop, even
            # if the grace timer happened to fire in the same instant.
            stopped_early = planned_stop and timeout_reason is None

        # Give the drain threads a chance to flush buffered output; they
        # finish on EOF. A leftover grandchild holding a pipe open only
        # means we proceed with what we have.
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)

        stdout_text = b"".join(stdout_chunks).decode(errors="replace")
        stderr_text = b"".join(stderr_chunks).decode(errors="replace")
        return (
            proc.returncode,
            stdout_text,
            stderr_text,
            timeout_reason,
            stopped_early,
        )

    finally:
        # A timer left armed after the run would fire into a finished turn;
        # cancelling it also keeps the thread count honest.
        _disarm_grace()
        # Ensure the process is reaped if not already.
        if proc.returncode is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


def _split_stdout_lines(pending: bytes, chunk: bytes) -> tuple[list[str], bytes]:
    """Split ``pending + chunk`` into complete stdout lines and a remainder.

    Drain threads read arbitrary chunks, so one NDJSON event can span several
    reads; the line hook must see whole lines.  The returned remainder is
    whatever follows the last newline and is carried into the next call.
    """
    parts = (pending + chunk).split(b"\n")
    remainder = parts.pop()
    return [part.decode(errors="replace").rstrip("\r") for part in parts], remainder


def _tail(text: str, lines: int = 30) -> str:
    """Return the last *lines* lines of *text*."""
    all_lines = text.splitlines()
    return "\n".join(all_lines[-lines:])
