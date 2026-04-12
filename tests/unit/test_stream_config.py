"""Tests for agenticore.agent_mode.stream_config."""

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(
        "agenticore.agent_mode.stream_config._get_redis",
        lambda: fake,
    )
    return fake


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestStripTokens:
    def test_no_token(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        assert strip_tokens("hello world") == ("hello world", [])

    def test_single_show_thinking(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, tokens = strip_tokens("hello /show-thinking")
        assert clean == "hello"
        assert tokens == ["/show-thinking"]

    def test_token_at_start(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, tokens = strip_tokens("/show-tools list files")
        assert clean == "list files"
        assert tokens == ["/show-tools"]

    def test_token_in_middle(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, tokens = strip_tokens("do this /show-thinking and that")
        assert clean == "do this and that"
        assert tokens == ["/show-thinking"]

    def test_multiple_tokens(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, tokens = strip_tokens("hi /show-thinking /show-tools")
        assert clean == "hi"
        assert tokens == ["/show-thinking", "/show-tools"]

    def test_unknown_token_passes_through(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, tokens = strip_tokens("hi /unknown-thing")
        assert clean == "hi /unknown-thing"
        assert tokens == []

    def test_path_not_matched(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, tokens = strip_tokens("look at /etc/foo and /tmp/bar")
        assert "/etc/foo" in clean
        assert "/tmp/bar" in clean
        assert tokens == []

    def test_stream_status_token(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, tokens = strip_tokens("/stream-status")
        assert clean == ""
        assert tokens == ["/stream-status"]

    def test_hide_all_token(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, tokens = strip_tokens("reset /hide-all")
        assert clean == "reset"
        assert tokens == ["/hide-all"]

    def test_empty_message(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        assert strip_tokens("") == ("", [])

    def test_whitespace_collapsed(self):
        from agenticore.agent_mode.stream_config import strip_tokens

        clean, _ = strip_tokens("a   b /show-tools   c")
        assert clean == "a b c"


class TestApplyTokens:
    def test_show_thinking_idempotent(self):
        from agenticore.agent_mode.stream_config import apply_tokens, DEFAULT_CONFIG

        c = apply_tokens(DEFAULT_CONFIG, ["/show-thinking"])
        assert c["show_thinking"] is True
        c2 = apply_tokens(c, ["/show-thinking"])
        assert c2 == c

    def test_hide_thinking(self):
        from agenticore.agent_mode.stream_config import apply_tokens

        start = {"show_thinking": True, "show_tools": False, "show_text": True}
        c = apply_tokens(start, ["/hide-thinking"])
        assert c["show_thinking"] is False

    def test_show_tools(self):
        from agenticore.agent_mode.stream_config import apply_tokens, DEFAULT_CONFIG

        c = apply_tokens(DEFAULT_CONFIG, ["/show-tools"])
        assert c["show_tools"] is True
        assert c["show_thinking"] is False
        assert c["show_text"] is True

    def test_show_all(self):
        from agenticore.agent_mode.stream_config import apply_tokens, DEFAULT_CONFIG

        c = apply_tokens(DEFAULT_CONFIG, ["/show-all"])
        assert c == {"show_thinking": True, "show_tools": True, "show_text": True}

    def test_hide_all(self):
        from agenticore.agent_mode.stream_config import apply_tokens

        start = {"show_thinking": True, "show_tools": True, "show_text": True}
        c = apply_tokens(start, ["/hide-all"])
        assert c == {"show_thinking": False, "show_tools": False, "show_text": True}

    def test_stream_status_no_op(self):
        from agenticore.agent_mode.stream_config import apply_tokens

        start = {"show_thinking": True, "show_tools": False, "show_text": True}
        c = apply_tokens(start, ["/stream-status"])
        assert c == start

    def test_does_not_mutate_input(self):
        from agenticore.agent_mode.stream_config import apply_tokens, DEFAULT_CONFIG

        original = dict(DEFAULT_CONFIG)
        apply_tokens(original, ["/show-all"])
        assert original == DEFAULT_CONFIG

    def test_sequential_tokens(self):
        from agenticore.agent_mode.stream_config import apply_tokens, DEFAULT_CONFIG

        c = apply_tokens(DEFAULT_CONFIG, ["/show-thinking", "/show-tools", "/hide-thinking"])
        assert c["show_thinking"] is False
        assert c["show_tools"] is True


class TestLoadSave:
    def test_default_when_unset(self, fake_redis, isolated_home):
        from agenticore.agent_mode.stream_config import load_stream_config, DEFAULT_CONFIG

        cfg = load_stream_config("agent-new")
        assert cfg == DEFAULT_CONFIG

    def test_save_then_load(self, fake_redis, isolated_home):
        from agenticore.agent_mode.stream_config import save_stream_config, load_stream_config

        target = {"show_thinking": True, "show_tools": True, "show_text": False}
        save_stream_config("agent-1", target)
        loaded = load_stream_config("agent-1")
        assert loaded == target

    def test_per_agent_isolation(self, fake_redis, isolated_home):
        from agenticore.agent_mode.stream_config import save_stream_config, load_stream_config

        save_stream_config("agent-a", {"show_thinking": True, "show_tools": False, "show_text": True})
        save_stream_config("agent-b", {"show_thinking": False, "show_tools": True, "show_text": True})
        a = load_stream_config("agent-a")
        b = load_stream_config("agent-b")
        assert a["show_thinking"] is True
        assert a["show_tools"] is False
        assert b["show_thinking"] is False
        assert b["show_tools"] is True

    def test_file_fallback_when_no_redis(self, monkeypatch, isolated_home):
        import agenticore.agent_mode.stream_config as mod

        monkeypatch.setattr(mod, "_get_redis", lambda: None)
        target = {"show_thinking": True, "show_tools": False, "show_text": True}
        mod.save_stream_config("agent-fb", target)
        loaded = mod.load_stream_config("agent-fb")
        assert loaded == target


class TestGetForRequest:
    def test_no_token_returns_loaded(self, fake_redis, isolated_home):
        from agenticore.agent_mode.stream_config import get_for_request, save_stream_config

        save_stream_config("agent-x", {"show_thinking": True, "show_tools": False, "show_text": True})
        msg, cfg, tokens = get_for_request("agent-x", "do something")
        assert msg == "do something"
        assert cfg["show_thinking"] is True
        assert tokens == []

    def test_show_thinking_persists(self, fake_redis, isolated_home):
        from agenticore.agent_mode.stream_config import get_for_request

        msg, cfg, tokens = get_for_request("agent-y", "hello /show-thinking")
        assert msg == "hello"
        assert cfg["show_thinking"] is True
        assert tokens == ["/show-thinking"]

        msg2, cfg2, tokens2 = get_for_request("agent-y", "next call")
        assert cfg2["show_thinking"] is True
        assert tokens2 == []

    def test_hide_after_show(self, fake_redis, isolated_home):
        from agenticore.agent_mode.stream_config import get_for_request

        get_for_request("agent-z", "x /show-thinking")
        _, cfg, _ = get_for_request("agent-z", "y /hide-thinking")
        assert cfg["show_thinking"] is False
        _, cfg2, _ = get_for_request("agent-z", "z")
        assert cfg2["show_thinking"] is False

    def test_stream_status_does_not_persist(self, fake_redis, isolated_home):
        from agenticore.agent_mode.stream_config import get_for_request, load_stream_config, DEFAULT_CONFIG

        msg, cfg, tokens = get_for_request("agent-q", "/stream-status")
        assert tokens == ["/stream-status"]
        on_disk = load_stream_config("agent-q")
        assert on_disk == DEFAULT_CONFIG


class TestIsVisible:
    def test_thinking_gated(self):
        from agenticore.agent_mode.stream_config import is_visible

        assert is_visible("thinking", {"show_thinking": True, "show_tools": False, "show_text": True})
        assert not is_visible("thinking", {"show_thinking": False, "show_tools": True, "show_text": True})

    def test_tool_use_gated_by_show_tools(self):
        from agenticore.agent_mode.stream_config import is_visible

        cfg = {"show_thinking": False, "show_tools": True, "show_text": True}
        assert is_visible("tool_use", cfg)
        assert is_visible("tool_result", cfg)
        cfg["show_tools"] = False
        assert not is_visible("tool_use", cfg)
        assert not is_visible("tool_result", cfg)

    def test_assistant_text_gated_by_show_text(self):
        from agenticore.agent_mode.stream_config import is_visible

        assert is_visible("assistant_text", {"show_thinking": False, "show_tools": False, "show_text": True})
        assert not is_visible("assistant_text", {"show_thinking": False, "show_tools": False, "show_text": False})

    def test_done_always_visible(self):
        from agenticore.agent_mode.stream_config import is_visible

        cfg = {"show_thinking": False, "show_tools": False, "show_text": False}
        assert is_visible("done", cfg)

    def test_stream_config_always_visible(self):
        from agenticore.agent_mode.stream_config import is_visible

        cfg = {"show_thinking": False, "show_tools": False, "show_text": False}
        assert is_visible("stream_config", cfg)

    def test_unknown_event_hidden(self):
        from agenticore.agent_mode.stream_config import is_visible

        cfg = {"show_thinking": True, "show_tools": True, "show_text": True}
        assert not is_visible("garbage", cfg)
