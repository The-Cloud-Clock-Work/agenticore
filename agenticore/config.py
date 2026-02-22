"""Configuration loader for Agenticore.

Loads from ``~/.agenticore/config.yml`` with environment variable overrides.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


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


@dataclass
class ClaudeConfig:
    binary: str = "claude"
    timeout: int = 3600
    default_profile: str = "code"
    home_path: str = ""


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


@dataclass
class Config:
    repos: ReposConfig = field(default_factory=ReposConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    otel: OtelConfig = field(default_factory=OtelConfig)
    github: GithubConfig = field(default_factory=GithubConfig)


def _default_repos_root() -> str:
    return str(Path.home() / "agenticore-repos")


def _config_path() -> Path:
    return Path.home() / ".agenticore" / "config.yml"


def _load_yaml(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


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
    )

    # Claude — env overrides
    claude = ClaudeConfig(
        binary=_env("AGENTICORE_CLAUDE_BINARY", claude_raw.get("binary", "claude")),
        timeout=_env_int("AGENTICORE_CLAUDE_TIMEOUT", str(claude_raw.get("timeout", 3600))),
        default_profile=_env("AGENTICORE_DEFAULT_PROFILE", claude_raw.get("default_profile", "code")),
        home_path=_env("AGENTICORE_CLAUDE_HOME_PATH", claude_raw.get("home_path", "")),
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
    )

    return Config(
        repos=repos,
        claude=claude,
        server=server,
        redis=redis,
        otel=otel,
        github=github,
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
