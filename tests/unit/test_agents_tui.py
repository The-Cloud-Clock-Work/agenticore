"""Unit tests for the agents TUI/headless CLI.

Two things are load-bearing here and are asserted hard:

1. **Kubernetes is opt-in.** With K8s disabled, no ``kubectl`` process may be
   spawned — not even one that fails. Agenticore must stay usable on a laptop
   with no cluster, and K8s is only one of several possible backends.
2. **Local agent rendering never crashes.** ``_render_list`` filters on fields
   of ``LocalAgent``; a missing field there is an AttributeError in the middle
   of the operator's TUI.
"""

import json
import sys

import pytest

from agenticore import agents_tui
from agenticore.agents_tui import (
    AgenticorePod,
    K8sConfig,
    LocalAgent,
    _local_matches,
    _namespaces_summary,
    _parse_namespaces,
    _render_list,
    discover_local_agents,
    discover_pods,
    read_package_manifest,
    resolve_k8s_config,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point ~/.agenticore/state.json at a temp dir and clear K8s env vars."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTICORE_K8S_ENABLED", raising=False)
    monkeypatch.delenv("AGENTICORE_K8S_NAMESPACES", raising=False)
    monkeypatch.delenv("AGENTIHUB_DIR", raising=False)
    return tmp_path


@pytest.fixture
def no_kubectl(monkeypatch):
    """Any subprocess spawn is a test failure. Proves K8s stays untouched."""

    def _boom(*args, **kwargs):
        raise AssertionError(f"kubectl must not be spawned when K8s is disabled: {args!r}")

    monkeypatch.setattr(agents_tui.subprocess, "run", _boom)


def _write_state(home, payload):
    state_dir = home / ".agenticore"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(payload))


def _make_hub(root, name, *, command_yml=None):
    package = root / "agents" / name / "package"
    package.mkdir(parents=True)
    (package / "CLAUDE.md").write_text(f"# {name}")
    if command_yml is not None:
        (package / "command.yml").write_text(command_yml)
    return package


def _pod_item(name, namespace, *, agenticore=True):
    env = [{"name": "AGENTICORE_TRANSPORT", "value": "sse"}] if agenticore else []
    return {
        "metadata": {"name": name, "namespace": namespace},
        "status": {"phase": "Running"},
        "spec": {"containers": [{"name": "agenticore", "env": env}]},
    }


class _FakeCompleted:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


# ── K8s is opt-in ─────────────────────────────────────────────────────────────


class TestK8sOptIn:
    def test_disabled_by_default(self):
        assert resolve_k8s_config().enabled is False

    def test_disabled_by_default_spawns_no_kubectl(self, no_kubectl):
        # The whole point: a laptop with no cluster must not shell out at all.
        assert discover_pods(resolve_k8s_config()) == []

    def test_discover_pods_defaults_to_resolved_config_when_omitted(self, no_kubectl):
        assert discover_pods() == []

    def test_cli_flag_enables(self):
        assert resolve_k8s_config(enabled=True).enabled is True

    def test_env_enables(self, monkeypatch):
        monkeypatch.setenv("AGENTICORE_K8S_ENABLED", "true")
        assert resolve_k8s_config().enabled is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
    def test_env_disables(self, monkeypatch, raw):
        monkeypatch.setenv("AGENTICORE_K8S_ENABLED", raw)
        assert resolve_k8s_config().enabled is False

    def test_state_enables(self, isolated_home):
        _write_state(isolated_home, {"k8s": {"enabled": True, "namespaces": ["anton-prod"]}})
        cfg = resolve_k8s_config()
        assert cfg.enabled is True
        assert cfg.namespaces == ["anton-prod"]

    def test_env_overrides_state(self, isolated_home, monkeypatch):
        _write_state(isolated_home, {"k8s": {"enabled": True}})
        monkeypatch.setenv("AGENTICORE_K8S_ENABLED", "false")
        assert resolve_k8s_config().enabled is False

    def test_cli_flag_overrides_env(self, monkeypatch):
        monkeypatch.setenv("AGENTICORE_K8S_ENABLED", "true")
        assert resolve_k8s_config(enabled=False).enabled is False

    def test_cli_namespace_implies_enabled(self):
        cfg = resolve_k8s_config(namespaces="anton-prod")
        assert cfg.enabled is True
        assert cfg.namespaces == ["anton-prod"]

    def test_explicit_no_k8s_beats_namespace(self):
        cfg = resolve_k8s_config(enabled=False, namespaces="anton-prod")
        assert cfg.enabled is False

    def test_ambient_namespaces_do_not_enable(self, monkeypatch, isolated_home):
        """Ambient config must never resurrect K8s for an operator who never asked."""
        monkeypatch.setenv("AGENTICORE_K8S_NAMESPACES", "anton-prod")
        _write_state(isolated_home, {"k8s": {"namespaces": ["other"]}})
        assert resolve_k8s_config().enabled is False

    def test_cli_namespace_outranks_ambient_env_false(self, monkeypatch):
        """`--namespace` is a CLI signal, so it beats an AGENTICORE_K8S_ENABLED=false in a shell rc."""
        monkeypatch.setenv("AGENTICORE_K8S_ENABLED", "false")
        assert resolve_k8s_config(namespaces="anton-prod").enabled is True

    def test_env_namespaces_beat_state_namespaces(self, isolated_home, monkeypatch):
        _write_state(isolated_home, {"k8s": {"enabled": True, "namespaces": ["from-state"]}})
        monkeypatch.setenv("AGENTICORE_K8S_NAMESPACES", "from-env")
        assert resolve_k8s_config().namespaces == ["from-env"]

    def test_cli_namespaces_beat_env_namespaces(self, monkeypatch):
        monkeypatch.setenv("AGENTICORE_K8S_NAMESPACES", "from-env")
        assert resolve_k8s_config(enabled=True, namespaces="from-cli").namespaces == ["from-cli"]


class TestCorruptState:
    """A hand-edited or version-skewed state.json must never crash or silently enable K8s."""

    def test_corrupt_json_does_not_enable(self, isolated_home):
        state_dir = isolated_home / ".agenticore"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text("{ not json")
        assert resolve_k8s_config().enabled is False

    def test_non_dict_k8s_state_does_not_crash(self, isolated_home):
        _write_state(isolated_home, {"k8s": "yes-please"})
        assert resolve_k8s_config().enabled is False

    @pytest.mark.parametrize("raw,expected", [("false", False), ("0", False), ("off", False), ("true", True)])
    def test_string_typed_enabled_is_honoured_not_truthy_cast(self, isolated_home, raw, expected):
        """`{"enabled": "false"}` is a string — a naive bool() would read it as True and turn K8s ON."""
        _write_state(isolated_home, {"k8s": {"enabled": raw}})
        assert resolve_k8s_config().enabled is expected

    @pytest.mark.parametrize("scalar", [5, True, 3.14, {"a": 1}])
    def test_scalar_namespaces_do_not_crash(self, isolated_home, scalar):
        _write_state(isolated_home, {"k8s": {"enabled": False, "namespaces": scalar}})
        cfg = resolve_k8s_config()  # must not raise TypeError
        assert cfg.namespaces == [] or all(isinstance(n, str) for n in cfg.namespaces)

    def test_write_k8s_over_non_dict_section_does_not_crash(self, isolated_home):
        _write_state(isolated_home, {"k8s": "garbage"})
        agents_tui._write_k8s_to_state(True, ["anton-prod"])
        assert resolve_k8s_config().enabled is True

    def test_write_agentihub_over_non_dict_section_does_not_crash(self, isolated_home, tmp_path):
        _write_state(isolated_home, {"agentihub": "garbage"})
        hub = tmp_path / "hub"
        (hub / "agents").mkdir(parents=True)
        agents_tui._write_agentihub_to_state(hub)
        assert agents_tui._resolve_agentihub_dir() == hub

    def test_disabling_k8s_preserves_persisted_namespaces(self, isolated_home):
        """Toggling the backend off must not erase the scope the operator configured."""
        _write_state(isolated_home, {"k8s": {"enabled": True, "namespaces": ["anton-prod"]}})

        agents_tui._write_k8s_to_state(False, None)

        state = json.loads((isolated_home / ".agenticore" / "state.json").read_text())
        assert state["k8s"] == {"enabled": False, "namespaces": ["anton-prod"]}


class TestNamespaceParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", []),
            ("a", ["a"]),
            ("a,b", ["a", "b"]),
            ("a, b , c", ["a", "b", "c"]),
            ("a a b", ["a", "b"]),  # dedup, order preserved
            (["a", "b"], ["a", "b"]),
            (None, []),
        ],
    )
    def test_parse(self, raw, expected):
        assert _parse_namespaces(raw) == expected


# ── Pod discovery scoping ─────────────────────────────────────────────────────


class TestDiscoverPods:
    def test_all_namespaces_when_none_configured(self, monkeypatch):
        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _FakeCompleted(json.dumps({"items": [_pod_item("agenticore-0", "anton-prod")]}))

        monkeypatch.setattr(agents_tui.subprocess, "run", _fake_run)

        pods = discover_pods(K8sConfig(enabled=True))
        assert [p.name for p in pods] == ["agenticore-0"]
        assert calls == [["kubectl", "get", "pods", "--all-namespaces", "-o", "json"]]

    def test_one_call_per_namespace(self, monkeypatch):
        """kubectl honours only the last -n, so each namespace needs its own call."""
        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            ns = cmd[cmd.index("-n") + 1]
            return _FakeCompleted(json.dumps({"items": [_pod_item(f"pod-{ns}", ns)]}))

        monkeypatch.setattr(agents_tui.subprocess, "run", _fake_run)

        pods = discover_pods(K8sConfig(enabled=True, namespaces=["ns-a", "ns-b"]))
        assert sorted(p.name for p in pods) == ["pod-ns-a", "pod-ns-b"]
        assert len(calls) == 2
        assert ["kubectl", "get", "pods", "-n", "ns-a", "-o", "json"] in calls

    def test_dedups_pods_seen_in_overlapping_scopes(self, monkeypatch):
        def _fake_run(cmd, **kwargs):
            return _FakeCompleted(json.dumps({"items": [_pod_item("agenticore-0", "anton-prod")]}))

        monkeypatch.setattr(agents_tui.subprocess, "run", _fake_run)

        pods = discover_pods(K8sConfig(enabled=True, namespaces=["anton-prod", "anton-prod-2"]))
        assert len(pods) == 1

    def test_ignores_pods_without_agenticore_transport(self, monkeypatch):
        def _fake_run(cmd, **kwargs):
            return _FakeCompleted(json.dumps({"items": [_pod_item("redis-0", "anton-prod", agenticore=False)]}))

        monkeypatch.setattr(agents_tui.subprocess, "run", _fake_run)
        assert discover_pods(K8sConfig(enabled=True)) == []

    def test_missing_kubectl_binary_is_not_fatal(self, monkeypatch):
        def _fake_run(cmd, **kwargs):
            raise FileNotFoundError("kubectl")

        monkeypatch.setattr(agents_tui.subprocess, "run", _fake_run)
        assert discover_pods(K8sConfig(enabled=True)) == []

    def test_kubectl_failure_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(agents_tui.subprocess, "run", lambda *a, **k: _FakeCompleted("", returncode=1))
        assert discover_pods(K8sConfig(enabled=True)) == []


class TestNamespacesSummary:
    def test_reports_configured_scope_when_no_pods(self):
        cfg = K8sConfig(enabled=True, namespaces=["anton-prod"])
        assert _namespaces_summary([], cfg) == "anton-prod (no pods)"

    def test_reports_all_namespaces_when_unscoped_and_empty(self):
        assert _namespaces_summary([], K8sConfig(enabled=True)) == "all-namespaces (no pods)"

    def test_reports_namespaces_actually_seen(self):
        pods = [AgenticorePod("a", "ns-b", "Running", False, "", "8200", "c")]
        assert _namespaces_summary(pods, K8sConfig(enabled=True)) == "ns-b"


# ── Local agents ──────────────────────────────────────────────────────────────


class TestPackageManifest:
    def test_reads_description_and_capabilities(self, tmp_path):
        pkg = _make_hub(
            tmp_path,
            "finops",
            command_yml="name: finops\ndescription: AWS cost analyst\ncapabilities:\n  - cost-analysis\n",
        )
        assert read_package_manifest(pkg) == {
            "description": "AWS cost analyst",
            "capabilities": ["cost-analysis"],
        }

    def test_missing_manifest_yields_empty(self, tmp_path):
        pkg = _make_hub(tmp_path, "bare")
        assert read_package_manifest(pkg) == {}

    def test_malformed_yaml_never_breaks_discovery(self, tmp_path):
        pkg = _make_hub(tmp_path, "broken", command_yml="name: [unterminated\n  : :")
        assert read_package_manifest(pkg) == {}
        agents = discover_local_agents(str(tmp_path))
        assert [a.name for a in agents] == ["broken"]
        assert agents[0].description == ""

    def test_scalar_yaml_yields_empty(self, tmp_path):
        pkg = _make_hub(tmp_path, "weird", command_yml="just-a-string")
        assert read_package_manifest(pkg) == {}


class TestDiscoverLocalAgents:
    def test_requires_claude_md(self, tmp_path):
        (tmp_path / "agents" / "notanagent").mkdir(parents=True)
        _make_hub(tmp_path, "real")
        assert [a.name for a in discover_local_agents(str(tmp_path))] == ["real"]

    def test_populates_manifest_fields(self, tmp_path):
        _make_hub(
            tmp_path,
            "video-editor-agent",
            command_yml="description: Runs the full video pipeline\ncapabilities:\n  - video-editing\n  - transcription\n",
        )
        agent = discover_local_agents(str(tmp_path))[0]
        assert agent.description == "Runs the full video pipeline"
        assert agent.capabilities == ["video-editing", "transcription"]


# ── Rendering / filtering (the /filter crash) ─────────────────────────────────


class _FakeTTY:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.lines)


class TestRenderList:
    def test_filtering_local_agents_does_not_crash(self):
        """Regression: _render_list filtered on LocalAgent.description before it existed."""
        t = _FakeTTY()
        agents = [LocalAgent(name="finops", package_path="/hub/agents/finops", description="AWS cost analyst")]

        pods, local = _render_list(t, [], agents, filter_str="cost")

        assert [a.name for a in local] == ["finops"]
        assert pods == []

    def test_filter_matches_name_description_and_capabilities(self):
        agent = LocalAgent(
            name="video-editor-agent",
            package_path="/hub/agents/video-editor-agent",
            description="Runs the full video pipeline",
            capabilities=["video-editing"],
        )
        assert _local_matches(agent, "video-editor")  # name
        assert _local_matches(agent, "pipeline")  # description
        assert _local_matches(agent, "video-editing")  # capability
        assert not _local_matches(agent, "kubernetes")

    def test_filter_with_no_matches_renders_message(self):
        t = _FakeTTY()
        agents = [LocalAgent(name="finops", package_path="/hub/agents/finops")]
        _render_list(t, [], agents, filter_str="nope")
        assert "No matches for 'nope'" in t.text

    def test_local_agent_without_description_renders_placeholder(self):
        t = _FakeTTY()
        _render_list(t, [], [LocalAgent(name="bare", package_path="/hub/agents/bare")], filter_str="")
        assert "bare" in t.text


# ── Headless output ───────────────────────────────────────────────────────────


class TestHeadless:
    def test_list_reports_k8s_disabled_and_spawns_no_kubectl(self, tmp_path, no_kubectl, capsys):
        _make_hub(tmp_path, "finops", command_yml="description: AWS cost analyst\ncapabilities: [cost-analysis]\n")

        with pytest.raises(SystemExit) as exc:
            agents_tui.headless_list(str(tmp_path), K8sConfig(enabled=False))
        assert exc.value.code == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["k8s"] == {"enabled": False, "namespaces": []}
        assert payload["pods"] == []
        assert payload["local_agents"][0]["name"] == "finops"
        assert payload["local_agents"][0]["capabilities"] == ["cost-analysis"]

    def test_pod_action_refuses_when_k8s_disabled(self, no_kubectl, capsys):
        with pytest.raises(SystemExit) as exc:
            agents_tui._headless_require_pod("agenticore-0", K8sConfig(enabled=False))
        assert exc.value.code == 2

        err = json.loads(capsys.readouterr().err)
        assert "Kubernetes is disabled" in err["error"]

    def test_local_action_works_with_k8s_disabled(self, tmp_path, no_kubectl, capsys):
        _make_hub(tmp_path, "finops", command_yml="description: AWS cost analyst\n")

        with pytest.raises(SystemExit) as exc:
            agents_tui.headless_local("finops", str(tmp_path))
        assert exc.value.code == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["name"] == "finops"
        assert payload["description"] == "AWS cost analyst"


# ── End-to-end wiring: argv -> argparse -> _cmd_agents -> main() ──────────────


class TestCliWiring:
    """The flags are only worth anything if they survive the trip to resolve_k8s_config()."""

    def _resolved_config(self, argv, monkeypatch):
        """Drive the real CLI end-to-end and capture the K8sConfig that reaches discover_pods()."""
        from agenticore import cli

        captured = {}

        def _spy(k8s=None):
            captured["k8s"] = k8s
            return []

        monkeypatch.setattr(agents_tui, "discover_pods", _spy)
        monkeypatch.setattr(agents_tui, "discover_local_agents", lambda *a, **k: [])
        monkeypatch.setattr(agents_tui, "_resolve_agentihub_dir", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["agenticore", *argv])

        with pytest.raises(SystemExit):  # headless_list always exits
            cli.main()
        return captured["k8s"]

    def test_no_flag_leaves_k8s_off(self, monkeypatch):
        cfg = self._resolved_config(["agents", "--headless", "list"], monkeypatch)
        assert cfg.enabled is False

    def test_k8s_flag_reaches_discovery(self, monkeypatch):
        cfg = self._resolved_config(["agents", "--headless", "list", "--k8s"], monkeypatch)
        assert cfg.enabled is True

    def test_namespace_flag_reaches_discovery_and_implies_k8s(self, monkeypatch):
        cfg = self._resolved_config(["agents", "--headless", "list", "--namespace", "anton-prod,other"], monkeypatch)
        assert cfg.enabled is True
        assert cfg.namespaces == ["anton-prod", "other"]

    def test_no_k8s_flag_beats_env(self, monkeypatch):
        monkeypatch.setenv("AGENTICORE_K8S_ENABLED", "true")
        cfg = self._resolved_config(["agents", "--headless", "list", "--no-k8s"], monkeypatch)
        assert cfg.enabled is False


class TestMainGlue:
    """main() maps CLI kwargs onto resolve_k8s_config — a silent rename here disables every flag."""

    def test_main_forwards_k8s_and_namespace_kwargs(self, monkeypatch, tmp_path):
        captured = {}

        monkeypatch.setattr(agents_tui, "discover_local_agents", lambda *a, **k: [])
        monkeypatch.setattr(agents_tui, "_resolve_agentihub_dir", lambda *a, **k: None)
        monkeypatch.setattr(agents_tui, "discover_pods", lambda k8s=None: captured.setdefault("k8s", k8s) and [])

        with pytest.raises(SystemExit):
            agents_tui.main(headless=True, action="list", k8s=True, namespace="ns-a")

        assert captured["k8s"].enabled is True
        assert captured["k8s"].namespaces == ["ns-a"]

    def test_main_defaults_to_k8s_off(self, monkeypatch, no_kubectl):
        monkeypatch.setattr(agents_tui, "discover_local_agents", lambda *a, **k: [])
        monkeypatch.setattr(agents_tui, "_resolve_agentihub_dir", lambda *a, **k: None)

        with pytest.raises(SystemExit):
            agents_tui.main(headless=True, action="list")  # no_kubectl asserts no spawn
