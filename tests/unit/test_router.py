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
        """Unknown profile falls back to default."""
        result = route(profile="nonexistent")
        assert result == get_config().claude.default_profile

    def test_no_profile_with_repo_uses_default(self):
        result = route(repo_url="https://github.com/org/repo")
        assert result == get_config().claude.default_profile

    def test_no_profile_no_repo_uses_code(self):
        result = route()
        assert result == get_config().claude.default_profile

    @patch.dict(os.environ, {"AGENTICORE_DEFAULT_PROFILE": "review"}, clear=False)
    def test_custom_default_profile(self):
        reset_config()
        result = route(repo_url="https://github.com/org/repo")
        assert result == "review"
