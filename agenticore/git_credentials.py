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
            # Override credential config via env to:
            # 1. Set username to x-access-token (GitHub App/PAT convention)
            # 2. Disable any credential.helper (e.g. gh auth git-credential)
            #    so GIT_ASKPASS is actually used
            # Block url.*.insteadOf from .git/config — Claude Code writes
            # stale tokens there which override GIT_ASKPASS.
            # GIT_CONFIG_COUNT env vars override local config at runtime.
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "credential.username",
            "GIT_CONFIG_VALUE_0": "x-access-token",
            "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "",
            "GIT_CONFIG_KEY_3": "protocol.version",
            "GIT_CONFIG_VALUE_3": "2",
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
    """Strip all embedded credentials from a repo's git config.

    Cleans two attack surfaces:
    1. ``remote.origin.url`` with embedded ``x-access-token:TOKEN@`` (old clone style)
    2. ``url.*.insteadOf`` entries with embedded tokens (written by Claude Code's
       internal git operations — these override GIT_ASKPASS and cause stale token
       failures on cached repos)

    Called on every clone/fetch cycle to ensure repos stay clean.
    """
    try:
        # 1. Clean remote URL
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
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

        # 2. Remove url.*.insteadOf entries containing access tokens.
        # Claude Code writes these during git push/branch operations:
        #   [url "https://x-access-token:ghs_xxx@github.com/"]
        #       insteadOf = https://github.com/
        # These override GIT_ASKPASS and cause auth failures when the token expires.
        result = subprocess.run(
            ["git", "config", "--local", "--get-regexp", r"url\..*\.insteadof"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                # Each line: "url.https://x-access-token:TOKEN@github.com/.insteadof https://github.com/"
                key = line.split()[0] if line.split() else ""
                if "x-access-token" in key or "x-access-token" in line:
                    # Extract the section name: url."https://x-access-token:...@github.com/"
                    # key format: url.SECTION_URL.insteadof
                    section_url = key.replace(".insteadof", "").replace("url.", "", 1)
                    subprocess.run(
                        ["git", "config", "--local", "--remove-section", f"url.{section_url}"],
                        cwd=repo_path,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    logger.info("Removed stale url.insteadOf credential from %s", repo_path)
    except Exception as exc:
        logger.debug("sanitize_remote_url failed for %s: %s", repo_path, exc)
