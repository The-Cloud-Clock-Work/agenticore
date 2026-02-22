"""Request router — code fast-path with AI fallback stub.

The code router handles the common case: explicit profile or default.
The AI router (future) handles ambiguous requests by spawning a
lightweight Claude session to decide which profile to use.
"""

from agenticore.config import get_config
from agenticore.profiles import get_profile


def route(profile: str = "", repo_url: str = "") -> str:
    """Determine which profile to use for a request.

    Code fast-path logic:
    1. Explicit profile → use it
    2. Has repo_url → use default profile
    3. Fallback → "code"

    Args:
        profile: Explicitly requested profile name
        repo_url: Repository URL (if any)

    Returns:
        Profile name to use
    """
    if profile:
        # Validate it exists
        p = get_profile(profile)
        if p is not None:
            return profile
        # Fall through to default if profile not found

    cfg = get_config()
    return cfg.claude.default_profile


async def ai_route(task: str, repo_url: str = "") -> str:
    """AI router stub — analyzes the request and picks a profile.

    Future implementation: spawn a lightweight Claude session to examine
    the repo and decide which profile to use.

    For now, falls back to code router logic.
    """
    return route(repo_url=repo_url)
