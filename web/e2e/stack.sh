#!/usr/bin/env bash
# E2E 被测栈编排（docs/11 §4.1）：FAKE 全家桶 + 独立 my_agent_test 库 + 单端口 :18080。
#
# 由 playwright.config.ts 的 webServer 启停：本脚本先后台起认知面（:15051，等 TCP 可达），
# 再前台驻留控制面（:18080，WEB_DIR 托管 web/dist）；Playwright 用 /healthz 判 ready，
# 结束时向本脚本发信号，trap 连带清理认知面子进程。
#
# hermetic 红线：强制 FAKE 全家桶 + unset 一切真实外呼 key——E2E 期间任何一跳都不许
# 触真实 LLM/生图/embedding API（否则上传图片可能触发真 vision OCR 入真库）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# —— 1. 读 deploy/.env（MinIO 凭据/端口等本机基础设施配置，与 Makefile 的 LOAD_ENV 同源）——
set -a
# shellcheck disable=SC1091
[ -f "$ROOT/deploy/.env" ] && . "$ROOT/deploy/.env"
set +a

# —— 2. 强制覆盖 E2E 专属配置（.env 里的开发值一律让位）——
# 独立测试库：E2E 真实写 runs/events/checkpoint，绝不许打到开发库（GuardTestDSN 同源教训）。
export PG_DSN="postgres://agent:agent_pwd@localhost:55432/my_agent_test"
export COGNITION_PG_DSN="$PG_DSN"
export COGNITION_FAKE_MODEL=1
export COGNITION_IMAGE_GEN_PROVIDER=fake
export COGNITION_MINIO_UPLOAD_ENABLED=true
# 封死一切真实外呼（.env 里有真实 key；fake 模式本不该触网，unset 是第二道保险）。
unset ANTHROPIC_API_KEY DEEPSEEK_API_KEY IMAGE_GEN_API_KEY OPENAI_API_KEY \
    ARK_API_KEY DASHSCOPE_API_KEY SILICONFLOW_API_KEY IMAGE_GEN_PROVIDER \
    COGNITION_ANTHROPIC_API_KEY COGNITION_DEEPSEEK_API_KEY COGNITION_IMAGE_GEN_API_KEY || true
# RAG 供给链全 fake（零模型下载、零外呼）；独立 e2e 集合——开发集合是 fastembed 512 维，
# fake 是 64 维，共用集合会维度冲突。
export COGNITION_EMBEDDING_PROVIDER=fake
export COGNITION_EMBEDDING_DIMENSION=64
export COGNITION_SPARSE_PROVIDER=fake
export COGNITION_RERANK_PROVIDER=fake
export COGNITION_QDRANT_COLLECTION=cognition_docs_e2e

# 独立端口（不与 dev :50051/:8080 冲突，可并行开发）；单端口托管 build 产物（prod 形态）。
export COGNITION_GRPC_PORT=15051
export COGNITION_ADDR="localhost:15051"
export HTTP_ADDR=":18080"
export WEB_DIR="$ROOT/web/dist"

[ -d "$WEB_DIR" ] || { echo "[e2e-stack] 缺 $WEB_DIR —— 先 make web-build（或 make e2e）" >&2; exit 1; }

# —— 3. 测试库不存在则创建（幂等）——
docker exec my-agent-postgres psql -U agent -d my_agent -c "CREATE DATABASE my_agent_test" >/dev/null 2>&1 || true

# —— 4. 清掉 e2e 专用端口上的残留进程（上次异常退出的孤儿）——
for port in 15051 18080; do
  stale="$(lsof -ti "tcp:${port}" || true)"
  if [ -n "$stale" ]; then
    echo "[e2e-stack] 清理 :${port} 残留进程 ${stale}" >&2
    # shellcheck disable=SC2086
    kill $stale 2>/dev/null || true
    sleep 1
  fi
done

# —— 5. 预编译控制面（比 go run 少一层进程包装，信号直达、退出干净）——
BIN="$ROOT/web/e2e/.cache/controlplane"
mkdir -p "$(dirname "$BIN")"
(cd "$ROOT/control-plane" && go build -o "$BIN" ./cmd/controlplane)

# —— 6. 后台起认知面；trap 保证其随本脚本退出（Playwright 停 webServer 即全栈收摊）——
(cd "$ROOT/cognition" && exec uv run python -m cognition.server.grpc_server) &
COG_PID=$!
CTRL_PID=""
cleanup() {
  [ -n "$CTRL_PID" ] && kill "$CTRL_PID" 2>/dev/null || true
  kill "$COG_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# 等 :15051 TCP 可达（fake 全家桶装配很快，仍给 120s 裕量；认知面死了立即失败）。
ready=0
for _ in $(seq 1 240); do
  if ! kill -0 "$COG_PID" 2>/dev/null; then echo "[e2e-stack] cognition 启动失败" >&2; exit 1; fi
  if nc -z localhost 15051 >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
[ "$ready" = 1 ] || { echo "[e2e-stack] 等待 cognition :15051 超时" >&2; exit 1; }
echo "[e2e-stack] cognition ready on :15051, starting control-plane on :18080" >&2

# —— 7. 控制面驻留（healthz 200 后 Playwright 开跑；残余就绪竞态由用例内 expect.poll 兜底）——
"$BIN" &
CTRL_PID=$!
wait "$CTRL_PID"
