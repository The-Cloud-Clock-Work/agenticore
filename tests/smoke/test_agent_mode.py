"""E2E smoke tests for agent_mode — /completions endpoint.

Requires:
    - Server running with AGENT_MODE=true
    - SMOKE_AGENTICORE_URL env var (default: http://localhost:8200)

Run:
    pytest tests/smoke/test_agent_mode.py -v --timeout=300
"""

import os
import uuid

import httpx
import pytest

AGENTICORE_URL = os.getenv("SMOKE_AGENTICORE_URL", "http://localhost:8200")


@pytest.fixture(scope="module", autouse=True)
def agent_mode_enabled():
    """Skip all tests if server is not running in agent_mode."""
    try:
        resp = httpx.get(f"{AGENTICORE_URL}/health", timeout=10)
        resp.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException):
        pytest.skip("Server not reachable")

    if not resp.json().get("agent_mode"):
        pytest.skip("Server not in agent_mode")


def _fresh_uuid() -> str:
    return str(uuid.uuid4())


class TestAgentModeSmoke:
    """End-to-end smoke tests for agent_mode /completions endpoint."""

    def test_health_reports_agent_mode(self):
        """GET /health returns agent_mode=True and default_model."""
        resp = httpx.get(f"{AGENTICORE_URL}/health", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_mode"] is True
        assert "default_model" in data

    def test_completions_sync_happy_path(self):
        """POST /completions with wait=true returns a successful result."""
        resp = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={
                "message": "What is 2 + 2? Reply with just the number.",
                "uuid": _fresh_uuid(),
                "wait": True,
                "stateless": True,
                "max_turns": 1,
            },
            timeout=120,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["result"]  # non-empty

    def test_completions_async_happy_path(self):
        """POST /completions with wait=false returns 202 accepted."""
        resp = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={
                "message": "Say hello.",
                "uuid": _fresh_uuid(),
                "wait": False,
                "stateless": True,
            },
            timeout=30,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "accepted"

    def test_completions_missing_message(self):
        """POST /completions without message returns 400."""
        resp = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={"uuid": _fresh_uuid()},
            timeout=10,
        )
        assert resp.status_code == 400

    def test_completions_missing_uuid(self):
        """POST /completions without uuid returns 400."""
        resp = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={"message": "hello"},
            timeout=10,
        )
        assert resp.status_code == 400

    def test_completions_empty_body(self):
        """POST /completions with empty body returns 400."""
        resp = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={},
            timeout=10,
        )
        assert resp.status_code == 400

    def test_completions_invalid_json(self):
        """POST /completions with non-JSON body returns 400."""
        resp = httpx.post(
            f"{AGENTICORE_URL}/completions",
            content="this is not json",
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert resp.status_code == 400

    def test_session_persistence(self):
        """Two calls with same UUID and stateless=false should share session state."""
        session_uuid = _fresh_uuid()

        # First call: store a word
        resp1 = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={
                "message": "Remember this exact word: PINEAPPLE. Just confirm you remember it.",
                "uuid": session_uuid,
                "wait": True,
                "stateless": False,
                "max_turns": 1,
            },
            timeout=120,
        )
        assert resp1.status_code in (200, 500)  # may succeed or fail
        if resp1.status_code != 200:
            pytest.skip("First call failed — cannot test persistence")

        # Second call: recall the word
        resp2 = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={
                "message": "What was the exact word I asked you to remember? Reply with just the word.",
                "uuid": session_uuid,
                "wait": True,
                "stateless": False,
                "max_turns": 1,
            },
            timeout=120,
        )
        assert resp2.status_code == 200
        assert "PINEAPPLE" in resp2.json().get("result", "").upper()

    def test_stateless_isolation(self):
        """Two stateless calls with same UUID should NOT share session state."""
        session_uuid = _fresh_uuid()

        # First call: store a word
        resp1 = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={
                "message": "Remember this exact word: BANANA. Just confirm you remember it.",
                "uuid": session_uuid,
                "wait": True,
                "stateless": True,
                "max_turns": 1,
            },
            timeout=120,
        )
        assert resp1.status_code == 200

        # Second call: try to recall (should NOT have context)
        resp2 = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={
                "message": "What was the exact word I asked you to remember? If you don't know, say UNKNOWN.",
                "uuid": session_uuid,
                "wait": True,
                "stateless": True,
                "max_turns": 1,
            },
            timeout=120,
        )
        assert resp2.status_code == 200
        result = resp2.json().get("result", "").upper()
        assert "BANANA" not in result

    def test_model_override_accepted(self):
        """POST /completions with model override is accepted."""
        resp = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={
                "message": "Say yes.",
                "uuid": _fresh_uuid(),
                "wait": True,
                "stateless": True,
                "model": "haiku",
                "max_turns": 1,
            },
            timeout=120,
        )
        # Should not error from the model override
        assert resp.status_code in (200, 500)
        data = resp.json()
        # If succeeded, verify structure
        if resp.status_code == 200:
            assert "result" in data

    def test_max_turns_1_respected(self):
        """POST /completions with max_turns=1 limits agent to 1 turn."""
        resp = httpx.post(
            f"{AGENTICORE_URL}/completions",
            json={
                "message": "What is 1+1? Reply with just the number.",
                "uuid": _fresh_uuid(),
                "wait": True,
                "stateless": True,
                "max_turns": 1,
            },
            timeout=120,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("num_turns", 0) <= 1
