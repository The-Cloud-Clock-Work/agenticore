"""Unit tests for router module."""

import os
from unittest.mock import patch

import pytest

from agenticore.config import get_config, reset_config
from agenticore.router import route


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


@pytest.mark.unit
class TestRoute:
    def test_explicit_profile_code(self):
        """Explicit profile name is used directly."""
        from agenticore.profiles import Profile

        with patch("agenticore.router.get_profile", return_value=Profile(name="code")):
            result = route(profile="code")
        assert result == "code"

    def test_explicit_profile_review(self):
        from agenticore.profiles import Profile

        with patch("agenticore.router.get_profile", return_value=Profile(name="review")):
            result = route(profile="review")
        assert result == "review"

    def test_unknown_profile_falls_to_default(self):
        """Unknown profile falls back to agentihooks_profile."""
        result = route(profile="nonexistent")
        assert result == get_config().agentihooks_profile

    def test_no_profile_with_repo_uses_default(self):
        result = route(repo_url="https://github.com/org/repo")
        assert result == get_config().agentihooks_profile

    def test_no_profile_no_repo_uses_default(self):
        result = route()
        assert result == get_config().agentihooks_profile

    @patch.dict(os.environ, {"AGENTIHOOKS_PROFILE": "review"}, clear=False)
    def test_custom_agentihooks_profile(self):
        reset_config()
        result = route(repo_url="https://github.com/org/repo")
        assert result == "review"
