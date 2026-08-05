TEST_PG_DSN ?= postgres://agent:agent_pwd@localhost:55432/my_agent_test

.PHONY: help proto proto-tools proto-go proto-py infra-up infra-down cognition control web check test-db web-build e2e stack-up stack-down stack-down-all

# 读取 deploy/.env 并导出给应用进程（deploy/.env 不提交；含密钥与模型/连接配置）。
LOAD_ENV = set -a; [ -f deploy/.env ] && . ./deploy/.env; set +a;

help: ## 列出可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

cognition: ## 启动认知面（Python gRPC :50051，读 deploy/.env：DeepSeek + checkpointer）
	@bash -c '$(LOAD_ENV) cd cognition && uv run python -m cognition.server.grpc_server'

control: ## 启动控制面（Go HTTP/SSE :8080，读 deploy/.env）
	@bash -c '$(LOAD_ENV) cd control-plane && go run ./cmd/controlplane'

web: ## 启动前端（Vite :5173，代理到 :8080）
	cd web && npm run dev

check: ## 跑全部测试（Go + Python + 前端纯逻辑）
	# TEST_PG_DSN 必须指向独立测试库 my_agent_test——集成测试会 TRUNCATE runs/events，
	# 指向开发库会清空真实对话历史（踩过的坑）。首次：make test-db
	cd control-plane && TEST_PG_DSN=$(TEST_PG_DSN) go test ./...
	cd cognition && uv run pytest -q
	cd web && npm run test

test-db: ## 创建独立测试数据库（集成测试专用，防 TRUNCATE 清掉开发库对话历史）
	docker exec my-agent-postgres psql -U agent -d my_agent -c "CREATE DATABASE my_agent_test" || true

proto-tools: ## 安装 Go 的 protobuf 插件（首次/CI 用）
	go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
	go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest

proto-go: ## 生成 Go stub（buf + 本地插件）
	PATH="$(shell go env GOPATH)/bin:$$PATH" buf generate

proto-py: ## 生成 Python stub（grpcio-tools）
	mkdir -p cognition/cognition/genproto
	cd cognition && uv run python -m grpc_tools.protoc \
		-I ../proto \
		--python_out=cognition/genproto \
		--grpc_python_out=cognition/genproto \
		--pyi_out=cognition/genproto \
		../proto/agent/v1/agent.proto
	find cognition/cognition/genproto -type d -exec touch {}/__init__.py \;

proto: proto-go proto-py ## 生成双端 stub

infra-up: ## 起本地依赖（postgres/qdrant/redis/minio/nats）
	cd deploy && docker compose up -d

infra-down: ## 停本地依赖
	cd deploy && docker compose down

web-build: ## 前端生产构建（web/dist，供 WEB_DIR 托管）
	cd web && npm run build

e2e: test-db web-build ## E2E 浏览器测试（Playwright；FAKE 全家桶+my_agent_test 库+:18080，docs/11 §4。首次先 cd web && npx playwright install chromium）
	cd web && npx playwright test

stack-up: ## 一键起完整平台（基础设施+控制面+认知面，前端单端口 :8080）
	cd deploy && docker compose --profile app up -d --build

stack-down: ## 只停业务服务（控制面/认知面）；基础设施留给 infra-down
	cd deploy && docker compose --profile app rm -sf control-plane cognition

stack-down-all: ## 停完整平台（业务+基础设施）
	cd deploy && docker compose --profile app down
