"""Tests for config loading and validation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from symphony_lite.config import (
    AppConfig,
    _expand,
    _expand_values,
    load_config,
)


# ---------------------------------------------------------------------------
# Unit: env-var expansion
# ---------------------------------------------------------------------------


class TestExpand:
    def test_tilde_expansion(self) -> None:
        result = _expand("~/foo/bar")
        assert result == str(Path.home() / "foo" / "bar")

    def test_dollar_var_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _expand("$MY_VAR/world") == "hello/world"

    def test_braced_var_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _expand("${MY_VAR}/world") == "hello/world"

    def test_unset_var_preserved(self) -> None:
        assert _expand("$NO_SUCH_VAR") == "$NO_SUCH_VAR"

    def test_literal_string_no_expansion(self) -> None:
        assert _expand("just a string") == "just a string"


class TestExpandValues:
    def test_dict_recursion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEY", "secret")
        data = {
            "linear": {"api_key": "$KEY"},
            "workspace_root": "~/ws",
        }
        result = _expand_values(data)
        assert result["linear"]["api_key"] == "secret"
        assert result["workspace_root"] == str(Path.home() / "ws")

    def test_list_recursion(self) -> None:
        data = ["~/a", "~/b"]
        result = _expand_values(data)
        assert result == [str(Path.home() / "a"), str(Path.home() / "b")]


# ---------------------------------------------------------------------------
# Integration: load_config
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


class TestLoadConfig:
    def test_valid_minimal_config(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
        }
        path = tmp_path / "config.yaml"
        _write_yaml(path, cfg)

        config = load_config(path)
        assert isinstance(config, AppConfig)
        assert config.linear.api_key == "test-key"
        assert config.linear.trigger_label == "agent"  # default
        assert config.sandbox.hide_paths  # defaults populated
        assert config.poll_interval_seconds == 30
        assert config.turn_timeout_seconds == 1800

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "bot_user_email": "bot@example.com",
            },
            # missing opencode section entirely
        }
        path = tmp_path / "config.yaml"
        _write_yaml(path, cfg)

        with pytest.raises(ValueError, match="Config validation failed"):
            load_config(path)

    def test_env_var_in_api_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LINEAR_KEY", "my-secret-token")
        cfg = {
            "linear": {
                "api_key": "$LINEAR_KEY",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
        }
        path = tmp_path / "config.yaml"
        _write_yaml(path, cfg)

        config = load_config(path)
        assert config.linear.api_key == "my-secret-token"

    def test_workspace_root_expansion(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "key",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
            "workspace_root": "~/myprojects",
        }
        path = tmp_path / "config.yaml"
        _write_yaml(path, cfg)

        config = load_config(path)
        assert config.workspace_root == Path.home() / "myprojects"

    def test_nonexistent_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config(Path("/nonexistent/config.yaml"))

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_config(path)

    def test_custom_hide_paths(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "key",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
            "sandbox": {
                "hide_paths": ["/secret", "~/private"],
            },
        }
        path = tmp_path / "config.yaml"
        _write_yaml(path, cfg)

        config = load_config(path)
        assert config.sandbox.hide_paths == ["/secret", str(Path.home() / "private")]

    def test_symphony_config_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = {
            "linear": {
                "api_key": "key",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
        }
        path = tmp_path / "custom.yaml"
        _write_yaml(path, cfg)
        monkeypatch.setenv("SYMPHONY_CONFIG", str(path))

        config = load_config()
        assert config.linear.api_key == "key"

    def test_poll_interval_must_be_positive(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "key",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
            "poll_interval_seconds": 0,
        }
        path = tmp_path / "config.yaml"
        _write_yaml(path, cfg)

        with pytest.raises(ValueError, match="Config validation failed"):
            load_config(path)
