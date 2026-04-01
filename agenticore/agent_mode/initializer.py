"""Agent mode startup logic.

Called when AGENT_MODE=true. Clones the package repo (if configured),
validates the package directory, and runs startup scripts.
"""

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

from agenticore.config import get_config

_log = logging.getLogger(__name__)


def _provision_from_agentihub(cfg) -> None:
    """Clone agentihub and copy agent's package/ and evaluation/ to /app/.

    Requires AGENTICORE_AGENTIHUB_URL and AGENTIHUB_AGENT to be set. Clones the repo,
    finds agents/{agent_name}/, then copies package/ → /app/package/ and
    evaluation/ → /app/evaluation/.
    """
    import shutil

    from agenticore.repos import resolve_github_token
    from agenticore.git_credentials import git_askpass_env

    hub_url = os.getenv("AGENTICORE_AGENTIHUB_URL", "")
    agent_name = cfg.agent_mode.agent
    if not hub_url or not agent_name:
        _log.debug("No AGENTICORE_AGENTIHUB_URL or AGENTIHUB_AGENT — skipping agentihub provision")
        return

    _log.info("Provisioning from agentihub: agent=%s url=%s", agent_name, hub_url)

    # Use AGENTICORE_AGENTIHUB_PATH if hooks.sync_agentihub already cloned it
    hub_path = os.getenv("AGENTICORE_AGENTIHUB_PATH", "")
    if hub_path and (Path(hub_path) / "agents" / agent_name).exists():
        _log.info("Using pre-cloned agentihub at %s", hub_path)
        clone_dir = Path(hub_path)
    else:
        clone_dir = Path("/tmp/agentihub-clone")
        if clone_dir.exists():
            shutil.rmtree(clone_dir)

        # Try unauthenticated clone first (works for public repos like agentihub)
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", hub_url, str(clone_dir)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if result.returncode != 0:
            # Fall back to authenticated clone for private repos
            _log.info("Unauthenticated clone failed, trying with GitHub App token")
            if clone_dir.exists():
                shutil.rmtree(clone_dir)
            token = resolve_github_token()
            with git_askpass_env(token) as extra_env:
                env.update(extra_env)
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", hub_url, str(clone_dir)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Agentihub clone failed: {result.stderr.strip()}")

    agent_dir = clone_dir / "agents" / agent_name
    if not agent_dir.exists():
        raise RuntimeError(f"Agent '{agent_name}' not found in agentihub at {agent_dir}")

    # Copy package/ → /app/package/
    src_package = agent_dir / "package"
    if src_package.exists():
        dest_package = Path(cfg.agent_mode.package_dir)
        if dest_package.exists():
            shutil.rmtree(dest_package)
        shutil.copytree(src_package, dest_package)
        _log.info("Copied package/ → %s", dest_package)
    else:
        _log.warning("No package/ dir in agent '%s'", agent_name)

    # Copy evaluation/ → /app/evaluation/
    src_eval = agent_dir / "evaluation"
    if src_eval.exists():
        dest_eval = Path(cfg.agent_mode.evaluation_dir)
        if dest_eval.exists():
            shutil.rmtree(dest_eval)
        shutil.copytree(src_eval, dest_eval)
        _log.info("Copied evaluation/ → %s", dest_eval)

    # Clean up clone
    shutil.rmtree(clone_dir, ignore_errors=True)
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
    """Install hook_notifier.py into .claude/hooks/ and wire settings.json."""
    import json
    import shutil

    pkg = Path(package_dir)
    hooks_dir = pkg / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Copy hook_notifier.py to package hooks dir
    src = Path(__file__).parent / "hook_notifier.py"
    dst = hooks_dir / "notifier.py"
    if src.exists():
        shutil.copy2(src, dst)
        _log.info("Installed notification hook: %s", dst)
    else:
        _log.warning("hook_notifier.py not found at %s", src)
        return

    # Wire hooks into settings.json
    settings_path = pkg / ".claude" / "settings.json"
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

    _log.info("=== Agent Mode Ready ===")
    _log.info("  Package dir: %s", am.package_dir)
    _log.info("  Default model: %s", am.model)
    _log.info("  Max turns: %d", am.max_turns)
    _log.info("  Permission mode: %s", am.permission_mode)
