"""Unit tests for profiles module."""

import json
import warnings

import pytest

from agenticore.profiles import (
    Profile,
    ProfileClaude,
    _copy_profile_chain_to,
    _find_mcp_json_files,
    _merge_mcp_lib_into,
    build_cli_args,
    materialize_profile,
    profile_to_dict,
    render_template,
)


@pytest.mark.unit
class TestRenderTemplate:
    def test_basic_replacement(self):
        result = render_template("Task: {{TASK}}", {"TASK": "fix bug"})
        assert result == "Task: fix bug"

    def test_multiple_vars(self):
        template = "{{TASK}} on {{REPO_URL}} branch {{BASE_REF}}"
        result = render_template(
            template,
            {
                "TASK": "fix bug",
                "REPO_URL": "https://github.com/org/repo",
                "BASE_REF": "main",
            },
        )
        assert result == "fix bug on https://github.com/org/repo branch main"

    def test_missing_var_unchanged(self):
        result = render_template("{{TASK}} and {{UNKNOWN}}", {"TASK": "fix"})
        assert result == "fix and {{UNKNOWN}}"

    def test_empty_template(self):
        assert render_template("", {"TASK": "x"}) == ""


@pytest.mark.unit
class TestBuildCliArgs:
    def test_basic_code_profile(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(model="sonnet", max_turns=80, worktree=True),
        )
        args = build_cli_args(profile, "fix the bug")
        assert "--worktree" in args
        assert "--model" in args
        assert args[args.index("--model") + 1] == "sonnet"
        assert "--max-turns" in args
        assert args[args.index("--max-turns") + 1] == "80"
        assert "-p" in args
        assert args[args.index("-p") + 1] == "fix the bug"

    def test_permission_mode_bypass(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(permission_mode="bypassPermissions"),
        )
        args = build_cli_args(profile, "task")
        assert "--permission-mode" in args
        assert args[args.index("--permission-mode") + 1] == "bypassPermissions"

    def test_no_worktree(self):
        profile = Profile(
            name="local",
            claude=ProfileClaude(worktree=False),
        )
        args = build_cli_args(profile, "task")
        assert "--worktree" not in args

    def test_no_session_persistence(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(no_session_persistence=True),
        )
        args = build_cli_args(profile, "task")
        assert "--no-session-persistence" in args

    def test_no_session_persistence_disabled(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(no_session_persistence=False),
        )
        args = build_cli_args(profile, "task")
        assert "--no-session-persistence" not in args

    def test_effort_flag(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(effort="high"),
        )
        args = build_cli_args(profile, "task")
        assert "--effort" in args
        assert args[args.index("--effort") + 1] == "high"

    def test_no_effort_by_default(self):
        profile = Profile(name="code", claude=ProfileClaude())
        args = build_cli_args(profile, "task")
        assert "--effort" not in args

    def test_max_budget_usd(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(max_budget_usd=5.0),
        )
        args = build_cli_args(profile, "task")
        assert "--max-budget-usd" in args
        assert args[args.index("--max-budget-usd") + 1] == "5.0"

    def test_fallback_model(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(fallback_model="haiku"),
        )
        args = build_cli_args(profile, "task")
        assert "--fallback-model" in args
        assert args[args.index("--fallback-model") + 1] == "haiku"

    def test_output_format(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(output_format="json"),
        )
        args = build_cli_args(profile, "task")
        assert "--output-format" in args
        assert args[args.index("--output-format") + 1] == "json"

    def test_dynamic_context_variables(self):
        """New-format profiles inject job context via --append-system-prompt."""
        profile = Profile(name="code", claude=ProfileClaude())
        variables = {
            "JOB_ID": "abc123",
            "TASK": "fix bug",
            "REPO_URL": "https://github.com/org/repo",
            "BASE_REF": "main",
        }
        args = build_cli_args(profile, "fix bug", variables)
        assert "--append-system-prompt" in args
        idx = args.index("--append-system-prompt")
        prompt = args[idx + 1]
        assert "Job: abc123" in prompt
        assert "Task: fix bug" in prompt
        assert "Repo: https://github.com/org/repo" in prompt
        assert "Branch: main" in prompt

    def test_legacy_append_prompt_with_vars(self):
        """Legacy profiles render full template via --append-system-prompt."""
        profile = Profile(
            name="code",
            claude=ProfileClaude(),
            append_prompt="Task: {{TASK}}\nRepo: {{REPO_URL}}",
            _legacy=True,
        )
        args = build_cli_args(profile, "fix bug", {"TASK": "fix bug", "REPO_URL": "https://example.com"})
        assert "--append-system-prompt" in args
        idx = args.index("--append-system-prompt")
        assert "fix bug" in args[idx + 1]
        assert "https://example.com" in args[idx + 1]

    def test_dangerously_skip_permissions_compat(self):
        """Legacy permission mode value still works."""
        profile = Profile(
            name="code",
            claude=ProfileClaude(permission_mode="dangerously-skip-permissions"),
        )
        args = build_cli_args(profile, "task")
        assert "--dangerously-skip-permissions" in args
        assert "--permission-mode" not in args


@pytest.fixture
def minimal_profile(tmp_path):
    """Minimal directory-based profile for materialization tests."""
    profile_dir = tmp_path / "minimal_profile_code"
    claude_dir = profile_dir / ".claude"
    claude_dir.mkdir(parents=True)
    (profile_dir / "profile.yml").write_text(
        "name: code\ndescription: Minimal test profile\nauto_pr: true\nclaude:\n  model: sonnet\n  max_turns: 80\n"
    )
    (claude_dir / "settings.json").write_text(
        '{"permissions": {"allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)"]}}'
    )
    (claude_dir / "CLAUDE.md").write_text("# Test Profile\n")
    (profile_dir / ".mcp.json").write_text('{"mcpServers": {}}')
    from agenticore.profiles import _load_profile_dir

    return _load_profile_dir(profile_dir)


@pytest.mark.unit
class TestMaterializeProfile:
    def test_simple_profile_returns_profile_path(self, minimal_profile):
        """Simple profile (no extends) returns its own path — zero I/O."""
        result = materialize_profile(minimal_profile)
        assert result == minimal_profile.path

    def test_simple_profile_no_files_copied(self, tmp_path, minimal_profile):
        """Simple profile never copies files anywhere."""
        result = materialize_profile(minimal_profile, job_id="test-job")
        assert result == minimal_profile.path
        # No agenticore-jobs dir created
        import tempfile

        assert not (tmp_path / "agenticore-jobs").exists()
        jobs_tmp = __import__("pathlib").Path(tempfile.gettempdir()) / "agenticore-jobs" / "test-job"
        # The simple path should not have been created
        assert result != jobs_tmp

    def test_materialize_legacy_returns_none(self):
        """Legacy profiles return None."""
        profile = Profile(name="old", _legacy=True)
        result = materialize_profile(profile)
        assert result is None

    def test_materialize_no_path_returns_none(self):
        """Profiles with no path return None."""
        profile = Profile(name="test", path=None)
        result = materialize_profile(profile)
        assert result is None

    def test_extends_profile_copies_to_temp_dir(self, tmp_path):
        """Extends profile merges into a temp dir, not the profile dir."""
        base_dir = tmp_path / "base"
        (base_dir / ".claude").mkdir(parents=True)
        (base_dir / "profile.yml").write_text("name: base\n")
        (base_dir / ".claude" / "CLAUDE.md").write_text("# Base")

        child_dir = tmp_path / "child"
        (child_dir / ".claude").mkdir(parents=True)
        (child_dir / "profile.yml").write_text("name: child\nextends: base\n")
        (child_dir / ".claude" / "CLAUDE.md").write_text("# Child")

        from agenticore.profiles import _load_profile_dir, _resolve_extends

        base = _load_profile_dir(base_dir)
        child = _load_profile_dir(child_dir)
        all_profiles = {"base": base, "child": child}
        resolved = _resolve_extends(child, all_profiles)

        result = materialize_profile(resolved, job_id="test-job", all_profiles=all_profiles)

        import tempfile

        assert result is not None
        assert str(result).startswith(tempfile.gettempdir())
        assert "agenticore-jobs" in str(result)

    def test_extends_merges_mcp_json(self, tmp_path):
        """Extends chain merges .mcp.json servers from all profiles."""
        base_dir = tmp_path / "base"
        (base_dir / ".claude").mkdir(parents=True)
        (base_dir / "profile.yml").write_text("name: base\n")
        (base_dir / ".mcp.json").write_text('{"mcpServers": {"base-server": {}}}')

        child_dir = tmp_path / "child"
        (child_dir / ".claude").mkdir(parents=True)
        (child_dir / "profile.yml").write_text("name: child\nextends: base\n")
        (child_dir / ".mcp.json").write_text('{"mcpServers": {"child-server": {}}}')

        from agenticore.profiles import _load_profile_dir, _resolve_extends

        base = _load_profile_dir(base_dir)
        child = _load_profile_dir(child_dir)
        all_profiles = {"base": base, "child": child}
        resolved = _resolve_extends(child, all_profiles)

        result = materialize_profile(resolved, job_id="merge-test", all_profiles=all_profiles)

        assert result is not None
        with open(result / ".mcp.json") as f:
            data = json.load(f)
        assert "base-server" in data["mcpServers"]
        assert "child-server" in data["mcpServers"]

    def test_extends_settings_content(self, tmp_path):
        """Extends chain copies settings from base profile."""
        base_dir = tmp_path / "base"
        (base_dir / ".claude").mkdir(parents=True)
        (base_dir / "profile.yml").write_text("name: base\n")
        (base_dir / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(*)"]}}')

        child_dir = tmp_path / "child"
        (child_dir / ".claude").mkdir(parents=True)
        (child_dir / "profile.yml").write_text("name: child\nextends: base\n")
        (child_dir / ".claude" / "CLAUDE.md").write_text("# Child")

        from agenticore.profiles import _load_profile_dir, _resolve_extends

        base = _load_profile_dir(base_dir)
        child = _load_profile_dir(child_dir)
        all_profiles = {"base": base, "child": child}
        resolved = _resolve_extends(child, all_profiles)

        result = materialize_profile(resolved, job_id="settings-test", all_profiles=all_profiles)

        assert result is not None
        with open(result / ".claude" / "settings.json") as f:
            settings = json.load(f)
        assert "Bash(*)" in settings["permissions"]["allow"]


@pytest.mark.unit
class TestExtendsInheritance:
    def test_extends_merges_claude_fields(self, tmp_path):
        """Child profile overrides parent's claude fields."""
        # Create base profile
        base_dir = tmp_path / "profiles" / "base"
        base_dir.mkdir(parents=True)
        (base_dir / "profile.yml").write_text(
            "name: base\ndescription: Base profile\nclaude:\n  model: sonnet\n  max_turns: 100\n  timeout: 7200\n"
        )

        # Create child profile that extends base
        child_dir = tmp_path / "profiles" / "child"
        child_dir.mkdir(parents=True)
        (child_dir / "profile.yml").write_text(
            "name: child\ndescription: Child profile\nextends: base\nclaude:\n  model: opus\n"
        )

        from agenticore.profiles import _load_profile_dir, _resolve_extends

        base = _load_profile_dir(base_dir)
        child = _load_profile_dir(child_dir)
        all_profiles = {"base": base, "child": child}

        resolved = _resolve_extends(child, all_profiles)

        assert resolved.claude.model == "opus"  # child override
        assert resolved.claude.max_turns == 100  # inherited from base
        assert resolved.claude.timeout == 7200  # inherited from base
        assert resolved.description == "Child profile"

    def test_extends_unknown_parent_warns(self, tmp_path):
        """Extending a non-existent profile logs a warning."""
        child_dir = tmp_path / "profiles" / "child"
        child_dir.mkdir(parents=True)
        (child_dir / "profile.yml").write_text("name: child\nextends: nonexistent\n")

        from agenticore.profiles import _load_profile_dir, _resolve_extends

        child = _load_profile_dir(child_dir)
        resolved = _resolve_extends(child, {"child": child})

        # Should return child unchanged
        assert resolved.name == "child"
        assert resolved.extends == "nonexistent"

    def test_extends_chain_overlay(self, tmp_path):
        """Materialization copies parent files first, then child overlay."""
        base_dir = tmp_path / "profiles" / "base"
        base_claude = base_dir / ".claude"
        base_claude.mkdir(parents=True)
        (base_dir / "profile.yml").write_text("name: base\n")
        (base_claude / "CLAUDE.md").write_text("# Base instructions")
        (base_claude / "base_only.txt").write_text("base file")

        child_dir = tmp_path / "profiles" / "child"
        child_claude = child_dir / ".claude"
        child_claude.mkdir(parents=True)
        (child_dir / "profile.yml").write_text("name: child\nextends: base\n")
        (child_claude / "CLAUDE.md").write_text("# Child instructions")

        from agenticore.profiles import _load_profile_dir, _resolve_extends

        base = _load_profile_dir(base_dir)
        child = _load_profile_dir(child_dir)
        all_profiles = {"base": base, "child": child}
        resolved = _resolve_extends(child, all_profiles)

        result = materialize_profile(resolved, job_id="test-job", all_profiles=all_profiles)

        assert result is not None
        # Child's CLAUDE.md overwrites base's
        assert (result / ".claude" / "CLAUDE.md").read_text() == "# Child instructions"
        # Base-only file is still present
        assert (result / ".claude" / "base_only.txt").read_text() == "base file"


@pytest.mark.unit
class TestLegacyBackwardCompat:
    def test_legacy_yaml_loads_with_warning(self, tmp_path):
        """Old .yml format still loads with a deprecation warning."""
        legacy = tmp_path / "old.yml"
        legacy.write_text(
            "name: old\n"
            "description: legacy profile\n"
            "claude:\n"
            "  model: haiku\n"
            "  max_turns: 10\n"
            "  permission_mode: dangerously-skip-permissions\n"
            "append_prompt: 'Task: {{TASK}}'\n"
            "auto_pr: false\n"
        )

        from agenticore.profiles import _load_legacy_yaml

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            profile = _load_legacy_yaml(legacy)
            assert len(w) == 1
            assert "legacy YAML format" in str(w[0].message)

        assert profile.name == "old"
        assert profile.claude.model == "haiku"
        assert profile.claude.max_turns == 10
        # Old permission_mode mapped to new value
        assert profile.claude.permission_mode == "bypassPermissions"
        assert profile.append_prompt == "Task: {{TASK}}"
        assert profile.auto_pr is False
        assert profile._legacy is True


@pytest.mark.unit
class TestProfileToDict:
    def test_serialization(self):
        profile = Profile(
            name="test",
            description="A test profile",
            claude=ProfileClaude(model="opus", max_turns=50),
            auto_pr=False,
        )
        d = profile_to_dict(profile)
        assert d["name"] == "test"
        assert d["description"] == "A test profile"
        assert d["model"] == "opus"
        assert d["max_turns"] == 50
        assert d["auto_pr"] is False

    def test_serialization_with_extends(self):
        profile = Profile(
            name="child",
            description="Child",
            claude=ProfileClaude(),
            extends="base",
        )
        d = profile_to_dict(profile)
        assert d["extends"] == "base"

    def test_serialization_with_effort(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(effort="high"),
        )
        d = profile_to_dict(profile)
        assert d["effort"] == "high"

    def test_serialization_no_effort(self):
        profile = Profile(name="code", claude=ProfileClaude())
        d = profile_to_dict(profile)
        assert "effort" not in d


# ── Shared FS / Kubernetes materialization ────────────────────────────────


@pytest.mark.unit
class TestMaterializeProfileSharedFs:
    def test_simple_profile_ignores_shared_fs(self, tmp_path, monkeypatch, minimal_profile):
        """Simple profile always returns profile.path — shared FS root is irrelevant."""
        shared = tmp_path / "shared"
        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(shared))

        result = materialize_profile(minimal_profile, job_id="job-abc")

        assert result == minimal_profile.path
        assert not (shared / "jobs").exists()

    def test_extends_profile_writes_to_shared_fs(self, tmp_path, monkeypatch):
        """Extends profiles write to shared_fs_root/jobs/{job_id}/."""
        shared = tmp_path / "shared"
        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(shared))

        base_dir = tmp_path / "base"
        (base_dir / ".claude").mkdir(parents=True)
        (base_dir / "profile.yml").write_text("name: base\n")
        (base_dir / ".claude" / "CLAUDE.md").write_text("# Base")

        child_dir = tmp_path / "child"
        (child_dir / ".claude").mkdir(parents=True)
        (child_dir / "profile.yml").write_text("name: child\nextends: base\n")
        (child_dir / ".claude" / "CLAUDE.md").write_text("# Child")

        from agenticore.profiles import _load_profile_dir, _resolve_extends

        base = _load_profile_dir(base_dir)
        child = _load_profile_dir(child_dir)
        all_profiles = {"base": base, "child": child}
        resolved = _resolve_extends(child, all_profiles)

        result = materialize_profile(resolved, job_id="job-k8s", all_profiles=all_profiles)

        assert result == shared / "jobs" / "job-k8s"
        assert (result / ".claude" / "CLAUDE.md").exists()

    def test_extends_creates_job_dir_on_shared_fs(self, tmp_path, monkeypatch):
        """The per-job directory under shared FS is created automatically."""
        shared = tmp_path / "shared"
        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(shared))

        base_dir = tmp_path / "base"
        (base_dir / ".claude").mkdir(parents=True)
        (base_dir / "profile.yml").write_text("name: base\n")

        child_dir = tmp_path / "child"
        (child_dir / ".claude").mkdir(parents=True)
        (child_dir / "profile.yml").write_text("name: child\nextends: base\n")

        from agenticore.profiles import _load_profile_dir, _resolve_extends

        base = _load_profile_dir(base_dir)
        child = _load_profile_dir(child_dir)
        all_profiles = {"base": base, "child": child}
        resolved = _resolve_extends(child, all_profiles)

        result = materialize_profile(resolved, job_id="job-new", all_profiles=all_profiles)

        assert result is not None
        assert result.is_dir()
        assert str(result).startswith(str(shared))


@pytest.mark.unit
class TestMcpLibMerging:
    def test_find_mcp_json_files_returns_valid(self, tmp_path):
        """_find_mcp_json_files returns only .json files with mcpServers key."""
        valid = tmp_path / "a.json"
        valid.write_text('{"mcpServers": {"srv": {}}}')
        invalid = tmp_path / "b.json"
        invalid.write_text('{"other": true}')
        non_json = tmp_path / "c.txt"
        non_json.write_text("text")

        result = _find_mcp_json_files(tmp_path)
        assert result == [valid]

    def test_find_mcp_json_files_sorted(self, tmp_path):
        """Results are sorted by filename."""
        (tmp_path / "z.json").write_text('{"mcpServers": {"z": {}}}')
        (tmp_path / "a.json").write_text('{"mcpServers": {"a": {}}}')
        result = _find_mcp_json_files(tmp_path)
        assert [f.name for f in result] == ["a.json", "z.json"]

    def test_merge_mcp_lib_into_new_file(self, tmp_path):
        """Merges servers into a fresh target dir (no existing .mcp.json)."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "extra.json").write_text('{"mcpServers": {"extra-srv": {"command": "foo"}}}')

        target = tmp_path / "target"
        target.mkdir()

        _merge_mcp_lib_into(lib_dir, target)

        data = json.loads((target / ".mcp.json").read_text())
        assert "extra-srv" in data["mcpServers"]

    def test_merge_mcp_lib_into_existing(self, tmp_path):
        """Merges servers into existing .mcp.json without losing existing servers."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "extra.json").write_text('{"mcpServers": {"lib-srv": {}}}')

        target = tmp_path / "target"
        target.mkdir()
        (target / ".mcp.json").write_text('{"mcpServers": {"profile-srv": {}}}')

        _merge_mcp_lib_into(lib_dir, target)

        data = json.loads((target / ".mcp.json").read_text())
        assert "profile-srv" in data["mcpServers"]
        assert "lib-srv" in data["mcpServers"]

    def test_merge_mcp_lib_skips_non_mcp_json(self, tmp_path):
        """.json files without mcpServers key are ignored."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "readme.json").write_text('{"description": "no servers here"}')

        target = tmp_path / "target"
        target.mkdir()

        _merge_mcp_lib_into(lib_dir, target)

        # No .mcp.json should be created when no valid files found
        assert not (target / ".mcp.json").exists()

    def test_mcp_lib_not_set_fast_path(self, monkeypatch, minimal_profile):
        """No AGENTICORE_MCP_LIB_PATH → simple profile returns own path (fast-path)."""
        monkeypatch.delenv("AGENTICORE_MCP_LIB_PATH", raising=False)
        result = materialize_profile(minimal_profile)
        assert result == minimal_profile.path

    def test_mcp_lib_forces_materialization(self, monkeypatch, tmp_path, minimal_profile):
        """AGENTICORE_MCP_LIB_PATH set → simple profile materializes into per-job dir."""
        lib_dir = tmp_path / "mcp-lib"
        lib_dir.mkdir()
        (lib_dir / "extra.json").write_text('{"mcpServers": {"extra": {}}}')
        monkeypatch.setenv("AGENTICORE_MCP_LIB_PATH", str(lib_dir))
        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)

        result = materialize_profile(minimal_profile, job_id="test-mcp-lib")

        assert result != minimal_profile.path
        assert result is not None

    def test_mcp_lib_merges_servers(self, monkeypatch, tmp_path, minimal_profile):
        """MCP lib servers are merged into the materialized .mcp.json."""
        lib_dir = tmp_path / "mcp-lib"
        lib_dir.mkdir()
        (lib_dir / "cluster.json").write_text('{"mcpServers": {"cluster-tool": {"command": "ct"}}}')
        monkeypatch.setenv("AGENTICORE_MCP_LIB_PATH", str(lib_dir))
        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)

        result = materialize_profile(minimal_profile, job_id="merge-lib-test")

        assert result is not None
        mcp_data = json.loads((result / ".mcp.json").read_text())
        assert "cluster-tool" in mcp_data["mcpServers"]

    def test_mcp_lib_multiple_files(self, monkeypatch, tmp_path, minimal_profile):
        """Multiple lib files → all servers merged."""
        lib_dir = tmp_path / "mcp-lib"
        lib_dir.mkdir()
        (lib_dir / "a.json").write_text('{"mcpServers": {"srv-a": {}}}')
        (lib_dir / "b.json").write_text('{"mcpServers": {"srv-b": {}}}')
        monkeypatch.setenv("AGENTICORE_MCP_LIB_PATH", str(lib_dir))
        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)

        result = materialize_profile(minimal_profile, job_id="multi-lib-test")

        assert result is not None
        mcp_data = json.loads((result / ".mcp.json").read_text())
        assert "srv-a" in mcp_data["mcpServers"]
        assert "srv-b" in mcp_data["mcpServers"]


@pytest.mark.unit
class TestCopyProfileChainTo:
    def test_copies_claude_dir(self, tmp_path, minimal_profile):
        """_copy_profile_chain_to copies .claude/ from each profile in chain."""
        target = tmp_path / "target"
        target.mkdir()

        created = _copy_profile_chain_to([minimal_profile], target)

        assert (target / ".claude").exists()
        assert len(created) > 0

    def test_skips_profiles_with_no_path(self, tmp_path):
        """Profiles without a path are skipped silently."""
        profile_no_path = Profile(name="noop", claude=ProfileClaude(), path=None)
        target = tmp_path / "target"
        target.mkdir()

        created = _copy_profile_chain_to([profile_no_path], target)

        assert created == []

    def test_returns_created_paths(self, tmp_path, minimal_profile):
        """Returns list of paths that were created."""
        target = tmp_path / "target"
        target.mkdir()

        created = _copy_profile_chain_to([minimal_profile], target)

        assert all(p.exists() for p in created)
