"""Tests for the shared raw sandbox process runner."""

from __future__ import annotations

import io
import subprocess
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

    assert result == (0, "agent stdout", "agent stderr", None)
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
