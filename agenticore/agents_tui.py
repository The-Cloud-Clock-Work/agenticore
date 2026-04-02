"""Interactive TUI + headless CLI for discovering and managing agenticore pods in Kubernetes.

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
import tty
import uuid
from dataclasses import asdict, dataclass
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


@dataclass
class AgenticorePod:
    name: str
    phase: str
    agent_mode: bool
    agent_name: str
    port: str
    container: str


@dataclass
class LocalAgent:
    name: str
    description: str
    model: str
    effort: str
    package_path: str


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
                t.write("\n")
                t.flush()
                return "q"
            if b in (0x0D, 0x0A):
                t.write("\n")
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


# ── K8s discovery ─────────────────────────────────────────────────────────────


def discover_pods() -> list[AgenticorePod]:
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        return []

    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    pods = []
    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        phase = item.get("status", {}).get("phase", "Unknown")
        containers = item.get("spec", {}).get("containers", [])

        for container in containers:
            envs = {}
            for e in container.get("env", []):
                envs[e["name"]] = e.get("value", "")

            if "AGENTICORE_TRANSPORT" not in envs:
                continue

            pods.append(
                AgenticorePod(
                    name=name,
                    phase=phase,
                    agent_mode=envs.get("AGENT_MODE", "").lower() == "true",
                    agent_name=envs.get("AGENTIHUB_AGENT", ""),
                    port=envs.get("AGENTICORE_PORT", "8200"),
                    container=container.get("name", "agenticore"),
                )
            )
            break

    return pods


def _get_namespace() -> str:
    try:
        result = subprocess.run(
            ["kubectl", "config", "view", "--minify", "-o", "jsonpath={.contexts[0].context.namespace}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "default"
    except Exception:
        return "default"


def _read_state() -> dict:
    state_path = Path.home() / ".agenticore" / "state.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except Exception:
            pass
    return {}


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
    return None


def discover_local_agents(agentihub_dir: str = "") -> list[LocalAgent]:
    hub = _resolve_agentihub_dir(agentihub_dir)
    if not hub:
        return []

    agents_dir = hub / "agents"
    if not agents_dir.is_dir():
        return []

    agents = []
    for agent_yml in sorted(agents_dir.glob("*/agent.yml")):
        try:
            data = yaml.safe_load(agent_yml.read_text())
        except Exception:
            continue

        claude = data.get("claude", {})
        package_path = agent_yml.parent / "package"
        if not package_path.is_dir():
            continue

        agents.append(
            LocalAgent(
                name=data.get("name", agent_yml.parent.name),
                description=data.get("description", ""),
                model=claude.get("model", ""),
                effort=claude.get("effort", ""),
                package_path=str(package_path),
            )
        )

    return agents


def _resolve_local_agent(name: str, agentihub_dir: str = "") -> Optional[LocalAgent]:
    for a in discover_local_agents(agentihub_dir):
        if a.name == name:
            return a
    return None


def _resolve_pod(pod_name: str) -> Optional[AgenticorePod]:
    pods = discover_pods()
    for p in pods:
        if p.name == pod_name:
            return p
    return None


# ── Pod actions (shared by both modes) ────────────────────────────────────────


def _kubectl_exec_curl(pod: AgenticorePod, method: str, path: str, body: Optional[dict] = None) -> dict:
    cmd = [
        "kubectl",
        "exec",
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
            ["kubectl", "exec", pod.name, "-c", pod.container, "--", "agenticore", "hooks", "sync"],
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


def _headless_require_pod(pod_name: Optional[str]) -> AgenticorePod:
    if not pod_name:
        _headless_error("--pod is required", exit_code=2)
    pod = _resolve_pod(pod_name)
    if not pod:
        _headless_error(f"Pod '{pod_name}' not found or not an agenticore pod")
    return pod


def headless_list(agentihub_dir: str = ""):
    pods = discover_pods()
    local_agents = discover_local_agents(agentihub_dir)
    _headless_output(
        {
            "namespace": _get_namespace(),
            "pods": [asdict(p) for p in pods],
            "local_agents": [asdict(a) for a in local_agents],
        }
    )


def headless_chat(pod_name: str, message: str, wait: bool = True):
    pod = _headless_require_pod(pod_name)
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


def headless_job(pod_name: str, task: str, repo: str = ""):
    pod = _headless_require_pod(pod_name)

    body: dict = {"task": task, "wait": False}
    if repo:
        body["repo_url"] = repo

    resp = _kubectl_exec_curl(pod, "POST", "/jobs", body)
    _headless_output(resp, exit_code=1 if "error" in resp else 0)


def headless_sync(pod_name: str):
    pod = _headless_require_pod(pod_name)
    resp = _kubectl_exec_sync(pod)
    _headless_output(resp, exit_code=0 if resp.get("success") else 1)


def headless_health(pod_name: str):
    pod = _headless_require_pod(pod_name)
    resp = _kubectl_exec_curl(pod, "GET", "/health")
    _headless_output(resp, exit_code=1 if "error" in resp else 0)


# ── Interactive actions ───────────────────────────────────────────────────────


def _action_chat(t, pod: AgenticorePod):
    _write(t, "")
    message = _prompt_multiline(t, "Enter message for the agent:")
    if not message:
        return

    _write(t, f"\n  {YL}Sending to {pod.name}...{R}")
    t.flush()

    resp = _kubectl_exec_curl(
        pod,
        "POST",
        "/completions",
        {
            "message": message,
            "uuid": str(uuid.uuid4()),
            "wait": True,
        },
    )

    _write(t, "")
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
    os.execvp("kubectl", ["kubectl", "exec", "-it", pod.name, "-c", pod.container, "--", "bash"])


def _action_logs(pod: AgenticorePod):
    os.execvp("kubectl", ["kubectl", "logs", "-f", pod.name, "-c", pod.container])


# ── TUI screens ──────────────────────────────────────────────────────────────


def _render_header(t, namespace: str):
    W = 50
    _write(t, f"  {GRB}{'━' * W}{R}")
    _write(t, f"  {GRB}  ◆ Agenticore Agents{R}  {YL}K8S{R}")
    _write(t, f"  {LG}  namespace: {namespace}{R}")
    _write(t, f"  {GRB}{'━' * W}{R}")
    _write(t, "")


def _render_list(
    t, pods: list[AgenticorePod], local_agents: list[LocalAgent], filter_str: str = ""
) -> tuple[list, list]:
    filtered_pods = (
        [p for p in pods if filter_str.lower() in p.name.lower() or filter_str.lower() in p.agent_name.lower()]
        if filter_str
        else pods
    )
    filtered_local = (
        [a for a in local_agents if filter_str.lower() in a.name.lower() or filter_str.lower() in a.description.lower()]
        if filter_str
        else local_agents
    )

    if not filtered_pods and not filtered_local:
        _write(t, f"  {RDB}No agents found{R}" if not filter_str else f"  {RDB}No matches for '{filter_str}'{R}")
    else:
        idx = 1
        for p in filtered_pods:
            if p.agent_mode:
                kind = f"{CY}agent:{p.agent_name}{R}"
            else:
                kind = f"{BL}orchestrator{R}"
            phase_color = GR if p.phase == "Running" else YL if p.phase == "Pending" else RD
            _write(t, f"  {GRB}[{idx}]{R}  {LGB}{p.name:<30}{R} {kind:<30} {phase_color}{p.phase:<12}{R} {YL}K8S{R}")
            idx += 1

        if filtered_pods and filtered_local:
            _write(t, "")

        for a in filtered_local:
            model_str = f"{CY}{a.model}{R}" if a.model else f"{LG}—{R}"
            _write(t, f"  {GRB}[{idx}]{R}  {LGB}{a.name:<30}{R} {model_str:<30} {LG}{'local':<12}{R} {GR}LOCAL{R}")
            idx += 1

    _write(t, "")
    return filtered_pods, filtered_local


def _render_footer(t, filter_str: str = ""):
    W = 50
    _write(t, f"  {LG}{'─' * W}{R}")
    if filter_str:
        _write(t, f"  {YL}filter: {GRB}'{filter_str}'{R}  {LG}│  clear: /{R}")
    _write(
        t, f"  {LG}filter: {LGB}/word{R}  {LG}│  select: {LGB}1-N{R}  {LG}│  refresh: {LGB}r{R}  {LG}│  quit: {RDB}q{R}"
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
            _write(t, f"  {GRB}[{next_idx}]{R}  {LGB}Live Chat{R}  {LG}← --model {local_match.model}{R}")
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
    config_path = Path(agent.package_path).parent / "agent.yml"
    cmd = ["claude"]
    if not config_path.exists():
        return cmd

    try:
        data = yaml.safe_load(config_path.read_text())
    except Exception:
        return cmd

    claude = data.get("claude", {})
    flag_map = {
        "model": "--model",
        "permission_mode": "--permission-mode",
        "max_turns": "--max-turns",
        "output_format": "--output-format",
    }
    for key, flag in flag_map.items():
        val = claude.get(key)
        if val is not None:
            cmd.extend([flag, str(val)])

    # no_session_persistence requires --print mode, skip for interactive use

    return cmd


def _local_action_menu(t, agent: LocalAgent) -> bool:
    claude_cmd = _build_claude_cmd(agent)

    while True:
        _clear(t)
        W = 50
        _write(t, f"  {GRB}{'━' * W}{R}")
        _write(t, f"  {LG}Selected  {GRB}▶{R}  {LGB}{agent.name}{R}  {GR}LOCAL{R}")
        if agent.description:
            _write(t, f"  {LG}  {agent.description}{R}")
        _write(t, f"  {GRB}{'━' * W}{R}")
        _write(t, "")
        _write(t, f"  {GRB}[1]{R}  {LGB}Open Chat{R}  {LG}← --model {agent.model}{R}")
        _write(t, f"  {GRB}[2]{R}  {LGB}Open in VS Code{R}  {LG}← code <package>{R}")
        _write(t, f"  {GRB}[3]{R}  {LGB}View Config{R}  {LG}← agent.yml{R}")
        _write(t, "")
        _write(t, f"  {LG}[b]{R}  {LG}Back{R}   {RDB}[q]{R}  {RD}Quit{R}")
        _write(t, "")

        choice = _prompt(t).strip().lower()

        if choice == "q":
            return True
        if choice == "b":
            return False
        if choice == "1":
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
            config_path = Path(agent.package_path).parent / "agent.yml"
            _write(t, "")
            if config_path.exists():
                for line in config_path.read_text().splitlines():
                    _write(t, f"  {LG}{line}{R}")
            else:
                _write(t, f"  {RDB}agent.yml not found{R}")
            _write(t, f"\n  {LG}(press Enter){R}", end="")
            t.flush()
            _prompt(t)


def main_interactive(agentihub_dir: str = ""):
    namespace = _get_namespace()
    pods = discover_pods()
    local_agents = discover_local_agents(agentihub_dir)
    filter_str = ""

    t = open("/dev/tty", "w")

    while True:
        _clear(t)
        _render_header(t, namespace)
        filtered_pods, filtered_local = _render_list(t, pods, local_agents, filter_str)
        _render_footer(t, filter_str)

        raw = _prompt(t).strip()

        if not raw or raw.lower() == "q":
            break

        if raw.lower() == "r":
            _write(t, f"\n  {YL}Refreshing...{R}")
            t.flush()
            pods = discover_pods()
            local_agents = discover_local_agents(agentihub_dir)
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
            pods = discover_pods()
            local_agents = discover_local_agents(agentihub_dir)

    t.close()


def headless_local(agent_name: str, agentihub_dir: str = ""):
    agent = _resolve_local_agent(agent_name, agentihub_dir)
    if not agent:
        _headless_error(f"Local agent '{agent_name}' not found")
    _headless_output(asdict(agent))


def main(headless: bool = False, action: str = "", **kwargs):
    agentihub_dir = kwargs.get("agentihub_dir", "")

    if not headless:
        main_interactive(agentihub_dir)
        return

    if not action or action == "list":
        headless_list(agentihub_dir)
    elif action == "chat":
        if not kwargs.get("message"):
            _headless_error("--message is required for chat", exit_code=2)
        headless_chat(
            pod_name=kwargs.get("pod", ""),
            message=kwargs["message"],
            wait=kwargs.get("wait", True),
        )
    elif action == "job":
        if not kwargs.get("task"):
            _headless_error("--task is required for job", exit_code=2)
        headless_job(
            pod_name=kwargs.get("pod", ""),
            task=kwargs["task"],
            repo=kwargs.get("repo", ""),
        )
    elif action == "sync":
        headless_sync(pod_name=kwargs.get("pod", ""))
    elif action == "health":
        headless_health(pod_name=kwargs.get("pod", ""))
    elif action == "local":
        if not kwargs.get("agent"):
            _headless_error("--agent is required for local", exit_code=2)
        headless_local(agent_name=kwargs["agent"], agentihub_dir=agentihub_dir)
    else:
        _headless_error(f"Unknown action '{action}'. Valid: list, chat, job, sync, health, local", exit_code=2)
