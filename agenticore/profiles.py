"""Profile loader and CLI flag builder.

Profiles map to Claude Code CLI flags. Each profile is a YAML file that
specifies model, max_turns, permissions, and optional system prompt additions.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass
class ProfileClaude:
    model: str = "sonnet"
    max_turns: int = 80
    output_format: str = "json"
    permission_mode: str = "dangerously-skip-permissions"
    timeout: int = 3600
    worktree: bool = True


@dataclass
class ProfileSettings:
    permissions: dict = field(default_factory=dict)


@dataclass
class Profile:
    name: str = "code"
    description: str = ""
    claude: ProfileClaude = field(default_factory=ProfileClaude)
    append_prompt: str = ""
    settings: Optional[ProfileSettings] = None
    claude_md: Optional[str] = None
    auto_pr: bool = True


def _defaults_dir() -> Path:
    """Bundled default profiles directory."""
    return Path(__file__).parent.parent / "defaults" / "profiles"


def _user_profiles_dir() -> Path:
    """User profile directory: ~/.agenticore/profiles/"""
    return Path.home() / ".agenticore" / "profiles"


def _load_profile_yaml(path: Path) -> Profile:
    """Load a single profile from a YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    claude_raw = raw.get("claude", {})
    claude = ProfileClaude(
        model=claude_raw.get("model", "sonnet"),
        max_turns=claude_raw.get("max_turns", 80),
        output_format=claude_raw.get("output_format", "json"),
        permission_mode=claude_raw.get("permission_mode", "dangerously-skip-permissions"),
        timeout=claude_raw.get("timeout", 3600),
        worktree=claude_raw.get("worktree", True),
    )

    settings = None
    settings_raw = raw.get("settings")
    if settings_raw:
        settings = ProfileSettings(permissions=settings_raw.get("permissions", {}))

    return Profile(
        name=raw.get("name", path.stem),
        description=raw.get("description", ""),
        claude=claude,
        append_prompt=raw.get("append_prompt", ""),
        settings=settings,
        claude_md=raw.get("claude_md"),
        auto_pr=raw.get("auto_pr", True),
    )


def load_profiles() -> Dict[str, Profile]:
    """Load all profiles from defaults/ and ~/.agenticore/profiles/.

    User profiles override defaults with the same name.
    """
    profiles: Dict[str, Profile] = {}

    # Load bundled defaults
    defaults = _defaults_dir()
    if defaults.exists():
        for path in sorted(defaults.glob("*.yml")):
            p = _load_profile_yaml(path)
            profiles[p.name] = p

    # Load user profiles (override defaults)
    user_dir = _user_profiles_dir()
    if user_dir.exists():
        for path in sorted(user_dir.glob("*.yml")):
            p = _load_profile_yaml(path)
            profiles[p.name] = p

    return profiles


def get_profile(name: str) -> Optional[Profile]:
    """Load a single profile by name."""
    profiles = load_profiles()
    return profiles.get(name)


def render_template(template: str, variables: Dict[str, str]) -> str:
    """Render template variables like {{TASK}}, {{REPO_URL}}, etc."""
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value)
    return result


def build_cli_args(profile: Profile, task: str, variables: Optional[Dict[str, str]] = None) -> List[str]:
    """Build Claude CLI argument list from a profile.

    Args:
        profile: The profile to use
        task: The task description
        variables: Template variables for prompt rendering

    Returns:
        List of CLI arguments (without the 'claude' binary)
    """
    args: List[str] = []

    # Worktree
    if profile.claude.worktree:
        args.append("--worktree")

    # Model
    args.extend(["--model", profile.claude.model])

    # Max turns
    args.extend(["--max-turns", str(profile.claude.max_turns)])

    # Output format
    args.extend(["--output-format", profile.claude.output_format])

    # Permission mode
    if profile.claude.permission_mode == "dangerously-skip-permissions":
        args.append("--dangerously-skip-permissions")

    # Append system prompt (with template rendering)
    if profile.append_prompt:
        vars_ = variables or {}
        rendered = render_template(profile.append_prompt, vars_)
        args.extend(["--append-system-prompt", rendered])

    # Settings (permissions etc.)
    if profile.settings and profile.settings.permissions:
        settings_json = json.dumps({"permissions": profile.settings.permissions})
        args.extend(["--settings", settings_json])

    # Task prompt
    args.extend(["-p", task])

    return args


def profile_to_dict(profile: Profile) -> dict:
    """Serialize a profile to a dict for API responses."""
    return {
        "name": profile.name,
        "description": profile.description,
        "model": profile.claude.model,
        "max_turns": profile.claude.max_turns,
        "worktree": profile.claude.worktree,
        "auto_pr": profile.auto_pr,
        "permission_mode": profile.claude.permission_mode,
    }
