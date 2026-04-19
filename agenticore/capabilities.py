"""Auto-discovers enabled capabilities and renders them for LLM context.

To add a new capability: append one entry to _build_registry(). That's it.
"""

import os
from dataclasses import dataclass

from agenticore.config import get_config


@dataclass
class Capability:
    name: str
    enabled: bool
    description: str


def _build_registry() -> list[Capability]:
    cfg = get_config()
    caps = []

    caps.append(Capability(
        name="voice",
        enabled=bool(os.environ.get("VOICE_SERVICE_URL")),
        description=(
            "You have voice capabilities (speech-to-text and text-to-speech). "
            "Users can send voice messages and you can respond with voice. "
            "Voice mode is toggled per-conversation with 'enable voice' / 'disable voice' "
            "or the /voice command. Voice input is always transcribed regardless of mode — "
            "mode controls output format only."
        ),
    ))

    caps.append(Capability(
        name="telegram",
        enabled=bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        description="You are connected to Telegram as a bot. Users interact via text and voice messages.",
    ))

    caps.append(Capability(
        name="agent_mode",
        enabled=cfg.agent_mode.enabled,
        description=(
            f"Running in AGENT_MODE as a persistent AI agent. "
            f"Model: {cfg.agent_mode.model}. Max turns: {cfg.agent_mode.max_turns}."
        ),
    ))

    caps.append(Capability(
        name="agentibridge",
        enabled=bool(cfg.agentibridge.url),
        description="Connected to AgentiBridge for agent-to-agent communication and fleet discovery.",
    ))

    caps.append(Capability(
        name="redis",
        enabled=bool(cfg.redis.url),
        description="Redis connected — session persistence, job queue, and event streaming available.",
    ))

    caps.append(Capability(
        name="observability",
        enabled=cfg.otel.enabled,
        description="OpenTelemetry tracing enabled — your actions are instrumented.",
    ))

    has_github = bool(cfg.github.token or cfg.github.app_id)
    caps.append(Capability(
        name="github",
        enabled=has_github,
        description="GitHub integration active — can interact with repositories.",
    ))

    caps.append(Capability(
        name="langfuse",
        enabled=bool(cfg.langfuse.public_key),
        description="Langfuse tracing enabled for LLM observability.",
    ))

    caps.append(Capability(
        name="litellm_mcp",
        enabled=bool(os.environ.get("LITELLM_MCP_GATEWAY_KEY")),
        description="LiteLLM MCP gateway connected — access to external tool servers.",
    ))

    return caps


def discover_capabilities() -> list[Capability]:
    return _build_registry()


def render_capabilities_prompt() -> str:
    caps = [c for c in discover_capabilities() if c.enabled]
    if not caps:
        return ""

    agent_name = os.environ.get("AGENTIHUB_AGENT", "agenticore")
    lines = [
        "## Agenticore Agent Capabilities",
        "",
        f"You are **{agent_name}**, an agenticore-powered AI agent.",
        "",
        "The following capabilities are active on this instance:",
        "",
    ]
    for c in caps:
        lines.append(f"- **{c.name}**: {c.description}")

    lines.append("")
    lines.append(
        "Use these capabilities when relevant to user requests. "
        "Do not claim you lack a capability listed above."
    )

    return "\n".join(lines)
