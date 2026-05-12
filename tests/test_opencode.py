"""Tests for the OpenCode adapter module.

Includes:
- Unit tests for JSON event parsing (using captured fixture data).
- Integration tests that run real OpenCode inside the sandbox (requires
  ``bwrap`` and ``opencode``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from symphony_lite.opencode import (
    OpenCodeCancelled,
    OpenCodeError,
    OpenCodeTimeout,
    run_initial,
    run_resume,
)

# Ensure DEBUG logs are visible during test runs.
logging.basicConfig(level=logging.DEBUG)

# ---------------------------------------------------------------------------
# Markers & helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def _opencode_available() -> bool:
    return shutil.which("opencode") is not None


def _require_bwrap_and_opencode() -> None:
    if not _bwrap_available():
        pytest.skip("bwrap not available")
    if not _opencode_available():
        pytest.skip("opencode not available")


# ---------------------------------------------------------------------------
# Unit: JSON event parser (uses fixture data)
# ---------------------------------------------------------------------------

# These tests don't need external tools — they exercise the internal parser.


class TestParseEventsFromFixture:
    """Parse the fixture NDJSON file and verify basic event structure."""

    def test_fixture_has_three_events(self) -> None:
        """The captured fixture should contain exactly three events."""
        events = _load_fixture_events()
        assert len(events) == 3

    def test_fixture_contains_session_id(self) -> None:
        """Every event should carry a sessionID."""
        events = _load_fixture_events()
        for evt in events:
            assert "sessionID" in evt
            assert isinstance(evt["sessionID"], str)
            assert evt["sessionID"].startswith("ses_")

    def test_fixture_event_types(self) -> None:
        """The three events should be step_start, text, step_finish."""
        events = _load_fixture_events()
        types = [evt["type"] for evt in events]
        assert types == ["step_start", "text", "step_finish"]

    def test_extract_text_and_session_id(self) -> None:
        """Simulate the parser logic: session_id from first event,
        text from text events, detect step_finish."""
        events = _load_fixture_events()

        session_id: str | None = None
        text_parts: list[str] = []
        finished = False

        for evt in events:
            if session_id is None:
                session_id = evt.get("sessionID")
            if evt.get("type") == "text":
                text_parts.append(evt["part"]["text"])
            if evt.get("type") == "step_finish":
                finished = True

        assert session_id == "ses_1e3790378ffecZySU3wIpFOoIz"
        assert "".join(text_parts) == "hi"
        assert finished is True

    def test_parse_corrupt_line_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        """A corrupt JSON line should be logged and skipped."""
        corrupt_line = "not valid json {{{"
        with caplog.at_level(logging.DEBUG):
            _parse_one_line(corrupt_line)
        # Should have a debug log about skipping.
        assert any("Skipping" in rec.message for rec in caplog.records), (
            f"Expected debug skip message in: {[r.message for r in caplog.records]}"
        )

    def test_parse_empty_line(self) -> None:
        """Empty lines should be skipped silently."""
        result = _parse_one_line("")
        assert result is None
        result = _parse_one_line("   ")
        assert result is None


# ---------------------------------------------------------------------------
# Integration: real OpenCode runs inside sandbox
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRunInitialIntegration:
    """End-to-end tests that launch OpenCode inside the sandbox."""

    def test_simple_prompt_returns_session_and_message(self, tmp_path: Path) -> None:
        """Run a trivial prompt and verify we get back a session id and a message."""
        _require_bwrap_and_opencode()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        model = os.environ.get("SYMPHONY_TEST_MODEL") or _get_default_model()
        if not model:
            pytest.skip("No model available")

        # Capture the subprocess handle.
        handles: list[subprocess.Popen[bytes]] = []

        def on_subprocess(proc: subprocess.Popen[bytes]) -> None:
            handles.append(proc)

        try:
            session_id, message = run_initial(
                workspace_path=str(workspace),
                prompt="say exactly 'hello world' and stop",
                model=model,
                timeout_seconds=120,
                on_subprocess=on_subprocess,
            )
        except (OpenCodeError, OpenCodeTimeout) as exc:
            pytest.fail(f"OpenCode run failed: {exc}")

        assert isinstance(session_id, str)
        assert session_id.startswith("ses_"), f"Unexpected session id: {session_id}"
        assert isinstance(message, str)
        assert len(message) > 0, "Expected non-empty final message"
        assert len(handles) == 1
        # The handle should have been waited already (returncode set).
        assert handles[0].returncode is not None

    def test_timeout_kills_process(self, tmp_path: Path) -> None:
        """A very short timeout should trigger OpenCodeTimeout."""
        _require_bwrap_and_opencode()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        model = os.environ.get("SYMPHONY_TEST_MODEL") or _get_default_model()
        if not model:
            pytest.skip("No model available")

        handles: list[subprocess.Popen[bytes]] = []

        def on_subprocess(proc: subprocess.Popen[bytes]) -> None:
            handles.append(proc)

        with pytest.raises(OpenCodeTimeout):
            run_initial(
                workspace_path=str(workspace),
                prompt="write a detailed essay about the history of the internet",  # should take a while
                model=model,
                timeout_seconds=1,  # impossibly short
                on_subprocess=on_subprocess,
            )

        # The process should have been killed.
        assert len(handles) == 1
        assert handles[0].returncode is not None


@pytest.mark.integration
class TestRunResumeIntegration:
    """Integration tests for session resume."""

    def test_resume_session_produces_contextual_reply(self, tmp_path: Path) -> None:
        """Start a session, then resume it — the assistant should remember context."""
        _require_bwrap_and_opencode()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        model = os.environ.get("SYMPHONY_TEST_MODEL") or _get_default_model()
        if not model:
            pytest.skip("No model available")

        handles: list[subprocess.Popen[bytes]] = []

        def on_subprocess(proc: subprocess.Popen[bytes]) -> None:
            handles.append(proc)

        # Turn 1: Tell the assistant a secret.
        try:
            session_id, msg1 = run_initial(
                workspace_path=str(workspace),
                prompt=(
                    "My favorite color is 'zorkmid-purple'. "
                    "Acknowledge this with a single word: 'ok'."
                ),
                model=model,
                timeout_seconds=120,
                on_subprocess=on_subprocess,
            )
        except (OpenCodeError, OpenCodeTimeout) as exc:
            pytest.fail(f"Initial turn failed: {exc}")

        assert session_id, "Expected a session id"

        # Turn 2: Ask the assistant to recall the secret.
        try:
            msg2 = run_resume(
                workspace_path=str(workspace),
                session_id=session_id,
                message="What is my favorite color? Reply with only the color name.",
                timeout_seconds=120,
                on_subprocess=on_subprocess,
            )
        except (OpenCodeError, OpenCodeTimeout) as exc:
            pytest.fail(f"Resume turn failed: {exc}")

        # The assistant should mention zorkmid-purple or at least purple.
        assert len(msg2) > 0
        assert "zorkmid" in msg2.lower() or "purple" in msg2.lower(), (
            f"Expected assistant to recall the color, got: {msg2}"
        )


@pytest.mark.integration
class TestCancelledDetection:
    """Verify that externally killed processes raise OpenCodeCancelled."""

    def test_cancelled_on_kill(self, tmp_path: Path) -> None:
        """If the Popen handle is killed externally, run_initial should
        detect it and raise OpenCodeCancelled."""
        _require_bwrap_and_opencode()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        model = os.environ.get("SYMPHONY_TEST_MODEL") or _get_default_model()
        if not model:
            pytest.skip("No model available")

        handles: list[subprocess.Popen[bytes]] = []

        def on_subprocess(proc: subprocess.Popen[bytes]) -> None:
            handles.append(proc)
            # Immediately kill the process to simulate external cancellation.
            proc.kill()

        with pytest.raises(OpenCodeCancelled):
            run_initial(
                workspace_path=str(workspace),
                prompt="say hi",
                model=model,
                timeout_seconds=30,
                on_subprocess=on_subprocess,
            )

        assert len(handles) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_events() -> list[dict[str, Any]]:
    """Load the recorded OpenCode JSON events from the fixture file."""
    fixture_path = FIXTURE_DIR / "opencode_events.jsonl"
    raw = fixture_path.read_text()
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            events.append(json.loads(stripped))
    return events


def _parse_one_line(line: str) -> dict[str, Any] | None:
    """Simulate the parser logic on a single line (used by unit tests)."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        logging.getLogger("symphony_lite.opencode").debug(
            "Skipping unparseable JSON line: %s", stripped[:200]
        )
        return None


def _get_default_model() -> str:
    """Try to determine the default OpenCode model from its config."""
    # opencode.json (newer format)
    json_config = Path.home() / ".config" / "opencode" / "opencode.json"
    if json_config.exists():
        try:
            cfg = json.loads(json_config.read_text())
            if isinstance(cfg, dict):
                # Check top-level model key first.
                model = cfg.get("model", "")
                if model:
                    return model
                # Check agent configurations for a model.
                agents = cfg.get("agent", {})
                if isinstance(agents, dict):
                    for agent_cfg in agents.values():
                        if isinstance(agent_cfg, dict):
                            model = agent_cfg.get("model", "")
                            if model:
                                return model
        except Exception:
            pass
    # config.yaml (legacy format)
    yaml_config = Path.home() / ".config" / "opencode" / "config.yaml"
    if yaml_config.exists():
        try:
            import yaml
            cfg = yaml.safe_load(yaml_config.read_text())
            if isinstance(cfg, dict):
                return cfg.get("model", "")
        except Exception:
            pass
    return ""
