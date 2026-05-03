"""Agentihooks integration — PyPI-first install with optional git overlay.

agentihooks is a pip dependency of agenticore (declared in pyproject.toml),
so the default install path is ``pip install agenticore`` pulling it from
PyPI transitively. The runtime only clones or pip-installs-editable when an
override is requested, and there is no periodic re-sync — a pod restart is
how you pick up a new version.

Override semantics (PATH wins over URL):

    AGENTICORE_AGENTIHOOKS_PATH  → uv pip install -e <PATH>        (dev loopback)
    AGENTICORE_AGENTIHOOKS_URL   → clone once, uv pip install -e   (bleeding edge)
    neither                      → trust the PyPI install

Bundle and agentihub are content repos, not Python packages; their clone +
watcher logic is unchanged.
"""

import fcntl
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from agenticore.config import get_config
from agenticore.git_credentials import git_askpass_env
from agenticore.mgmt_log import get_mgmt_logger
from agenticore.repos import _run_git, _with_redis_lock, resolve_github_token

logger = logging.getLogger(__name__)


def _scoped_lock_key(base_key: str) -> str:
    """Return a pod-scoped Redis lock key when storage is isolated (PVC).

    Pods on shared NFS need global lock keys for coordination.
    Pods on isolated PVC (local-path) get pod-specific keys to avoid contention.
    Controlled by AGENTICORE_SHARED_LOCKS env var (default: true for backwards compat).
    """
    shared = os.environ.get("AGENTICORE_SHARED_LOCKS", "true").lower() in ("true", "1", "yes")
    if shared:
        return base_key
    hostname = os.environ.get("HOSTNAME", "unknown")
    return f"{base_key}:{hostname}"


def _get_head_ref(dest: Path) -> str:
    """Return the short HEAD commit ref for a git repo, or '?' on failure."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(dest), "rev-parse", "--short", "HEAD"],
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return "?"


def resolve_repo_paths(cfg=None):
    """Resolve paths for agentihooks, bundle, and agentihub.

    Dev mode: use pre-mounted paths from env vars.
    Prod mode: derive from SHARED_FS_ROOT, explicit overrides honored.

    Returns (hooks_path, bundle_path, hub_path) — all Optional[Path].
    """
    if cfg is None:
        cfg = get_config()
    if cfg.dev_mode:
        hooks = Path(cfg.agentihooks_path) if cfg.agentihooks_path else None
        bundle = Path(cfg.agentihooks_bundle_path) if cfg.agentihooks_bundle_path else None
        hub = Path(cfg.agentihub_path) if cfg.agentihub_path else None
        return hooks, bundle, hub
    shared = cfg.repos.shared_fs_root
    base = Path(shared) if shared else Path.home() / ".agenticore"
    hooks = Path(cfg.agentihooks_path) if cfg.agentihooks_path else base / "agentihooks"
    bundle = Path(cfg.agentihooks_bundle_path) if cfg.agentihooks_bundle_path else base / "agentihooks-bundle"
    hub = Path(cfg.agentihub_path) if cfg.agentihub_path else base / "agentihub"
    return hooks, bundle, hub


def _install_dir() -> Path:
    """Determine where agentihooks should be installed.

    Checks AGENTICORE_AGENTIHOOKS_PATH first (explicit override), then
    AGENTICORE_SHARED_FS_ROOT for K8s deployments, then local default.
    """
    hooks, _, _ = resolve_repo_paths()
    return hooks or Path.home() / ".agenticore" / "agentihooks"


def _clone_or_fetch(url: str, dest: Path, branch: str = "") -> None:
    """Clone or update agentihooks repo, flock/Redis-protected."""
    t0 = time.monotonic()
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / ".agentihooks.lock"

    def _do():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if (dest / ".git").exists():
                _run_git(["git", "-C", str(dest), "fetch", "--all", "--prune"], extra_env=extra_env)
                if branch:
                    _run_git(
                        ["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"], extra_env=extra_env
                    )
                else:
                    _run_git(["git", "-C", str(dest), "reset", "--hard", "origin/HEAD"], extra_env=extra_env)
                _run_git(["git", "-C", str(dest), "clean", "-fdx", "-e", "*.env"], extra_env=extra_env)
            else:
                cmd = ["git", "clone"]
                if branch:
                    cmd += ["--branch", branch]
                cmd += [url, str(dest)]
                _run_git(cmd, extra_env=extra_env)

    if get_config().repos.shared_fs_root:
        _with_redis_lock(_scoped_lock_key("agenticore:lock:agentihooks"), _do)
    else:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _do()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    logger.info("_clone_or_fetch agentihooks done in %.2fs", time.monotonic() - t0)


def start_bundle_watcher(
    url: str,
    dest: Path,
    interval: int,
    branch: str = "",
    hooks_path: Optional[Path] = None,
    repo_dir: Optional[Path] = None,
) -> Optional[threading.Thread]:
    """Daemon thread that periodically re-fetches the agentihooks bundle repo
    and re-runs ``agentihooks init`` so updated rules/skills/settings.json
    propagate to the live pod's ``~/.claude/`` without a restart."""
    if get_config().dev_mode:
        logger.info("dev mode: skipping bundle watcher")
        return None
    if interval <= 0:
        raise ValueError(f"interval must be > 0, got {interval}")

    mgmt = get_mgmt_logger()

    def _watch():
        while True:
            time.sleep(interval)
            try:
                _clone_or_fetch_bundle(url, dest, branch)
                ref = _get_head_ref(dest)
                logger.info("agentihooks-bundle hot-reload complete (%s)", dest)
                mgmt.info("hot-reload agentihooks-bundle OK ref=%s", ref)
            except Exception as exc:
                logger.warning("agentihooks-bundle hot-reload failed: %s", exc)
                mgmt.warning("hot-reload agentihooks-bundle FAIL: %s", exc)
                continue
            try:
                run_agentihooks_init(hooks_path=hooks_path, bundle_path=dest, repo_dir=repo_dir)
                logger.info("agentihooks-bundle init re-applied (%s)", dest)
                mgmt.info("hot-reload agentihooks-bundle init OK")
            except Exception as exc:
                logger.warning("agentihooks-bundle init re-apply failed: %s", exc)
                mgmt.warning("hot-reload agentihooks-bundle init FAIL: %s", exc)

    t = threading.Thread(target=_watch, name="agentihooks-bundle-watcher", daemon=True)
    t.start()
    logger.info("agentihooks-bundle watcher started (interval=%ds, dest=%s)", interval, dest)
    mgmt.info("watcher agentihooks-bundle started interval=%ds dest=%s", interval, dest)
    return t


def start_agentihub_watcher(url: str, dest: Path, interval: int, branch: str = "") -> Optional[threading.Thread]:
    """Daemon thread that periodically re-fetches the agentihub repo."""
    if get_config().dev_mode:
        logger.info("dev mode: skipping agentihub watcher")
        return None
    if interval <= 0:
        raise ValueError(f"interval must be > 0, got {interval}")

    mgmt = get_mgmt_logger()

    def _watch():
        while True:
            time.sleep(interval)
            try:
                _clone_or_fetch_agentihub(url, dest, branch)
                ref = _get_head_ref(dest)
                logger.info("agentihub hot-reload complete (%s)", dest)
                mgmt.info("hot-reload agentihub OK ref=%s", ref)
            except Exception as exc:
                logger.warning("agentihub hot-reload failed: %s", exc)
                mgmt.warning("hot-reload agentihub FAIL: %s", exc)

    t = threading.Thread(target=_watch, name="agentihub-watcher", daemon=True)
    t.start()
    logger.info("agentihub watcher started (interval=%ds, dest=%s)", interval, dest)
    mgmt.info("watcher agentihub started interval=%ds dest=%s", interval, dest)
    return t


def _agentihub_install_dir() -> Path:
    """Determine where agentihub should be installed."""
    _, _, hub = resolve_repo_paths()
    return hub or Path.home() / ".agenticore" / "agentihub"


def _clone_or_fetch_agentihub(url: str, dest: Path, branch: str = "") -> None:
    """Clone or update agentihub repo (no profile build — agent mode handles provisioning)."""
    t0 = time.monotonic()
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / ".agentihub.lock"

    def _do():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if (dest / ".git").exists():
                _run_git(["git", "-C", str(dest), "fetch", "--all", "--prune"], extra_env=extra_env)
                if branch:
                    _run_git(
                        ["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"], extra_env=extra_env
                    )
                else:
                    _run_git(["git", "-C", str(dest), "reset", "--hard", "origin/HEAD"], extra_env=extra_env)
                _run_git(["git", "-C", str(dest), "clean", "-fdx"], extra_env=extra_env)
            else:
                cmd = ["git", "clone"]
                if branch:
                    cmd += ["--branch", branch]
                cmd += [url, str(dest)]
                _run_git(cmd, extra_env=extra_env)

    if get_config().repos.shared_fs_root:
        _with_redis_lock(_scoped_lock_key("agenticore:lock:agentihub"), _do)
    else:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _do()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    logger.info("_clone_or_fetch_agentihub done in %.2fs", time.monotonic() - t0)


def sync_agentihub(url: str = "") -> Optional[Path]:
    """Clone/fetch agentihub + run agent_hub + rebuild profiles.

    Sets AGENTICORE_AGENTIHUB_PATH in-process. Returns the install directory,
    or None if no URL is configured.
    """
    cfg = get_config()
    if cfg.dev_mode:
        _, _, hub = resolve_repo_paths(cfg)
        if hub and hub.exists():
            logger.info("dev mode: agentihub at %s (no clone)", hub)
            os.environ["AGENTICORE_AGENTIHUB_PATH"] = str(hub)
            return hub
        return None
    url = url or cfg.agentihub_url
    if not url:
        explicit = os.getenv("AGENTICORE_AGENTIHUB_PATH")
        if explicit:
            return Path(explicit)
        return None
    dest = _agentihub_install_dir()
    _clone_or_fetch_agentihub(url, dest, cfg.agentihub_branch)
    os.environ["AGENTICORE_AGENTIHUB_PATH"] = str(dest)
    logger.info("AGENTICORE_AGENTIHUB_PATH → %s (branch=%s)", dest, cfg.agentihub_branch or "HEAD")
    return dest


def _bundle_dir() -> Path:
    """Determine where the agentihooks bundle should be installed."""
    _, bundle, _ = resolve_repo_paths()
    return bundle or Path.home() / ".agenticore" / "agentihooks-bundle"


def _clone_or_fetch_bundle(url: str, dest: Path, branch: str = "") -> None:
    """Clone or update agentihooks bundle repo, with GitHub App auth."""
    t0 = time.monotonic()
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / ".agentihooks-bundle.lock"

    def _do():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if (dest / ".git").exists():
                _run_git(["git", "-C", str(dest), "fetch", "--all", "--prune"], extra_env=extra_env)
                if branch:
                    _run_git(
                        ["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"], extra_env=extra_env
                    )
                else:
                    _run_git(["git", "-C", str(dest), "reset", "--hard", "origin/HEAD"], extra_env=extra_env)
            else:
                cmd = ["git", "clone"]
                if branch:
                    cmd += ["--branch", branch]
                cmd += [url, str(dest)]
                _run_git(cmd, extra_env=extra_env)

    if get_config().repos.shared_fs_root:
        _with_redis_lock(_scoped_lock_key("agenticore:lock:agentihooks-bundle"), _do)
    else:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _do()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    logger.info("_clone_or_fetch_bundle done in %.2fs", time.monotonic() - t0)


def _venv_python() -> str:
    return os.environ.get("AGENTICORE_VENV_PYTHON", "/opt/venv/bin/python")


def _log_uv_output(result: subprocess.CompletedProcess) -> None:
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger.info("uv: %s", line)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger.info("uv stderr: %s", line)


def _pip_install_editable(path: Path) -> None:
    """Editable install from a local path. Used by dev-mode PATH override only.

    Raises on failure — agentihooks is required for the agent to function.
    """
    logger.info("uv pip install -e %s", path)
    result = subprocess.run(
        ["uv", "pip", "install", "--python", _venv_python(), "--reinstall", "-e", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    _log_uv_output(result)
    if result.returncode != 0:
        raise RuntimeError(f"uv pip install -e {path} failed (exit {result.returncode})")


def _pip_install_pypi(pkg: str) -> None:
    """Install a package from PyPI into the agenticore venv via uv.

    Raises on failure — agentihooks is required for the agent to function.
    """
    logger.info("uv pip install %s (from PyPI)", pkg)
    result = subprocess.run(
        ["uv", "pip", "install", "--python", _venv_python(), pkg],
        check=False,
        capture_output=True,
        text=True,
    )
    _log_uv_output(result)
    if result.returncode != 0:
        raise RuntimeError(f"uv pip install {pkg} failed (exit {result.returncode})")


def sync_agentihooks(url: str = "") -> Optional[Path]:
    """Install agentihooks at runtime. Image is lean — only ``uv`` is baked.

    Resolution order:

    1. ``AGENTICORE_AGENTIHOOKS_PATH`` set (or dev_mode with pre-mounted path):
       ``uv pip install -e <PATH>`` — local editable loopback for dev work
       on agentihooks itself. Returns the path.

    2. ``AGENTICORE_AGENTIHOOKS_URL`` AND ``AGENTICORE_AGENTIHOOKS_BRANCH``
       both set: clone the repo and ``uv pip install -e <clone>``. The
       cloned source becomes the active agentihooks. Use this to run a
       branch / fork (e.g. dev) that isn't on PyPI yet. Returns the
       clone path.

    3. Default (URL+BRANCH unset): ``uv pip install agentihooks`` from
       PyPI. Returns ``None``.
    """
    cfg = get_config()

    # 1. PATH override (dev loopback). In dev_mode also accept a path
    # surfaced by resolve_repo_paths() from pre-mounted volumes.
    path_str = cfg.agentihooks_path or os.getenv("AGENTICORE_AGENTIHOOKS_PATH", "")
    if not path_str and cfg.dev_mode:
        hooks, _, _ = resolve_repo_paths(cfg)
        if hooks and hooks.exists():
            path_str = str(hooks)
    if path_str:
        path = Path(path_str)
        if path.exists():
            _pip_install_editable(path)
            os.environ["AGENTICORE_AGENTIHOOKS_PATH"] = str(path)
            logger.info("agentihooks editable from PATH → %s", path)
            return path
        logger.warning("AGENTICORE_AGENTIHOOKS_PATH %s does not exist; falling back", path)

    # 2. URL+BRANCH both set → clone + editable install from the clone.
    resolved_url = url or cfg.agentihooks_url
    branch = cfg.agentihooks_branch
    if resolved_url and branch:
        dest = _install_dir()
        _clone_or_fetch(resolved_url, dest, branch)
        _pip_install_editable(dest)
        os.environ["AGENTICORE_AGENTIHOOKS_PATH"] = str(dest)
        logger.info("agentihooks editable from URL → %s (branch=%s)", dest, branch)
        return dest

    # 3. Default: install from PyPI.
    _pip_install_pypi("agentihooks")
    logger.info("agentihooks: installed from PyPI via uv")
    return None


def sync_bundle() -> Optional[Path]:
    """Clone/fetch the agentihooks bundle repo.

    Returns the bundle directory, or None if no bundle URL is configured.
    """
    cfg = get_config()
    if cfg.dev_mode:
        _, bundle, _ = resolve_repo_paths(cfg)
        if bundle and bundle.exists():
            logger.info("dev mode: agentihooks-bundle at %s (no clone)", bundle)
            return bundle
        return None
    url = cfg.agentihooks_bundle_url
    if not url:
        return None
    dest = _bundle_dir()
    _clone_or_fetch_bundle(url, dest, cfg.agentihooks_bundle_branch)
    logger.info("agentihooks bundle synced → %s (branch=%s)", dest, cfg.agentihooks_bundle_branch or "HEAD")
    return dest


def run_agentihooks_init(
    hooks_path: Optional[Path] = None,
    bundle_path: Optional[Path] = None,
    repo_dir: Optional[Path] = None,
    force: bool = False,
) -> None:
    """Run ``agentihooks init`` with profile and optional bundle/repo.

    Assumes the ``agentihooks`` CLI is already on PATH — installed by
    :func:`sync_agentihooks` at boot. The ``hooks_path`` parameter is
    accepted for backwards compatibility with existing call sites but is
    no longer used for installation.

    *force* controls whether ``--force`` is passed to agentihooks. ``--force``
    wipes scoped state (state.json, ~/.claude assets) and re-installs from
    a clean slate — operator's drift-recovery tool, NOT a per-restart
    hammer. Default is False. Caller decides:

    - First-time provisioning of a new ``AGENTIHOOKS_HOME`` (no state.json)
      → force=True
    - Boot when state.json already exists → force=False (idempotent rerun)
    - Bundle watcher periodic refresh → force=False
    - Operator-triggered ``/admin/sync`` → force=True

    When *repo_dir* is given, ``--repo <dir>`` is passed so agentihooks
    processes the per-repo ``.agentihooks.json`` whitelist and writes
    ``disabledMcpServers`` into ``~/.claude.json`` for that project path.
    """
    del hooks_path  # Accepted for backwards compat; install is handled in sync_agentihooks.
    t0 = time.monotonic()

    # Clear stale sync locks from previous pod incarnations. /shared is a
    # PVC so sync.lock + sync-daemon.pid survive pod restarts. If a prior
    # pod crashed or was force-killed mid-init, the locks stay on disk and
    # new agentihooks invocations deadlock waiting for a process that will
    # never release them. PID in sync-daemon.pid is from the dead pod and
    # references nothing in the new pod's namespace.
    try:
        state_dir = Path("/shared/.agentihooks")
        if state_dir.exists():
            for stale in ("sync.lock", "sync-daemon.pid"):
                p = state_dir / stale
                if p.exists():
                    logger.info("Clearing stale %s from /shared (previous pod)", stale)
                    p.unlink(missing_ok=True)
    except Exception as exc:  # pragma: no cover
        logger.warning("stale-lock cleanup failed: %s", exc)

    profile = get_config().agentihooks_profile

    # Persist the bundle link BEFORE init. Passing --bundle to init is
    # transient — it tells init where to read this one time but doesn't
    # write the link to state.json. Without a persisted link, subsequent
    # agentihooks invocations (including the pre-call MCP render hook and
    # any interactive shell) see "No bundle linked" and refuse to merge
    # the bundle's master .mcp.json into ~/.claude.json. Net effect:
    # Claude Code sessions spawn with zero MCP servers, which silently
    # breaks agents that rely on MCP tool access. Run `bundle link`
    # explicitly so state.json carries the linkage across restarts.
    if bundle_path and bundle_path.exists():
        link_cmd = ["agentihooks", "bundle", "link", str(bundle_path)]
        logger.info("Running: %s", " ".join(link_cmd))
        link_result = subprocess.run(link_cmd, capture_output=True, text=True)
        if link_result.returncode != 0:
            logger.warning(
                "agentihooks bundle link failed (exit %d): %s",
                link_result.returncode,
                link_result.stderr.strip(),
            )
        else:
            for line in (link_result.stdout or "").strip().splitlines():
                logger.info("agentihooks: %s", line)

    cmd = ["agentihooks", "init"]
    if force:
        cmd.append("--force")
    if profile:
        cmd.extend(["--profile", profile])
    if bundle_path and bundle_path.exists():
        cmd.extend(["--bundle", str(bundle_path)])
    if repo_dir and repo_dir.exists():
        cmd.extend(["--repo", str(repo_dir)])

    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger.info("agentihooks: %s", line)
    if result.returncode != 0:
        logger.error("agentihooks init failed (exit %d):\n%s", result.returncode, result.stderr)
        raise RuntimeError(f"agentihooks init failed: {result.stderr}")

    # Post-init assertion: if a bundle was provided, ~/.claude.json MUST
    # have at least one MCP server registered. Zero servers = bundle link
    # or init silently no-op'd, meaning every subsequent claude subprocess
    # will start with no MCP tools. Fail loudly instead of limping on.
    if bundle_path and bundle_path.exists():
        try:
            from pathlib import Path as _P
            import json as _json

            claude_json = _P("/shared/.claude.json")
            if claude_json.exists():
                data = _json.loads(claude_json.read_text())
                mcp_count = len(data.get("mcpServers", {}))
                if mcp_count == 0:
                    logger.error(
                        "agentihooks init: bundle linked but ~/.claude.json "
                        "mcpServers is empty. Agent will start with no MCP "
                        "tools. Check bundle .claude/.mcp.json integrity."
                    )
                else:
                    logger.info(
                        "agentihooks init: ~/.claude.json mcpServers count=%d",
                        mcp_count,
                    )
        except Exception as exc:  # pragma: no cover — belt-and-braces observability
            logger.warning("agentihooks init post-check failed: %s", exc)

    logger.info(
        "agentihooks init complete in %.2fs (profile=%s, bundle=%s, repo=%s)",
        time.monotonic() - t0,
        profile or "(agentihooks default)",
        bundle_path,
        repo_dir,
    )


def render_mcp_whitelist(repo_dir: Path, disable_servers: Optional[list] = None) -> None:
    """Pre-call MCP render: re-apply .agentihooks.json whitelist for a repo.

    Runs ``agentihooks init --repo <dir>`` to render disabledMcpServers
    into ``~/.claude.json`` from the committed .agentihooks.json whitelist.
    This is the subtractive step: global has all disabled, this enables
    only what the agent needs for this call.

    When *disable_servers* is provided, those servers are temporarily removed
    from the enabledMcpServers before rendering. This is the per-call
    subtraction layer: the caller can narrow the agent's lifetime whitelist
    for a specific task. The .agentihooks.json file is restored after rendering.

    Designed to run before every ``claude -p`` invocation so that
    hot-reloads, daemon writes, or previous sessions cannot leave
    stale MCP state.
    """
    if not repo_dir or not repo_dir.exists():
        logger.debug("render_mcp_whitelist: no repo_dir, skipping")
        return

    agentihooks_json = repo_dir / ".agentihooks.json"
    if not agentihooks_json.exists():
        logger.debug("render_mcp_whitelist: no .agentihooks.json in %s, skipping", repo_dir)
        return

    # Per-call subtraction: temporarily narrow the whitelist
    original_content = None
    if disable_servers:
        import json as _json

        original_content = agentihooks_json.read_text()
        config = _json.loads(original_content)
        enabled = config.get("enabledMcpServers", [])
        narrowed = [s for s in enabled if s not in disable_servers]
        config["enabledMcpServers"] = narrowed
        agentihooks_json.write_text(_json.dumps(config, indent=2) + "\n")
        logger.info("Per-call subtraction: disabled %s → enabled %s", disable_servers, narrowed)

    try:
        profile = get_config().agentihooks_profile
        cmd = ["agentihooks", "init", "--force", "--repo", str(repo_dir)]
        if profile:
            cmd.extend(["--profile", profile])

        logger.info("Pre-call MCP render: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                logger.info("agentihooks: %s", line)
        if result.returncode != 0:
            logger.warning("Pre-call MCP render failed (exit %d): %s", result.returncode, result.stderr)
        else:
            logger.info("Pre-call MCP render complete for %s", repo_dir)
    finally:
        # Always restore the original .agentihooks.json
        if original_content is not None:
            agentihooks_json.write_text(original_content)
            logger.debug("Restored .agentihooks.json to committed state")
