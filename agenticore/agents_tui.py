"""Interactive TUI + headless CLI for discovering and managing agenticore agents.

Local agents (AgentiHub packages) are always discovered. Kubernetes is an
**opt-in backend**: no ``kubectl`` process is ever spawned unless K8s is
explicitly enabled, so the CLI stays portable on machines with no cluster.

Enable K8s (highest precedence first):
    agenticore agents --k8s [--namespace anton-prod]
    AGENTICORE_K8S_ENABLED=true [AGENTICORE_K8S_NAMESPACES=anton-prod,other]
    ~/.agenticore/state.json  ->  {"k8s": {"enabled": true, "namespaces": [...]}}

Interactive (default):  agenticore agents
Headless (AI/scripts):  agenticore agents --headless list
                        agenticore agents --headless chat --pod NAME --message "task"
                        agenticore agents --headless job --pod NAME --task "fix bug" --repo URL
                        agenticore agents --headless sync --pod NAME
                        agenticore agents --headless health --pod NAME
"""

import json
import os
import subprocess
import sys
import termios
import threading
import tty
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# ── ANSI ──────────────────────────────────────────────────────────────────────
R = "\033[0m"
B = "\033[1m"
D = "\033[2m"
CY = "\033[36m"
GR = "\033[32m"
GRB = "\033[1;32m"
YL = "\033[33m"
MG = "\033[35m"
RD = "\033[31m"
RDB = "\033[1;31m"
BL = "\033[34m"
LG = "\033[38;5;245m"
LGB = "\033[1;38;5;250m"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

K8S_DISABLED_HINT = "Kubernetes is disabled. Enable it with --k8s, AGENTICORE_K8S_ENABLED=true, or 'k' in the TUI."


@dataclass
class AgenticorePod:
    name: str
    namespace: str
    phase: str
    agent_mode: bool
    agent_name: str
    port: str
    container: str


@dataclass
class LocalAgent:
    name: str
    package_path: str
    description: str = ""
    capabilities: list[str] = field(default_factory=list)


@dataclass
class K8sConfig:
    """Resolved Kubernetes backend settings.

    Disabled by default. Agenticore is local-first: K8s is one optional
    backend among several (Fargate/ECS adapters may follow), so nothing here
    may shell out to ``kubectl`` unless the operator asked for it.
    """

    enabled: bool = False
    namespaces: list[str] = field(default_factory=list)  # empty => all namespaces


# ── TTY helpers (interactive only) ────────────────────────────────────────────


def _write(t, *args, end="\n"):
    t.write("".join(str(a) for a in args) + end)
    t.flush()


def _clear(t):
    t.write("\033[2J\033[H")
    t.flush()


def _prompt(t, msg="") -> str:
    t.write(f"  {GRB}▶{R} {msg}")
    t.flush()
    fd = os.open("/dev/tty", os.O_RDONLY)
    old = termios.tcgetattr(fd)
    buf = []
    try:
        tty.setraw(fd)
        while True:
            ch = os.read(fd, 1)
            if not ch:
                return "q"
            b = ch[0]
            if b == 0x1B:
                os.read(fd, 1)
                while True:
                    c = os.read(fd, 1)
                    if c and c[0] in range(0x40, 0x7F):
                        break
                continue
            if b in (0x03, 0x04):
                t.write("\r\n")
                t.flush()
                return "q"
            if b in (0x0D, 0x0A):
                t.write("\r\n")
                t.flush()
                return "".join(buf)
            if b in (0x7F, 0x08):
                if buf:
                    buf.pop()
                    t.write("\b \b")
                    t.flush()
                continue
            if 0x20 <= b < 0x7F:
                buf.append(chr(b))
                t.write(chr(b))
                t.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)


def _prompt_multiline(t, msg="") -> str:
    _write(t, f"  {LG}{msg}{R}")
    _write(t, f"  {LG}(empty line to submit, Ctrl-C to cancel){R}")
    lines = []
    while True:
        line = _prompt(t)
        if line == "q" and not lines:
            return ""
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


# ── State (~/.agenticore/state.json) ──────────────────────────────────────────


def _state_path() -> Path:
    return Path.home() / ".agenticore" / "state.json"


def _read_state() -> dict:
    state_path = _state_path()
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}
    return {}


def _write_state(mutate) -> None:
    state = _read_state()
    mutate(state)
    sp = _state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(state, indent=2))


def _state_section(state: dict, key: str) -> dict:
    """Return state[key] as a dict, replacing any non-dict value in place.

    ``setdefault`` is not enough: it returns the *existing* value untouched when
    the key is present, so a hand-corrupted ``"k8s": "yes"`` would blow up on the
    next item assignment.
    """
    section = state.get(key)
    if not isinstance(section, dict):
        section = {}
        state[key] = section
    return section


def _write_agentihub_to_state(path: Path) -> None:
    """Persist a user-chosen agentihub directory into ~/.agenticore/state.json."""

    def _mutate(state):
        _state_section(state, "agentihub")["path"] = str(path)

    _write_state(_mutate)


def _write_k8s_to_state(enabled: bool, namespaces: Optional[list[str]] = None) -> None:
    """Persist the K8s backend toggle into ~/.agenticore/state.json.

    ``namespaces=None`` leaves the persisted scope untouched — turning the
    backend off must not erase the namespaces the operator configured.
    """

    def _mutate(state):
        k8s = _state_section(state, "k8s")
        k8s["enabled"] = bool(enabled)
        if namespaces is not None:
            k8s["namespaces"] = list(namespaces)

    _write_state(_mutate)


# ── K8s opt-in resolution ─────────────────────────────────────────────────────


def _parse_namespaces(raw) -> list[str]:
    """Normalize a namespace spec to a unique, ordered list.

    Accepts a comma/space-separated string or a sequence. Anything else — a
    scalar left in ``state.json`` by a hand edit or a version skew — yields an
    empty list. A corrupt config file must never crash the agent list.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        items = raw.replace(",", " ").split()
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        return []
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def _parse_bool(raw) -> Optional[bool]:
    """Coerce a config value to a bool. Returns None when it says nothing.

    Handles JSON booleans *and* the string forms ("false", "0", "off") that land
    in state.json via hand edits or shell-templated JSON — a naive ``bool()``
    would read the string "false" as True and silently switch K8s on.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if not isinstance(raw, str):
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return None


def resolve_k8s_config(enabled: Optional[bool] = None, namespaces: str = "") -> K8sConfig:
    """Resolve whether the K8s backend is active, and against which namespaces.

    Precedence: **CLI > env (``AGENTICORE_K8S_ENABLED``) > ``state.json`` > off.**

    Naming a namespace *on the command line* is a CLI-level request for the K8s
    backend and therefore implies ``--k8s`` — it outranks an ambient
    ``AGENTICORE_K8S_ENABLED=false`` sitting in a shell rc, exactly as ``--k8s``
    itself would. Namespaces coming from the env or from state.json do **not**
    imply it: ambient config must never resurrect Kubernetes for an operator who
    never asked for it. An explicit ``--no-k8s`` always wins.
    """
    state_k8s = _read_state().get("k8s")
    if not isinstance(state_k8s, dict):
        state_k8s = {}

    cli_ns = _parse_namespaces(namespaces)

    if enabled is not None:
        resolved = bool(enabled)  # explicit --k8s / --no-k8s
    elif cli_ns:
        resolved = True  # --namespace is a CLI signal; it outranks the env
    else:
        env_enabled = _parse_bool(os.environ.get("AGENTICORE_K8S_ENABLED", ""))
        state_enabled = _parse_bool(state_k8s.get("enabled"))
        if env_enabled is not None:
            resolved = env_enabled
        elif state_enabled is not None:
            resolved = state_enabled
        else:
            resolved = False

    ns = (
        cli_ns
        or _parse_namespaces(os.environ.get("AGENTICORE_K8S_NAMESPACES", ""))
        or _parse_namespaces(state_k8s.get("namespaces"))
    )
    return K8sConfig(enabled=resolved, namespaces=ns)


# ── K8s discovery (only reachable when K8sConfig.enabled) ─────────────────────


def _kubectl_get_pods(scope: list[str]) -> list[dict]:
    """Run one `kubectl get pods` against a namespace scope. Never raises.

    OSError covers a missing kubectl (FileNotFoundError) and a present-but-not-
    executable one (PermissionError) alike — neither may take down the TUI.
    """
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", *scope, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except OSError:
        return []
    except subprocess.TimeoutExpired:
        return []

    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    return data.get("items", [])


def discover_pods(k8s: Optional[K8sConfig] = None) -> list[AgenticorePod]:
    """Discover agenticore pods — **only** when the K8s backend is enabled.

    A pod is an agenticore pod when one of its containers exposes the
    AGENTICORE_TRANSPORT env var. Namespace is taken from the pod's own
    metadata so callers dispatch kubectl against the right namespace.

    With no namespaces configured, scope is all-namespaces. Otherwise each
    namespace is queried separately (kubectl honours only the last ``-n``).
    """
    if k8s is None:
        k8s = resolve_k8s_config()
    if not k8s.enabled:
        return []

    scopes = [["-n", ns] for ns in k8s.namespaces] or [["--all-namespaces"]]

    pods = []
    seen: set[tuple[str, str]] = set()
    for scope in scopes:
        for item in _kubectl_get_pods(scope):
            meta = item.get("metadata", {})
            name = meta.get("name", "")
            namespace = meta.get("namespace", "")
            if (namespace, name) in seen:
                continue
            phase = item.get("status", {}).get("phase", "Unknown")
            containers = item.get("spec", {}).get("containers", [])

            for container in containers:
                envs = {}
                for e in container.get("env", []):
                    envs[e["name"]] = e.get("value", "")

                if "AGENTICORE_TRANSPORT" not in envs:
                    continue

                seen.add((namespace, name))
                pods.append(
                    AgenticorePod(
                        name=name,
                        namespace=namespace,
                        phase=phase,
                        agent_mode=envs.get("AGENT_MODE", "").lower() == "true",
                        agent_name=envs.get("AGENTIHUB_AGENT", ""),
                        port=envs.get("AGENTICORE_PORT", "8200"),
                        container=container.get("name", "agenticore"),
                    )
                )
                break

    return pods


def _namespaces_summary(pods: list[AgenticorePod], k8s: K8sConfig) -> str:
    """Human-readable summary of the namespace scope actually in use."""
    seen = sorted({p.namespace for p in pods if p.namespace})
    if seen:
        return ", ".join(seen)
    if k8s.namespaces:
        return f"{', '.join(k8s.namespaces)} (no pods)"
    return "all-namespaces (no pods)"


# ── Local agent discovery ─────────────────────────────────────────────────────


def _resolve_agentihub_dir(agentihub_dir: str = "") -> Optional[Path]:
    if agentihub_dir:
        p = Path(agentihub_dir)
        if p.is_dir():
            return p
    env = os.environ.get("AGENTIHUB_DIR", "")
    if env:
        p = Path(env)
        if p.is_dir():
            return p
    # Read from state.json
    state = _read_state()
    state_hub = state.get("agentihub", {}).get("path", "")
    if state_hub:
        p = Path(state_hub)
        if p.is_dir():
            return p
    # Sibling-discovery fallback: walk up from this file looking for an
    # `agentihub/agents/` next to us. Survives ecosystem dir renames.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "agentihub"
        if (candidate / "agents").is_dir():
            return candidate
        # Also check siblings of intermediate parents (flat ecosystem layout).
        for sibling in parent.glob("*/agentihub/agents"):
            return sibling.parent
    return None


def read_package_manifest(package_path) -> dict:
    """Read ``package/command.yml`` -> {description, capabilities}.

    Best-effort by design: a missing or malformed manifest yields ``{}``.
    Authoring a package must never be able to break agent discovery.
    """
    manifest = Path(package_path) / "command.yml"
    try:
        raw = yaml.safe_load(manifest.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(raw, dict):
        return {}

    out: dict = {}
    desc = raw.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()
    caps = raw.get("capabilities")
    if isinstance(caps, list):
        out["capabilities"] = [str(c).strip() for c in caps if str(c).strip()]
    return out


def discover_local_agents(agentihub_dir: str = "") -> list[LocalAgent]:
    hub = _resolve_agentihub_dir(agentihub_dir)
    if not hub:
        return []

    agents_dir = hub / "agents"
    if not agents_dir.is_dir():
        return []

    agents = []
    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir():
            continue
        package_path = entry / "package"
        # An agent is any dir containing package/CLAUDE.md.
        if not (package_path / "CLAUDE.md").is_file():
            continue
        manifest = read_package_manifest(package_path)
        agents.append(
            LocalAgent(
                name=entry.name,
                package_path=str(package_path),
                description=manifest.get("description", ""),
                capabilities=manifest.get("capabilities", []),
            )
        )

    return agents


def _resolve_local_agent(name: str, agentihub_dir: str = "") -> Optional[LocalAgent]:
    for a in discover_local_agents(agentihub_dir):
        if a.name == name:
            return a
    return None


def _resolve_pod(pod_name: str, k8s: Optional[K8sConfig] = None) -> Optional[AgenticorePod]:
    pods = discover_pods(k8s)
    for p in pods:
        if p.name == pod_name:
            return p
    return None


# ── Pod actions (shared by both modes) ────────────────────────────────────────


def _kubectl_exec_curl(pod: AgenticorePod, method: str, path: str, body: Optional[dict] = None) -> dict:
    cmd = [
        "kubectl",
        "exec",
        "-n",
        pod.namespace,
        pod.name,
        "-c",
        pod.container,
        "--",
        "curl",
        "-s",
        "-X",
        method,
        f"http://localhost:{pod.port}{path}",
        "-H",
        "Content-Type: application/json",
    ]
    if body:
        cmd.extend(["-d", json.dumps(body)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or f"exit code {result.returncode}"}
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout.strip()}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}


def _kubectl_exec_sync(pod: AgenticorePod) -> dict:
    try:
        result = subprocess.run(
            [
                "kubectl",
                "exec",
                "-n",
                pod.namespace,
                pod.name,
                "-c",
                pod.container,
                "--",
                "agenticore",
                "hooks",
                "sync",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Headless mode ─────────────────────────────────────────────────────────────


def _headless_output(data, exit_code: int = 0):
    print(json.dumps(data, indent=2, default=str))
    sys.exit(exit_code)


def _headless_error(msg: str, exit_code: int = 1):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(exit_code)


def _headless_require_pod(pod_name: Optional[str], k8s: K8sConfig) -> AgenticorePod:
    if not k8s.enabled:
        _headless_error(f"'{pod_name or 'pod'}' is a Kubernetes pod. {K8S_DISABLED_HINT}", exit_code=2)
    if not pod_name:
        _headless_error("--pod is required", exit_code=2)
    pod = _resolve_pod(pod_name, k8s)
    if not pod:
        _headless_error(f"Pod '{pod_name}' not found or not an agenticore pod")
    return pod


def headless_list(agentihub_dir: str = "", k8s: Optional[K8sConfig] = None):
    if k8s is None:
        k8s = resolve_k8s_config()
    pods = discover_pods(k8s)
    local_agents = discover_local_agents(agentihub_dir)
    hub = _resolve_agentihub_dir(agentihub_dir)
    _headless_output(
        {
            "k8s": {
                "enabled": k8s.enabled,
                "namespaces": sorted({p.namespace for p in pods if p.namespace}),
            },
            "agentihub": str(hub) if hub else None,
            "pods": [asdict(p) for p in pods],
            "local_agents": [asdict(a) for a in local_agents],
        }
    )


def headless_chat(pod_name: str, message: str, wait: bool = True, k8s: Optional[K8sConfig] = None):
    pod = _headless_require_pod(pod_name, k8s or resolve_k8s_config())
    if not pod.agent_mode:
        _headless_error(f"Pod '{pod_name}' is not in agent mode — use 'job' instead")

    resp = _kubectl_exec_curl(
        pod,
        "POST",
        "/completions",
        {
            "message": message,
            "uuid": str(uuid.uuid4()),
            "wait": wait,
        },
    )
    _headless_output(resp, exit_code=1 if "error" in resp else 0)


def headless_job(pod_name: str, task: str, repo: str = "", k8s: Optional[K8sConfig] = None):
    pod = _headless_require_pod(pod_name, k8s or resolve_k8s_config())

    body: dict = {"task": task, "wait": False}
    if repo:
        body["repo_url"] = repo

    resp = _kubectl_exec_curl(pod, "POST", "/jobs", body)
    _headless_output(resp, exit_code=1 if "error" in resp else 0)


def headless_sync(pod_name: str, k8s: Optional[K8sConfig] = None):
    pod = _headless_require_pod(pod_name, k8s or resolve_k8s_config())
    resp = _kubectl_exec_sync(pod)
    _headless_output(resp, exit_code=0 if resp.get("success") else 1)


def headless_health(pod_name: str, k8s: Optional[K8sConfig] = None):
    pod = _headless_require_pod(pod_name, k8s or resolve_k8s_config())
    resp = _kubectl_exec_curl(pod, "GET", "/health")
    _headless_output(resp, exit_code=1 if "error" in resp else 0)


# ── Interactive actions ───────────────────────────────────────────────────────


def _action_chat(t, pod: AgenticorePod):
    _write(t, "")
    message = _prompt_multiline(t, "Enter message for the agent:")
    if not message:
        return

    _write(t, f"\n  {YL}Sending to {pod.name}...{R} ", end="")
    t.flush()

    result_box: list = []
    done = threading.Event()

    def _run():
        result_box.append(
            _kubectl_exec_curl(
                pod,
                "POST",
                "/completions",
                {
                    "message": message,
                    "uuid": str(uuid.uuid4()),
                    "wait": True,
                },
            )
        )
        done.set()

    threading.Thread(target=_run, daemon=True).start()

    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    elapsed = 0
    idx = 0
    while not done.wait(0.1):
        elapsed += 0.1
        secs = int(elapsed)
        t.write(f"\r  {YL}{frames[idx]} Waiting for response... {secs}s{R}   ")
        t.flush()
        idx = (idx + 1) % len(frames)

    t.write(f"\r  {GRB}✓ Response received ({int(elapsed)}s){R}       \n")
    t.flush()

    resp = result_box[0] if result_box else {"error": "no response"}

    if "error" in resp:
        _write(t, f"  {RDB}Error:{R} {resp['error']}")
    else:
        result_text = resp.get("result", resp.get("output", json.dumps(resp, indent=2)))
        _write(t, f"  {GRB}Response:{R}")
        for line in str(result_text).splitlines():
            _write(t, f"  {line}")

    _write(t, f"\n  {LG}(press Enter){R}", end="")
    t.flush()
    _prompt(t)


def _action_job(t, pod: AgenticorePod):
    _write(t, "")
    _write(t, f"  {LG}Task:{R}")
    task = _prompt_multiline(t, "Enter task description:")
    if not task:
        return

    _write(t, f"  {LG}Repo URL (empty to skip):{R}")
    repo = _prompt(t).strip()

    _write(t, f"\n  {YL}Submitting job to {pod.name}...{R}")
    t.flush()

    body: dict = {"task": task, "wait": False}
    if repo:
        body["repo_url"] = repo

    resp = _kubectl_exec_curl(pod, "POST", "/jobs", body)

    _write(t, "")
    if "error" in resp:
        _write(t, f"  {RDB}Error:{R} {resp['error']}")
    else:
        job = resp.get("job", resp)
        _write(t, f"  {GRB}Job submitted:{R} {job.get('id', 'unknown')}")
        _write(t, f"  {LG}Status:{R} {job.get('status', 'unknown')}")

    _write(t, f"\n  {LG}(press Enter){R}", end="")
    t.flush()
    _prompt(t)


def _action_sync(t, pod: AgenticorePod):
    _write(t, f"\n  {YL}Syncing repos on {pod.name}...{R}")
    t.flush()

    resp = _kubectl_exec_sync(pod)

    _write(t, "")
    if resp.get("success"):
        for line in resp.get("stdout", "").splitlines():
            _write(t, f"  {GR}{line}{R}")
    if resp.get("stderr"):
        for line in resp["stderr"].splitlines():
            _write(t, f"  {YL}{line}{R}")
    if resp.get("error"):
        _write(t, f"  {RDB}Error:{R} {resp['error']}")

    _write(t, f"\n  {LG}(press Enter){R}", end="")
    t.flush()
    _prompt(t)


def _action_health(t, pod: AgenticorePod):
    _write(t, f"\n  {YL}Checking health on {pod.name}...{R}")
    t.flush()

    resp = _kubectl_exec_curl(pod, "GET", "/health")

    _write(t, "")
    if "error" in resp:
        _write(t, f"  {RDB}Error:{R} {resp['error']}")
    else:
        for k, v in resp.items():
            _write(t, f"  {LG}{k}:{R} {v}")

    _write(t, f"\n  {LG}(press Enter){R}", end="")
    t.flush()
    _prompt(t)


def _action_exec(pod: AgenticorePod):
    os.execvp(
        "kubectl",
        ["kubectl", "exec", "-it", "-n", pod.namespace, pod.name, "-c", pod.container, "--", "bash"],
    )


def _action_logs(pod: AgenticorePod):
    os.execvp(
        "kubectl",
        ["kubectl", "logs", "-f", "-n", pod.namespace, pod.name, "-c", pod.container],
    )


# ── TUI screens ──────────────────────────────────────────────────────────────


def _render_header(t, k8s: K8sConfig, namespace: str = "", agentihub: Optional[Path] = None):
    W = 50
    badge = f"{YL}K8S{R}" if k8s.enabled else f"{GR}LOCAL{R}"
    _write(t, f"  {GRB}{'━' * W}{R}")
    _write(t, f"  {GRB}  ◆ Agenticore Agents{R}  {badge}")
    if k8s.enabled:
        _write(t, f"  {LG}  namespaces: {namespace}{R}")
    hub_label = str(agentihub) if agentihub else f"{RDB}<unset — press 'c'>{LG}"
    _write(t, f"  {LG}  agentihub:  {hub_label}{R}")
    _write(t, f"  {GRB}{'━' * W}{R}")
    _write(t, "")


def _local_matches(a: LocalAgent, needle: str) -> bool:
    haystack = " ".join([a.name, a.description, *a.capabilities]).lower()
    return needle in haystack


def _render_list(
    t, pods: list[AgenticorePod], local_agents: list[LocalAgent], filter_str: str = ""
) -> tuple[list, list]:
    needle = filter_str.lower()
    filtered_pods = (
        [p for p in pods if needle in p.name.lower() or needle in p.agent_name.lower()] if filter_str else pods
    )
    filtered_local = [a for a in local_agents if _local_matches(a, needle)] if filter_str else local_agents

    if not filtered_pods and not filtered_local:
        _write(t, f"  {RDB}No agents found{R}" if not filter_str else f"  {RDB}No matches for '{filter_str}'{R}")
    else:
        idx = 1
        for p in filtered_pods:
            if p.agent_mode:
                kind_plain = f"agent:{p.agent_name}"
                kind_col = f"{CY}{kind_plain:<30}{R}"
            else:
                kind_col = f"{BL}{'orchestrator':<30}{R}"
            phase_color = GR if p.phase == "Running" else YL if p.phase == "Pending" else RD
            ns_label = p.namespace or "—"
            _write(
                t, f"  {GRB}[{idx}]{R}  {LGB}{p.name:<30}{R} {kind_col} {phase_color}{p.phase:<12}{R} {YL}{ns_label}{R}"
            )
            idx += 1

        if filtered_pods and filtered_local:
            _write(t, "")

        for a in filtered_local:
            desc = a.description or "—"
            if len(desc) > 44:
                desc = desc[:43] + "…"
            _write(t, f"  {GRB}[{idx}]{R}  {LGB}{a.name:<24}{R} {LG}{desc:<45}{R} {GR}LOCAL{R}")
            idx += 1

    _write(t, "")
    return filtered_pods, filtered_local


def _render_footer(t, k8s: K8sConfig, filter_str: str = ""):
    W = 50
    k8s_label = f"{GR}on{R}" if k8s.enabled else f"{LG}off{R}"
    _write(t, f"  {LG}{'─' * W}{R}")
    if filter_str:
        _write(t, f"  {YL}filter: {GRB}'{filter_str}'{R}  {LG}│  clear: /{R}")
    _write(
        t,
        f"  {LG}filter: {LGB}/word{R}  {LG}│  select: {LGB}1-N{R}  {LG}│  agentihub: {LGB}c{R}"
        f"  {LG}│  k8s ({k8s_label}{LG}): {LGB}k{R}  {LG}│  refresh: {LGB}r{R}  {LG}│  quit: {RDB}q{R}",
    )
    _write(t, "")


def _action_menu(t, pod: AgenticorePod, local_agents: list[LocalAgent] = None) -> bool:
    # Find matching local agent package for Live Chat
    local_match = None
    if pod.agent_name and local_agents:
        for a in local_agents:
            if a.name == pod.agent_name:
                local_match = a
                break

    while True:
        _clear(t)
        W = 50
        _write(t, f"  {GRB}{'━' * W}{R}")
        kind = f"agent: {pod.agent_name}" if pod.agent_mode else "orchestrator"
        _write(t, f"  {LG}Selected  {GRB}▶{R}  {LGB}{pod.name}{R}  {LG}({kind}){R}  {YL}K8S{R}")
        _write(t, f"  {GRB}{'━' * W}{R}")
        _write(t, "")

        if pod.agent_mode:
            _write(t, f"  {GRB}[1]{R}  {LGB}Remote Chat{R}  {LG}← POST /completions{R}")
        else:
            _write(t, f"  {GRB}[1]{R}  {LGB}Submit job{R}  {LG}← POST /jobs{R}")

        next_idx = 2
        if local_match:
            _write(t, f"  {GRB}[{next_idx}]{R}  {LGB}Live Chat{R}  {LG}← claude (local){R}")
            next_idx += 1

        _write(t, f"  {GRB}[{next_idx}]{R}  {LGB}Sync repos{R}  {LG}← agenticore hooks sync{R}")
        _write(t, f"  {GRB}[{next_idx + 1}]{R}  {LGB}Exec shell{R}  {LG}← kubectl exec -it{R}")
        _write(t, f"  {GRB}[{next_idx + 2}]{R}  {LGB}Logs{R}  {LG}← kubectl logs -f{R}")
        _write(t, f"  {GRB}[{next_idx + 3}]{R}  {LGB}Health{R}  {LG}← GET /health{R}")
        _write(t, "")
        _write(t, f"  {LG}[b]{R}  {LG}Back{R}   {RDB}[q]{R}  {RD}Quit{R}")
        _write(t, "")

        choice = _prompt(t).strip().lower()

        if choice == "q":
            return True
        if choice == "b":
            return False

        try:
            num = int(choice)
        except ValueError:
            continue

        if num == 1:
            if pod.agent_mode:
                _action_chat(t, pod)
            else:
                _action_job(t, pod)
        elif num == 2 and local_match:
            t.close()
            os.execvp(
                "kubectl",
                [
                    "kubectl",
                    "exec",
                    "-it",
                    "-n",
                    pod.namespace,
                    pod.name,
                    "-c",
                    pod.container,
                    "--",
                    "bash",
                    "-ic",
                    "anton",
                ],
            )
        else:
            # Adjust for optional Live Chat slot
            adjusted = num - (1 if local_match else 0)
            if adjusted == 2:
                _action_sync(t, pod)
            elif adjusted == 3:
                t.close()
                _action_exec(pod)
            elif adjusted == 4:
                t.close()
                _action_logs(pod)
            elif adjusted == 5:
                _action_health(t, pod)


# ── Main entrypoints ─────────────────────────────────────────────────────────


def _build_claude_cmd(agent: LocalAgent) -> list[str]:
    """Local interactive chat — spawns plain ``claude`` from the package
    directory. Runtime flags (model, max_turns, permission_mode, …) come
    from the agentihooks profile that ``agentihooks init`` already
    provisioned into ``~/.claude/`` for this package."""
    return ["claude"]


def _local_action_menu(t, agent: LocalAgent) -> bool:
    claude_cmd = _build_claude_cmd(agent)

    while True:
        _clear(t)
        W = 50
        _write(t, f"  {GRB}{'━' * W}{R}")
        _write(t, f"  {LG}Selected  {GRB}▶{R}  {LGB}{agent.name}{R}  {GR}LOCAL{R}")
        if agent.description:
            _write(t, f"  {LG}{agent.description}{R}")
        if agent.capabilities:
            _write(t, f"  {LG}capabilities: {CY}{', '.join(agent.capabilities)}{R}")
        _write(t, f"  {GRB}{'━' * W}{R}")
        _write(t, "")
        _write(t, f"  {GRB}[Enter]{R} {LGB}Enter Agent Dir{R}  {LG}← cd {agent.package_path}{R}")
        _write(t, f"  {GRB}[1]{R}  {LGB}Open Chat{R}  {LG}← claude (local){R}")
        _write(t, f"  {GRB}[2]{R}  {LGB}Open in VS Code{R}  {LG}← code <package>{R}")
        _write(t, f"  {GRB}[3]{R}  {LGB}View CLAUDE.md{R}  {LG}← package/CLAUDE.md{R}")
        _write(t, "")
        _write(t, f"  {LG}[b]{R}  {LG}Back{R}   {RDB}[q]{R}  {RD}Quit{R}")
        _write(t, "")

        choice = _prompt(t).strip().lower()

        if choice == "q":
            return True
        if choice == "b":
            return False
        if choice == "":
            t.close()
            os.chdir(agent.package_path)
            os.execlp("bash", "bash")
        elif choice == "1":
            t.close()
            os.chdir(agent.package_path)
            os.execvp(claude_cmd[0], claude_cmd)
        elif choice == "2":
            subprocess.Popen(
                ["code", agent.package_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _write(t, f"\n  {GRB}✔  VS Code opening:{R}  {LGB}{agent.name}{R}")
            _write(t, f"  {LG}(press Enter){R}", end="")
            t.flush()
            _prompt(t)
        elif choice == "3":
            claude_md = Path(agent.package_path) / "CLAUDE.md"
            _write(t, "")
            if claude_md.exists():
                for line in claude_md.read_text().splitlines():
                    _write(t, f"  {LG}{line}{R}")
            else:
                _write(t, f"  {RDB}CLAUDE.md not found{R}")
            _write(t, f"\n  {LG}(press Enter){R}", end="")
            t.flush()
            _prompt(t)


def _action_set_agentihub(t) -> Optional[Path]:
    """Prompt the operator for an agentihub directory and persist it.

    Returns the validated path on success, or None if the operator cancelled
    or supplied an invalid path. The new value is written to state.json so
    subsequent runs pick it up automatically.
    """
    _write(t, "")
    _write(t, f"  {LG}Set agentihub directory{R}")
    _write(t, f"  {LG}(absolute path containing an `agents/` subdir; empty to cancel){R}")
    raw = _prompt(t).strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_dir():
        _write(t, f"  {RDB}Not a directory: {candidate}{R}  (press Enter)")
        t.flush()
        _prompt(t)
        return None
    if not (candidate / "agents").is_dir():
        _write(t, f"  {RDB}No agents/ subdirectory under: {candidate}{R}  (press Enter)")
        t.flush()
        _prompt(t)
        return None
    _write_agentihub_to_state(candidate)
    _write(t, f"  {GR}Saved to ~/.agenticore/state.json{R}  (press Enter)")
    t.flush()
    _prompt(t)
    return candidate


def _action_toggle_k8s(t, k8s: K8sConfig) -> K8sConfig:
    """Toggle the K8s backend on/off and persist the choice to state.json.

    Turning it on prompts for an optional namespace scope (empty =
    all-namespaces). Turning it off drops every kubectl code path — the TUI
    goes back to local agents only.
    """
    if k8s.enabled:
        # namespaces=None: keep the persisted scope. k8s.namespaces may have come
        # from a --namespace flag or the env, and turning the backend off must not
        # overwrite what the operator saved.
        _write_k8s_to_state(False, None)
        _write(t, f"\n  {LG}Kubernetes backend {RDB}disabled{LG} — local agents only.{R}  (press Enter)")
        t.flush()
        _prompt(t)
        return K8sConfig(enabled=False, namespaces=k8s.namespaces)

    _write(t, "")
    _write(t, f"  {LG}Enable Kubernetes backend{R}")
    current = ", ".join(k8s.namespaces) if k8s.namespaces else "all-namespaces"
    _write(t, f"  {LG}Namespaces (comma-separated; empty = all-namespaces). Current: {current}{R}")
    raw = _prompt(t).strip()
    namespaces = _parse_namespaces(raw) if raw else k8s.namespaces

    _write_k8s_to_state(True, namespaces)
    scope = ", ".join(namespaces) if namespaces else "all-namespaces"
    _write(t, f"  {GR}Kubernetes enabled{R} {LG}({scope}) — saved to ~/.agenticore/state.json{R}  (press Enter)")
    t.flush()
    _prompt(t)
    return K8sConfig(enabled=True, namespaces=namespaces)


def main_interactive(agentihub_dir: str = "", k8s: Optional[K8sConfig] = None):
    if k8s is None:
        k8s = resolve_k8s_config()
    pods = discover_pods(k8s)
    local_agents = discover_local_agents(agentihub_dir)
    hub_path = _resolve_agentihub_dir(agentihub_dir)
    filter_str = ""

    t = open("/dev/tty", "w")

    while True:
        _clear(t)
        _render_header(t, k8s, _namespaces_summary(pods, k8s), hub_path)
        filtered_pods, filtered_local = _render_list(t, pods, local_agents, filter_str)
        _render_footer(t, k8s, filter_str)

        raw = _prompt(t).strip()

        if not raw or raw.lower() == "q":
            break

        if raw.lower() == "r":
            _write(t, f"\n  {YL}Refreshing...{R}")
            t.flush()
            pods = discover_pods(k8s)
            local_agents = discover_local_agents(agentihub_dir)
            hub_path = _resolve_agentihub_dir(agentihub_dir)
            continue

        if raw.lower() == "k":
            k8s = _action_toggle_k8s(t, k8s)
            pods = discover_pods(k8s)
            continue

        if raw.lower() == "c":
            new_hub = _action_set_agentihub(t)
            if new_hub is not None:
                # Operator-supplied path takes precedence over CLI flag for this session.
                agentihub_dir = str(new_hub)
            local_agents = discover_local_agents(agentihub_dir)
            hub_path = _resolve_agentihub_dir(agentihub_dir)
            continue

        if raw.startswith("/"):
            filter_str = raw[1:].strip()
            continue

        try:
            num = int(raw)
        except ValueError:
            continue

        total_pods = len(filtered_pods)
        total = total_pods + len(filtered_local)

        if 1 <= num <= total:
            if num <= total_pods:
                should_quit = _action_menu(t, filtered_pods[num - 1], local_agents)
            else:
                should_quit = _local_action_menu(t, filtered_local[num - 1 - total_pods])
            if should_quit:
                break
            pods = discover_pods(k8s)
            local_agents = discover_local_agents(agentihub_dir)
            hub_path = _resolve_agentihub_dir(agentihub_dir)

    t.close()


def headless_local(agent_name: str, agentihub_dir: str = ""):
    agent = _resolve_local_agent(agent_name, agentihub_dir)
    if not agent:
        _headless_error(f"Local agent '{agent_name}' not found")
    _headless_output(asdict(agent))


def main(headless: bool = False, action: str = "", **kwargs):
    agentihub_dir = kwargs.get("agentihub_dir", "")
    k8s = resolve_k8s_config(
        enabled=kwargs.get("k8s"),
        namespaces=kwargs.get("namespace", ""),
    )

    if not headless:
        main_interactive(agentihub_dir, k8s)
        return

    if not action or action == "list":
        headless_list(agentihub_dir, k8s)
    elif action == "chat":
        if not kwargs.get("message"):
            _headless_error("--message is required for chat", exit_code=2)
        headless_chat(
            pod_name=kwargs.get("pod", ""),
            message=kwargs["message"],
            wait=kwargs.get("wait", True),
            k8s=k8s,
        )
    elif action == "job":
        if not kwargs.get("task"):
            _headless_error("--task is required for job", exit_code=2)
        headless_job(
            pod_name=kwargs.get("pod", ""),
            task=kwargs["task"],
            repo=kwargs.get("repo", ""),
            k8s=k8s,
        )
    elif action == "sync":
        headless_sync(pod_name=kwargs.get("pod", ""), k8s=k8s)
    elif action == "health":
        headless_health(pod_name=kwargs.get("pod", ""), k8s=k8s)
    elif action == "local":
        if not kwargs.get("agent"):
            _headless_error("--agent is required for local", exit_code=2)
        headless_local(agent_name=kwargs["agent"], agentihub_dir=agentihub_dir)
    else:
        _headless_error(f"Unknown action '{action}'. Valid: list, chat, job, sync, health, local", exit_code=2)
