"""Tests for config loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from symphony_linear.config import (
    DEFAULT_HIDE_PATHS,
    AppConfig,
    _expand,
    _expand_values,
    load_config,
)


# ---------------------------------------------------------------------------
# Unit: env-var expansion
# ---------------------------------------------------------------------------


class TestExpand:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("~/foo/bar", str(Path.home() / "foo" / "bar")),
            ("$MY_VAR/world", "hello/world"),
            ("${MY_VAR}/world", "hello/world"),
            ("just a string", "just a string"),
        ],
    )
    def test_expand(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: str
    ) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _expand(value) == expected

    def test_unset_var_becomes_empty(self) -> None:
        assert _expand("$NO_SUCH_VAR/something") == "/something"


class TestExpandValues:
    def test_recursive_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEY", "secret")
        data = {
            "linear": {"api_key": "$KEY"},
            "sandbox": {"extra_rw_paths": ["~/model"]},
        }
        result = _expand_values(data)
        assert result["linear"]["api_key"] == "secret"
        assert result["sandbox"]["extra_rw_paths"] == [str(Path.home() / "model")]


# ---------------------------------------------------------------------------
# Integration: load_config
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


def _webui_block(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {
        "port": 8080,
        "password": "secret",
    }
    block.update(overrides)
    return block


class TestLoadConfig:
    def test_minimal_config_loads(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "test-key",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert isinstance(config, AppConfig)
        assert config.linear is not None
        assert config.linear.api_key == "test-key"
        assert config.github is None
        # An omitted sandbox block still hides the default credential paths.
        assert config.sandbox.hide_paths == list(DEFAULT_HIDE_PATHS)

    def test_empty_trigger_label_is_rejected(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "config.yaml",
            {"linear": {"api_key": "test-key", "trigger_label": ""}},
        )

        with pytest.raises(ValueError, match="trigger_label"):
            load_config(tmp_path)

    @pytest.mark.parametrize(
        "linear_block",
        [{}, {"api_key": ""}],
        ids=["missing-key", "empty-key"],
    )
    def test_linear_api_key_env_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        linear_block: dict[str, str],
    ) -> None:
        """A missing or empty linear.api_key falls back to LINEAR_API_KEY."""
        monkeypatch.setenv("LINEAR_API_KEY", "env-provided-key")
        _write_yaml(tmp_path / "config.yaml", {"linear": linear_block})

        config = load_config(tmp_path)
        assert config.linear is not None
        assert config.linear.api_key == "env-provided-key"

    @pytest.mark.parametrize(
        "linear_block",
        [{}, {"api_key": "${NO_SUCH_LINEAR_KEY}"}],
        ids=["missing-key", "unresolved-var"],
    )
    def test_linear_api_key_missing_everywhere_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        linear_block: dict[str, str],
    ) -> None:
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        _write_yaml(tmp_path / "config.yaml", {"linear": linear_block})

        with pytest.raises(ValueError, match="LINEAR_API_KEY"):
            load_config(tmp_path)

    def test_env_var_in_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LINEAR_KEY", "my-secret-token")
        cfg = {
            "linear": {
                "api_key": "$LINEAR_KEY",
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
            },
            "sandbox": {
                "hide_paths": ["/secret", "~/private"],
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.sandbox.hide_paths == ["/secret", str(Path.home() / "private")]

    def test_dir_map_keys_tilde_expanded(self, tmp_path: Path) -> None:
        """dir_map keys get ~ expansion (values are expanded by the loader)."""
        cfg = {
            "linear": {
                "api_key": "key",
            },
            "sandbox": {
                "dir_map": {
                    "~/.config/npm": "npm",
                    "~/.npm": "~/sandboxes/caches/.npm",
                },
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.sandbox.dir_map == {
            str(Path.home() / ".config" / "npm"): "npm",
            str(Path.home() / ".npm"): str(Path.home() / "sandboxes/caches/.npm"),
        }

    @pytest.mark.parametrize(
        ("dir_map", "message"),
        [
            ({"npm": "npm"}, "dir_map key"),
            ({"~/.config/npm": ""}, "non-empty"),
            ({"~/.config/npm": "../escape"}, r"'\.\.'"),
        ],
        ids=["relative-key", "empty-value", "dotdot-value"],
    )
    def test_invalid_dir_map_is_rejected(
        self, tmp_path: Path, dir_map: dict[str, str], message: str
    ) -> None:
        cfg = {
            "linear": {
                "api_key": "key",
            },
            "sandbox": {"dir_map": dir_map},
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match=message):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# models: alias map validation
# ---------------------------------------------------------------------------


class TestModelMapValidation:
    @pytest.mark.parametrize(
        "model_id",
        ["claude-fable-5", "/claude-opus-5", "anthropic/"],
        ids=["no-slash", "no-provider", "no-model"],
    )
    def test_invalid_model_id_is_rejected(self, tmp_path: Path, model_id: str) -> None:
        cfg = {
            "linear": {"api_key": "test-key"},
            "models": {"Strong": model_id},
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="provider/model"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# GitHub backend config tests
# ---------------------------------------------------------------------------


class TestLoadConfigGitHub:
    @pytest.mark.parametrize(
        "project",
        ["orgs/my-org/projects/1", "users/alice/projects/42"],
    )
    def test_minimal_github_config_loads(self, tmp_path: Path, project: str) -> None:
        cfg = {"github": {"token": "ghp_test", "project": project}}
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.token == "ghp_test"
        assert config.github.project == project
        assert config.linear is None

    def test_invalid_project_ref_rejected(self, tmp_path: Path) -> None:
        cfg = {
            "github": {
                "token": "ghp_test",
                "project": "not-a-valid-ref",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="Invalid project ref"):
            load_config(tmp_path)

    @pytest.mark.parametrize(
        "github_block",
        [
            {"project": "orgs/my-org/projects/1"},
            {"token": "", "project": "orgs/my-org/projects/1"},
        ],
        ids=["missing-token", "empty-token"],
    )
    def test_github_token_env_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        github_block: dict[str, str],
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "env-provided-token")
        _write_yaml(tmp_path / "config.yaml", {"github": github_block})

        config = load_config(tmp_path)
        assert config.github is not None
        assert config.github.token == "env-provided-token"

    def test_github_token_neither_set_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        _write_yaml(
            tmp_path / "config.yaml",
            {"github": {"project": "orgs/my-org/projects/1"}},
        )

        with pytest.raises(ValueError, match="GITHUB_TOKEN"):
            load_config(tmp_path)

    def test_clone_protocol_invalid_value_rejected(self, tmp_path: Path) -> None:
        cfg = {
            "github": {
                "token": "ghp_test",
                "project": "orgs/my-org/projects/1",
                "clone_protocol": "git",
            },
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="clone_protocol"):
            load_config(tmp_path)


class TestExactlyOneTracker:
    @pytest.mark.parametrize(
        "cfg",
        [
            {
                "linear": {"api_key": "key"},
                "github": {
                    "token": "ghp_test",
                    "project": "orgs/my-org/projects/1",
                },
            },
            {
                "linear": {"api_key": "key"},
                "webui": _webui_block(),
            },
            {
                "github": {
                    "token": "ghp_test",
                    "project": "orgs/my-org/projects/1",
                },
                "webui": _webui_block(),
            },
        ],
        ids=["linear-github", "linear-webui", "github-webui"],
    )
    def test_more_than_one_backend_raises(
        self, tmp_path: Path, cfg: dict[str, object]
    ) -> None:
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="More than one tracker backend"):
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
        cfg: dict[str, object] = {
            "linear": {},
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear is not None
        assert config.linear.api_key == "my-token"
        assert config.github is None


# ---------------------------------------------------------------------------
# Web UI config tests
# ---------------------------------------------------------------------------


class TestWebUIConfig:
    def test_webui_only_config_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A webui-only config needs none of the other backends' credentials."""
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        _write_yaml(tmp_path / "config.yaml", {"webui": _webui_block()})

        config = load_config(tmp_path)
        assert config.webui is not None
        assert config.webui.port == 8080
        assert config.webui.password == "secret"
        assert config.webui.host == "127.0.0.1"
        assert config.linear is None
        assert config.github is None

    def test_empty_api_token_raises(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "config.yaml", {"webui": _webui_block(api_token="  ")})

        with pytest.raises(ValueError, match="non-empty"):
            load_config(tmp_path)

    @pytest.mark.parametrize("field", ["port", "password"])
    def test_missing_required_field_raises(self, tmp_path: Path, field: str) -> None:
        block = _webui_block()
        del block[field]
        _write_yaml(tmp_path / "config.yaml", {"webui": block})

        with pytest.raises(ValueError, match=field):
            load_config(tmp_path)

    def test_empty_password_raises(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "config.yaml", {"webui": _webui_block(password="")})

        with pytest.raises(ValueError, match="non-empty"):
            load_config(tmp_path)

    def test_an_unknown_soniox_region_is_rejected(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "config.yaml",
            {"webui": _webui_block(soniox_region="mars")},
        )

        # A typo must fail at load time, not at the first dictation or speak
        # click, where the only symptom would be "Incorrect API key provided".
        with pytest.raises(ValueError, match="soniox_region"):
            load_config(tmp_path)

    def test_tts_defaults_to_adrian_at_1_15(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "config.yaml", {"webui": _webui_block()})

        config = load_config(tmp_path)
        assert config.webui is not None
        assert config.webui.tts_voice == "Adrian"
        assert config.webui.tts_speed == 1.15

    @pytest.mark.parametrize("speed", [0, -0.5, float("inf"), float("nan")])
    def test_a_non_positive_or_non_finite_tts_speed_is_rejected(
        self, tmp_path: Path, speed: float
    ) -> None:
        # No upper or lower bound is checked: Soniox owns the accepted range
        # and its refusal is shown on the button.  Zero and below cannot mean
        # anything, though, and an infinity or NaN cannot even be encoded as
        # JSON, so it would fail locally instead of being refused by Soniox.
        _write_yaml(
            tmp_path / "config.yaml",
            {"webui": _webui_block(tts_speed=speed)},
        )

        with pytest.raises(ValueError, match="tts_speed"):
            load_config(tmp_path)

    def test_an_empty_tts_voice_is_rejected(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "config.yaml",
            {"webui": _webui_block(tts_voice="  ")},
        )

        with pytest.raises(ValueError, match="non-empty"):
            load_config(tmp_path)

    def test_public_url_loses_trailing_slash(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "config.yaml",
            {"webui": _webui_block(public_url="https://symphony.example.com/")},
        )

        config = load_config(tmp_path)
        assert config.webui is not None
        assert config.webui.public_url == "https://symphony.example.com"

    def test_public_url_user_information_is_rejected(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "config.yaml",
            {"webui": _webui_block(public_url="https://user:pass@example.com")},
        )

        with pytest.raises(ValueError, match="user information"):
            load_config(tmp_path)

    @pytest.mark.parametrize("suffix", ["?", "#", "?/", "#/"])
    def test_public_url_trailing_empty_query_or_fragment_is_rejected(
        self, tmp_path: Path, suffix: str
    ) -> None:
        """urlsplit hides a bare '?' or '#', even with a trailing slash."""
        _write_yaml(
            tmp_path / "config.yaml",
            {"webui": _webui_block(public_url=f"https://example.com{suffix}")},
        )

        with pytest.raises(ValueError, match="query string or fragment"):
            load_config(tmp_path)

    @pytest.mark.parametrize(
        ("base_url", "message"),
        [
            ("https://:443", "host name"),
            ("https://host.example:abc", "valid port"),
            ("https://host.example:99999", "valid port"),
        ],
    )
    def test_base_url_rejects_a_missing_host_or_bad_port(
        self, tmp_path: Path, base_url: str, message: str
    ) -> None:
        _write_yaml(
            tmp_path / "config.yaml",
            {"webui": _webui_block(anthropic_base_url=base_url)},
        )

        with pytest.raises(ValueError, match=message):
            load_config(tmp_path)

    def test_anthropic_base_url_is_validated_like_public_url(
        self, tmp_path: Path
    ) -> None:
        _write_yaml(
            tmp_path / "config.yaml",
            {
                "webui": _webui_block(
                    anthropic_base_url="https://anthropic-gateway.example.com/"
                )
            },
        )

        config = load_config(tmp_path)
        assert config.webui is not None
        assert (
            config.webui.anthropic_base_url == "https://anthropic-gateway.example.com"
        )

        _write_yaml(
            tmp_path / "config.yaml",
            {"webui": _webui_block(anthropic_base_url="anthropic-gateway.example.com")},
        )
        with pytest.raises(ValueError, match="absolute http"):
            load_config(tmp_path)


class TestNullSubconfigs:
    @pytest.mark.parametrize("key", ["webui", "webhook"])
    def test_a_null_block_is_treated_as_absent(self, tmp_path: Path, key: str) -> None:
        """A YAML key with an empty value parses as None, not as a block."""
        cfg = {"linear": {"api_key": "key"}, key: None}
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.linear is not None
        assert getattr(config, key) is None


class TestRemovedConfigFields:
    @pytest.mark.parametrize(
        ("cfg", "message"),
        [
            pytest.param(
                {
                    "linear": {
                        "api_key": "test-key",
                        "bot_user_email": "bot@example.com",
                    }
                },
                "bot_user_email",
                id="linear-bot-user-email",
            ),
            pytest.param(
                {"linear": {"api_key": "key"}, "workspace_root": "~/anything"},
                "workspace_root",
                id="workspace-root",
            ),
            pytest.param(
                {"webui": _webui_block(username="admin")},
                "username",
                id="webui-username",
            ),
            pytest.param(
                {"webui": _webui_block(ntfy={"url": "https://example.com/mytopic"})},
                "webui -> ntfy: Extra inputs are not permitted",
                id="webui-ntfy",
            ),
        ],
    )
    def test_a_removed_field_fails_loudly(
        self, tmp_path: Path, cfg: dict[str, object], message: str
    ) -> None:
        """Old configs must be fixed, not silently ignored."""
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match=message):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# Webhook config tests
# ---------------------------------------------------------------------------


class TestWebhookConfig:
    def test_block_with_port_and_secret_loads(self, tmp_path: Path) -> None:
        cfg = {
            "linear": {
                "api_key": "test-key",
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

    @pytest.mark.parametrize(
        "webhook_block",
        [{"port": 4000, "linear_secret": ""}, {"port": 4000}],
        ids=["empty-secret", "missing-secret"],
    )
    def test_secret_env_var_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        webhook_block: dict[str, object],
    ) -> None:
        monkeypatch.setenv("SYMPHONY_LINEAR_WEBHOOK_SECRET", "env-secret")
        cfg = {
            "linear": {"api_key": "test-key"},
            "webhook": webhook_block,
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        config = load_config(tmp_path)
        assert config.webhook is not None
        assert config.webhook.linear_secret == "env-secret"

    @pytest.mark.parametrize(
        "webhook_block",
        [{"port": 4000, "linear_secret": ""}, {"port": 4000}],
        ids=["empty-secret", "missing-secret"],
    )
    def test_secret_missing_everywhere_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        webhook_block: dict[str, object],
    ) -> None:
        monkeypatch.delenv("SYMPHONY_LINEAR_WEBHOOK_SECRET", raising=False)
        cfg = {
            "linear": {"api_key": "test-key"},
            "webhook": webhook_block,
        }
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(ValueError, match="SYMPHONY_LINEAR_WEBHOOK_SECRET"):
            load_config(tmp_path)

    @pytest.mark.parametrize(
        "backend",
        [
            {
                "github": {
                    "token": "ghp_test",
                    "project": "orgs/my-org/projects/1",
                }
            },
            {"webui": _webui_block()},
        ],
        ids=["github", "webui"],
    )
    def test_a_backend_without_linear_rejects_a_webhook(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        backend: dict[str, object],
    ) -> None:
        """Webhooks are Linear-only; the scope error wins over a secret error."""
        monkeypatch.setenv("SYMPHONY_LINEAR_WEBHOOK_SECRET", "env-secret")
        cfg = {**backend, "webhook": {"port": 4000, "linear_secret": ""}}
        _write_yaml(tmp_path / "config.yaml", cfg)

        with pytest.raises(
            ValueError,
            match="webhook block requires a 'linear:' backend",
        ):
            load_config(tmp_path)
