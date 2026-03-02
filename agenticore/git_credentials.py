"""GIT_ASKPASS credential helper — keeps tokens out of URLs, args, and .git/config.

Instead of embedding ``x-access-token:TOKEN@github.com`` in the clone URL, this
module creates a temporary GIT_ASKPASS script that echoes the token from an
environment variable. Git calls the script when it needs a password, and the
token never appears in ``ps aux``, ``.git/config``, or error output.
"""

import logging
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

_TOKEN_IN_URL_RE = re.compile(r"(https?://)x-access-token:[^@]+@")


@contextmanager
def git_askpass_env(token: Optional[str]):
    """Context manager yielding an env dict overlay for authenticated git operations.

    When *token* is non-empty, creates a temporary GIT_ASKPASS script that echoes
    the credential from ``_AGENTICORE_GIT_CREDENTIAL``.  The script is removed
    on exit.

    When *token* is None or empty, yields an empty dict (unauthenticated mode).
    """
    if not token:
        yield {}
        return

    fd, script_path = tempfile.mkstemp(prefix="agenticore-askpass-", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write('#!/bin/sh\necho "$_AGENTICORE_GIT_CREDENTIAL"\n')
        os.chmod(script_path, stat.S_IRWXU)  # 0o700

        env = {
            "GIT_ASKPASS": script_path,
            "_AGENTICORE_GIT_CREDENTIAL": token,
            "GIT_TERMINAL_PROMPT": "0",
            # Set credential.username via env so git uses x-access-token as
            # the username without needing a .gitconfig entry.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.username",
            "GIT_CONFIG_VALUE_0": "x-access-token",
        }
        yield env
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def strip_credentials_from_url(url: str) -> str:
    """Remove embedded ``x-access-token:...@`` from a URL for safe logging."""
    return _TOKEN_IN_URL_RE.sub(r"\1", url)


def sanitize_remote_url(repo_path: str) -> None:
    """Detect and fix a ``.git/config`` remote URL with an embedded token.

    One-time migration for repos cloned with the old token-in-URL approach.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return
        current_url = result.stdout.strip()
        clean_url = strip_credentials_from_url(current_url)
        if clean_url != current_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", clean_url],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            logger.info("Sanitized embedded token from remote URL in %s", repo_path)
    except Exception as exc:
        logger.debug("sanitize_remote_url failed for %s: %s", repo_path, exc)
