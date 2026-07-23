---
title: Teams Connector
nav_order: 10
parent: Reference
---

# Microsoft Teams Connector

Native Microsoft Teams bot connector that bridges Teams messages to agenticore's
completions engine in-process — the third transport on the shared `ProgressSink`
pipeline, alongside the Telegram connector and the SSE `/v1/chat/completions`
path.

Unlike Telegram (outbound long-polling), **Teams is push-based**: the Bot
Framework Connector service POSTs activities to a public HTTPS endpoint. The
connector therefore exposes an inbound route rather than a polling loop.

## Enable

Set environment variables:

```bash
TEAMS_APP_ID=<bot Entra application (client) id>
TEAMS_APP_PASSWORD=<bot client credential>     # referenced by env only
```

Optional:

```bash
TEAMS_OWNER_AAD_ID=<Entra object id>   # restrict to one user (owner filter);
                                       # unset = any authenticated user
TEAMS_TENANT_ID=<tenant id>            # single-tenant bots; omit for multi-tenant
TEAMS_SYSTEM_PROMPT=<custom system prompt>
TEAMS_MAX_MESSAGES=20                  # conversation history depth
TEAMS_CONVERSATION_TTL=86400           # history TTL in seconds
TEAMS_SKIP_JWT_VALIDATION=1            # LOCAL DEV ONLY — skip inbound JWT check
```

When `TEAMS_APP_ID` + `TEAMS_APP_PASSWORD` are set, the ASGI app registers
`POST /api/messages` at startup. Point your Azure Bot resource's messaging
endpoint at `https://<host>/api/messages`.

Progress visibility (Reasoning / Tool call / Tool result messages) is controlled
by the per-agent sticky `stream_config`, scoped to a **Teams-specific agent id**
(`{AGENTIHUB_AGENT}:teams`) so Teams toggles are independent of the SSE and
Telegram transports. Send `/show-thinking`, `/hide-tools`, `/show-all`, etc. to
the bot to toggle for Teams only. See
[SSE streaming](sse-streaming.md#slash-tokens-visibility-toggles) for the full
token list.

## Why titled messages, not a reasoning panel

Teams has **no native reasoning/thinking panel** anywhere in the platform (unlike
LibreChat/OpenWebUI, which render `reasoning_content` over SSE automatically). So
this connector renders intermediate agent activity as ordinary titled chat
messages, gated by the visibility flags:

| Canonical event | Rendered as |
|---|---|
| `narration` | posted as-is (the model's interleaved text) |
| `thinking` | `**Reasoning:**\n<text>` |
| `tool_call` | `**Tool call:** <name>` + a fenced args preview |
| `tool_result` | `**Tool result[ (error)]:**` + a fenced (truncated) body |
| `final` | the answer, as its own message(s) |
| `error` | `**Error:** <message>` |

`thinking` and `narration` arrive as many small deltas. The sink **coalesces
each block** into one message (flushed on a block boundary or when the buffer
passes `_FLUSH_CHARS`), so it never posts one message per token — Teams caps
sends per conversation per hour.

## Architecture

```
agenticore/connectors/teams.py
    │
    ├── is_enabled()            — env gate (TEAMS_APP_ID + TEAMS_APP_PASSWORD)
    ├── authenticate_request()  — validate the inbound Bot Framework JWT
    │                             (PyJWT + JWKS; no botbuilder SDK)
    ├── handle_activity()       — per-message core: owner filter → resolve
    │                             stream_config → typing → sink → completions
    ├── TeamsProgressSink        — ProgressSink → titled Teams messages
    ├── TeamsClient              — Bot Connector REST (httpx + OAuth2 token)
    ├── ConversationStore        — per-conversation history
    └── ConversationRef          — serviceUrl + ids + bot/user accounts
```

`server.py` registers `POST /api/messages` on the existing ASGI app (no second
listener). The handler validates the JWT, then dispatches to `handle_activity`.

### Auth (no heavy SDK)

- **Inbound:** the Bot Framework JWT is validated with `PyJWT` + `PyJWKClient`
  against the Bot Framework OpenID metadata — issuer `https://api.botframework.com`,
  audience = the bot app id, RS256, with a `serviceurl`-claim cross-check. Both
  `PyJWT[crypto]` and `httpx` are already core agenticore dependencies, so the
  connector needs **no optional extra** (unlike Telegram's `aiogram`).
- **Outbound:** a bot access token is acquired via OAuth2 client-credentials
  (`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, scope
  `https://api.botframework.com/.default`), cached until shortly before expiry,
  and used to `POST {serviceUrl}/v3/conversations/{id}/activities`.

## Scope (v1)

- **1:1 DMs only.** Owner-filtered on `from.aadObjectId`.
- Final answer and intermediate events are sent as normal messages.
- **Not yet:** native Teams token streaming (the `streaminfo` protocol),
  channels/group chats, Adaptive Card tool UI. These are planned v2.

## Out of scope (operator-owned)

Azure Bot resource creation, Entra app registration, and public HTTPS ingress
exposure are handled outside agenticore. The connector only consumes
`/api/messages` and validates the JWT.

## Testing

`tests/unit/test_teams_connector.py` covers the sink event→message mapping, delta
coalescing, the owner filter, the env-gate, and the `handle_activity` command
paths with a faked Bot Connector client (no network).
