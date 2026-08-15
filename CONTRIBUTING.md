# Contributing to agent-go

> [简体中文](CONTRIBUTING.zh-CN.md)

Thanks for the interest in contributing. agent-go is a full-stack multi-agent platform split across three runtimes: a Go control plane (`control-plane/`), a Python cognition plane (`cognition/`), and a React frontend (`web/`).

## Prerequisites

- **Go** 1.25+
- **Python** 3.12+ (managed with [uv](https://docs.astral.sh/uv/))
- **Node.js** 20+ (with npm)
- **Docker** (for the Postgres / Qdrant / Redis / MinIO infrastructure)

## Local development

```bash
git clone https://github.com/LingMi1/agent-go.git
cd agent-go

# 1. Infrastructure (Postgres, Qdrant, Redis, MinIO)
make infra-up

# 2. Control plane (Go, :8080)
make control

# 3. Cognition plane (Python, :50051)
make cognition

# 4. Frontend (React, :5173)
make web
```

The Makefile sources `deploy/.env` before launching the Go and Python processes. Copy `deploy/.env.example` to `deploy/.env` first — the default uses fake models, so no API key is required.

## Testing

```bash
# All three runtimes at once
make check

# Or individually
cd control-plane && go test -race -count=1 ./...
cd cognition && uv run pytest -q
cd web && npm run test
```

`make check` needs `TEST_PG_DSN` to point at a dedicated test database (`my_agent_test`) because the integration tests TRUNCATE tables — never point it at a database holding real conversation history. Run `make test-db` once to create it.

## Linting

```bash
cd control-plane && go vet ./...
cd cognition && uv run ruff check .
cd web && npm run typecheck
```

## End-to-end tests

Playwright drives a real browser against the fake-model stack:

```bash
make e2e   # first run: cd web && npx playwright install chromium
```

## Regenerating protobuf stubs

Both planes share `proto/agent/v1/agent.proto`. Regenerate the Go and Python stubs with:

```bash
make proto
```

## Pull request guidelines

- Open PRs against `main`.
- Use **conventional commit** messages (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
- Keep PRs focused: one logical change per PR.
- Add or update tests for any behavioral change.

## CI requirements

Every PR must pass the three-language gates before merging: `go vet` + `go test -race`, `ruff` + `pytest`, `tsc --noEmit` + frontend tests, plus Docker image builds for both planes. Run `make check` locally to catch issues early.
