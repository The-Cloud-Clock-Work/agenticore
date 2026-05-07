"""Agent mode startup logic.

Called when AGENT_MODE=true. Clones the package repo (if configured),
validates the package directory, and runs startup scripts.
"""

import logging
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from agenticore.config import get_config

_log = logging.getLogger(__name__)


def _provision_from_agentihub(cfg) -> None:
    """Resolve agent package/evaluation dirs from agentihub — reference in place, no copy.

    The hub is already cloned to a deterministic path by sync_agentihub() at startup.
    This function points package_dir and evaluation_dir at the hub checkout directly.
    """
    agent_name = cfg.agent_mode.agent
    if not agent_name:
        _log.debug("AGENTIHUB_AGENT not set — skipping agentihub provision")
        return

    hub_path = cfg.runtime.hub_dir
    if not hub_path:
        _log.warning("agentihub clone not available — sync_agentihub did not populate cfg.runtime.hub_dir")
        return
    agent_dir = hub_path / "agents" / agent_name
    if not agent_dir.exists():
        # Try aliases: check for aliases.yaml in the hub root, or fuzzy-match
        # by checking if any agent dir contains an agent.yml with a matching name
        aliases_file = hub_path / "aliases.yaml"
        resolved = None
        if aliases_file.exists():
            try:
                import yaml

                aliases = yaml.safe_load(aliases_file.read_text()) or {}
                if agent_name in aliases:
                    resolved = hub_path / "agents" / aliases[agent_name]
                    if resolved.exists():
                        _log.info("Resolved agent '%s' → '%s' via aliases.yaml", agent_name, aliases[agent_name])
                        agent_dir = resolved
            except Exception as e:
                _log.warning("Failed to read aliases.yaml: %s", e)

        if resolved is None or not agent_dir.exists():
            available = []
            agents_dir = hub_path / "agents"
            if agents_dir.exists():
                available = [d.name for d in agents_dir.iterdir() if d.is_dir()]
            raise RuntimeError(
                f"Agent '{agent_name}' not found in agentihub at {hub_path / 'agents' / agent_name}. Available: {available}"
            )

    # Point package_dir and evaluation_dir at the hub path — no copy
    src_package = agent_dir / "package"
    if src_package.exists():
        cfg.agent_mode.package_dir = str(src_package)
        _log.info("package_dir → %s (from agentihub, no copy)", src_package)
    else:
        _log.warning("No package/ dir in agent '%s'", agent_name)

    src_eval = agent_dir / "evaluation"
    if src_eval.exists():
        cfg.agent_mode.evaluation_dir = str(src_eval)
        _log.info("evaluation_dir → %s (from agentihub, no copy)", src_eval)

    _log.info("Agentihub provision complete for agent '%s'", agent_name)


def _clone_package_repo(repo_url: str, branch: str, target_dir: str) -> None:
    """Clone the package repo to /app using git subprocess."""
    from agenticore.repos import resolve_github_token
    from agenticore.git_credentials import git_askpass_env

    _log.info("Cloning package repo: %s (branch: %s)", repo_url, branch)

    token = resolve_github_token()
    with git_askpass_env(token) as extra_env:
        env = os.environ.copy()
        env.update(extra_env)

        # Clone to a temp dir, then move contents to target
        # (target_dir may already exist as an empty dir from the image baseline)
        target = Path(target_dir)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, repo_url, str(target / "_clone_tmp")],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Git clone failed: {result.stderr.strip()}")

        # Move contents from clone to target (overwriting existing dirs)
        clone_tmp = target / "_clone_tmp"
        for item in clone_tmp.iterdir():
            if item.name == ".git":
                continue
            dest = target / item.name
            if dest.exists():
                import shutil

                shutil.rmtree(dest)
            item.rename(dest)

        # Clean up temp clone dir
        import shutil

        shutil.rmtree(clone_tmp, ignore_errors=True)

    _log.info("Package repo cloned to %s", target_dir)


def _validate_package_dir(package_dir: str) -> None:
    """Validate that the package directory exists and has basic structure."""
    pkg = Path(package_dir)
    if not pkg.exists():
        raise RuntimeError(f"Package directory does not exist: {package_dir}")

    _log.info("Package directory validated: %s", package_dir)

    # Log what's available
    claude_md = pkg / "CLAUDE.md"
    system_md = pkg / "system.md"
    mcp_json = pkg / ".mcp.json"
    settings = pkg / ".claude" / "settings.json"
    runners = pkg / "runners"

    for path, label in [
        (claude_md, "CLAUDE.md"),
        (system_md, "system.md"),
        (mcp_json, ".mcp.json"),
        (settings, ".claude/settings.json"),
        (runners, "runners/"),
    ]:
        if path.exists():
            _log.info("  [found] %s", label)
        else:
            _log.debug("  [missing] %s", label)


def _run_startup_scripts(package_dir: str) -> None:
    """Run numbered scripts from package/runners/ in order."""
    runners_dir = Path(package_dir) / "runners"
    if not runners_dir.exists():
        _log.debug("No runners/ directory — skipping startup scripts")
        return

    scripts = sorted(runners_dir.iterdir(), key=lambda p: p.name)
    scripts = [s for s in scripts if s.is_file() and not s.name.startswith(".")]

    if not scripts:
        _log.debug("No scripts in runners/")
        return

    _log.info("Running %d startup script(s) from runners/", len(scripts))

    for script in scripts:
        _log.info("  Running: %s", script.name)

        if script.suffix == ".py":
            cmd = [sys.executable, str(script)]
        elif script.suffix == ".sh":
            # Ensure executable bit
            if not os.access(script, os.X_OK):
                script.chmod(script.stat().st_mode | stat.S_IEXEC)
            cmd = ["bash", str(script)]
        else:
            _log.warning("  Skipping unknown script type: %s", script.name)
            continue

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=package_dir)

        if result.stdout:
            _log.info("  stdout: %s", result.stdout.strip()[:500])
        if result.returncode != 0:
            _log.warning(
                "  Script %s exited with code %d: %s", script.name, result.returncode, result.stderr.strip()[:500]
            )
        else:
            _log.info("  Script %s completed successfully", script.name)


def _log_telegram_status() -> None:
    """Log Telegram connector status. Actual startup happens in ASGI lifespan."""
    from agenticore.connectors.telegram import is_enabled

    if is_enabled():
        _log.info("Telegram connector: configured (will start with ASGI server)")
    else:
        _log.info("Telegram: not configured (no TELEGRAM_BOT_TOKEN)")


def _bg_run_startup_scripts(package_dir: str) -> None:
    """Wrapper for background thread — catches all exceptions."""
    try:
        _run_startup_scripts(package_dir)
    except Exception as e:
        _log.warning("Startup scripts error (non-fatal, background): %s", e)


def _resolve_event_relay_script() -> Optional[str]:
    """Locate the agentihooks event_relay.py regardless of install mode.

    The agentihooks PyPI distribution ships its top-level packages as
    ``hooks``, ``profiles``, ``scripts`` (not ``agentihooks.*``), so the
    importable module name is ``hooks.observability.event_relay``. Tries
    the Python import first (works for PyPI / PATH / URL editable install
    — all resolve to a real file via ``__file__``), then falls back to the
    legacy PVC path for backwards compat with clusters still on the
    clone-based setup.
    """
    try:
        import hooks.observability.event_relay as _relay_mod

        path = getattr(_relay_mod, "__file__", None)
        if path and Path(path).exists():
            return str(path)
    except Exception as exc:
        _log.debug("hooks.observability.event_relay import failed: %s", exc)
    legacy = "/shared/agentihooks/hooks/observability/event_relay.py"
    if Path(legacy).exists():
        return legacy
    return None


def _install_event_relay_hook() -> None:
    """Wire the agentihooks event_relay.py script for SSE event streaming.

    Adds PostToolUse + Stop + Notification entries to ~/.claude/settings.json
    pointing at the relay script shipped with the installed agentihooks
    package (PyPI install, editable PATH overlay, or URL-override clone all
    resolve via ``hooks.observability.event_relay.__file__`` — note the
    PyPI distribution exposes its top-level as ``hooks`` not ``agentihooks``).
    The script self-gates on AGENTICORE_EVENT_STREAM=1 so non-streaming
    traffic is a no-op. Idempotent — safe to call multiple times.
    """
    import json

    relay_script = _resolve_event_relay_script()
    if not relay_script:
        _log.info(
            "Event relay script not resolvable (agentihooks import failed and no legacy PVC path) — skipping wire-up"
        )
        return

    claude_home = Path(os.getenv("CLAUDE_CODE_HOME_DIR", str(Path.home())))
    settings_path = claude_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            settings = {}

    hooks = settings.get("hooks", {})
    relay_cmd = f"python3 {relay_script}"

    hook_entries = {
        "PostToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": relay_cmd}]}],
        "Stop": [{"hooks": [{"type": "command", "command": relay_cmd}]}],
        "Notification": [{"hooks": [{"type": "command", "command": relay_cmd}]}],
    }

    # Prune stale event_relay entries (previous boots may have wired a different
    # path — e.g. /shared/agentihooks/... before the PyPI refactor, or
    # site-packages after). ~/.claude/settings.json persists on the shared FS
    # across pod restarts, so a path migration would otherwise double-fire every
    # tool event.
    def _is_event_relay_entry(entry: dict) -> bool:
        for h in entry.get("hooks", []):
            if "event_relay.py" in h.get("command", ""):
                return True
        return False

    for hook_name, entries in hook_entries.items():
        existing = hooks.get(hook_name, [])
        existing = [e for e in existing if not _is_event_relay_entry(e)]
        existing.extend(entries)
        hooks[hook_name] = existing

    settings["hooks"] = hooks
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    _log.info("Event relay hooks wired in settings.json (PostToolUse + Stop + Notification)")


def _run_bootstrap_script() -> None:
    """Run the operator-defined bootstrap script (if any).

    Path comes from ``AGENTICORE_BOOTSTRAP_SCRIPT`` env. Empty/unset → no-op.
    Non-zero exit is logged but never fatal — agent continues to start.

    Use cases: install agent-specific CLI tools (e.g. ``gh`` for memory-mirror
    contributor), mint runtime tokens, prime caches, fix up PVC-persisted
    state that Helm changes don't propagate to automatically.

    Runs after agentihooks init so the script can safely call
    ``agentihooks`` CLI and import the ``hooks`` package.
    """
    script = os.getenv("AGENTICORE_BOOTSTRAP_SCRIPT", "").strip()
    if not script:
        _log.debug("No AGENTICORE_BOOTSTRAP_SCRIPT set — skipping")
        return
    script_path = Path(script)
    if not script_path.exists():
        _log.warning("AGENTICORE_BOOTSTRAP_SCRIPT %s does not exist — skipping", script)
        return
    _log.info("Running bootstrap script: %s", script)
    t0 = time.monotonic()
    try:
        if not os.access(script_path, os.X_OK):
            script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines()[-20:]:
                _log.info("bootstrap: %s", line)
        if result.returncode != 0:
            _log.warning(
                "Bootstrap script exited %d: %s",
                result.returncode,
                (result.stderr or "").strip()[:500],
            )
        else:
            _log.info("Bootstrap script OK (%.2fs)", time.monotonic() - t0)
    except subprocess.TimeoutExpired:
        _log.warning("Bootstrap script timed out after 180s: %s", script)
    except Exception as exc:  # pragma: no cover
        _log.warning("Bootstrap script error: %s", exc)


def _bg_run_bootstrap_script() -> None:
    """Background wrapper for _run_bootstrap_script. Swallows all exceptions."""
    try:
        _run_bootstrap_script()
    except Exception as exc:  # pragma: no cover
        _log.warning("Bootstrap thread crashed: %s", exc)


def _bg_install_event_relay_hook() -> None:
    """Wrapper for background thread — catches all exceptions.

    Polls up to 60s for the relay script to resolve. Under the default PyPI
    install the import succeeds on the first attempt; under the URL-override
    path the clone may still be in flight when this thread starts, so we
    retry every 5s for a minute before giving up.
    """
    import time as _time

    for _ in range(12):
        if _resolve_event_relay_script():
            break
        _time.sleep(5)
    try:
        _install_event_relay_hook()
    except Exception as e:
        _log.warning("Event relay hook install failed (non-fatal, background): %s", e)


def initialize_agent_mode() -> list[threading.Thread]:
    """Main initialization entry point for agent mode.

    Fatal steps (provision, clone, validate) run synchronously.
    Non-fatal steps (startup scripts, notification hooks) run as background daemon threads.

    Returns list of background threads for callers that need to join them (e.g. tests).
    """
    t0 = time.monotonic()
    cfg = get_config()
    am = cfg.agent_mode

    _log.info("=== Agent Mode Initialization ===")

    # Step 0: Provision from agentihub if AGENTIHUB_AGENT is set (FATAL)
    if am.agent:
        try:
            _provision_from_agentihub(cfg)
        except Exception as e:
            _log.error("Agentihub provision failed: %s", e)
            print(f"FATAL: Agentihub provision failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Step 1: Clone package repo if URL is configured (FATAL)
    if am.repo_url:
        try:
            _clone_package_repo(am.repo_url, am.repo_branch, str(Path(am.package_dir).parent))
        except Exception as e:
            _log.error("Package repo clone failed: %s", e)
            print(f"FATAL: Package repo clone failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Step 2: Validate package dir (FATAL)
    try:
        _validate_package_dir(am.package_dir)
    except RuntimeError as e:
        _log.error(str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)

    # Step 3: Cache system prompt (instant file read)
    from agenticore.agent_mode.agent import _load_system_prompt

    prompt = _load_system_prompt(am.package_dir)
    if prompt:
        _log.info("Default system prompt loaded (%d chars)", len(prompt))
    else:
        _log.info("No default system prompt (system.md not found)")

    # Step 4+5: Non-fatal steps — fire as background daemon threads
    bg_threads: list[threading.Thread] = []
    t_scripts = threading.Thread(
        target=_bg_run_startup_scripts,
        args=(am.package_dir,),
        name="startup-scripts",
        daemon=True,
    )
    t_scripts.start()
    bg_threads.append(t_scripts)

    t_relay = threading.Thread(
        target=_bg_install_event_relay_hook,
        name="event-relay-hook",
        daemon=True,
    )
    t_relay.start()
    bg_threads.append(t_relay)

    # Memory mirror is fully owned by agentihooks in v5 — consumer/contributor
    # pulls fire on UserPromptSubmit / Stop hooks; authority runs the 60s
    # daemon via the agentihooks sync_daemon. agenticore no longer carries a
    # memory-mirror bootstrap thread.

    # Step 5.5: Per-agent bootstrap script. Operator-controlled hook for
    # installing CLI tools (e.g. gh for contributor memory-mirror), minting
    # tokens, priming caches, or any other agent-specific prep that
    # agenticore shouldn't know about. Non-fatal; agent starts regardless.
    t_bootstrap = threading.Thread(
        target=_bg_run_bootstrap_script,
        name="bootstrap-script",
        daemon=True,
    )
    t_bootstrap.start()
    bg_threads.append(t_bootstrap)

    # Step 6: Telegram channel (already Popen/detached)
    _log_telegram_status()

    _log.info("=== Agent Mode Ready (%.2fs) ===", time.monotonic() - t0)
    _log.info("  Package dir: %s", am.package_dir)
    from agenticore.agent_mode.agent import _active_profile_claude

    pc = _active_profile_claude()
    _log.info("  Default model: %s (from profile)", pc.model)
    _log.info("  Max turns: %d (from profile)", pc.max_turns)
    _log.info("  Permission mode: %s (from profile)", pc.permission_mode)
    return bg_threads
