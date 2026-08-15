# Security Policy

> [简体中文](SECURITY.zh-CN.md)

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Report vulnerabilities privately via either:
- **GitHub's private vulnerability reporting** (the *Security* tab → *Report a vulnerability*), or
- **Email**: `LingMi1@users.noreply.github.com`

### What to include

- A description of the issue and its potential impact
- Steps to reproduce (minimal example, affected endpoint/file)
- Affected version or commit SHA
- Any suggested fix or mitigation

## Response timeline

- **Acknowledgement**: within 48 hours.
- **Initial assessment**: within 5 business days.
- A fix or advisory is coordinated with you before any public disclosure.

## Scope

agent-go handles several security-sensitive surfaces. Please pay particular attention to:
- **Authentication & authorization** — bcrypt hashing, Bearer tokens, RBAC, and per-owner resource isolation (`WHERE owner_id = ?` on every query).
- **Prompt injection** — the three-layer defense (input detection, prompt isolation, output filtering) in the cognition plane.
- **Secret handling** — AES-256-GCM encryption of stored secrets, and the SSRF guard on `web_fetch`.
- **Skill sandboxing** — the Docker-based skill executor and its enable/disable boundary.

## Production deployment notes

When deploying agent-go, treat secrets as sensitive:
- Keep `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` and the Postgres / Qdrant / Redis / MinIO credentials in your secret manager — never commit them or bake them into images.
- Change the default `deploy/.env` values before exposing anything publicly.
- Keep the control plane behind TLS when it is reachable from outside.

This project is MIT licensed. Security reports are handled by the maintainer, LingMi1.
