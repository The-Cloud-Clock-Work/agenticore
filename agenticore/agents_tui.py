"""Interactive TUI for discovering and managing agenticore pods in Kubernetes."""

import json
import os
import subprocess
import termios
import tty
import uuid
from dataclasses import dataclass
from typing import Optional

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


# ── TTY helpers ───────────────────────────────────────────────────────────────

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
    """Prompt for multi-line input. Empty line submits."""
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

            pods.append(AgenticorePod(
                name=name,
                phase=phase,
                agent_mode=envs.get("AGENT_MODE", "").lower() == "true",
                agent_name=envs.get("AGENTIHUB_AGENT", ""),
                port=envs.get("AGENTICORE_PORT", "8200"),
                container=container.get("name", "agenticore"),
            ))
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


# ── Pod actions ───────────────────────────────────────────────────────────────

def _kubectl_exec_curl(pod: AgenticorePod, method: str, path: str, body: Optional[dict] = None) -> dict:
    cmd = [
        "kubectl", "exec", pod.name, "-c", pod.container, "--",
        "curl", "-s", "-X", method,
        f"http://localhost:{pod.port}{path}",
        "-H", "Content-Type: application/json",
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


def _action_chat(t, pod: AgenticorePod):
    _write(t, "")
    message = _prompt_multiline(t, "Enter message for the agent:")
    if not message:
        return

    _write(t, f"\n  {YL}Sending to {pod.name}...{R}")
    t.flush()

    resp = _kubectl_exec_curl(pod, "POST", "/completions", {
        "message": message,
        "uuid": str(uuid.uuid4()),
        "wait": True,
    })

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

    body = {"task": task, "wait": False}
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

    try:
        result = subprocess.run(
            ["kubectl", "exec", pod.name, "-c", pod.container, "--",
             "agenticore", "hooks", "sync"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        _write(t, "")
        for line in result.stdout.strip().splitlines():
            _write(t, f"  {GR}{line}{R}")
        if result.stderr.strip():
            for line in result.stderr.strip().splitlines():
                _write(t, f"  {YL}{line}{R}")
    except subprocess.TimeoutExpired:
        _write(t, f"  {RDB}Timeout{R}")
    except Exception as e:
        _write(t, f"  {RDB}Error:{R} {e}")

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
    _write(t, f"  {GRB}  ◆ Agenticore Agents{R}")
    _write(t, f"  {LG}  namespace: {namespace}{R}")
    _write(t, f"  {GRB}{'━' * W}{R}")
    _write(t, "")


def _render_list(t, pods: list[AgenticorePod], filter_str: str = "") -> list[AgenticorePod]:
    filtered = (
        [p for p in pods if filter_str.lower() in p.name.lower() or filter_str.lower() in p.agent_name.lower()]
        if filter_str else pods
    )

    if not filtered:
        _write(t, f"  {RDB}No agenticore pods found{R}" if not filter_str
               else f"  {RDB}No matches for '{filter_str}'{R}")
    else:
        for i, p in enumerate(filtered, 1):
            if p.agent_mode:
                kind = f"{CY}agent:{p.agent_name}{R}"
            else:
                kind = f"{BL}orchestrator{R}"
            phase_color = GR if p.phase == "Running" else YL if p.phase == "Pending" else RD
            _write(t, f"  {GRB}[{i}]{R}  {LGB}{p.name:<30}{R} {kind:<30} {phase_color}{p.phase}{R}")

    _write(t, "")
    return filtered


def _render_footer(t, filter_str: str = ""):
    W = 50
    _write(t, f"  {LG}{'─' * W}{R}")
    if filter_str:
        _write(t, f"  {YL}filter: {GRB}'{filter_str}'{R}  {LG}│  clear: /{R}")
    _write(t, f"  {LG}filter: {LGB}/word{R}  {LG}│  select: {LGB}1-N{R}  {LG}│  refresh: {LGB}r{R}  {LG}│  quit: {RDB}q{R}")
    _write(t, "")


def _action_menu(t, pod: AgenticorePod) -> bool:
    """Show action submenu. Returns True to quit, False to go back."""
    while True:
        _clear(t)
        W = 50
        _write(t, f"  {GRB}{'━' * W}{R}")
        kind = f"agent: {pod.agent_name}" if pod.agent_mode else "orchestrator"
        _write(t, f"  {LG}Selected  {GRB}▶{R}  {LGB}{pod.name}{R}  {LG}({kind}){R}")
        _write(t, f"  {GRB}{'━' * W}{R}")
        _write(t, "")

        if pod.agent_mode:
            _write(t, f"  {GRB}[1]{R}  {LGB}Chat{R}  {LG}← POST /completions{R}")
        else:
            _write(t, f"  {GRB}[1]{R}  {LGB}Submit job{R}  {LG}← POST /jobs{R}")
        _write(t, f"  {GRB}[2]{R}  {LGB}Sync repos{R}  {LG}← agenticore hooks sync{R}")
        _write(t, f"  {GRB}[3]{R}  {LGB}Exec shell{R}  {LG}← kubectl exec -it{R}")
        _write(t, f"  {GRB}[4]{R}  {LGB}Logs{R}  {LG}← kubectl logs -f{R}")
        _write(t, f"  {GRB}[5]{R}  {LGB}Health{R}  {LG}← GET /health{R}")
        _write(t, "")
        _write(t, f"  {LG}[b]{R}  {LG}Back{R}   {RDB}[q]{R}  {RD}Quit{R}")
        _write(t, "")

        choice = _prompt(t).strip().lower()

        if choice == "q":
            return True
        if choice == "b":
            return False
        if choice == "1":
            if pod.agent_mode:
                _action_chat(t, pod)
            else:
                _action_job(t, pod)
        elif choice == "2":
            _action_sync(t, pod)
        elif choice == "3":
            t.close()
            _action_exec(pod)
        elif choice == "4":
            t.close()
            _action_logs(pod)
        elif choice == "5":
            _action_health(t, pod)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    namespace = _get_namespace()
    pods = discover_pods()
    filter_str = ""

    t = open("/dev/tty", "w")

    while True:
        _clear(t)
        _render_header(t, namespace)
        filtered = _render_list(t, pods, filter_str)
        _render_footer(t, filter_str)

        raw = _prompt(t).strip()

        if not raw or raw.lower() == "q":
            break

        if raw.lower() == "r":
            _write(t, f"\n  {YL}Refreshing...{R}")
            t.flush()
            pods = discover_pods()
            continue

        if raw.startswith("/"):
            filter_str = raw[1:].strip()
            continue

        try:
            num = int(raw)
        except ValueError:
            continue

        if 1 <= num <= len(filtered):
            should_quit = _action_menu(t, filtered[num - 1])
            if should_quit:
                break
            pods = discover_pods()

    t.close()
