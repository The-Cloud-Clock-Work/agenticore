"""Provider registry — loads provider definitions from providers.yaml."""

from typing import Dict, List, Optional

import yaml

from auth_broker.config import get_config

_registry: Dict[str, dict] = {}


def load_providers() -> None:
    global _registry
    cfg = get_config()
    try:
        with open(cfg.providers_path) as f:
            data = yaml.safe_load(f) or {}
        _registry = data.get("providers", {})
    except FileNotFoundError:
        _registry = {}


def get_provider(name: str) -> Optional[dict]:
    return _registry.get(name)


def list_providers() -> Dict[str, dict]:
    return _registry


def list_provider_names() -> List[str]:
    return list(_registry.keys())
