"""Unit tests for the Microsoft Teams connector.

Covers the TeamsProgressSink event→titled-message mapping, delta coalescing,
the owner filter, the env-gate, and the handle_activity command paths. No
network: the Bot Connector client is faked.
"""

from __future__ import annotations

import asyncio

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
    tc._conv_locks.clear()
    yield
    tc._store = None
    tc._client = None
    tc._conv_locks.clear()


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
        assert sink._client.sent[1] == "**Tool call:** Bash\n```\nls /tmp\n```"

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

    @pytest.mark.asyncio
    async def test_tool_result_fences_backtick_content(self, sink):
        # Content with its own ``` must not break the wrapping fence.
        await sink.on_tool_result("t1", "```bash\necho hi\n```", False)
        msg = sink._client.sent[0]
        assert msg.startswith("**Tool result:**\n````\n")
        assert msg.endswith("\n````")

    @pytest.mark.asyncio
    async def test_tool_call_fences_backtick_preview(self, sink):
        # A ``` run in the preview forces a >=4-backtick fence, never a broken span.
        await sink.on_tool_call("Bash", {"command": "echo ```x```"}, "t1")
        msg = sink._client.sent[0]
        assert "````" in msg


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
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
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
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
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
            await asyncio.sleep(0)  # yield so the background typing task runs
            return "final answer"

        monkeypatch.setattr(tc, "_call_completions", fake_call)

        await tc.handle_activity(
            {
                "type": "message",
                "text": "do a thing",
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
                "conversation": {"id": "c", "conversationType": "personal"},
                "from": {"aadObjectId": "u"},
            }
        )

        assert fake.typing >= 1
        assert captured["stream_cfg"] == {"show_thinking": True}
        assert captured["messages"][-1] == {"role": "user", "content": "do a thing"}

    @pytest.mark.asyncio
    async def test_stream_config_scoped_per_conversation(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        monkeypatch.setenv("AGENTIHUB_AGENT", "publisher")
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        monkeypatch.setattr("agenticore.capabilities.render_capabilities_prompt", lambda: "")
        seen = {}

        def fake_gfr(agent_id, text):
            seen["agent_id"] = agent_id
            return (text, {}, [])

        monkeypatch.setattr("agenticore.agent_mode.stream_config.get_for_request", fake_gfr)

        async def fake_call(messages, conv_id, *, sink, stream_cfg):
            return "ok"

        monkeypatch.setattr(tc, "_call_completions", fake_call)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "hi",
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
                "conversation": {"id": "conv-XYZ", "conversationType": "personal"},
                "from": {"aadObjectId": "u"},
            }
        )
        # Visibility key carries the conversation id → no cross-chat bleed.
        assert seen["agent_id"] == "publisher:teams:conv-XYZ"

    @pytest.mark.asyncio
    async def test_formatting_hint_injected_into_system_prompt(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        monkeypatch.delenv("TEAMS_SYSTEM_PROMPT", raising=False)
        monkeypatch.delenv("TEAMS_FORMATTING_HINT", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        monkeypatch.setattr("agenticore.capabilities.render_capabilities_prompt", lambda: "")
        monkeypatch.setattr(
            "agenticore.agent_mode.stream_config.get_for_request",
            lambda a, t: (t, {}, []),
        )
        captured = {}

        async def fake_call(messages, conv_id, *, sink, stream_cfg):
            captured["messages"] = messages
            return "ok"

        monkeypatch.setattr(tc, "_call_completions", fake_call)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "hi",
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
                "conversation": {"id": "c", "conversationType": "personal"},
                "from": {"aadObjectId": "u"},
            }
        )
        system_msg = captured["messages"][0]
        assert system_msg["role"] == "system"
        assert "Teams mobile does not render" in system_msg["content"]

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
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
                "conversation": {"id": "c"},
                "from": {"aadObjectId": "u"},
            }
        )
        assert len(fake.sent) == 1
        assert fake.sent[0].startswith("Visibility updated:")

    @pytest.mark.asyncio
    async def test_non_msteams_channel_refused(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "hi",
                "channelId": "webchat",
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
                "conversation": {"id": "c"},
                "from": {"aadObjectId": "u"},
            }
        )
        assert fake.sent == []

    @pytest.mark.asyncio
    async def test_concurrent_turn_rejected(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        monkeypatch.setattr(
            "agenticore.agent_mode.stream_config.get_for_request",
            lambda a, t: (t, {}, []),
        )
        # Simulate an in-flight turn holding the conversation lock.
        held = asyncio.Lock()
        await held.acquire()
        tc._conv_locks["c"] = held
        try:
            await tc.handle_activity(
                {
                    "type": "message",
                    "text": "second message",
                    "serviceUrl": "https://smba.trafficmanager.net/amer/",
                    "conversation": {"id": "c", "conversationType": "personal"},
                    "from": {"aadObjectId": "u"},
                }
            )
            assert len(fake.sent) == 1
            assert "Still working" in fake.sent[0]
        finally:
            held.release()

    @pytest.mark.asyncio
    async def test_empty_final_posts_fallback(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        monkeypatch.setattr("agenticore.capabilities.render_capabilities_prompt", lambda: "")
        monkeypatch.setattr(
            "agenticore.agent_mode.stream_config.get_for_request",
            lambda a, t: (t, {}, []),
        )

        async def fake_call(messages, conv_id, *, sink, stream_cfg):
            return ""  # agent produced no final

        monkeypatch.setattr(tc, "_call_completions", fake_call)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "do a thing",
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
                "conversation": {"id": "c", "conversationType": "personal"},
                "from": {"aadObjectId": "u"},
            }
        )
        assert any("no output" in m for m in fake.sent)

    @pytest.mark.asyncio
    async def test_untrusted_service_url_refused(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        monkeypatch.delenv("TEAMS_SKIP_JWT_VALIDATION", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "hi",
                "serviceUrl": "https://attacker.example/",
                "conversation": {"id": "c"},
                "from": {"aadObjectId": "u"},
            }
        )
        # No token-bearing send may go to an untrusted host.
        assert fake.sent == []
        assert fake.typing == 0

    @pytest.mark.asyncio
    async def test_non_personal_conversation_refused(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "hi",
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
                "conversation": {"id": "c", "conversationType": "channel"},
                "from": {"aadObjectId": "u"},
            }
        )
        assert fake.sent == []

    @pytest.mark.asyncio
    async def test_mention_markup_stripped(self, monkeypatch):
        monkeypatch.delenv("TEAMS_OWNER_AAD_ID", raising=False)
        fake = FakeClient()
        monkeypatch.setattr(tc, "_get_client", lambda: fake)
        monkeypatch.setattr("agenticore.capabilities.render_capabilities_prompt", lambda: "")
        monkeypatch.setattr(
            "agenticore.agent_mode.stream_config.get_for_request",
            lambda agent_id, text: (text, {}, []),
        )
        captured = {}

        async def fake_call(messages, conv_id, *, sink, stream_cfg):
            captured["messages"] = messages
            return "ok"

        monkeypatch.setattr(tc, "_call_completions", fake_call)
        await tc.handle_activity(
            {
                "type": "message",
                "text": "<at>Bot</at> summarize logs",
                "serviceUrl": "https://smba.trafficmanager.net/amer/",
                "conversation": {"id": "c", "conversationType": "personal"},
                "from": {"aadObjectId": "u"},
            }
        )
        assert captured["messages"][-1] == {"role": "user", "content": "summarize logs"}


class TestTrustedServiceUrl:
    def test_teams_hosts_trusted(self, monkeypatch):
        monkeypatch.delenv("TEAMS_SKIP_JWT_VALIDATION", raising=False)
        assert tc._is_trusted_service_url("https://smba.trafficmanager.net/amer/") is True
        assert tc._is_trusted_service_url("https://x.botframework.com/") is True

    def test_arbitrary_trafficmanager_subdomain_untrusted(self, monkeypatch):
        # KEY: any Azure customer can register <name>.trafficmanager.net, so the
        # whole zone must NOT be trusted — only the exact Teams service host.
        monkeypatch.delenv("TEAMS_SKIP_JWT_VALIDATION", raising=False)
        assert tc._is_trusted_service_url("https://attacker.trafficmanager.net/") is False
        assert tc._is_trusted_service_url("https://evil.skype.com/") is False

    def test_attacker_host_untrusted(self, monkeypatch):
        monkeypatch.delenv("TEAMS_SKIP_JWT_VALIDATION", raising=False)
        assert tc._is_trusted_service_url("https://attacker.example/") is False
        assert tc._is_trusted_service_url("https://smba.trafficmanager.net@attacker.example/") is False
        assert tc._is_trusted_service_url("") is False
        # localhost only trusted in dev (JWT validation disabled)
        assert tc._is_trusted_service_url("http://localhost:3978/") is False
        monkeypatch.setenv("TEAMS_SKIP_JWT_VALIDATION", "1")
        assert tc._is_trusted_service_url("http://localhost:3978/") is True


class TestCodeFence:
    def test_min_three_backticks(self):
        assert tc._code_fence("plain text") == "```"

    def test_longer_than_inner_run(self):
        assert tc._code_fence("has ``` inside") == "````"
        assert tc._code_fence("````") == "`````"

    def test_ignores_non_consecutive(self):
        assert tc._code_fence("`a`b`c`") == "```"


class TestSplitForTeams:
    def test_ascii_under_budget_single_chunk(self):
        assert tc._split_for_teams("hello", max_bytes=100) == ["hello"]

    def test_byte_budget_respected(self):
        big = "a" * 40000
        parts = tc._split_for_teams(big, max_bytes=18000)
        assert all(len(p.encode("utf-8")) <= 18000 for p in parts)
        assert "".join(parts) == big

    def test_multibyte_never_exceeds_budget(self):
        s = "😀" * 10000  # 4 bytes each in UTF-8
        parts = tc._split_for_teams(s, max_bytes=18000)
        assert all(len(p.encode("utf-8")) <= 18000 for p in parts)
        assert "".join(parts) == s

    def test_prefers_newline_boundary(self):
        text = ("x" * 100 + "\n") * 200
        parts = tc._split_for_teams(text, max_bytes=5000)
        # every chunk but the last should end on a newline
        assert all(p.endswith("\n") for p in parts[:-1])


class TestAuthNeverRaises:
    @pytest.mark.asyncio
    async def test_jwks_failure_returns_false(self, monkeypatch):
        monkeypatch.delenv("TEAMS_SKIP_JWT_VALIDATION", raising=False)
        monkeypatch.setenv("TEAMS_APP_ID", "app")

        async def boom():
            raise RuntimeError("metadata endpoint down")

        monkeypatch.setattr(tc, "_get_jwks_client", boom)
        ok = await tc.authenticate_request("Bearer abc.def.ghi", {"serviceUrl": "x"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_missing_header_returns_false(self, monkeypatch):
        monkeypatch.delenv("TEAMS_SKIP_JWT_VALIDATION", raising=False)
        assert await tc.authenticate_request("", {}) is False
        assert await tc.authenticate_request("Basic xyz", {}) is False


class TestSinkErrorGuard:
    @pytest.mark.asyncio
    async def test_on_error_fires_once(self, sink):
        await sink.on_error("boom")
        await sink.on_error("boom again")
        assert sink._client.sent == ["**Error:** boom"]

    @pytest.mark.asyncio
    async def test_on_error_suppressed_after_final(self, sink):
        await sink.on_final("the answer")
        await sink.on_error("late error")
        assert sink._client.sent == ["the answer"]
