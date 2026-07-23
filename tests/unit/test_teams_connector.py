"""Unit tests for the Microsoft Teams connector.

Covers the TeamsProgressSink event→titled-message mapping, delta coalescing,
the owner filter, the env-gate, and the handle_activity command paths. No
network: the Bot Connector client is faked.
"""

from __future__ import annotations

import pytest

from agenticore.connectors import teams as tc
from agenticore.connectors.teams import (
    ConversationRef,
    TeamsProgressSink,
    _is_owner,
    _uuid_for_conversation,
    is_enabled,
)

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------
# Fakes / fixtures


class FakeClient:
    """Records outbound messages instead of hitting the Bot Connector."""

    def __init__(self):
        self.sent: list[str] = []
        self.typing = 0

    async def send_message(self, ref, text):
        self.sent.append(text)

    async def send_typing(self, ref):
        self.typing += 1


def _ref() -> ConversationRef:
    return ConversationRef(
        service_url="https://smba.example/teams/",
        conversation_id="a:1;b",
        bot_account={"id": "bot"},
        user_account={"id": "user"},
    )


@pytest.fixture
def sink():
    return TeamsProgressSink(FakeClient(), _ref())


@pytest.fixture(autouse=True)
def _reset_singletons():
    tc._store = None
    tc._client = None
    yield
    tc._store = None
    tc._client = None


# ----------------------------------------------------------------------
# Pure helpers / gating


class TestEnableGate:
    def test_enabled_needs_both(self, monkeypatch):
        monkeypatch.delenv("TEAMS_APP_ID", raising=False)
        monkeypatch.delenv("TEAMS_APP_PASSWORD", raising=False)
        assert is_enabled() is False
        monkeypatch.setenv("TEAMS_APP_ID", "app")
        assert is_enabled() is False  # credential still missing
        monkeypatch.setenv("TEAMS_APP_PASSWORD", "x")
        assert is_enabled() is True


class TestOwnerFilter:
    def test_no_owner_allows_any(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        assert _is_owner({"from": {"aadObjectId": "whoever"}}) is True

    def test_owner_match(self, monkeypatch):
        monkeypatch.setenv("TEAMS_OWNER_AAD_ID", "owner-123")
        assert _is_owner({"from": {"aadObjectId": "owner-123"}}) is True
        assert _is_owner({"from": {"aadObjectId": "someone-else"}}) is False
        assert _is_owner({"from": {}}) is False


class TestConversationUuid:
    def test_stable_and_scoped(self):
        a = _uuid_for_conversation("conv-x")
        b = _uuid_for_conversation("conv-x")
        c = _uuid_for_conversation("conv-y")
        assert a == b
        assert a != c


# ----------------------------------------------------------------------
# Sink rendering


class TestSinkRendering:
    @pytest.mark.asyncio
    async def test_thinking_renders_titled_and_coalesced(self, sink):
        await sink.on_thinking("Let me ")
        await sink.on_thinking("check.")
        # A boundary event flushes the buffered thinking block as ONE message.
        await sink.on_tool_call("Bash", {"command": "ls /tmp"}, "t1")
        assert sink._client.sent[0] == "**Reasoning:**\nLet me check."
        assert sink._client.sent[1] == "**Tool call:** Bash\n`ls /tmp`"

    @pytest.mark.asyncio
    async def test_narration_posts_untitled(self, sink):
        await sink.on_narration("Hello ")
        await sink.on_narration("world")
        await sink.on_final("The answer")
        assert sink._client.sent == ["Hello world", "The answer"]

    @pytest.mark.asyncio
    async def test_kind_change_flushes_previous_block(self, sink):
        await sink.on_narration("plain text")
        await sink.on_thinking("a thought")
        # narration flushed when thinking started; thinking still buffered
        assert sink._client.sent == ["plain text"]
        await sink.on_final("")
        assert sink._client.sent == ["plain text", "**Reasoning:**\na thought"]

    @pytest.mark.asyncio
    async def test_tool_result_error_label(self, sink):
        await sink.on_tool_result("t1", "boom", True)
        assert sink._client.sent[0].startswith("**Tool result (error):**")
        assert "boom" in sink._client.sent[0]

    @pytest.mark.asyncio
    async def test_tool_result_ok_label(self, sink):
        await sink.on_tool_result("t1", "done", False)
        assert sink._client.sent[0].startswith("**Tool result:**")

    @pytest.mark.asyncio
    async def test_final_is_idempotent(self, sink):
        await sink.on_final("answer")
        await sink.on_final("answer again")
        assert sink._client.sent == ["answer"]

    @pytest.mark.asyncio
    async def test_error_titled(self, sink):
        await sink.on_error("it broke")
        assert sink._client.sent == ["**Error:** it broke"]

    @pytest.mark.asyncio
    async def test_large_block_auto_flushes(self, sink):
        big = "x" * (tc._FLUSH_CHARS + 10)
        await sink.on_narration(big)
        # exceeded the flush threshold → emitted without waiting for a boundary
        assert len(sink._client.sent) == 1
        assert sink._client.sent[0] == big

    @pytest.mark.asyncio
    async def test_tool_result_truncated(self, sink):
        huge = "y" * (tc.TeamsProgressSink._RESULT_MAX + 500)
        await sink.on_tool_result("t1", huge, False)
        assert "… (truncated)" in sink._client.sent[0]


# ----------------------------------------------------------------------
# handle_activity


class TestHandleActivity:
    @pytest.mark.asyncio
    async def test_non_message_ignored(self, monkeypatch):
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        await tc.handle_activity({"type": "typing"})
        assert fake.sent == []

    @pytest.mark.asyncio
    async def test_non_owner_ignored(self, monkeypatch):
        monkeypatch.setenv("TEAMS_OWNER_AAD_ID", "owner-123")
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "hi",
                "serviceUrl": "https://s/",
                "conversation": {"id": "c"},
                "from": {"aadObjectId": "intruder"},
            }
        )
        assert fake.sent == []

    @pytest.mark.asyncio
    async def test_clear_command(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "/clear",
                "serviceUrl": "https://s/",
                "conversation": {"id": "c"},
                "from": {"aadObjectId": "u"},
            }
        )
        assert fake.sent == ["Conversation cleared."]

    @pytest.mark.asyncio
    async def test_normal_turn_calls_completions(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        monkeypatch.setattr("agenticore.capabilities.render_capabilities_prompt", lambda: "")
        monkeypatch.setattr(
            "agenticore.agent_mode.stream_config.get_for_request",
            lambda agent_id, text: (text, {"show_thinking": True}, []),
        )

        captured = {}

        async def fake_call(messages, conv_id, *, sink, stream_cfg):
            captured["messages"] = messages
            captured["stream_cfg"] = stream_cfg
            return "final answer"

        monkeypatch.setattr(tc, "_call_completions", fake_call)

        await tc.handle_activity(
            {
                "type": "message",
                "text": "do a thing",
                "serviceUrl": "https://s/",
                "conversation": {"id": "c"},
                "from": {"aadObjectId": "u"},
            }
        )

        assert fake.typing == 1
        assert captured["stream_cfg"] == {"show_thinking": True}
        assert captured["messages"][-1] == {"role": "user", "content": "do a thing"}

    @pytest.mark.asyncio
    async def test_toggle_only_message_acks(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        monkeypatch.setattr(
            "agenticore.agent_mode.stream_config.get_for_request",
            lambda agent_id, text: ("", {"show_tools": False}, ["/hide-tools"]),
        )
        await tc.handle_activity(
            {
                "type": "message",
                "text": "/hide-tools",
                "serviceUrl": "https://s/",
                "conversation": {"id": "c"},
                "from": {"aadObjectId": "u"},
            }
        )
        assert len(fake.sent) == 1
        assert fake.sent[0].startswith("Visibility updated:")
