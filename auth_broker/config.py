"""Auth Broker configuration — loaded from environment variables."""

import os
from dataclasses import dataclass
from typing import Optional


def _env(key: str, default: str = "") -> str:
    val = os.getenv(key, "")
    return val if val else default


def _env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key, "")
    return int(val) if val else default


@dataclass
class Config:
    api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8300
    redis_url: str = "redis://localhost:6379/6"
    openbao_addr: str = "http://localhost:8200"
    openbao_token: str = ""
    providers_path: str = "/app/providers.yaml"
    # Public base URL for OAuth redirect_uri — e.g. https://auth.homeofanton.com
    callback_base_url: str = ""
    # Google email authorized to access the dashboard
    admin_email: str = ""


_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config(
            api_key=_env("AUTH_BROKER_API_KEY"),
            host=_env("AUTH_BROKER_HOST", "0.0.0.0"),
            port=_env_int("AUTH_BROKER_PORT", 8300),
            redis_url=_env("REDIS_URL", "redis://localhost:6379/6"),
            openbao_addr=_env("OPENBAO_ADDR", "http://localhost:8200"),
            openbao_token=_env("OPENBAO_TOKEN"),
            providers_path=_env("PROVIDERS_PATH", "/app/providers.yaml"),
            callback_base_url=_env("CALLBACK_BASE_URL", "http://localhost:8300"),
            admin_email=_env("AUTH_BROKER_ADMIN_EMAIL"),
        )
    return _config


def reset_config() -> None:
    global _config
    _config = None
