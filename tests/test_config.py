"""Tests for config loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from symphony_linear.config import (
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
            "sandbox": {"extra_rw_paths": ["~/model"]},
        }
        result = _expand_values(data)
        assert result["linear"]["api_key"] == "secret"
        assert result["sandbox"]["extra_rw_paths"] == [str(Path.home() / "model")]

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
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert isinstance(config, AppConfig)
        assert config.linear is not None
        assert config.linear.api_key == "test-key"
        assert config.linear.trigger_label == "Agent"  # default
        assert config.sandbox.hide_paths  # defaults populated
        assert config.poll_interval_seconds == 30
        assert config.turn_timeout_seconds == 1800
        assert config.auto_branch is True  # default

    def test_auto_branch_can_be_disabled(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "auto_branch": False,
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.auto_branch is False

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

    def test_env_var_in_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LINEAR_KEY", "my-secret-token")
        cfg = {
            "linear": {
                "api_key": "$LINEAR_KEY",
                "bot_user_email": "bot@example.com",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear is not None
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
            "sandbox": {
                "hide_paths": ["/secret", "~/private"],
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.sandbox.hide_paths == ["/secret", str(Path.home() / "private")]

    def test_linear_api_key_env_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If linear.api_key is missing from YAML, LINEAR_API_KEY env var is used."""
        monkeypatch.setenv("LINEAR_API_KEY", "env-provided-key")
        cfg = {
            "linear": {
                "bot_user_email": "bot@example.com",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear is not None
        assert config.linear.api_key == "env-provided-key"

    def test_linear_api_key_empty_string_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If linear.api_key is an empty string, fall back to LINEAR_API_KEY."""
        monkeypatch.setenv("LINEAR_API_KEY", "env-provided-key")
        cfg = {
            "linear": {
                "api_key": "",
                "bot_user_email": "bot@example.com",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear is not None
        assert config.linear.api_key == "env-provided-key"

    def test_linear_api_key_neither_set_raises(self, tmp_path: Path) -> None:
        """If neither YAML nor env var provides the key, raise a ValueError."""
        cfg = {
            "linear": {
                "bot_user_email": "bot@example.com",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="LINEAR_API_KEY"):
            load_config(tmp_path)

    def test_unresolved_api_key_env_var_triggers_fallback_error(
        self,
        tmp_path: Path,
    ) -> None:
        """api_key: ${LINEAR_API_KEY} with unset env var → empty string → fallback error."""
        cfg = {
            "linear": {
                "api_key": "${LINEAR_API_KEY}",
                "bot_user_email": "bot@example.com",
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

    def test_qa_state_defaults_to_none(self, tmp_path: Path) -> None:
        """qa_state is optional and defaults to None."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear is not None
        assert config.linear.qa_state is None

    def test_qa_state_round_trips_through_yaml(self, tmp_path: Path) -> None:
        """qa_state is accepted and preserved when set in YAML."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
                "qa_state": "In Review",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear is not None
        assert config.linear.qa_state == "In Review"


# ---------------------------------------------------------------------------
# GitHub backend config tests
# ---------------------------------------------------------------------------


class TestLoadConfigGitHub:
    def test_valid_minimal_github_config(self, tmp_path: Path) -> None:
        cfg = {
            "github": {
                "token": "ghp_test",
                "project": "orgs/my-org/projects/1",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.token == "ghp_test"
        assert config.github.project == "orgs/my-org/projects/1"
        assert config.github.trigger_field == "Symphony"  # default
        assert config.github.status_field == "Status"  # default
        assert config.github.in_progress_status == "In Progress"
        assert config.github.needs_input_status == "Needs Input"
        assert config.github.qa_status is None  # default
        assert config.linear is None

    def test_minimal_github_config_defaults_statuses(self, tmp_path: Path) -> None:
        """A GitHub config with only token and project validates, using
        default In Progress / Needs Input status names."""
        cfg = {
            "github": {
                "token": "ghp_test",
                "project": "orgs/my-org/projects/1",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.in_progress_status == "In Progress"
        assert config.github.needs_input_status == "Needs Input"
        assert config.github.qa_status is None

    def test_user_project_ref_accepted(self, tmp_path: Path) -> None:
        cfg = {
            "github": {
                "token": "ghp_test",
                "project": "users/alice/projects/42",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.project == "users/alice/projects/42"

    def test_invalid_project_ref_rejected(self, tmp_path: Path) -> None:
        cfg = {
            "github": {
                "token": "ghp_test",
                "project": "not-a-valid-ref",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="Invalid project ref"):
            load_config(tmp_path)

    def test_github_token_env_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "env-provided-token")
        cfg = {
            "github": {
                "project": "orgs/my-org/projects/1",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.token == "env-provided-token"

    def test_github_token_empty_string_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "env-provided-token")
        cfg = {
            "github": {
                "token": "",
                "project": "orgs/my-org/projects/1",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.token == "env-provided-token"

    def test_github_token_neither_set_raises(self, tmp_path: Path) -> None:
        cfg = {
            "github": {
                "project": "orgs/my-org/projects/1",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="GITHUB_TOKEN"):
            load_config(tmp_path)

    def test_github_qa_status_round_trips(self, tmp_path: Path) -> None:
        cfg = {
            "github": {
                "token": "ghp_test",
                "project": "orgs/my-org/projects/1",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
                "qa_status": "In Review",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.qa_status == "In Review"


class TestExactlyOneTracker:
    def test_both_blocks_set_raises(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "key",
                "bot_user_email": "bot@example.com",
            },
            "github": {
                "token": "ghp_test",
                "project": "orgs/my-org/projects/1",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="Both 'linear' and 'github'"):
            load_config(tmp_path)

    def test_neither_block_set_raises(self, tmp_path: Path) -> None:
        cfg: dict[str, object] = {
            "poll_interval_seconds": 60,
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="No tracker backend"):
            load_config(tmp_path)

    def test_github_only_does_not_require_linear_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A github-only config must not fail because LINEAR_API_KEY is unset."""
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        cfg = {
            "github": {
                "project": "orgs/my-org/projects/1",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.token == "ghp_test"
        assert config.linear is None

    def test_linear_only_does_not_require_github_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A linear-only config must not fail because GITHUB_TOKEN is unset."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("LINEAR_API_KEY", "my-token")
        cfg = {
            "linear": {
                "bot_user_email": "bot@example.com",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear is not None
        assert config.linear.api_key == "my-token"
        assert config.github is None


# ---------------------------------------------------------------------------
# Webhook config tests
# ---------------------------------------------------------------------------


class TestWebhookConfig:
    def test_block_absent_webhook_is_none(self, tmp_path: Path) -> None:
        """When no webhook block is in config, config.webhook is None."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.webhook is None

    def test_block_present_with_port_and_secret(self, tmp_path: Path) -> None:
        """Webhook config loads with explicit port and secret."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": 4000,
                "linear_secret": "my-secret",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.webhook is not None
        assert config.webhook.port == 4000
        assert config.webhook.linear_secret == "my-secret"

    def test_secret_empty_env_var_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If linear_secret is empty in YAML, fall back to env var."""
        monkeypatch.setenv("SYMPHONY_LINEAR_WEBHOOK_SECRET", "env-secret")
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": 4000,
                "linear_secret": "",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.webhook is not None
        assert config.webhook.linear_secret == "env-secret"

    def test_secret_missing_env_var_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If linear_secret key is absent from YAML, fall back to env var."""
        monkeypatch.setenv("SYMPHONY_LINEAR_WEBHOOK_SECRET", "env-secret")
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": 4000,
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.webhook is not None
        assert config.webhook.linear_secret == "env-secret"

    def test_secret_empty_env_var_unset_raises(self, tmp_path: Path) -> None:
        """If linear_secret is empty and env var is unset, raise ValueError."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": 4000,
                "linear_secret": "",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="SYMPHONY_LINEAR_WEBHOOK_SECRET"):
            load_config(tmp_path)

    def test_secret_missing_env_var_unset_raises(self, tmp_path: Path) -> None:
        """If linear_secret key is absent and env var unset, raise ValueError."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": 4000,
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="SYMPHONY_LINEAR_WEBHOOK_SECRET"):
            load_config(tmp_path)

    def test_secret_env_var_in_yaml_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """$VAR expansion works for linear_secret in YAML."""
        monkeypatch.setenv("MY_SECRET", "expanded-secret")
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": 4000,
                "linear_secret": "$MY_SECRET",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.webhook is not None
        assert config.webhook.linear_secret == "expanded-secret"

    def test_port_zero_raises_validation_error(self, tmp_path: Path) -> None:
        """Port 0 is invalid (must be gt=0)."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": 0,
                "linear_secret": "secret",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="Config validation failed"):
            load_config(tmp_path)

    def test_port_negative_raises_validation_error(self, tmp_path: Path) -> None:
        """Negative port is invalid."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": -1,
                "linear_secret": "secret",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="Config validation failed"):
            load_config(tmp_path)

    def test_port_above_65535_raises_validation_error(self, tmp_path: Path) -> None:
        """Port > 65535 is invalid."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": {
                "port": 65536,
                "linear_secret": "secret",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="Config validation failed"):
            load_config(tmp_path)

    def test_webhook_null_in_yaml_becomes_none(self, tmp_path: Path) -> None:
        """webhook: with no sub-fields is treated as absent (None)."""
        cfg = {
            "linear": {
                "api_key": "test-key",
                "bot_user_email": "bot@example.com",
            },
            "webhook": None,
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.webhook is None

    def test_webhook_with_github_backend_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Webhook block with GitHub backend is rejected — only Linear supports webhooks."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("SYMPHONY_LINEAR_WEBHOOK_SECRET", "env-secret")
        cfg = {
            "github": {
                "project": "orgs/my-org/projects/1",
                "in_progress_status": "In Progress",
                "needs_input_status": "Needs Input",
            },
            "webhook": {
                "port": 4000,
                "linear_secret": "",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(
            ValueError,
            match="webhook block requires a 'linear:' backend",
        ):
            load_config(tmp_path)
