"""Tests for agenticore.agent_mode.conversation_key."""

import pytest

from agenticore.agent_mode.conversation_key import (
    _is_uuid,
    compose_storage_key,
    hash_content_prefix,
    resolve_conversation_key,
)

pytestmark = pytest.mark.unit

AGENT = "anton-agent"


class TestIsUuid:
    def test_valid(self):
        assert _is_uuid("550e8400-e29b-41d4-a716-446655440000")

    def test_invalid(self):
        assert not _is_uuid("not-a-uuid")
        assert not _is_uuid("")
        assert not _is_uuid("550e8400")


class TestComposeStorageKey:
    def test_with_user(self):
        k = compose_storage_key("agent1", "user1", "conv123")
        assert k == "conv:agent1:user1:conv123"

    def test_without_user(self):
        k = compose_storage_key("agent1", "", "conv123")
        assert k == "conv:agent1:-:conv123"


class TestHashContentPrefix:
    def test_deterministic(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
        ]
        h1 = hash_content_prefix(msgs)
        h2 = hash_content_prefix(msgs)
        assert h1 == h2
        assert len(h1) == 16

    def test_different_system_different_hash(self):
        m1 = [{"role": "system", "content": "a"}, {"role": "user", "content": "x"}]
        m2 = [{"role": "system", "content": "b"}, {"role": "user", "content": "x"}]
        assert hash_content_prefix(m1) != hash_content_prefix(m2)

    def test_different_user_different_hash(self):
        m1 = [{"role": "system", "content": "s"}, {"role": "user", "content": "a"}]
        m2 = [{"role": "system", "content": "s"}, {"role": "user", "content": "b"}]
        assert hash_content_prefix(m1) != hash_content_prefix(m2)

    def test_no_system_prompt(self):
        msgs = [{"role": "user", "content": "hi"}]
        h = hash_content_prefix(msgs)
        assert len(h) == 16

    def test_multi_turn_stable(self):
        turn1 = [{"role": "user", "content": "hello"}]
        turn2 = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "how are you"},
        ]
        assert hash_content_prefix(turn1) == hash_content_prefix(turn2)


class TestTier1Header:
    def test_x_conversation_id(self):
        headers = {"x-conversation-id": "abc-123"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        key, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier == "header"
        assert "abc-123" in key

    def test_x_librechat_conversation_id(self):
        headers = {"x-librechat-conversation-id": "libre-456"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        key, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier == "header"
        assert "libre-456" in key

    def test_x_openwebui_chat_id(self):
        headers = {"x-openwebui-chat-id": "owui-789"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        key, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier == "header"
        assert "owui-789" in key

    def test_priority_order(self):
        headers = {
            "x-conversation-id": "first",
            "x-librechat-conversation-id": "second",
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}
        key, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert "first" in key

    def test_empty_header_skipped(self):
        headers = {"x-conversation-id": "  "}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        _, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier != "header"


class TestTier2Body:
    def test_metadata_conversation_id(self):
        headers = {}
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"conversation_id": "meta-conv"},
        }
        key, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier == "body"
        assert "meta-conv" in key

    def test_user_field_uuid(self):
        headers = {}
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "user": "550e8400-e29b-41d4-a716-446655440000",
        }
        key, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier == "body"

    def test_user_field_non_uuid_skipped(self):
        headers = {}
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "user": "john@example.com",
        }
        _, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier != "body"


class TestTier3ContentHash:
    def test_content_hash_fallback(self):
        headers = {}
        body = {"messages": [{"role": "user", "content": "hello world"}]}
        key, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier == "content_hash"

    def test_same_first_message_same_key(self):
        headers = {}
        b1 = {"messages": [{"role": "user", "content": "hi"}]}
        b2 = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "follow up"},
            ]
        }
        k1, _, _ = resolve_conversation_key(headers, b1, AGENT)
        k2, _, _ = resolve_conversation_key(headers, b2, AGENT)
        assert k1 == k2

    def test_disabled_by_toggle(self):
        headers = {}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        _, tier, _ = resolve_conversation_key(
            headers, body, AGENT, hash_fallback=False
        )
        assert tier == "ephemeral"


class TestTier4Ephemeral:
    def test_no_messages_ephemeral(self):
        headers = {}
        body = {"messages": []}
        _, tier, _ = resolve_conversation_key(headers, body, AGENT)
        assert tier == "ephemeral"

    def test_unique_per_call(self):
        headers = {}
        body = {"messages": []}
        k1, _, _ = resolve_conversation_key(headers, body, AGENT)
        k2, _, _ = resolve_conversation_key(headers, body, AGENT)
        assert k1 != k2


class TestUserScoping:
    def test_user_hint_from_header(self):
        headers = {"x-conversation-id": "conv1", "x-user-id": "alice"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        key, _, user_hint = resolve_conversation_key(headers, body, AGENT)
        assert user_hint == "alice"
        assert "alice" in key

    def test_user_hint_from_body(self):
        headers = {"x-conversation-id": "conv1"}
        body = {"messages": [{"role": "user", "content": "hi"}], "user": "bob"}
        key, _, user_hint = resolve_conversation_key(headers, body, AGENT)
        assert user_hint == "bob"
        assert "bob" in key

    def test_different_users_different_keys(self):
        h1 = {"x-conversation-id": "same", "x-user-id": "alice"}
        h2 = {"x-conversation-id": "same", "x-user-id": "bob"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        k1, _, _ = resolve_conversation_key(h1, body, AGENT)
        k2, _, _ = resolve_conversation_key(h2, body, AGENT)
        assert k1 != k2

    def test_different_agents_different_keys(self):
        headers = {"x-conversation-id": "same"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        k1, _, _ = resolve_conversation_key(headers, body, "agent-a")
        k2, _, _ = resolve_conversation_key(headers, body, "agent-b")
        assert k1 != k2
