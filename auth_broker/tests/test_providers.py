"""Unit tests for auth_broker.providers."""

import os
import tempfile

import pytest
import yaml

from auth_broker import providers
from auth_broker.config import get_config, reset_config

SAMPLE_YAML = {
    "providers": {
        "github": {
            "auth_type": "oauth2",
            "display_name": "GitHub",
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "client_id_env": "GITHUB_CLIENT_ID",
            "client_secret_env": "GITHUB_CLIENT_SECRET",
            "scopes": ["repo"],
        },
        "anthropic": {
            "auth_type": "manual",
            "display_name": "Anthropic",
        },
    }
}


@pytest.fixture(autouse=True)
def reset_registry():
    providers._registry = {}
    yield
    providers._registry = {}


class TestLoadProviders:
    def test_loads_valid_yaml(self, tmp_path):
        p = tmp_path / "providers.yaml"
        p.write_text(yaml.dump(SAMPLE_YAML))
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("PROVIDERS_PATH", str(p))
            reset_config()
            providers.load_providers()
        assert "github" in providers._registry
        assert "anthropic" in providers._registry

    def test_missing_file_gives_empty_registry(self, tmp_path):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("PROVIDERS_PATH", str(tmp_path / "nonexistent.yaml"))
            reset_config()
            providers.load_providers()
        assert providers._registry == {}

    def test_empty_yaml_gives_empty_registry(self, tmp_path):
        p = tmp_path / "providers.yaml"
        p.write_text("")
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("PROVIDERS_PATH", str(p))
            reset_config()
            providers.load_providers()
        assert providers._registry == {}


class TestGetProvider:
    def test_known_provider_returns_dict(self):
        providers._registry = {"github": {"auth_type": "oauth2"}}
        result = providers.get_provider("github")
        assert result == {"auth_type": "oauth2"}

    def test_unknown_provider_returns_none(self):
        providers._registry = {}
        result = providers.get_provider("unknown")
        assert result is None


class TestListProviders:
    def test_list_providers_returns_all(self):
        providers._registry = {"a": {}, "b": {}}
        assert providers.list_providers() == {"a": {}, "b": {}}

    def test_list_provider_names(self):
        providers._registry = {"github": {}, "anthropic": {}}
        names = providers.list_provider_names()
        assert set(names) == {"github", "anthropic"}
