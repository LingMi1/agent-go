# 参与 agent-go 开发

> [English](CONTRIBUTING.md)

感谢你的参与兴趣。agent-go 是一个跨三种运行时的全栈多智能体平台：Go 控制面（`control-plane/`）、Python 认知面（`cognition/`）和 React 前端（`web/`）。

## 环境准备

- **Go** 1.25+
- **Python** 3.12+（用 [uv](https://docs.astral.sh/uv/) 管理）
- **Node.js** 20+（含 npm）
- **Docker**（跑 Postgres / Qdrant / Redis / MinIO 基础设施）

## 本地开发

```bash
git clone https://github.com/LingMi1/agent-go.git
cd agent-go

# 1. 基础设施（Postgres、Qdrant、Redis、MinIO）
make infra-up

# 2. 控制面（Go，:8080）
make control

# 3. 认知面（Python，:50051）
make cognition

# 4. 前端（React，:5173）
make web
```

Makefile 会在启动 Go 和 Python 进程前读取 `deploy/.env`。先把 `deploy/.env.example` 复制成 `deploy/.env` —— 默认走 fake 模型，不用 API key 就能跑起来。

## 测试

```bash
# 三种运行时一次跑完
make check

# 或者分开跑
cd control-plane && go test -race -count=1 ./...
cd cognition && uv run pytest -q
cd web && npm run test
```

注意：`make check` 要求 `TEST_PG_DSN` 指向独立的测试库（`my_agent_test`），因为集成测试会 TRUNCATE 表 —— 千万别指到存有真实对话历史的库。首次先跑一次 `make test-db` 创建它。

## 代码检查

```bash
cd control-plane && go vet ./...
cd cognition && uv run ruff check .
cd web && npm run typecheck
```

## 端到端测试

Playwright 会在 fake 模型全家桶上跑真实浏览器：

```bash
make e2e   # 首次先执行：cd web && npx playwright install chromium
```

## 重新生成 protobuf stub

控制面和认知面共用 `proto/agent/v1/agent.proto`。重新生成 Go 和 Python 两端的 stub：

```bash
make proto
```

## 提交 PR 的约定

- 往 `main` 分支提 PR。
- 提交信息遵循 **conventional commit**（`feat:`、`fix:`、`refactor:`、`test:`、`docs:`、`chore:`）。
- 一个 PR 只做一件事。
- 有行为变更就要补或改测试。

## CI 门禁

每个 PR 合入前必须通过三语言门禁：`go vet` + `go test -race`、`ruff` + `pytest`、`tsc --noEmit` + 前端测试，外加两个平面的 Docker 镜像构建。本地先 `make check` 过一遍能少踩坑。
