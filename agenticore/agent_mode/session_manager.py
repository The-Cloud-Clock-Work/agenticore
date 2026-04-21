"""Session error detection and retry composition.

Pure functions — no external dependencies beyond stdlib.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class RetryableError:
    """A detected error that can be retried."""

    category: str  # token_limit, rate_limit, network, session_conflict
    message: str
    suggestion: str


# Patterns for errors that can be retried with a new prompt
_RETRYABLE_PATTERNS = [
    (
        re.compile(r"(token limit|context.*(limit|window|exceeded|too long))", re.IGNORECASE),
        "token_limit",
        "Summarize the conversation so far and continue from where you left off.",
    ),
    (
        re.compile(r"(rate.?limit|429|too many requests|overloaded)", re.IGNORECASE),
        "rate_limit",
        "Rate limited — wait and retry.",
    ),
    (
        re.compile(r"(connection.?(refused|reset|timeout)|ECONNREFUSED|network error)", re.IGNORECASE),
        "network",
        "Network error — retry after backoff.",
    ),
]

# Session-specific errors (not retryable via prompt)
_SESSION_CONFLICT_RE = re.compile(r"session.*already in use", re.IGNORECASE)
_NO_CONVERSATION_RE = re.compile(r"no conversation found", re.IGNORECASE)


def detect_retryable_error(output: str, stderr: str = "") -> Optional[RetryableError]:
    """Check if the output or stderr contains a retryable error pattern."""
    combined = f"{output}\n{stderr}"
    for pattern, category, suggestion in _RETRYABLE_PATTERNS:
        if pattern.search(combined):
            match = pattern.search(combined)
            return RetryableError(category=category, message=match.group(0), suggestion=suggestion)
    return None


def compose_retry_prompt(original_message: str, error: RetryableError) -> str:
    """Build a retry instruction for --resume."""
    if error.category == "token_limit":
        return (
            "The previous attempt hit the token limit. "
            "Summarize what you've done so far, then continue with the original task:\n\n"
            f"{original_message}"
        )
    return original_message


def handle_session_error(stderr: str) -> Optional[str]:
    """Detect session-specific errors that need special handling.

    Returns:
        Error category string ("session_conflict" or "no_conversation"), or None.
    """
    if _SESSION_CONFLICT_RE.search(stderr):
        return "session_conflict"
    if _NO_CONVERSATION_RE.search(stderr):
        return "no_conversation"
    return None
