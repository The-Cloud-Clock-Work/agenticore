"""Profile loader and CLI flag builder.

Profiles are directories containing native Claude Code config files
(.claude/, .mcp.json) plus a thin profile.yml for Agenticore metadata.

Layout::

    defaults/profiles/code/
    ├── profile.yml          # Agenticore-only metadata
    ├── .claude/
    │   ├── settings.json    # Native: hooks, permissions, env vars
    │   ├── CLAUDE.md        # Native: system instructions
    │   ├── agents/          # Native: custom subagents
    │   └── skills/          # Native: custom skills
    └── .mcp.json            # Native: MCP server config

Legacy .yml profiles (non-directory) are still loadable with a deprecation
warning and auto-converted to the new structure in memory.
"""

import json
import logging
import shutil
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ── Dataclasses ───────────────────────────────────────────────────────────


@dataclass
class ProfileClaude:
    """CLI flags that can't be expressed in .claude/ natively."""

    model: str = "sonnet"
    max_turns: int = 80
    permission_mode: str = "bypassPermissions"
    no_session_persistence: bool = True
    output_format: str = "json"
    worktree: bool = True
    effort: Optional[str] = None
    timeout: int = 3600
    max_budget_usd: Optional[float] = None
    fallback_model: Optional[str] = None


@dataclass
class Profile:
    """A profile IS a .claude package directory."""

    name: str = "code"
    description: str = ""
    claude: ProfileClaude = field(default_factory=ProfileClaude)
    auto_pr: bool = True
    extends: Optional[str] = None
    path: Optional[Path] = None  # directory path of this profile

    # Legacy fields — only used for backward compat with old YAML profiles
    append_prompt: str = ""
    _legacy: bool = field(default=False, repr=False)


# ── Paths ─────────────────────────────────────────────────────────────────


def _defaults_dir() -> Path:
    """Bundled default profiles directory."""
    return Path(__file__).parent.parent / "defaults" / "profiles"


def _user_profiles_dir() -> Path:
    """User profile directory: ~/.agenticore/profiles/"""
    return Path.home() / ".agenticore" / "profiles"


# ── Loading ───────────────────────────────────────────────────────────────


def _load_profile_dir(path: Path) -> Profile:
    """Load a profile from a directory containing profile.yml."""
    yml_path = path / "profile.yml"
    if not yml_path.exists():
        raise FileNotFoundError(f"No profile.yml in {path}")

    with open(yml_path) as f:
        raw = yaml.safe_load(f) or {}

    claude_raw = raw.get("claude", {})
    claude = ProfileClaude(
        model=claude_raw.get("model", "sonnet"),
        max_turns=claude_raw.get("max_turns", 80),
        permission_mode=claude_raw.get("permission_mode", "bypassPermissions"),
        no_session_persistence=claude_raw.get("no_session_persistence", True),
        output_format=claude_raw.get("output_format", "json"),
        worktree=claude_raw.get("worktree", True),
        effort=claude_raw.get("effort"),
        timeout=claude_raw.get("timeout", 3600),
        max_budget_usd=claude_raw.get("max_budget_usd"),
        fallback_model=claude_raw.get("fallback_model"),
    )

    return Profile(
        name=raw.get("name", path.name),
        description=raw.get("description", ""),
        claude=claude,
        auto_pr=raw.get("auto_pr", True),
        extends=raw.get("extends"),
        path=path,
        _legacy=False,
    )


def _load_legacy_yaml(path: Path) -> Profile:
    """Load an old-format .yml profile with deprecation warning."""
    warnings.warn(
        f"Profile '{path.stem}' uses legacy YAML format. "
        f"Migrate to directory-based profile: defaults/profiles/{path.stem}/profile.yml",
        DeprecationWarning,
        stacklevel=3,
    )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    claude_raw = raw.get("claude", {})

    # Map old permission_mode values
    perm_mode = claude_raw.get("permission_mode", "bypassPermissions")
    if perm_mode == "dangerously-skip-permissions":
        perm_mode = "bypassPermissions"

    claude = ProfileClaude(
        model=claude_raw.get("model", "sonnet"),
        max_turns=claude_raw.get("max_turns", 80),
        permission_mode=perm_mode,
        no_session_persistence=claude_raw.get("no_session_persistence", True),
        output_format=claude_raw.get("output_format", "json"),
        worktree=claude_raw.get("worktree", True),
        effort=claude_raw.get("effort"),
        timeout=claude_raw.get("timeout", 3600),
    )

    return Profile(
        name=raw.get("name", path.stem),
        description=raw.get("description", ""),
        claude=claude,
        append_prompt=raw.get("append_prompt", ""),
        auto_pr=raw.get("auto_pr", True),
        path=None,
        _legacy=True,
    )


def _resolve_extends(profile: Profile, all_profiles: Dict[str, Profile]) -> Profile:
    """Resolve profile inheritance (extends field)."""
    if not profile.extends:
        return profile

    parent_name = profile.extends
    parent = all_profiles.get(parent_name)
    if parent is None:
        logger.warning("Profile '%s' extends unknown profile '%s'", profile.name, parent_name)
        return profile

    # Recursively resolve parent first
    parent = _resolve_extends(parent, all_profiles)

    # Merge: child overrides parent for ProfileClaude fields
    merged_claude_fields: Dict[str, Any] = {}
    for f in ProfileClaude.__dataclass_fields__:
        parent_val = getattr(parent.claude, f)
        child_val = getattr(profile.claude, f)
        # Use child value if it differs from default
        default_val = ProfileClaude.__dataclass_fields__[f].default
        if child_val != default_val:
            merged_claude_fields[f] = child_val
        else:
            merged_claude_fields[f] = parent_val

    merged_claude = ProfileClaude(**merged_claude_fields)

    return Profile(
        name=profile.name,
        description=profile.description or parent.description,
        claude=merged_claude,
        auto_pr=profile.auto_pr,
        extends=profile.extends,
        path=profile.path,
        append_prompt=profile.append_prompt or parent.append_prompt,
        _legacy=profile._legacy,
    )


def load_profiles() -> Dict[str, Profile]:
    """Load all profiles from defaults/ and ~/.agenticore/profiles/.

    Supports both new directory-based profiles and legacy .yml files.
    User profiles override defaults with the same name.
    """
    profiles: Dict[str, Profile] = {}

    for base_dir in (_defaults_dir(), _user_profiles_dir()):
        if not base_dir.exists():
            continue

        # New format: directories with profile.yml
        for child in sorted(base_dir.iterdir()):
            if child.is_dir() and (child / "profile.yml").exists():
                try:
                    p = _load_profile_dir(child)
                    profiles[p.name] = p
                except Exception as e:
                    logger.warning("Failed to load profile dir %s: %s", child, e)

        # Legacy format: standalone .yml files
        for path in sorted(base_dir.glob("*.yml")):
            # Skip if a directory profile with same name already loaded
            if path.stem in profiles:
                continue
            try:
                p = _load_legacy_yaml(path)
                profiles[p.name] = p
            except Exception as e:
                logger.warning("Failed to load legacy profile %s: %s", path, e)

    # Resolve inheritance
    resolved: Dict[str, Profile] = {}
    for name, profile in profiles.items():
        resolved[name] = _resolve_extends(profile, profiles)

    return resolved


def get_profile(name: str) -> Optional[Profile]:
    """Load a single profile by name."""
    profiles = load_profiles()
    return profiles.get(name)


# ── Materialization ───────────────────────────────────────────────────────


def materialize_profile(
    profile: Profile,
    working_dir: Path,
    all_profiles: Optional[Dict[str, Profile]] = None,
) -> List[Path]:
    """Copy profile's .claude/ and .mcp.json into the working directory.

    If the profile uses ``extends``, the parent's files are copied first,
    then the child's files overlay on top.

    Args:
        profile: The resolved profile
        working_dir: Target directory (repo clone)
        all_profiles: All loaded profiles (for resolving extends chain).
                      If None, loads from defaults/user dirs.

    Returns:
        List of paths that were created/modified (for cleanup tracking)
    """
    if profile._legacy or profile.path is None:
        return []

    created: List[Path] = []
    if profile.extends:
        profiles = all_profiles if all_profiles is not None else load_profiles()
    else:
        profiles = {}

    # Build overlay chain: [base, ..., child]
    chain = _build_extends_chain(profile, profiles)

    for prof in chain:
        if prof.path is None:
            continue

        # Copy .claude/ directory
        src_claude = prof.path / ".claude"
        if src_claude.exists():
            dst_claude = working_dir / ".claude"
            shutil.copytree(src_claude, dst_claude, dirs_exist_ok=True)
            created.append(dst_claude)

        # Copy .mcp.json (deep merge for inheritance)
        src_mcp = prof.path / ".mcp.json"
        if src_mcp.exists():
            dst_mcp = working_dir / ".mcp.json"
            if dst_mcp.exists():
                # Deep merge: child servers added to parent servers
                with open(dst_mcp) as f:
                    existing = json.load(f)
                with open(src_mcp) as f:
                    incoming = json.load(f)
                existing.setdefault("mcpServers", {}).update(incoming.get("mcpServers", {}))
                with open(dst_mcp, "w") as f:
                    json.dump(existing, f, indent=2)
            else:
                shutil.copy2(src_mcp, dst_mcp)
            created.append(dst_mcp)

    return created


def _build_extends_chain(profile: Profile, all_profiles: Dict[str, Profile]) -> List[Profile]:
    """Build the overlay chain from base → child."""
    chain = [profile]
    current = profile
    seen = {profile.name}

    while current.extends and current.extends not in seen:
        parent = all_profiles.get(current.extends)
        if parent is None:
            break
        seen.add(current.extends)
        chain.append(parent)
        current = parent

    chain.reverse()  # base first, child last
    return chain


# ── CLI Args ──────────────────────────────────────────────────────────────


def build_cli_args(
    profile: Profile,
    task: str,
    variables: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Build Claude CLI argument list from a profile.

    For new-format profiles, only emits CLI flags from profile.yml.
    Native .claude/ config is handled by materialization.

    For legacy profiles, appends system prompt via --append-system-prompt.

    Args:
        profile: The profile to use
        task: The task description
        variables: Template variables for dynamic context

    Returns:
        List of CLI arguments (without the 'claude' binary)
    """
    args: List[str] = []
    c = profile.claude

    # Worktree
    if c.worktree:
        args.append("--worktree")

    # Model
    args.extend(["--model", c.model])

    # Max turns
    args.extend(["--max-turns", str(c.max_turns)])

    # Output format
    args.extend(["--output-format", c.output_format])

    # Permission mode
    if c.permission_mode == "dangerously-skip-permissions":
        args.append("--dangerously-skip-permissions")
    elif c.permission_mode:
        args.extend(["--permission-mode", c.permission_mode])

    # No session persistence
    if c.no_session_persistence:
        args.append("--no-session-persistence")

    # Effort
    if c.effort:
        args.extend(["--effort", c.effort])

    # Max budget
    if c.max_budget_usd is not None:
        args.extend(["--max-budget-usd", str(c.max_budget_usd)])

    # Fallback model
    if c.fallback_model:
        args.extend(["--fallback-model", c.fallback_model])

    # Dynamic context via --append-system-prompt
    vars_ = variables or {}
    if profile._legacy and profile.append_prompt:
        # Legacy: render full template
        rendered = render_template(profile.append_prompt, vars_)
        args.extend(["--append-system-prompt", rendered])
    elif vars_:
        # New format: inject job context as dynamic system prompt
        parts = []
        if vars_.get("JOB_ID"):
            parts.append(f"Job: {vars_['JOB_ID']}")
        if vars_.get("TASK"):
            parts.append(f"Task: {vars_['TASK']}")
        if vars_.get("REPO_URL"):
            parts.append(f"Repo: {vars_['REPO_URL']}")
        if vars_.get("BASE_REF"):
            parts.append(f"Branch: {vars_['BASE_REF']}")
        if parts:
            args.extend(["--append-system-prompt", " | ".join(parts)])

    # Task prompt
    args.extend(["-p", task])

    return args


def render_template(template: str, variables: Dict[str, str]) -> str:
    """Render template variables like {{TASK}}, {{REPO_URL}}, etc.

    Kept for backward compatibility with legacy profiles.
    """
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value)
    return result


# ── Serialization ─────────────────────────────────────────────────────────


def profile_to_dict(profile: Profile) -> dict:
    """Serialize a profile to a dict for API responses."""
    d = {
        "name": profile.name,
        "description": profile.description,
        "model": profile.claude.model,
        "max_turns": profile.claude.max_turns,
        "worktree": profile.claude.worktree,
        "auto_pr": profile.auto_pr,
        "permission_mode": profile.claude.permission_mode,
    }
    if profile.extends:
        d["extends"] = profile.extends
    if profile.claude.effort:
        d["effort"] = profile.claude.effort
    return d
