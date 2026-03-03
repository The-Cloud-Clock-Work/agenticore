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


@dataclass
class ClaudeConfig:
    binary: str = "claude"
    timeout: int = 3600
    default_profile: str = "coding"
    config_dir: str = ""
    default_mcp_file: str = ""  # AGENTICORE_DEFAULT_MCP_FILE — injected into every job


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
class AuthBrokerConfig:
    url: str = ""
    api_key: str = ""


@dataclass
class AgentModeConfig:
    enabled: bool = False
    package_dir: str = "/app/package"
    evaluation_dir: str = "/app/evaluation"
    repo_url: str = ""
    repo_branch: str = "main"
    model: str = "sonnet"
    max_turns: int = 80
    permission_mode: str = "bypassPermissions"
    output_format: str = "json"
    effort: str = ""
    timeout: int = 3600
    max_retry_attempts: int = 3
    session_ttl: int = 86400
    append_system_prompt: bool = True
    completion_queue_enabled: bool = True
    notification_timeout: int = 5
    max_queue_workers: int = 1
    default_notifications: str = "status"


@dataclass
class LangfuseConfig:
    host: str = "https://cloud.langfuse.com"
    public_key: str = ""
    secret_key: str = ""


@dataclass
class Config:
    repos: ReposConfig = field(default_factory=ReposConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    otel: OtelConfig = field(default_factory=OtelConfig)
    github: GithubConfig = field(default_factory=GithubConfig)
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)
    auth_broker: AuthBrokerConfig = field(default_factory=AuthBrokerConfig)
    agent_mode: AgentModeConfig = field(default_factory=AgentModeConfig)
    agentihooks_path: str = ""
    agentihooks_url: str = ""
    agentihooks_sync_interval: int = 300  # seconds between background re-syncs; 0 to disable
    mcp_lib_url: str = ""
    mcp_lib_path: str = ""
    agentihub_url: str = ""
    agentihub_path: str = ""
    agentihub_sync_interval: int = 0  # 0 = inherit from agentihooks_sync_interval


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
    )

    # Claude — env overrides
    claude = ClaudeConfig(
        binary=_env("AGENTICORE_CLAUDE_BINARY", claude_raw.get("binary", "claude")),
        timeout=_env_int("AGENTICORE_CLAUDE_TIMEOUT", str(claude_raw.get("timeout", 3600))),
        default_profile=_env("AGENTICORE_DEFAULT_PROFILE", claude_raw.get("default_profile", "coding")),
        config_dir=_env("AGENTICORE_CLAUDE_CONFIG_DIR", claude_raw.get("config_dir", "")),
        default_mcp_file=_env("AGENTICORE_DEFAULT_MCP_FILE", claude_raw.get("default_mcp_file", "")),
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
        package_dir=_env("AGENT_MODE_PACKAGE_DIR", "/app/package"),
        evaluation_dir=_env("AGENT_MODE_EVALUATION_DIR", "/app/evaluation"),
        repo_url=_env("PACKAGE_REPO_URL", ""),
        repo_branch=_env("PACKAGE_REPO_BRANCH", "main"),
        model=_env("AGENT_MODE_MODEL", "sonnet"),
        max_turns=_env_int("AGENT_MODE_MAX_TURNS", "80"),
        permission_mode=_env("AGENT_MODE_PERMISSION_MODE", "bypassPermissions"),
        output_format=_env("AGENT_MODE_OUTPUT_FORMAT", "json"),
        effort=_env("AGENT_MODE_EFFORT", ""),
        timeout=_env_int("AGENT_MODE_TIMEOUT", "3600"),
        max_retry_attempts=_env_int("AGENT_MODE_MAX_RETRIES", "3"),
        session_ttl=_env_int("AGENT_MODE_SESSION_TTL", "86400"),
        append_system_prompt=_env_bool("AGENT_MODE_APPEND_SYSTEM_PROMPT", "true"),
        completion_queue_enabled=_env_bool("AGENT_MODE_QUEUE_ENABLED", "true"),
        notification_timeout=_env_int("AGENT_MODE_NOTIFICATION_TIMEOUT", "5"),
        max_queue_workers=_env_int("AGENT_MODE_MAX_QUEUE_WORKERS", "1"),
        default_notifications=_env("AGENT_MODE_DEFAULT_NOTIFICATIONS", "status"),
    )

    agentihooks_path = _env("AGENTICORE_AGENTIHOOKS_PATH", raw.get("agentihooks_path", ""))
    agentihooks_url = _env("AGENTICORE_AGENTIHOOKS_URL", raw.get("agentihooks_url", ""))
    agentihooks_sync_interval = _env_int(
        "AGENTICORE_AGENTIHOOKS_SYNC_INTERVAL", str(raw.get("agentihooks_sync_interval", 300))
    )
    mcp_lib_url = _env("AGENTICORE_MCP_LIB_URL", raw.get("mcp_lib_url", ""))
    mcp_lib_path = _env("AGENTICORE_MCP_LIB_PATH", raw.get("mcp_lib_path", ""))
    agentihub_url = _env("AGENTIHUB_URL", raw.get("agentihub_url", ""))
    agentihub_path = _env("AGENTIHUB_PATH", raw.get("agentihub_path", ""))
    agentihub_sync_interval = _env_int("AGENTIHUB_SYNC_INTERVAL", str(raw.get("agentihub_sync_interval", 0)))

    # Auth Broker — env overrides
    auth_broker_raw = raw.get("auth_broker", {})
    auth_broker = AuthBrokerConfig(
        url=_env("AUTH_BROKER_URL", auth_broker_raw.get("url", "")),
        api_key=_env("AUTH_BROKER_API_KEY", auth_broker_raw.get("api_key", "")),
    )

    return Config(
        repos=repos,
        claude=claude,
        server=server,
        redis=redis,
        otel=otel,
        github=github,
        langfuse=langfuse,
        auth_broker=auth_broker,
        agent_mode=agent_mode,
        agentihooks_path=agentihooks_path,
        agentihooks_url=agentihooks_url,
        agentihooks_sync_interval=agentihooks_sync_interval,
        mcp_lib_url=mcp_lib_url,
        mcp_lib_path=mcp_lib_path,
        agentihub_url=agentihub_url,
        agentihub_path=agentihub_path,
        agentihub_sync_interval=agentihub_sync_interval,
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
