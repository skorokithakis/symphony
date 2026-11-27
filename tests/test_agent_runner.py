"""Tests for the shared raw sandbox process runner."""

from __future__ import annotations

import io
import subprocess
import threading
import time
from typing import Callable
from unittest.mock import patch

from symphony_linear import agent_runner


class _ExitedPopen:
    """Minimal completed-process stand-in with readable output streams."""

    def __init__(self, stdout: bytes, stderr: bytes, returncode: int = 0) -> None:
        self.stdout = io.BufferedReader(io.BytesIO(stdout))
        self.stderr = io.BufferedReader(io.BytesIO(stderr))
        self._returncode = returncode
        self.killed = False

    def poll(self) -> int:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode

    def kill(self) -> None:
        self.killed = True

    @property
    def returncode(self) -> int:
        return self._returncode


class _StalledPopen:
    """A process that stays alive until killed or told to exit."""

    def __init__(self, stdout: bytes = b"") -> None:
        self.stdout = io.BufferedReader(io.BytesIO(stdout))
        self.stderr = io.BufferedReader(io.BytesIO(b""))
        self.returncode: int | None = None
        self.killed = False
        self._exited = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exited.set()

    def exit(self, code: int) -> None:
        self.returncode = code
        self._exited.set()


class _CapturedTimer:
    """Stand-in for ``threading.Timer`` that records callbacks instead of
    running them, so a stale callback can be fired deterministically."""

    instances: list[_CapturedTimer] = []
    created = threading.Event()

    def __init__(
        self,
        delay: float,
        callback: Callable[..., None],
        args: tuple[object, ...] = (),
    ) -> None:
        self.delay = delay
        self.callback = callback
        self.args = args
        self.daemon = False
        self.cancelled = threading.Event()
        _CapturedTimer.instances.append(self)
        _CapturedTimer.created.set()

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled.set()


def test_run_returns_raw_result_and_forwards_adapter_environment() -> None:
    """The shared runner returns raw output for adapters to interpret."""
    proc = _ExitedPopen(b"agent stdout", b"agent stderr")
    registered: list[_ExitedPopen] = []
    env = {"HOME": "/home/test", "AGENT_SETTING": "value"}

    with patch(
        "symphony_linear.agent_runner.run_in_sandbox", return_value=proc
    ) as sandbox:
        result = agent_runner.run(
            cmd=["agent", "run"],
            workspace_path="/workspace",
            tmp_path="/workspace/tmp",
            timeout_seconds=60,
            idle_timeout_seconds=30,
            on_subprocess=registered.append,
            env=env,
        )

    assert result == (0, "agent stdout", "agent stderr", None, False)
    assert registered == [proc]
    assert sandbox.call_args.kwargs["env"] == env
    assert sandbox.call_args.kwargs["stdout"] is subprocess.PIPE
    assert sandbox.call_args.kwargs["stderr"] is subprocess.PIPE


def test_on_output_fires_for_every_chunk_on_both_streams() -> None:
    """The web UI's age signal is the idle watchdog's, not stdout alone.

    A chunk on either stream means the agent is alive, so both drain threads
    must report it; counting stdout only would show a stale age while a turn
    streams nothing but logs.
    """
    proc = _ExitedPopen(b"agent stdout", b"agent stderr")
    calls: list[None] = []

    with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
        agent_runner.run(
            cmd=["agent", "run"],
            workspace_path="/workspace",
            tmp_path="/workspace/tmp",
            timeout_seconds=60,
            idle_timeout_seconds=30,
            on_subprocess=lambda process: None,
            env={"HOME": "/home/test"},
            on_output=lambda: calls.append(None),
        )

    assert len(calls) == 2


def test_split_stdout_lines_frames_lines_across_chunk_boundaries() -> None:
    """A JSON event can span several reads; the hook must see whole lines."""
    lines, remainder = agent_runner._split_stdout_lines(
        b'{"type":"back', b'ground"}\r\nnext\npar'
    )
    assert lines == ['{"type":"background"}', "next"]
    assert remainder == b"par"


def test_stdout_line_hook_kill_is_reported_as_a_planned_stop() -> None:
    """An armed grace delay kills the process and is not a watchdog timeout."""
    proc = _StalledPopen(stdout=b"background_wait\n")
    seen: list[str] = []

    def hook(line: str) -> float | None:
        seen.append(line)
        return 0.2 if line == "background_wait" else None

    with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
        result = agent_runner.run(
            cmd=["agent", "run"],
            workspace_path="/workspace",
            tmp_path="/workspace/tmp",
            timeout_seconds=60,
            idle_timeout_seconds=30,
            on_subprocess=lambda process: None,
            env={"HOME": "/home/test"},
            on_stdout_line=hook,
        )

    returncode, _stdout, _stderr, timeout_reason, planned_stop = result
    assert seen == ["background_wait"]
    assert planned_stop is True
    assert timeout_reason is None
    assert returncode == -9
    assert proc.killed


def test_stdout_line_hook_disarms_on_a_later_line() -> None:
    """The next stdout line cancels the armed timer, so the normal watchdog
    handles the rest of the turn."""
    proc = _StalledPopen(stdout=b"background_wait\nworking\n")

    def hook(line: str) -> float | None:
        return 0.3 if line == "background_wait" else None

    started = time.monotonic()
    with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
        result = agent_runner.run(
            cmd=["agent", "run"],
            workspace_path="/workspace",
            tmp_path="/workspace/tmp",
            timeout_seconds=0.4,
            idle_timeout_seconds=30,
            on_subprocess=lambda process: None,
            env={"HOME": "/home/test"},
            on_stdout_line=hook,
        )

    _returncode, _stdout, _stderr, timeout_reason, planned_stop = result
    assert planned_stop is False
    assert timeout_reason == "exceeded 0.4s in total"
    assert proc.killed
    assert time.monotonic() - started >= 0.3


def test_stdout_line_hook_does_not_claim_an_external_kill() -> None:
    """A kill that lands before the grace expires stays a cancellation."""
    proc = _StalledPopen(stdout=b"background_wait\n")
    killer = threading.Timer(0.05, proc.kill)
    killer.start()
    try:
        with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
            result = agent_runner.run(
                cmd=["agent", "run"],
                workspace_path="/workspace",
                tmp_path="/workspace/tmp",
                timeout_seconds=60,
                idle_timeout_seconds=30,
                on_subprocess=lambda process: None,
                env={"HOME": "/home/test"},
                on_stdout_line=lambda line: 0.3,
            )
    finally:
        killer.cancel()

    returncode, _stdout, _stderr, timeout_reason, planned_stop = result
    assert planned_stop is False
    assert timeout_reason is None
    assert returncode == -9


def test_stdout_line_hook_sees_a_final_line_without_a_newline() -> None:
    """A killed process can leave the last event unterminated; EOF frames it."""
    proc = _StalledPopen(stdout=b"unterminated")
    seen: list[str] = []

    def hook(line: str) -> float | None:
        seen.append(line)
        return None

    threading.Timer(0.05, lambda: proc.exit(0)).start()

    with patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc):
        agent_runner.run(
            cmd=["agent", "run"],
            workspace_path="/workspace",
            tmp_path="/workspace/tmp",
            timeout_seconds=60,
            idle_timeout_seconds=30,
            on_subprocess=lambda process: None,
            env={"HOME": "/home/test"},
            on_stdout_line=hook,
        )

    assert seen == ["unterminated"]


def test_stale_grace_callback_cannot_kill_after_a_disarm() -> None:
    """Timer.cancel() cannot stop a callback that already started.

    A callback from a superseded timer must see the generation change and
    leave the turn alone, even though it was already waiting on the lock when
    a later stdout line disarmed it.
    """
    proc = _StalledPopen(stdout=b"arm\ndisarm\n")
    seen: list[str] = []

    def hook(line: str) -> float | None:
        seen.append(line)
        return 0.3 if line == "arm" else None

    def fire_stale_callback() -> None:
        assert _CapturedTimer.created.wait(timeout=5)
        timer = _CapturedTimer.instances[0]
        assert timer.cancelled.wait(timeout=5)
        timer.callback(*timer.args)
        proc.exit(0)

    _CapturedTimer.instances.clear()
    _CapturedTimer.created.clear()
    stale_thread = threading.Thread(target=fire_stale_callback, daemon=True)
    with (
        patch("symphony_linear.agent_runner.threading.Timer", _CapturedTimer),
        patch("symphony_linear.agent_runner.run_in_sandbox", return_value=proc),
    ):
        stale_thread.start()
        result = agent_runner.run(
            cmd=["agent", "run"],
            workspace_path="/workspace",
            tmp_path="/workspace/tmp",
            timeout_seconds=30,
            idle_timeout_seconds=30,
            on_subprocess=lambda process: None,
            env={"HOME": "/home/test"},
            on_stdout_line=hook,
        )
        stale_thread.join(timeout=5)

    returncode, _stdout, _stderr, timeout_reason, planned_stop = result
    assert not stale_thread.is_alive()
    assert seen == ["arm", "disarm"]
    assert planned_stop is False
    assert timeout_reason is None
    assert returncode == 0
    assert proc.killed is False
