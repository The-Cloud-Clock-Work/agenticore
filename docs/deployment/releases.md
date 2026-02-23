---
title: Releases
nav_order: 4
---

# Releases and CI/CD

Agenticore follows semantic versioning and provides multiple distribution methods:
PyPI package, Docker image, and Helm chart.

## Versioning

Agenticore uses [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH).

| Location | Purpose |
|----------|---------|
| `agenticore/__init__.py` | `__version__ = "0.1.0"` — runtime version |
| `pyproject.toml` | `version = "0.1.0"` — package metadata |
| `helm/agenticore/Chart.yaml` | `version: 0.1.0` — chart version |
| `helm/agenticore/Chart.yaml` | `appVersion: "0.1.0"` — app version |

All four locations must be updated together for a release.

## Development Setup

```bash
# Clone
git clone https://github.com/The-Cloud-Clock-Work/agenticore.git
cd agenticore

# Install with dev dependencies
pip install -e ".[dev]"
```

### Dependencies

**Runtime:**

| Package | Version | Purpose |
|---------|---------|---------|
| `fastmcp` | >= 2.0 | MCP server framework |
| `redis` | >= 7.0 | Redis client |
| `uvicorn[standard]` | >= 0.30 | ASGI server |
| `httpx` | >= 0.25 | HTTP client (CLI) |
| `pyyaml` | >= 6.0 | YAML config parser |

**Dev:**

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest` | >= 8.0 | Test runner |
| `pytest-cov` | >= 4.0 | Coverage reporting |
| `pytest-asyncio` | >= 0.23 | Async test support |
| `fakeredis` | >= 2.21 | Redis mocking |
| `ruff` | >= 0.4 | Linter + formatter |

## Test Suite

```bash
# Run unit tests with coverage
pytest tests/unit -v -m unit --cov=agenticore

# Run integration tests (requires Docker)
pytest tests/ -v -m integration

# Run all tests
pytest tests/ -v
```

### Test Markers

| Marker | Description | External Deps |
|--------|-------------|---------------|
| `unit` | No external dependencies | None |
| `integration` | Requires Docker services | Docker, Redis |

### Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "unit: no external deps",
    "integration: requires Docker",
]
```

## Linting

```bash
# Check for issues
ruff check agenticore/ tests/

# Check formatting
ruff format --check agenticore/ tests/

# Auto-fix
ruff check --fix agenticore/ tests/
ruff format agenticore/ tests/
```

Ruff is configured for Python 3.12 with a 120-character line length.

## Self-Update

The `agenticore update` command upgrades the installed package:

```bash
# From PyPI
agenticore update

# From git
agenticore update --source git+https://github.com/The-Cloud-Clock-Work/agenticore.git

# From local path
agenticore update --source /path/to/agenticore
```

The command runs `pip install --upgrade <source>` and reports the version change.

## Docker Image Build

```bash
# Build image
docker build -t agenticore:latest .

# Build and run with compose
docker compose up --build -d
```

The Dockerfile is based on `python:3.12-slim` and includes `git`, `curl`, and
the `gh` CLI for auto-PR functionality.

## Release Checklist

1. Update version in all 4 locations (see Versioning table above)
2. Run tests: `pytest tests/unit -v -m unit --cov=agenticore`
3. Run linter: `ruff check agenticore/ tests/`
4. Build Docker image: `docker build -t agenticore:v{version} .`
5. Test Docker deployment: `docker compose up --build -d`
6. Tag release: `git tag v{version}`
7. Push: `git push origin main --tags`
