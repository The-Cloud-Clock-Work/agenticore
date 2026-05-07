"""Profile loader and CLI flag builder.

Profiles are directories containing native Claude Code config files
(.claude/, .mcp.json) plus a thin profile.yml for Agenticore metadata.

Layout::

    <profiles-dir>/code/
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
import os
import shutil
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_MCP_JSON = ".mcp.json"

# ── Claude CLI flag rendering ─────────────────────────────────────────────

# Defaults applied when a key is missing from the profile's `claude:` block.
# Sessions persist by default — `no_session_persistence` is intentionally
# omitted so `--no-session-persistence` is NOT emitted unless a profile
# explicitly opts out.
CLAUDE_FLAG_DEFAULTS: Dict[str, Any] = {
    "model": "sonnet",
    "max_turns": 50,
    "permission_mode": "bypassPermissions",
    "effort": "high",
    "output_format": "json",
}

# Keys that live in the `claude:` block but are NOT claude CLI flags.
# Skipped by the renderer; consumed elsewhere by the orchestrator.
_NON_CLI_KEYS = frozenset({"worktree", "timeout"})


def render_claude_flags(
    claude_block: Optional[Dict[str, Any]] = None,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    skip_keys: Optional[set] = None,
) -> List[str]:
    """Render the per-call claude CLI flags from a profile `claude:` block.

    Merges defaults < block < overrides. Each remaining key is converted
    to ``--<kebab-case>`` and emitted with its value. Bools become bare
    flags (true → emit, false → skip). Adding a new claude CLI flag is
    a no-code change: drop it into the profile's ``claude:`` block.
    """
    merged: Dict[str, Any] = {**CLAUDE_FLAG_DEFAULTS}
    if claude_block:
        merged.update(claude_block)
    if overrides:
        merged.update(overrides)
    if skip_keys:
        for k in skip_keys:
            merged.pop(k, None)

    args: List[str] = []
    for key, value in merged.items():
        if key in _NON_CLI_KEYS:
            continue
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            if value:
                args.append(f"--{key.replace('_', '-')}")
            continue
        # CLI special-case: bare flag instead of paired value.
        if key == "permission_mode" and value == "dangerously-skip-permissions":
            args.append("--dangerously-skip-permissions")
            continue
        # Skip --model when value is the magic "default" sentinel.
        if key == "model" and isinstance(value, str) and value.lower() == "default":
            continue
        args.extend([f"--{key.replace('_', '-')}", str(value)])
    return args


# ── Dataclasses ───────────────────────────────────────────────────────────


class ProfileClaude:
    """Wrapper around a profile's `claude:` block.

    ``flags`` holds the raw dict — any key present here renders as a
    matching ``--<kebab-case>`` claude CLI flag (see ``render_claude_flags``).
    Keys missing from the dict fall back to ``CLAUDE_FLAG_DEFAULTS``.

    ``worktree`` and ``timeout`` are NOT claude CLI flags — they are
    orchestrator concerns (worktree=True means agenticore prepares a git
    worktree before invoking claude in fleet mode; timeout is the
    subprocess wall-clock limit). They live alongside the flag dict.

    Constructor accepts either the new dict form ``ProfileClaude(flags={...})``
    or legacy typed kwargs ``ProfileClaude(model="...", max_turns=...)`` for
    backward compatibility. Legacy kwargs are folded into ``flags``.
    """

    _LEGACY_FLAG_KEYS = frozenset(
        {
            "model",
            "max_turns",
            "permission_mode",
            "output_format",
            "effort",
            "no_session_persistence",
            "max_budget_usd",
            "fallback_model",
        }
    )

    def __init__(
        self,
        flags: Optional[Dict[str, Any]] = None,
        *,
        worktree: bool = True,
        timeout: int = 3600,
        **kwargs: Any,
    ) -> None:
        merged_flags: Dict[str, Any] = dict(flags) if flags else {}
        for k, v in kwargs.items():
            if k in self._LEGACY_FLAG_KEYS:
                if v is None:
                    continue
                merged_flags[k] = v
            else:
                raise TypeError(f"ProfileClaude got unexpected keyword argument '{k}'")
        self.flags: Dict[str, Any] = merged_flags
        self.worktree: bool = worktree
        self.timeout: int = timeout

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProfileClaude):
            return NotImplemented
        return (
            self.flags == other.flags
            and self.worktree == other.worktree
            and self.timeout == other.timeout
        )

    def __repr__(self) -> str:
        return f"ProfileClaude(flags={self.flags!r}, worktree={self.worktree!r}, timeout={self.timeout!r})"

    # Backward-compat property accessors so existing callers reading
    # ``pc.model`` / ``pc.max_turns`` / ... keep working. New flags should
    # be added by extending the profile YAML, not the dataclass.
    @property
    def model(self) -> str:
        return self.flags.get("model", CLAUDE_FLAG_DEFAULTS["model"])

    @property
    def max_turns(self) -> int:
        return self.flags.get("max_turns", CLAUDE_FLAG_DEFAULTS["max_turns"])

    @property
    def permission_mode(self) -> str:
        return self.flags.get("permission_mode", CLAUDE_FLAG_DEFAULTS["permission_mode"])

    @property
    def output_format(self) -> str:
        return self.flags.get("output_format", CLAUDE_FLAG_DEFAULTS["output_format"])

    @property
    def effort(self) -> Optional[str]:
        return self.flags.get("effort", CLAUDE_FLAG_DEFAULTS["effort"])

    @property
    def no_session_persistence(self) -> bool:
        # Default: false (sessions persist). Profile must opt out explicitly.
        return bool(self.flags.get("no_session_persistence", False))

    @property
    def max_budget_usd(self) -> Optional[float]:
        return self.flags.get("max_budget_usd")

    @property
    def fallback_model(self) -> Optional[str]:
        return self.flags.get("fallback_model")


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


def _user_profiles_dir() -> Path:
    """User profile directory: ~/.agenticore/profiles/"""
    return Path.home() / ".agenticore" / "profiles"


def _agentihooks_profiles_dir() -> Optional[Path]:
    """External agentihooks profiles dir — populated by sync_agentihooks."""
    from agenticore.config import get_config

    p = get_config().runtime.agentihooks_dir
    return p / "profiles" if p else None


# ── Loading ───────────────────────────────────────────────────────────────


def _load_profile_dir(path: Path) -> Profile:
    """Load a profile from a directory containing profile.yml."""
    yml_path = path / "profile.yml"
    if not yml_path.exists():
        raise FileNotFoundError(f"No profile.yml in {path}")

    with open(yml_path) as f:
        raw = yaml.safe_load(f) or {}

    claude_raw = dict(raw.get("claude") or {})
    worktree = bool(claude_raw.pop("worktree", True))
    timeout = int(claude_raw.pop("timeout", 3600))
    claude = ProfileClaude(flags=claude_raw, worktree=worktree, timeout=timeout)

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
        f"Migrate to directory-based profile: ~/.agenticore/profiles/{path.stem}/profile.yml",
        DeprecationWarning,
        stacklevel=3,
    )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    claude_raw = dict(raw.get("claude") or {})
    # Legacy profiles used to express dangerously-skip-permissions as the
    # permission_mode value; rewrite it to the standard bypassPermissions.
    if claude_raw.get("permission_mode") == "dangerously-skip-permissions":
        claude_raw["permission_mode"] = "bypassPermissions"
    worktree = bool(claude_raw.pop("worktree", True))
    timeout = int(claude_raw.pop("timeout", 3600))
    claude = ProfileClaude(flags=claude_raw, worktree=worktree, timeout=timeout)

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

    # Merge: parent flags first, child flags last (child wins per-key).
    merged_flags: Dict[str, Any] = {**parent.claude.flags, **profile.claude.flags}
    # worktree / timeout: child value if it differs from the dataclass
    # default, else parent value.
    pc_defaults = ProfileClaude()
    merged_worktree = (
        profile.claude.worktree
        if profile.claude.worktree != pc_defaults.worktree
        else parent.claude.worktree
    )
    merged_timeout = (
        profile.claude.timeout
        if profile.claude.timeout != pc_defaults.timeout
        else parent.claude.timeout
    )
    merged_claude = ProfileClaude(
        flags=merged_flags, worktree=merged_worktree, timeout=merged_timeout
    )

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


def _get_search_dirs() -> List[Path]:
    """Build the list of profile search directories.

    Search order (later entries override earlier on name collision):
      1. agentihooks/profiles/ (base execution profiles)
      2. agentihooks-bundle/profiles/ (operator/agent-specific profiles)
      3. ~/.agenticore/profiles/ (user overrides)
    """
    from agenticore.hooks import resolve_repo_paths

    hooks, bundle, _ = resolve_repo_paths()
    search_dirs = []
    if hooks:
        search_dirs.append(hooks / "profiles")
    if bundle:
        search_dirs.append(bundle / "profiles")
    search_dirs.append(_user_profiles_dir())
    return search_dirs


def _load_dir_profiles(base_dir: Path, profiles: Dict[str, Profile]) -> None:
    """Load directory-based profiles from a base directory."""
    for child in sorted(base_dir.iterdir()):
        if child.is_dir() and (child / "profile.yml").exists():
            try:
                p = _load_profile_dir(child)
                profiles[p.name] = p
            except Exception as e:
                logger.warning("Failed to load profile dir %s: %s", child, e)


def _load_legacy_profiles(base_dir: Path, profiles: Dict[str, Profile]) -> None:
    """Load legacy .yml profiles from a base directory."""
    for path in sorted(base_dir.glob("*.yml")):
        if path.stem in profiles:
            continue
        try:
            p = _load_legacy_yaml(path)
            profiles[p.name] = p
        except Exception as e:
            logger.warning("Failed to load legacy profile %s: %s", path, e)


def load_profiles() -> Dict[str, Profile]:
    """Load all profiles from the agentihooks clone (when present) and
    ~/.agenticore/profiles/.

    Supports both new directory-based profiles and legacy .yml files.
    Later search dirs override earlier ones with the same name.
    """
    profiles: Dict[str, Profile] = {}

    for base_dir in _get_search_dirs():
        if not base_dir.exists():
            continue
        _load_dir_profiles(base_dir, profiles)
        _load_legacy_profiles(base_dir, profiles)

    # Resolve inheritance
    return {name: _resolve_extends(profile, profiles) for name, profile in profiles.items()}


def get_profile(name: str) -> Optional[Profile]:
    """Load a single profile by name."""
    profiles = load_profiles()
    return profiles.get(name)


# ── Materialization ───────────────────────────────────────────────────────


def _copy_claude_dir(src_path: Path, working_dir: Path, created: List[Path]) -> None:
    """Copy .claude/ directory from profile to working directory."""
    src_claude = src_path / ".claude"
    if src_claude.exists():
        dst_claude = working_dir / ".claude"
        shutil.copytree(src_claude, dst_claude, dirs_exist_ok=True)
        created.append(dst_claude)


def _copy_mcp_json(src_path: Path, working_dir: Path, created: List[Path]) -> None:
    """Copy and merge .mcp.json from profile to working directory."""
    src_mcp = src_path / _MCP_JSON
    if not src_mcp.exists():
        return

    dst_mcp = working_dir / _MCP_JSON
    if dst_mcp.exists():
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


def _copy_profile_chain_to(chain: List[Profile], target_dir: Path) -> List[Path]:
    """Copy all profile files from the extends chain into target_dir."""
    created: List[Path] = []
    for prof in chain:
        if prof.path is None:
            continue
        _copy_claude_dir(prof.path, target_dir, created)
        _copy_mcp_json(prof.path, target_dir, created)
    return created


def _job_config_dir(job_id: str) -> Path:
    shared_fs_root = os.getenv("AGENTICORE_SHARED_FS_ROOT", "")
    if shared_fs_root:
        return Path(shared_fs_root) / "jobs" / (job_id or "default")
    return Path(tempfile.gettempdir()) / "agenticore-jobs" / (job_id or "default")


def materialize_profile(
    profile: Profile,
    job_id: str = "",
    all_profiles: Optional[Dict[str, Profile]] = None,
) -> Optional[Path]:
    """Return the profile directory path for tracking and MCP merging.

    Claude Code uses ``~/.claude/`` by default — ``CLAUDE_CONFIG_DIR`` is NOT
    set.  Agentihooks installs profiles into ``~/.claude/`` at container
    startup via ``agentihooks global``.

    Simple profiles (no ``extends``) are returned as-is — no I/O at all.
    Profiles with an ``extends`` chain are merged into a per-job temp
    directory for ``*.mcp.json`` content; the runner injects this into the
    job CWD, not into ``CLAUDE_CONFIG_DIR``.

    Args:
        profile: The resolved profile.
        job_id: Job UUID — used to create an isolated per-job directory for
                merged profiles.
        all_profiles: All loaded profiles (for resolving extends chain).
                      If None, loads from defaults/user dirs.

    Returns:
        Path to the profile directory (for audit/logging), or None.
    """
    if profile._legacy or profile.path is None:
        return None

    # Fast-path: simple profile with no extends — zero I/O
    if not profile.extends:
        return profile.path

    # Need a writable per-job dir for extends chain
    profiles = all_profiles if all_profiles is not None else load_profiles()
    chain = _build_extends_chain(profile, profiles)
    target_dir = _job_config_dir(job_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    _copy_profile_chain_to(chain, target_dir)

    return target_dir


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


def _build_core_cli_args(c: ProfileClaude) -> List[str]:
    """Build core CLI flags from a ProfileClaude. Thin wrapper around
    :func:`render_claude_flags` so callers don't need the ``flags`` dict."""
    return render_claude_flags(c.flags)


def _build_dynamic_prompt(vars_: Dict[str, str]) -> Optional[str]:
    """Build dynamic system prompt from job variables."""
    _CONTEXT_KEYS = [("JOB_ID", "Job"), ("TASK", "Task"), ("REPO_URL", "Repo"), ("BASE_REF", "Branch")]
    parts = [f"{label}: {vars_[key]}" for key, label in _CONTEXT_KEYS if vars_.get(key)]
    return " | ".join(parts) if parts else None


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
    args = _build_core_cli_args(profile.claude)

    vars_ = variables or {}
    if profile._legacy and profile.append_prompt:
        rendered = render_template(profile.append_prompt, vars_)
        args.extend(["--append-system-prompt", rendered])
    elif vars_:
        prompt = _build_dynamic_prompt(vars_)
        if prompt:
            args.extend(["--append-system-prompt", prompt])

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
