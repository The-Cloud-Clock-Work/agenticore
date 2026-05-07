"""Configuration loader for Agenticore.

Loads from ``~/.agenticore/config.yml`` with environment variable overrides.
"""

import base64
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

_log = logging.getLogger(__name__)


def _env(key: str, default: str = "") -> str:
    """Get env var, treating empty string as 'not set' (returns default)."""
    val = os.getenv(key, "")
    return val if val else default


def _env_bool(key: str, default: str = "false") -> bool:
    val = _env(key, "")
    return (val or default).lower() in ("true", "1", "yes")


def _env_int(key: str, default: str = "0") -> int:
    val = _env(key, "")
    return int(val) if val else int(default)


@dataclass
class ReposConfig:
    root: str = ""
    max_parallel_jobs: int = 3
    job_ttl_seconds: int = 86400
    shared_fs_root: str = ""  # AGENTICORE_SHARED_FS_ROOT — e.g. /shared
    jobs_dir: str = ""  # AGENTICORE_JOBS_DIR — override ~/.agenticore/jobs/
    pod_name: str = ""  # AGENTICORE_POD_NAME — set from K8s Downward API
    worktree_root: str = ""  # AGENTICORE_WORKTREE_ROOT


@dataclass
class ClaudeConfig:
    """Claude CLI integration config.

    Binary path, timeout, and per-flag behavior (model, permission, format,
    effort) are owned by the active agentihooks profile (ProfileClaude).
    """

    default_mcp_file: str = ""  # AGENTICORE_DEFAULT_MCP_FILE — injected into every job
    claude_home_dir: str = ""  # CLAUDE_CODE_HOME_DIR — home dir root (e.g. /shared)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8200
    transport: str = "sse"
    api_keys: List[str] = field(default_factory=list)


@dataclass
class RedisConfig:
    url: str = ""
    key_prefix: str = "agenticore"


@dataclass
class OtelConfig:
    enabled: bool = True
    endpoint: str = "http://otel-collector:4317"
    protocol: str = "grpc"
    log_prompts: bool = False
    log_tool_details: bool = True


@dataclass
class GithubConfig:
    token: str = ""
    app_id: str = ""
    app_private_key: str = ""  # resolved PEM text
    app_installation_id: str = ""


@dataclass
class AgentModeConfig:
    """Runtime ops config for AGENT_MODE pods.

    Claude CLI behavior (model, max_turns, permission_mode, output_format,
    effort) is owned by the active agentihooks profile — see ``ProfileClaude``
    in ``profiles.py``. Do not duplicate those fields here.
    """

    enabled: bool = False
    package_dir: str = ""
    evaluation_dir: str = ""
    repo_url: str = ""
    repo_branch: str = "main"
    agent: str = ""  # AGENTIHUB_AGENT — which agent to load from agentihub
    timeout: int = 3600
    max_retry_attempts: int = 3
    session_ttl: int = 86400
    conv_hash_fallback: bool = True
    append_system_prompt: bool = True
    completion_queue_enabled: bool = True
    max_queue_workers: int = 1


@dataclass
class AgentiBridgeConfig:
    url: str = ""  # AGENTIBRIDGE_URL — base URL; empty = feature disabled
    api_key: str = ""  # AGENTIBRIDGE_API_KEY — auth token
    heartbeat_interval: int = 60  # AGENTIBRIDGE_HEARTBEAT_INTERVAL — seconds
    registration_enabled: bool = True  # AGENTIBRIDGE_REGISTRATION_ENABLED
    agent_id: str = ""  # AGENTIBRIDGE_AGENT_ID — override derived agent ID


@dataclass
class LangfuseConfig:
    host: str = "https://cloud.langfuse.com"
    public_key: str = ""
    secret_key: str = ""


@dataclass
class RuntimePaths:
    """Resolved on-disk paths for cloned content repos.

    Populated by sync functions at boot (or on-demand sync) and read by
    profile loader, agent-mode initializer, and on-demand reload paths.
    Replaces the old AGENTICORE_*_PATH env-var round-trip.
    """

    agentihooks_dir: Optional[Path] = None
    bundle_dir: Optional[Path] = None
    hub_dir: Optional[Path] = None


@dataclass
class Config:
    repos: ReposConfig = field(default_factory=ReposConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    otel: OtelConfig = field(default_factory=OtelConfig)
    github: GithubConfig = field(default_factory=GithubConfig)
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)
    agent_mode: AgentModeConfig = field(default_factory=AgentModeConfig)
    agentibridge: AgentiBridgeConfig = field(default_factory=AgentiBridgeConfig)
    agentihooks_profile: str = ""  # AGENTIHOOKS_PROFILE — active profile; empty = let agentihooks pick its default
    agentihooks_url: str = ""
    agentihooks_bundle_url: str = ""
    agentihub_url: str = ""
    agentihooks_branch: str = ""  # AGENTICORE_AGENTIHOOKS_BRANCH — empty = remote HEAD
    agentihub_branch: str = ""  # AGENTICORE_AGENTIHUB_BRANCH — empty = remote HEAD
    agentihooks_bundle_branch: str = ""  # AGENTICORE_AGENTIHOOKS_BUNDLE_BRANCH — empty = remote HEAD
    runtime: RuntimePaths = field(default_factory=RuntimePaths)


def _default_repos_root() -> str:
    return str(Path.home() / "agenticore-repos")


def _config_path() -> Path:
    return Path.home() / ".agenticore" / "config.yml"


def _load_yaml(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _resolve_private_key(github_raw: dict) -> str:
    """Resolve GitHub App private key PEM from file path, raw PEM, base64, or YAML.

    Priority:
      1. GITHUB_APP_PRIVATE_KEY_PATH env → read file
      2. GITHUB_APP_PRIVATE_KEY env → raw PEM text (K8s --from-file secrets)
      3. GITHUB_APP_PRIVATE_KEY_BASE64 env → base64 decode
      4. github.app.private_key_path in YAML → read file
    """
    # 1. File path from env
    key_path = _env("GITHUB_APP_PRIVATE_KEY_PATH", "")
    if key_path:
        p = Path(key_path).expanduser()
        if p.exists():
            return p.read_text().strip()
        _log.warning("GITHUB_APP_PRIVATE_KEY_PATH does not exist: %s", p)
        return ""

    # 2. Raw PEM from env (K8s secretKeyRef with --from-file injects raw PEM text)
    raw_pem = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
    if raw_pem and raw_pem.startswith("-----"):
        return raw_pem.strip()

    # 3. Base64-encoded PEM from env
    b64 = _env("GITHUB_APP_PRIVATE_KEY_BASE64", "")
    if b64:
        try:
            return base64.b64decode(b64).decode().strip()
        except Exception as exc:
            _log.warning("Failed to decode GITHUB_APP_PRIVATE_KEY_BASE64: %s", exc)
            return ""

    # 4. File path from YAML
    yaml_path = github_raw.get("app", {}).get("private_key_path", "")
    if yaml_path:
        p = Path(yaml_path).expanduser()
        if p.exists():
            return p.read_text().strip()
        _log.warning("github.app.private_key_path does not exist: %s", p)

    return ""


def load_config(config_path: Optional[str] = None) -> Config:
    """Load configuration from YAML file with env var overrides.

    Priority: env vars > YAML file > defaults.
    """
    path = Path(config_path) if config_path else _config_path()
    raw = _load_yaml(path)

    repos_raw = raw.get("repos", {})
    claude_raw = raw.get("claude", {})
    server_raw = raw.get("server", {})
    redis_raw = raw.get("redis", {})
    otel_raw = raw.get("otel", {})
    github_raw = raw.get("github", {})

    # Repos — env overrides
    repos_root = _env("AGENTICORE_REPOS_ROOT", repos_raw.get("root", ""))
    if not repos_root:
        repos_root = _default_repos_root()
    repos_root = str(Path(repos_root).expanduser())

    repos = ReposConfig(
        root=repos_root,
        max_parallel_jobs=_env_int("AGENTICORE_MAX_PARALLEL_JOBS", str(repos_raw.get("max_parallel_jobs", 3))),
        job_ttl_seconds=_env_int("AGENTICORE_JOB_TTL", str(repos_raw.get("job_ttl_seconds", 86400))),
        shared_fs_root=_env("AGENTICORE_SHARED_FS_ROOT", repos_raw.get("shared_fs_root", "")),
        jobs_dir=_env("AGENTICORE_JOBS_DIR", repos_raw.get("jobs_dir", "")),
        pod_name=_env("AGENTICORE_POD_NAME", repos_raw.get("pod_name", "")),
        worktree_root=_env("AGENTICORE_WORKTREE_ROOT", repos_raw.get("worktree_root", "")),
    )

    # Claude — env overrides.
    # binary, config_dir, and timeout are owned by the active agentihooks
    # profile (ProfileClaude) — not configured here. Only DEFAULT_MCP_FILE
    # and CLAUDE_CODE_HOME_DIR are environment-driven (cluster-wide MCP
    # injection + per-pod ~/.claude/ root).
    claude_home_dir = _env("CLAUDE_CODE_HOME_DIR", claude_raw.get("claude_home_dir", ""))
    if not claude_home_dir:
        claude_home_dir = str(Path.home())
    claude = ClaudeConfig(
        default_mcp_file=_env("AGENTICORE_DEFAULT_MCP_FILE", claude_raw.get("default_mcp_file", "")),
        claude_home_dir=claude_home_dir,
    )

    # Server — env overrides
    api_keys_raw = _env("AGENTICORE_API_KEYS", "")
    if api_keys_raw:
        api_keys = [k.strip() for k in api_keys_raw.split(",") if k.strip()]
    else:
        api_keys = server_raw.get("api_keys", []) or []

    server = ServerConfig(
        host=_env("AGENTICORE_HOST", server_raw.get("host", "127.0.0.1")),
        port=_env_int("AGENTICORE_PORT", str(server_raw.get("port", 8200))),
        transport=_env("AGENTICORE_TRANSPORT", server_raw.get("transport", "sse")),
        api_keys=api_keys,
    )

    # Redis — env overrides
    redis = RedisConfig(
        url=_env("REDIS_URL", redis_raw.get("url", "")),
        key_prefix=_env("REDIS_KEY_PREFIX", redis_raw.get("key_prefix", "agenticore")),
    )

    # OTEL — env overrides
    otel = OtelConfig(
        enabled=_env_bool("AGENTICORE_OTEL_ENABLED", str(otel_raw.get("enabled", True)).lower()),
        endpoint=_env("OTEL_EXPORTER_OTLP_ENDPOINT", otel_raw.get("endpoint", "http://otel-collector:4317")),
        protocol=_env("OTEL_EXPORTER_OTLP_PROTOCOL", otel_raw.get("protocol", "grpc")),
        log_prompts=_env_bool("AGENTICORE_OTEL_LOG_PROMPTS", str(otel_raw.get("log_prompts", False)).lower()),
        log_tool_details=_env_bool(
            "AGENTICORE_OTEL_LOG_TOOL_DETAILS", str(otel_raw.get("log_tool_details", True)).lower()
        ),
    )

    # GitHub — env overrides
    github = GithubConfig(
        token=_env("GITHUB_TOKEN", github_raw.get("token", "")),
        app_id=_env("GITHUB_APP_ID", github_raw.get("app_id", "")),
        app_private_key=_resolve_private_key(github_raw),
        app_installation_id=_env("GITHUB_APP_INSTALLATION_ID", github_raw.get("app_installation_id", "")),
    )

    # Langfuse — env overrides
    langfuse_raw = raw.get("langfuse", {})
    langfuse = LangfuseConfig(
        host=_env("LANGFUSE_HOST", langfuse_raw.get("host", "https://cloud.langfuse.com")),
        public_key=_env("LANGFUSE_PUBLIC_KEY", langfuse_raw.get("public_key", "")),
        secret_key=_env("LANGFUSE_SECRET_KEY", langfuse_raw.get("secret_key", "")),
    )

    # Agent Mode — env overrides
    agent_mode = AgentModeConfig(
        enabled=_env_bool("AGENT_MODE", "false"),
        package_dir=_env("AGENT_MODE_PACKAGE_DIR", ""),
        evaluation_dir=_env("AGENT_MODE_EVALUATION_DIR", ""),
        repo_url=_env("PACKAGE_REPO_URL", ""),
        repo_branch=_env("PACKAGE_REPO_BRANCH", "main"),
        agent=_env("AGENTIHUB_AGENT", ""),
        timeout=_env_int("AGENT_MODE_TIMEOUT", "3600"),
        max_retry_attempts=_env_int("AGENT_MODE_MAX_RETRIES", "3"),
        session_ttl=_env_int("AGENT_MODE_SESSION_TTL", "86400"),
        conv_hash_fallback=_env_bool("AGENTICORE_CONV_HASH_FALLBACK", "true"),
        append_system_prompt=_env_bool("AGENT_MODE_APPEND_SYSTEM_PROMPT", "true"),
        completion_queue_enabled=_env_bool("AGENT_MODE_QUEUE_ENABLED", "true"),
        max_queue_workers=_env_int("AGENT_MODE_MAX_QUEUE_WORKERS", "1"),
    )

    # AgentiBridge — env overrides
    agentibridge_raw = raw.get("agentibridge", {})
    agentibridge = AgentiBridgeConfig(
        url=_env("AGENTIBRIDGE_URL", agentibridge_raw.get("url", "")),
        api_key=_env("AGENTIBRIDGE_API_KEY", agentibridge_raw.get("api_key", "")),
        heartbeat_interval=_env_int(
            "AGENTIBRIDGE_HEARTBEAT_INTERVAL", str(agentibridge_raw.get("heartbeat_interval", 60))
        ),
        registration_enabled=_env_bool(
            "AGENTIBRIDGE_REGISTRATION_ENABLED",
            str(agentibridge_raw.get("registration_enabled", True)).lower(),
        ),
        agent_id=_env("AGENTIBRIDGE_AGENT_ID", agentibridge_raw.get("agent_id", "")),
    )

    agentihooks_profile = _env("AGENTIHOOKS_PROFILE", raw.get("agentihooks_profile", ""))
    agentihooks_url = _env("AGENTICORE_AGENTIHOOKS_URL", raw.get("agentihooks_url", ""))
    agentihooks_bundle_url = _env("AGENTICORE_AGENTIHOOKS_BUNDLE_URL", raw.get("agentihooks_bundle_url", ""))
    agentihub_url = _env("AGENTICORE_AGENTIHUB_URL", raw.get("agentihub_url", ""))
    agentihooks_branch = _env("AGENTICORE_AGENTIHOOKS_BRANCH", raw.get("agentihooks_branch", ""))
    agentihub_branch = _env("AGENTICORE_AGENTIHUB_BRANCH", raw.get("agentihub_branch", ""))
    agentihooks_bundle_branch = _env("AGENTICORE_AGENTIHOOKS_BUNDLE_BRANCH", raw.get("agentihooks_bundle_branch", ""))

    return Config(
        repos=repos,
        claude=claude,
        server=server,
        redis=redis,
        otel=otel,
        github=github,
        langfuse=langfuse,
        agent_mode=agent_mode,
        agentibridge=agentibridge,
        agentihooks_profile=agentihooks_profile,
        agentihooks_url=agentihooks_url,
        agentihooks_bundle_url=agentihooks_bundle_url,
        agentihub_url=agentihub_url,
        agentihooks_branch=agentihooks_branch,
        agentihub_branch=agentihub_branch,
        agentihooks_bundle_branch=agentihooks_bundle_branch,
    )


# Module-level singleton, loaded lazily
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config singleton."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset the config singleton (for testing)."""
    global _config
    _config = None
