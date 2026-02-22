"""Unit tests for profiles module."""

import json
import warnings

import pytest

from agenticore.profiles import (
    Profile,
    ProfileClaude,
    build_cli_args,
    load_profiles,
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


@pytest.mark.unit
class TestLoadProfiles:
    def test_loads_bundled_defaults(self):
        profiles = load_profiles()
        assert "code" in profiles
        assert "review" in profiles

    def test_code_profile_values(self):
        profiles = load_profiles()
        code = profiles["code"]
        assert code.name == "code"
        assert code.claude.model == "sonnet"
        assert code.claude.max_turns == 80
        assert code.claude.worktree is True
        assert code.auto_pr is True
        assert code.path is not None
        assert code._legacy is False

    def test_review_profile_values(self):
        profiles = load_profiles()
        review = profiles["review"]
        assert review.name == "review"
        assert review.claude.model == "haiku"
        assert review.claude.max_turns == 20
        assert review.auto_pr is False

    def test_profile_has_directory_path(self):
        profiles = load_profiles()
        code = profiles["code"]
        assert code.path is not None
        assert code.path.is_dir()
        assert (code.path / "profile.yml").exists()
        assert (code.path / ".claude").is_dir()


@pytest.mark.unit
class TestMaterializeProfile:
    def test_materialize_copies_claude_dir(self, tmp_path):
        profiles = load_profiles()
        code = profiles["code"]
        working_dir = tmp_path / "repo"
        working_dir.mkdir()

        created = materialize_profile(code, working_dir)

        assert (working_dir / ".claude" / "settings.json").exists()
        assert (working_dir / ".claude" / "CLAUDE.md").exists()
        assert len(created) > 0

    def test_materialize_copies_mcp_json(self, tmp_path):
        profiles = load_profiles()
        code = profiles["code"]
        working_dir = tmp_path / "repo"
        working_dir.mkdir()

        materialize_profile(code, working_dir)

        assert (working_dir / ".mcp.json").exists()
        with open(working_dir / ".mcp.json") as f:
            data = json.load(f)
        assert "mcpServers" in data

    def test_materialize_overlays_existing(self, tmp_path):
        """Profile files overlay on top of existing repo .claude/ files."""
        profiles = load_profiles()
        code = profiles["code"]
        working_dir = tmp_path / "repo"
        working_dir.mkdir()

        # Pre-existing repo .claude/ file
        claude_dir = working_dir / ".claude"
        claude_dir.mkdir()
        (claude_dir / "existing.txt").write_text("keep me")

        materialize_profile(code, working_dir)

        # Original file preserved
        assert (claude_dir / "existing.txt").read_text() == "keep me"
        # Profile files added
        assert (claude_dir / "settings.json").exists()

    def test_materialize_merges_mcp_json(self, tmp_path):
        """If repo already has .mcp.json, servers are merged."""
        profiles = load_profiles()
        code = profiles["code"]
        working_dir = tmp_path / "repo"
        working_dir.mkdir()

        # Pre-existing .mcp.json with a server
        existing = {"mcpServers": {"existing": {"command": "node", "args": ["server.js"]}}}
        with open(working_dir / ".mcp.json", "w") as f:
            json.dump(existing, f)

        materialize_profile(code, working_dir)

        with open(working_dir / ".mcp.json") as f:
            data = json.load(f)
        # Existing server preserved
        assert "existing" in data["mcpServers"]

    def test_materialize_legacy_returns_empty(self, tmp_path):
        """Legacy profiles don't materialize any files."""
        profile = Profile(name="old", _legacy=True)
        working_dir = tmp_path / "repo"
        working_dir.mkdir()

        created = materialize_profile(profile, working_dir)

        assert created == []
        assert not (working_dir / ".claude").exists()

    def test_materialize_settings_content(self, tmp_path):
        profiles = load_profiles()
        code = profiles["code"]
        working_dir = tmp_path / "repo"
        working_dir.mkdir()

        materialize_profile(code, working_dir)

        with open(working_dir / ".claude" / "settings.json") as f:
            settings = json.load(f)
        assert "permissions" in settings
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

        working_dir = tmp_path / "repo"
        working_dir.mkdir()
        materialize_profile(resolved, working_dir, all_profiles=all_profiles)

        # Child's CLAUDE.md overwrites base's
        assert (working_dir / ".claude" / "CLAUDE.md").read_text() == "# Child instructions"
        # Base-only file is still present
        assert (working_dir / ".claude" / "base_only.txt").read_text() == "base file"


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
