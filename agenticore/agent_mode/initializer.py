"""Agent mode startup logic.

Called when AGENT_MODE=true. Clones the package repo (if configured),
validates the package directory, and runs startup scripts.
"""

import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

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

    hub_path_str = os.getenv("AGENTICORE_AGENTIHUB_PATH", "")
    if not hub_path_str:
        _log.warning("AGENTICORE_AGENTIHUB_PATH not set — hub not available for agent provision")
        return

    hub_path = Path(hub_path_str)
    agent_dir = hub_path / "agents" / agent_name
    if not agent_dir.exists():
        available = []
        agents_dir = hub_path / "agents"
        if agents_dir.exists():
            available = [d.name for d in agents_dir.iterdir() if d.is_dir()]
        raise RuntimeError(f"Agent '{agent_name}' not found in agentihub at {agent_dir}. Available: {available}")

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
        # (target_dir may already have /app/package and /app/evaluation as empty dirs)
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


def _install_notification_hook(package_dir: str) -> None:
    """Install hook_notifier.py into ~/.claude/hooks/ and wire settings.json.

    Writes outside the package_dir to avoid dirtying git checkouts
    (agentihub hot-reload does git clean -fdx which would delete hooks).
    """
    import json
    import shutil

    claude_home = Path(os.getenv("CLAUDE_CODE_HOME_DIR", str(Path.home())))
    hooks_dir = claude_home / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Copy hook_notifier.py to user-level hooks dir
    src = Path(__file__).parent / "hook_notifier.py"
    dst = hooks_dir / "notifier.py"
    if src.exists():
        shutil.copy2(src, dst)
        _log.info("Installed notification hook: %s", dst)
    else:
        _log.warning("hook_notifier.py not found at %s", src)
        return

    # Wire hooks into user-level settings.json (not package_dir)
    settings_path = claude_home / ".claude" / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            settings = {}

    hooks = settings.get("hooks", {})
    notifier_cmd = f"python3 {dst}"

    hook_entries = {
        "PostToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": notifier_cmd}]}],
        "Notification": [{"hooks": [{"type": "command", "command": notifier_cmd}]}],
    }

    for hook_name, entries in hook_entries.items():
        existing = hooks.get(hook_name, [])
        existing_cmds = set()
        for e in existing:
            for h in e.get("hooks", []):
                existing_cmds.add(h.get("command", ""))
        for entry in entries:
            cmd = entry["hooks"][0]["command"]
            if cmd not in existing_cmds:
                existing.append(entry)
        hooks[hook_name] = existing

    settings["hooks"] = hooks
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    _log.info("Notification hooks wired in settings.json")


def _start_telegram_channel(package_dir: str, model: str) -> None:
    """Start Telegram channel as a background process if configured.

    Platform capability — any agent with TELEGRAM_BOT_TOKEN gets Telegram
    automatically. No runner scripts needed.

    Follows OpenClaw pattern: if not configured, gracefully skip (ready but
    not connected). Credentials come from env vars (K8s secrets via ESO).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        _log.info("Telegram: not configured (no TELEGRAM_BOT_TOKEN). Ready but not connected.")
        return

    owner_id = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    home = Path(os.environ.get("HOME", "/shared"))

    # Write channel config (bot token + access control)
    channel_dir = home / ".claude" / "channels" / "telegram"
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / ".env").write_text(f"TELEGRAM_BOT_TOKEN={token}\n")

    if owner_id:
        import json

        access = {
            "dmPolicy": "allowlist",
            "allowFrom": [owner_id],
            "groups": {},
            "pending": {},
            "mentionPatterns": [],
        }
        (channel_dir / "access.json").write_text(json.dumps(access, indent=2))
        approved_dir = channel_dir / "approved"
        approved_dir.mkdir(parents=True, exist_ok=True)
        (approved_dir / owner_id).write_text(owner_id)
        _log.info("Telegram: allowlist locked to owner %s", owner_id)
    else:
        if not (channel_dir / "access.json").exists():
            import json

            access = {
                "dmPolicy": "pairing",
                "allowFrom": [],
                "groups": {},
                "pending": {},
                "mentionPatterns": [],
            }
            (channel_dir / "access.json").write_text(json.dumps(access, indent=2))
            _log.warning("Telegram: no TELEGRAM_OWNER_ID — using pairing mode")

    # Start channel from package dir (inside agentihub git repo — bypasses trust prompt)
    pkg = Path(package_dir)
    if not pkg.exists():
        _log.warning("Telegram: package dir %s not found — skipping channel start", pkg)
        return

    cmd = [
        "script", "-qfec",
        f"claude --model {model} --dangerously-skip-permissions "
        f"--channels plugin:telegram@claude-plugins-official",
        "/tmp/telegram-typescript",
    ]
    _log.info("Telegram: starting channel from %s (model=%s)", pkg, model)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(pkg),
            stdout=open("/tmp/telegram-channel.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _log.info("Telegram: channel started (PID %d)", proc.pid)
    except Exception as e:
        _log.warning("Telegram: failed to start channel: %s", e)


def initialize_agent_mode() -> None:
    """Main initialization entry point for agent mode.

    1. Clone package repo if PACKAGE_REPO_URL is set
    2. Validate package directory
    3. Run startup scripts
    4. Cache system prompt
    """
    cfg = get_config()
    am = cfg.agent_mode

    _log.info("=== Agent Mode Initialization ===")

    # Step 0: Provision from agentihub if AGENTIHUB_AGENT is set
    if am.agent:
        try:
            _provision_from_agentihub(cfg)
        except Exception as e:
            _log.error("Agentihub provision failed: %s", e)
            print(f"FATAL: Agentihub provision failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Step 1: Clone package repo if URL is configured (skipped if agentihub already populated)
    if am.repo_url:
        try:
            _clone_package_repo(am.repo_url, am.repo_branch, str(Path(am.package_dir).parent))
        except Exception as e:
            _log.error("Package repo clone failed: %s", e)
            print(f"FATAL: Package repo clone failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Step 2: Validate package dir
    try:
        _validate_package_dir(am.package_dir)
    except RuntimeError as e:
        _log.error(str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)

    # Step 3: Run startup scripts
    try:
        _run_startup_scripts(am.package_dir)
    except Exception as e:
        _log.warning("Startup scripts error (non-fatal): %s", e)

    # Step 4: Cache system prompt
    from agenticore.agent_mode.agent import _load_system_prompt

    prompt = _load_system_prompt(am.package_dir)
    if prompt:
        _log.info("Default system prompt loaded (%d chars)", len(prompt))
    else:
        _log.info("No default system prompt (system.md not found)")

    # Step 5: Install notification hooks
    try:
        _install_notification_hook(am.package_dir)
    except Exception as e:
        _log.warning("Notification hook install failed (non-fatal): %s", e)

    # Step 6: Telegram channel (platform capability)
    _start_telegram_channel(am.package_dir, am.model)

    _log.info("=== Agent Mode Ready ===")
    _log.info("  Package dir: %s", am.package_dir)
    _log.info("  Default model: %s", am.model)
    _log.info("  Max turns: %d", am.max_turns)
    _log.info("  Permission mode: %s", am.permission_mode)
