"""Unit tests for agenticore.git_credentials — GIT_ASKPASS helper."""

import os
import stat
import subprocess

import pytest

from agenticore.git_credentials import git_askpass_env, sanitize_remote_url, strip_credentials_from_url


@pytest.mark.unit
class TestGitAskpassEnv:
    def test_none_token_yields_empty_dict(self):
        with git_askpass_env(None) as env:
            assert env == {}

    def test_empty_token_yields_empty_dict(self):
        with git_askpass_env("") as env:
            assert env == {}

    def test_creates_executable_script(self):
        with git_askpass_env("ghp_test123") as env:
            script_path = env["GIT_ASKPASS"]
            assert os.path.exists(script_path)
            st = os.stat(script_path)
            assert st.st_mode & stat.S_IXUSR  # owner-executable

    def test_script_cleaned_up_after_exit(self):
        with git_askpass_env("ghp_test123") as env:
            script_path = env["GIT_ASKPASS"]
            assert os.path.exists(script_path)
        assert not os.path.exists(script_path)

    def test_script_cleaned_up_on_exception(self):
        script_path = None
        try:
            with git_askpass_env("ghp_test123") as env:
                script_path = env["GIT_ASKPASS"]
                raise ValueError("boom")
        except ValueError:
            pass
        assert script_path is not None
        assert not os.path.exists(script_path)

    def test_env_has_required_keys(self):
        with git_askpass_env("ghp_test123") as env:
            assert "GIT_ASKPASS" in env
            assert env["_AGENTICORE_GIT_CREDENTIAL"] == "ghp_test123"
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            assert env["GIT_CONFIG_COUNT"] == "4"
            assert env["GIT_CONFIG_KEY_0"] == "credential.username"
            assert env["GIT_CONFIG_VALUE_0"] == "x-access-token"

    def test_script_echoes_credential(self):
        """The askpass script should echo the credential from the env var."""
        with git_askpass_env("ghp_my_secret_token") as env:
            result = subprocess.run(
                [env["GIT_ASKPASS"]],
                capture_output=True,
                text=True,
                env={**os.environ, **env},
            )
        assert result.stdout.strip() == "ghp_my_secret_token"


@pytest.mark.unit
class TestStripCredentialsFromUrl:
    def test_strips_token(self):
        dirty = "https://x-access-token:ghp_abc123@github.com/org/repo.git"
        assert strip_credentials_from_url(dirty) == "https://github.com/org/repo.git"

    def test_passes_clean_url(self):
        clean = "https://github.com/org/repo.git"
        assert strip_credentials_from_url(clean) == clean

    def test_handles_http(self):
        dirty = "http://x-access-token:tok@github.com/repo"
        assert strip_credentials_from_url(dirty) == "http://github.com/repo"


@pytest.mark.unit
class TestSanitizeRemoteUrl:
    def test_fixes_embedded_token(self, tmp_path):
        """Detects and cleans an embedded token from .git/config remote URL."""
        # Set up a fake git repo with a dirty remote URL
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://x-access-token:ghp_secret@github.com/org/repo.git"],
            cwd=repo,
            capture_output=True,
            check=True,
        )

        sanitize_remote_url(str(repo))

        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "https://github.com/org/repo.git"

    def test_clean_url_unchanged(self, tmp_path):
        """Clean URL is not modified."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/org/repo.git"],
            cwd=repo,
            capture_output=True,
            check=True,
        )

        sanitize_remote_url(str(repo))

        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "https://github.com/org/repo.git"

    def test_no_remote_is_noop(self, tmp_path):
        """No remote origin → no crash."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        sanitize_remote_url(str(repo))  # should not raise
