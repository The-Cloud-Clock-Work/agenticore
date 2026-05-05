---
title: Self-Describing Capabilities
nav_order: 7
parent: Architecture
---

# Self-Describing Capabilities

Agenticore agents auto-discover their enabled features at runtime and inject a
capabilities block into every Claude session. This ensures the LLM knows what
it can do — voice, Telegram, A2A, observability — without manual prompt
maintenance.

## Problem

An agent pod may have voice, Telegram, GitHub, and MCP gateway features enabled
via environment variables. But Claude doesn't read env vars — it only sees its
system prompt. Without explicit injection, asking "reply by voice" gets
"I don't have voice capabilities."

## How It Works

```
Pod env vars + Config
        │
        ▼
capabilities.py._build_registry()
        │  reads get_config() + os.environ
        │  builds list[Capability]
        ▼
render_capabilities_prompt()
        │  filters to enabled only
        │  renders markdown block
        ▼
Injected into Claude sessions:
  - Agent Mode: --append-system-prompt in build_claude_cmd()
  - Telegram:   prepended to system message in _process_and_respond()
```

## Capability Registry

Each capability is a simple dataclass:

```python
@dataclass
class Capability:
    name: str        # identifier
    enabled: bool    # derived from env/config at runtime
    description: str # what the agent can DO with this feature
```

Current capabilities:

| Name | Enabled By | Description |
|------|-----------|-------------|
| `voice` | `VOICE_SERVICE_URL` | STT/TTS via HTTP voice service |
| `telegram` | `TELEGRAM_BOT_TOKEN` | Connected as Telegram bot |
| `agent_mode` | `AGENT_MODE=true` | Persistent agent with model/turns config |
| `agentibridge` | `AGENTIBRIDGE_URL` | Agent-to-agent communication |
| `redis` | `REDIS_URL` | Session persistence, job queue, events |
| `observability` | OTel config | OpenTelemetry tracing |
| `github` | `GITHUB_TOKEN` or GitHub App | Repository interaction |
| `langfuse` | `LANGFUSE_PUBLIC_KEY` | LLM observability |
| `litellm_mcp` | `LITELLM_MCP_GATEWAY_KEY` | External MCP tool servers |

## Injection Points

### Agent Mode (CLI sessions)

In `agent_mode/agent.py`, `build_claude_cmd()` appends the capabilities block
after the `system.md` system prompt:

```python
from agenticore.capabilities import render_capabilities_prompt

caps_prompt = render_capabilities_prompt()
if caps_prompt:
    cmd.extend(["--append-system-prompt", caps_prompt])
```

### Telegram Connector (in-process)

In `connectors/telegram.py`, capabilities are merged with
`TELEGRAM_SYSTEM_PROMPT` before building the message list:

```python
from agenticore.capabilities import render_capabilities_prompt

caps = render_capabilities_prompt()
full_system = "\n\n".join(filter(None, [system_prompt, caps]))
```

## Adding a New Capability

One entry in `_build_registry()` inside `agenticore/capabilities.py`:

```python
caps.append(Capability(
    name="my_feature",
    enabled=bool(os.environ.get("MY_FEATURE_URL")),
    description="Feature X is available — can do A, B, C.",
))
```

No other files need changes. Every agent session on pods with that env var
will automatically know about the feature.

## Output Example

```markdown
## Agenticore Agent Capabilities

You are **anton-agent**, an agenticore-powered AI agent.

The following capabilities are active on this instance:

- **voice**: You have voice capabilities (speech-to-text and text-to-speech)...
- **telegram**: You are connected to Telegram as a bot...
- **agent_mode**: Running in AGENT_MODE as a persistent AI agent...
- **agentibridge**: Connected to AgentiBridge for agent-to-agent communication...

Use these capabilities when relevant to user requests.
Do not claim you lack a capability listed above.
```

## Design Principles

1. **Zero-maintenance** — capabilities derived from existing config. No manual
   sync when features change.
2. **Declarative** — each capability is data, not logic.
3. **Graceful absence** — if no capabilities are enabled, `render_capabilities_prompt()`
   returns empty string. No injection, no noise.
