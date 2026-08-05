# 可观测：OTel 链路追踪 + Grafana 看板

本目录是 **docs/18** 的部署产物：单一事实源的 Grafana provisioning（datasource + dashboard）、
Tempo 配置、Prometheus scrape 片段。**默认全关**——不启用时两面服务零行为变化、零性能开销。

```
observability/
├── tempo/tempo.yaml                    # Tempo 单体配置（OTLP 4317/4318 receiver + 本地存储）
├── prometheus/my-agent-scrape.yaml     # Prometheus job 片段（抓控制面 /metrics）
├── grafana/
│   ├── datasources/datasources.yaml    # Prometheus + Tempo datasource（固定 uid）
│   └── dashboards/
│       ├── dashboards.yaml             # dashboard provider（从目录加载 JSON）
│       └── my-agent-overview.json      # 控制面概览看板（8 面板，读 myagent_ 指标）
└── README.md
```

## 架构（docs/18 §0、§3.2）

一条 run = 一根 trace，跨 Go↔Python 一条链：Go 在 `dispatch.Run` 建 `agent.run` 根 span →
gRPC `otelgrpc` stats handler 把 W3C `traceparent` 写进 outgoing metadata → Python gRPC 拦截器
提取并建 server span 覆盖 `servicer.Run`。两面 **OTLP 直连 Tempo**（无 collector 中转）。
两面结构化日志都带同一 `trace_id`（32 位 hex），可跨进程 grep。

指标与 trace 互补但不重叠：指标走既有 Prometheus（`internal/metrics` 的 13 个 `myagent_`，docs/11），
本篇只做 trace，**不碰 OTel metrics**（docs/18 §4.2）。

## 起 Tempo（trace 后端）

Tempo 挂 compose 的 `observability` profile（默认 `up` 不起它）：

```bash
# 起完整平台 + Tempo（trace 默认仍关，仅后端就绪）
docker compose --profile app --profile observability up -d

# 开启导出 + 冒烟（Go 根 span 开箱即导出到 tempo:4317）
OTEL_ENABLED=1 docker compose --profile app --profile observability up -d
```

Tempo 发布到宿主机：查询 API `localhost:3200`、OTLP/gRPC `localhost:4317`。

## 挂到你现有的 global-grafana

global-grafana / global-prometheus 在独立 `global-network`（docs/18 §1）。把本目录的 provisioning
文件挂进 global-grafana 容器，让它读 my-agent 的指标与 trace：

1. **Datasource**：把 `grafana/datasources/datasources.yaml` 放进 global-grafana 的
   `/etc/grafana/provisioning/datasources/`。
   - `Prometheus`（uid=`prometheus`）→ `http://global-prometheus:9090`（global 栈内兄弟容器；按你的容器名改）。
   - `Tempo`（uid=`tempo`）→ `http://host.docker.internal:3200`（跨网络经宿主机访问 my-agent 的 Tempo）。
2. **Dashboard**：把 `grafana/dashboards/dashboards.yaml` 放进
   `/etc/grafana/provisioning/dashboards/`，并把 `my-agent-overview.json` 挂到 provider 的 path
   （`/etc/grafana/provisioning/dashboards/my-agent`）。
3. **Prometheus scrape**：把 `prometheus/my-agent-scrape.yaml` 的 job 追加进 global-prometheus 的
   `prometheus.yml` 的 `scrape_configs:`（照现有 `host.docker.internal` 抓 host 端口范式），reload。

dashboard 面板按 datasource **uid** 引用（非名字），故 provisioned uid 固定即开箱可用。

看板面板（读 `myagent_` 指标）：进行中 run 数（并发水位）· run 速率 by status · run 时长分位
p50/p95/p99 · 准入拒绝速率（429）· HTTP 5xx 错误率 · 事件泵异常 by code · run 速率 by
agent_type · PG 连接池水位。

## 认知面追踪（Python server span）

Go 控制面的 otel 依赖已编入镜像，`OTEL_ENABLED=1` 开箱即导出 `agent.run` 根 span。
**认知面基础镜像不含 opentelemetry**（otel 属 `pyproject.toml` 的 `otel` optional 组，保 import-guard）——
启用后 `otel_seam` 会 import-guard 降级为 no-op（server span 不产，run 不受影响）。要真正导出
Python server span，需让认知面环境装上 otel extra：

- **host 进程开发流**：`cd cognition && uv sync --extra otel`，再
  `OTEL_ENABLED=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 make cognition`。
- **容器**：在 `deploy/Dockerfile.cognition` 的 `uv sync` 加 `--extra otel`（或用构建参数门控），重建镜像。
  暂未默认加入以保持镜像精简（docs/18 §3.5「不入核心 deps」）；属可选部署步骤。

## 冒烟核验（docs/18 §6 完成判据）

`OTEL_ENABLED=1` 跑一轮 run 后，在 Grafana 的 Tempo 里按 service `my-agent-control-plane` 搜最近
trace：应见 Go `agent.run` 根 span；若认知面也装了 otel extra，同一 trace 下挂 Python server span，
两面日志 `trace_id` 一致。（server-streaming span 可能以 `CANCELLED` 收尾，正常现象，见 docs/18 §4.3。）
