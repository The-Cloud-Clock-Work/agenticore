"""Unit tests for config module."""

import os
from unittest.mock import patch

import pytest
import yaml

from agenticore.config import get_config, load_config, reset_config

# Env vars that could leak from the host and affect config defaults
_CLEAN_ENV = {
    "REDIS_URL": "",
    "REDIS_KEY_PREFIX": "",
    "AGENTICORE_PORT": "",
    "AGENTICORE_HOST": "",
    "AGENTICORE_TRANSPORT": "",
    "AGENTICORE_CLAUDE_BINARY": "",
    "AGENTICORE_DEFAULT_PROFILE": "",
    "AGENTICORE_REPOS_ROOT": "",
    "AGENTICORE_API_KEYS": "",
    "AGENTICORE_OTEL_ENABLED": "",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "",
    "AGENTICORE_OTEL_LOG_PROMPTS": "",
    "AGENTICORE_OTEL_LOG_TOOL_DETAILS": "",
    "GITHUB_TOKEN": "",
    "AGENTICORE_CLAUDE_CONFIG_DIR": "",
}


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


@pytest.mark.unit
class TestLoadConfig:
    @patch.dict(os.environ, _CLEAN_ENV)
    def test_defaults_no_file(self, tmp_path):
        """Config loads with defaults when no YAML file exists."""
        cfg = load_config(str(tmp_path / "nonexistent.yml"))
        assert cfg.server.port == 8200
        assert cfg.server.host == "127.0.0.1"
        assert cfg.claude.binary == "claude"
        assert cfg.claude.default_profile == "code"
        assert cfg.redis.key_prefix == "agenticore"
        assert cfg.otel.enabled is True

    @patch.dict(os.environ, _CLEAN_ENV)
    def test_yaml_values(self, tmp_path):
        """Config loads values from YAML file."""
        config_file = tmp_path / "config.yml"
        config_file.write_text(
            yaml.dump(
                {
                    "server": {"port": 9000, "host": "0.0.0.0"},
                    "claude": {"binary": "/usr/bin/claude", "default_profile": "review"},
                    "redis": {"url": "redis://myhost:6379/1"},
                }
            )
        )
        cfg = load_config(str(config_file))
        assert cfg.server.port == 9000
        assert cfg.server.host == "0.0.0.0"
        assert cfg.claude.binary == "/usr/bin/claude"
        assert cfg.claude.default_profile == "review"
        assert cfg.redis.url == "redis://myhost:6379/1"

    @patch.dict(
        os.environ,
        {
            "AGENTICORE_PORT": "9999",
            "AGENTICORE_HOST": "10.0.0.1",
            "AGENTICORE_CLAUDE_BINARY": "/opt/claude",
            "REDIS_URL": "redis://env:6379/2",
        },
    )
    def test_env_overrides_yaml(self, tmp_path):
        """Env vars override YAML values."""
        config_file = tmp_path / "config.yml"
        config_file.write_text(
            yaml.dump(
                {
                    "server": {"port": 9000, "host": "0.0.0.0"},
                    "redis": {"url": "redis://yaml:6379/0"},
                }
            )
        )
        cfg = load_config(str(config_file))
        assert cfg.server.port == 9999
        assert cfg.server.host == "10.0.0.1"
        assert cfg.claude.binary == "/opt/claude"
        assert cfg.redis.url == "redis://env:6379/2"

    @patch.dict(os.environ, {"AGENTICORE_API_KEYS": "key1,key2,key3"})
    def test_api_keys_from_env(self, tmp_path):
        """API keys parsed from comma-separated env var."""
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.server.api_keys == ["key1", "key2", "key3"]

    @patch.dict(os.environ, {"AGENTICORE_OTEL_ENABLED": "false"})
    def test_otel_disabled(self, tmp_path):
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.otel.enabled is False

    @patch.dict(os.environ, {"AGENTICORE_REPOS_ROOT": ""}, clear=False)
    def test_repos_root_expands_tilde(self, tmp_path):
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({"repos": {"root": "~/my-repos"}}))
        cfg = load_config(str(config_file))
        assert "~" not in cfg.repos.root
        assert cfg.repos.root.endswith("my-repos")

    @patch.dict(
        os.environ,
        {
            "AGENTICORE_SHARED_FS_ROOT": "/shared",
            "AGENTICORE_JOBS_DIR": "/shared/job-state",
            "AGENTICORE_POD_NAME": "agenticore-0",
        },
        clear=False,
    )
    def test_kubernetes_env_vars(self, tmp_path):
        """K8s env vars load into ReposConfig."""
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.repos.shared_fs_root == "/shared"
        assert cfg.repos.jobs_dir == "/shared/job-state"
        assert cfg.repos.pod_name == "agenticore-0"

    @patch.dict(
        os.environ,
        {"AGENTICORE_SHARED_FS_ROOT": "", "AGENTICORE_JOBS_DIR": "", "AGENTICORE_POD_NAME": ""},
        clear=False,
    )
    def test_kubernetes_env_vars_default_empty(self, tmp_path):
        """K8s fields default to empty string when not set."""
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.repos.shared_fs_root == ""
        assert cfg.repos.jobs_dir == ""
        assert cfg.repos.pod_name == ""

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker:8300", "AUTH_BROKER_API_KEY": "secret"},
        clear=False,
    )
    def test_auth_broker_from_env(self, tmp_path):
        """Auth broker config loads from env vars."""
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.auth_broker.url == "http://broker:8300"
        assert cfg.auth_broker.api_key == "secret"

    @patch.dict(os.environ, {"AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": ""}, clear=False)
    def test_auth_broker_defaults_empty(self, tmp_path):
        """Auth broker defaults to empty when not configured."""
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.auth_broker.url == ""
        assert cfg.auth_broker.api_key == ""

    @patch.dict(os.environ, {"AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": ""}, clear=False)
    def test_auth_broker_from_yaml(self, tmp_path):
        """Auth broker config loads from YAML file."""
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({"auth_broker": {"url": "http://yaml-broker", "api_key": "yaml-key"}}))
        cfg = load_config(str(config_file))
        assert cfg.auth_broker.url == "http://yaml-broker"
        assert cfg.auth_broker.api_key == "yaml-key"

    def test_agentihooks_url_and_sync_interval(self, monkeypatch):
        """AGENTICORE_AGENTIHOOKS_URL and AGENTICORE_AGENTIHOOKS_SYNC_INTERVAL load from env."""
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_URL", "https://github.com/org/agentihooks")
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_SYNC_INTERVAL", "600")
        cfg = get_config()
        assert cfg.agentihooks_url == "https://github.com/org/agentihooks"
        assert cfg.agentihooks_sync_interval == 600
