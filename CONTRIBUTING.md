# Contributing to Agenticore

Thank you for your interest in contributing to Agenticore! This guide will help you get started.

## Prerequisites

- **Python 3.10+** (3.12 recommended)
- **Docker** (for integration tests and container builds)
- **Redis** (optional — file-based fallback works without it)
- **Git**

## Development Setup

```bash
# Clone the repository
git clone https://github.com/The-Cloud-Clock-Work/agenticore.git
cd agenticore

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

## Running Tests

```bash
# Unit tests
pytest tests/unit -v -m unit

# Unit tests with coverage
pytest tests/unit -v -m unit --cov=agenticore

# All tests (requires Docker)
pytest tests/ -v
```

## Code Style

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
# Check for lint issues
ruff check agenticore/ tests/

# Auto-fix lint issues
ruff check --fix agenticore/ tests/

# Check formatting
ruff format --check agenticore/ tests/

# Auto-format
ruff format agenticore/ tests/
```

## Making Changes

1. **Fork** the repository and create a feature branch from `main`.
2. **Write tests** for any new functionality.
3. **Run the full check suite** before submitting:
   ```bash
   ruff check agenticore/ tests/ && ruff format --check agenticore/ tests/
   pytest tests/unit -v -m unit
   ```
4. **Commit** with a clear, descriptive message.
5. **Open a Pull Request** against `main`.

## Pull Request Process

- Fill out the PR template completely.
- Ensure CI passes (lint, tests, build).
- Keep PRs focused — one logical change per PR.
- Update documentation if your change affects public APIs or configuration.

## Project Structure

| Module | Purpose |
|--------|---------|
| `agenticore/server.py` | FastMCP server + REST routes |
| `agenticore/config.py` | YAML config loader + env overrides |
| `agenticore/profiles.py` | Profile packages → CLI flags |
| `agenticore/repos.py` | Git clone/fetch with flock |
| `agenticore/jobs.py` | Job store (Redis + file fallback) |
| `agenticore/runner.py` | Spawn Claude subprocess with OTEL |
| `agenticore/router.py` | Code fast-path + AI fallback |
| `agenticore/pr.py` | Auto-PR (git push + gh pr create) |

## Reporting Issues

- Use the [bug report template](https://github.com/The-Cloud-Clock-Work/agenticore/issues/new?template=bug_report.md) for bugs.
- Use the [feature request template](https://github.com/The-Cloud-Clock-Work/agenticore/issues/new?template=feature_request.md) for ideas.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you agree to uphold this code.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
