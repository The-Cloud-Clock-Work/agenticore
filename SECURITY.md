# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

**Do not open a public issue.**

Instead, email **security@thecloudclockwork.com** with:

- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge your report within **48 hours** and aim to provide a fix
within **7 days** for critical issues.

## Security Best Practices

When deploying Agenticore:

- **Never expose the MCP/REST server to the public internet** without authentication.
- **Use environment variables** (not config files) for secrets like API keys and tokens.
- **Run the Docker container as non-root** (the default `agenticore` user).
- **Keep dependencies updated** — Dependabot is enabled for automated PRs.
- **Use Redis AUTH** in production (`REDIS_URL=redis://:password@host:6379/0`).
- **Review Claude CLI permissions** — `bypassPermissions` should only be set in trusted environments.

## Scope

This policy covers the `agenticore` Python package and its Docker image.
Third-party dependencies are covered by their own security policies.
