"""Unit tests for repos module."""

import pytest

from agenticore.repos import _repo_key


@pytest.mark.unit
class TestRepoKey:
    def test_deterministic(self):
        """Same URL always produces same key."""
        url = "https://github.com/org/repo.git"
        assert _repo_key(url) == _repo_key(url)

    def test_different_urls_different_keys(self):
        k1 = _repo_key("https://github.com/org/repo1.git")
        k2 = _repo_key("https://github.com/org/repo2.git")
        assert k1 != k2

    def test_length_12(self):
        key = _repo_key("https://github.com/org/repo.git")
        assert len(key) == 12

    def test_hex_chars_only(self):
        key = _repo_key("https://example.com/repo")
        assert all(c in "0123456789abcdef" for c in key)
