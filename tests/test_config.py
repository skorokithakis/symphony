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

    def test_unset_var_becomes_empty(self) -> None:
        assert _expand("$NO_SUCH_VAR") == ""

    def test_unset_var_in_path_keeps_surrounding_text(self) -> None:
        assert _expand("$NO_SUCH_VAR/something") == "/something"

    def test_literal_string_no_expansion(self) -> None:
        assert _expand("just a string") == "just a string"


class TestExpandValues:
    def test_dict_recursion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEY", "secret")
        data = {
            "linear": {"api_key": "$KEY"},
            "opencode": {"model": "~/model"},
        }
        result = _expand_values(data)
        assert result["linear"]["api_key"] == "secret"
        assert result["opencode"]["model"] == str(Path.home() / "model")

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
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert isinstance(config, AppConfig)
        assert config.linear.api_key == "test-key"
        assert config.linear.trigger_label == "agent"  # default
        assert config.sandbox.hide_paths  # defaults populated
        assert config.poll_interval_seconds == 30
        assert config.turn_timeout_seconds == 1800

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "test-key",
                # missing bot_user_email
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="Config validation failed"):
            load_config(tmp_path)

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
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear.api_key == "my-secret-token"

    def test_missing_config_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config(Path("/nonexistent"))

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        (tmp_path / "config.yaml").write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_config(tmp_path)

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
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.sandbox.hide_paths == ["/secret", str(Path.home() / "private")]

    def test_linear_api_key_env_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If linear.api_key is missing from YAML, LINEAR_API_KEY env var is used."""
        monkeypatch.setenv("LINEAR_API_KEY", "env-provided-key")
        cfg = {
            "linear": {
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear.api_key == "env-provided-key"

    def test_linear_api_key_empty_string_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If linear.api_key is an empty string, fall back to LINEAR_API_KEY."""
        monkeypatch.setenv("LINEAR_API_KEY", "env-provided-key")
        cfg = {
            "linear": {
                "api_key": "",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear.api_key == "env-provided-key"

    def test_linear_api_key_neither_set_raises(self, tmp_path: Path) -> None:
        """If neither YAML nor env var provides the key, raise a ValueError."""
        cfg = {
            "linear": {
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="LINEAR_API_KEY"):
            load_config(tmp_path)

    def test_unresolved_api_key_env_var_triggers_fallback_error(
        self, tmp_path: Path,
    ) -> None:
        """api_key: ${LINEAR_API_KEY} with unset env var → empty string → fallback error."""
        cfg = {
            "linear": {
                "api_key": "${LINEAR_API_KEY}",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="LINEAR_API_KEY"):
            load_config(tmp_path)

    def test_unknown_field_workspace_root_raises(self, tmp_path: Path) -> None:
        """Config containing the dead field workspace_root fails validation."""
        cfg = {
            "linear": {
                "api_key": "key",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
            "workspace_root": "~/anything",
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="workspace_root"):
            load_config(tmp_path)

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
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="Config validation failed"):
            load_config(tmp_path)

    def test_default_extra_rw_paths(self, tmp_path: Path) -> None:
        """extra_rw_paths defaults to an empty list."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.sandbox.extra_rw_paths == []

    def test_custom_extra_rw_paths_with_tilde(self, tmp_path: Path) -> None:
        """extra_rw_paths supports tilde expansion."""
        cfg = {
            "linear": {
                "api_key": "key",
                "bot_user_email": "bot@example.com",
            },
            "opencode": {
                "model": "anthropic/claude-sonnet-4",
            },
            "sandbox": {
                "extra_rw_paths": ["~/projects/shared", "/opt/tools"],
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.sandbox.extra_rw_paths == [
            str(Path.home() / "projects" / "shared"),
            "/opt/tools",
        ]
