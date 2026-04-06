"""AgentiBridge client — self-registration and heartbeat for A2A discovery.

On startup, Agenticore registers itself with AgentiBridge so other agents
can discover it. A daemon thread sends periodic heartbeats.

All operations are best-effort: failures log warnings but never block startup.
"""

import logging
import socket
import threading
import time
from typing import Optional

import httpx

from agenticore.config import Config
from agenticore.mgmt_log import get_mgmt_logger

logger = logging.getLogger(__name__)

_VERSION = "1.2.0"


def _derive_agent_id(cfg: Config) -> str:
    if cfg.agentibridge.agent_id:
        return cfg.agentibridge.agent_id
    pod_name = cfg.repos.pod_name
    if pod_name:
        return pod_name
    return socket.gethostname()


def _derive_endpoint(cfg: Config) -> str:
    host = cfg.server.host
    if host in ("0.0.0.0", "127.0.0.1", ""):
        try:
            host = socket.gethostbyname(socket.gethostname())
        except Exception:
            host = "localhost"
    return f"http://{host}:{cfg.server.port}"


def _build_capabilities(cfg: Config, profiles: dict) -> list:
    base = [
        "run_task", "plan_task", "execute_plan",
        "get_job", "list_jobs", "cancel_job",
        "prepare_worktree", "list_profiles",
    ]
    profile_caps = [f"profile:{name}" for name in sorted(profiles.keys())]
    agent_cap = ["agent_mode"] if cfg.agent_mode.enabled else []
    hub_cap = [f"agent:{cfg.agent_mode.agent}"] if cfg.agent_mode.agent else []
    return base + profile_caps + agent_cap + hub_cap


def build_agent_card(cfg: Config, profiles: dict) -> dict:
    agent_id = _derive_agent_id(cfg)
    endpoint = _derive_endpoint(cfg)
    return {
        "agent_id": agent_id,
        "agent_name": agent_id,
        "agent_type": "executor",
        "capabilities": _build_capabilities(cfg, profiles),
        "endpoint": endpoint,
        "metadata": {
            "version": _VERSION,
            "pod_name": cfg.repos.pod_name,
            "max_parallel_jobs": cfg.repos.max_parallel_jobs,
            "default_profile": cfg.claude.default_profile,
            "agent_mode_enabled": cfg.agent_mode.enabled,
            "agentihub_agent": cfg.agent_mode.agent,
            "mcp_url": f"{endpoint}/mcp",
            "health_url": f"{endpoint}/health",
        },
        "heartbeat_ttl": cfg.agentibridge.heartbeat_interval * 5,
    }


def _make_client(cfg: Config) -> httpx.Client:
    headers = {}
    if cfg.agentibridge.api_key:
        headers["X-API-Key"] = cfg.agentibridge.api_key
    return httpx.Client(
        base_url=cfg.agentibridge.url.rstrip("/"),
        headers=headers,
        timeout=10.0,
    )


def register_with_bridge(cfg: Config, profiles: dict) -> Optional[str]:
    card = build_agent_card(cfg, profiles)
    try:
        with _make_client(cfg) as client:
            resp = client.post("/agents/register", json=card)
            resp.raise_for_status()
            logger.info("Registered with AgentiBridge: %s", card["agent_id"])
            return card["agent_id"]
    except httpx.ConnectError as e:
        logger.warning("AgentiBridge unreachable (%s): %s", cfg.agentibridge.url, e)
    except httpx.TimeoutException:
        logger.warning("AgentiBridge registration timed out")
    except Exception as e:
        logger.warning("AgentiBridge registration failed: %s", e)
    return None


def _get_running_jobs_count(cfg: Config) -> int:
    try:
        import redis as redis_lib

        if not cfg.redis.url:
            return 0
        r = redis_lib.Redis.from_url(cfg.redis.url, decode_responses=True, socket_timeout=2)
        prefix = cfg.redis.key_prefix
        job_ids = r.zrevrange(f"{prefix}:job:idx", 0, -1) or []
        count = 0
        for jid in job_ids:
            status = r.hget(f"{prefix}:job:{jid}", "status")
            if status == "running":
                count += 1
        return count
    except Exception:
        return 0


def heartbeat(cfg: Config, agent_id: str) -> bool:
    running = _get_running_jobs_count(cfg)
    try:
        with _make_client(cfg) as client:
            resp = client.post(
                f"/agents/{agent_id}/heartbeat",
                json={
                    "status": "online",
                    "metadata": {
                        "running_jobs": running,
                        "available_capacity": max(0, cfg.repos.max_parallel_jobs - running),
                    },
                },
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning("AgentiBridge heartbeat failed: %s", e)
        return False


def start_heartbeat_thread(cfg: Config, agent_id: str) -> threading.Thread:
    interval = cfg.agentibridge.heartbeat_interval
    mgmt = get_mgmt_logger()

    def _beat():
        while True:
            time.sleep(interval)
            try:
                ok = heartbeat(cfg, agent_id)
                if ok:
                    mgmt.info("heartbeat agentibridge OK agent=%s", agent_id)
                else:
                    mgmt.warning("heartbeat agentibridge NOOP agent=%s", agent_id)
            except Exception as exc:
                logger.warning("heartbeat agentibridge FAIL: %s", exc)
                mgmt.warning("heartbeat agentibridge FAIL: %s", exc)

    t = threading.Thread(target=_beat, name="agentibridge-heartbeat", daemon=True)
    t.start()
    logger.info("AgentiBridge heartbeat started (interval=%ds, agent=%s)", interval, agent_id)
    return t
