"""Unit tests for profiles module."""

import pytest

from agenticore.profiles import (
    Profile,
    ProfileClaude,
    ProfileSettings,
    build_cli_args,
    load_profiles,
    render_template,
    profile_to_dict,
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
        assert "--dangerously-skip-permissions" in args

    def test_no_worktree(self):
        profile = Profile(
            name="local",
            claude=ProfileClaude(worktree=False),
        )
        args = build_cli_args(profile, "task")
        assert "--worktree" not in args

    def test_append_prompt_with_vars(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(),
            append_prompt="Task: {{TASK}}\nRepo: {{REPO_URL}}",
        )
        args = build_cli_args(profile, "fix bug", {"TASK": "fix bug", "REPO_URL": "https://example.com"})
        assert "--append-system-prompt" in args
        idx = args.index("--append-system-prompt")
        assert "fix bug" in args[idx + 1]
        assert "https://example.com" in args[idx + 1]

    def test_settings_permissions(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(),
            settings=ProfileSettings(permissions={"allow": ["Bash(*)", "Read(*)"]}),
        )
        args = build_cli_args(profile, "task")
        assert "--settings" in args
        idx = args.index("--settings")
        import json

        settings = json.loads(args[idx + 1])
        assert settings["permissions"]["allow"] == ["Bash(*)", "Read(*)"]

    def test_output_format(self):
        profile = Profile(
            name="code",
            claude=ProfileClaude(output_format="json"),
        )
        args = build_cli_args(profile, "task")
        assert "--output-format" in args
        assert args[args.index("--output-format") + 1] == "json"


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

    def test_review_profile_values(self):
        profiles = load_profiles()
        review = profiles["review"]
        assert review.name == "review"
        assert review.claude.model == "haiku"
        assert review.claude.max_turns == 20
        assert review.auto_pr is False


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
