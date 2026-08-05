"""gRPC 服务入口（grpc.aio）。

`python -m cognition.server.grpc_server` 在配置端口上启动认知服务。
启动时构建两套图：ReAct（agent_type=react）与 Plan-Execute（agent_type=plan_solve），
由 servicer 按 agent_type 路由。模型按角色分层（router.select_model）；可选 Postgres checkpointer。
COGNITION_FAKE_MODEL=1 时改用确定性脚本化模型（无 LLM key 端到端验证）。
"""

from __future__ import annotations

import asyncio
import logging
import signal

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from cognition._genproto import agent_pb2_grpc
from cognition.checkpoint.postgres import build_checkpointer
from cognition.config import Settings, get_settings
from cognition.graphs.history import HistoryPolicy
from cognition.graphs.plan_execute import build_plan_execute_graph
from cognition.graphs.react import build_react_graph
from cognition.observability import otel_seam
from cognition.providers.router import select_model
from cognition.server.servicer import CognitionServicer
from cognition.sop import default_sop_store
from cognition.tools.registry import build_tool_suite

logger = logging.getLogger(__name__)


async def serve(settings: Settings | None = None) -> None:
    """启动 gRPC 服务并阻塞直至终止。"""
    settings = settings or get_settings()

    # 可选 OTel 追踪（docs/18，默认关/未装即 no-op）：装配全局 TracerProvider + W3C 传播器。
    # 必须在建 server 前，让拦截器建的 server span 走到已注册的 provider。
    otel_seam.setup_tracing(settings)

    # 装配期聚合 local + MCP + Skill 工具；provider_map 注入事件映射，tool_closers 停机时关闭。
    tools, provider_map, tool_closers = await build_tool_suite(settings)

    # —— M11 HITL：受保护工具包装（仅 react 主图用 react_tools；plan 家族用原 tools——
    # executor 分支 except Exception 会吞 GraphInterrupt，审批门在 plan 模式不生效）。
    react_tools = tools
    if settings.approval_tools:
        from cognition.approval import wrap_with_approval

        react_tools = wrap_with_approval(tools, settings.approval_tools, reason=settings.approval_reason)
        logger.info("approval gate on tools: %s", settings.approval_tools)

    if settings.fake_model:
        from cognition.providers.fake import (
            build_fake_executor_model,
            build_fake_model,
            build_fake_plan_model,
        )

        logger.info("using scripted fake model (no LLM key)")
        react_model = build_fake_model().bind_tools(react_tools)
        planner_model = build_fake_plan_model()
        executor_model = build_fake_executor_model().bind_tools(tools)
    else:
        from cognition.graphs.plan_execute import planning_tool

        react_model = select_model("executor", tools=react_tools, settings=settings)
        # planner 必须绑定 planning 工具——否则真实模型只能把计划 JSON 写进正文，
        # 解析失败即永远退化"单步计划=原句"（fake planner 直接产 tool_calls 掩盖过此缺陷）。
        planner_model = select_model("planner", tools=[planning_tool], settings=settings)
        executor_model = select_model("executor", tools=tools, settings=settings)

    aclose = None
    checkpointer = None
    if settings.pg_dsn:
        checkpointer, aclose = await build_checkpointer(settings.pg_dsn)
        logger.info("postgres checkpointer enabled")
    if settings.approval_tools and checkpointer is None:
        # interrupt 依赖 checkpointer；fake/无 PG 场景用内存兜底（同进程恢复，够 e2e）。
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()
        logger.info("approval enabled without PG: using InMemorySaver (same-process resume only)")


    # 会话短期记忆：think 入模型前做「token 预算·近期优先」投影（超阈值折叠旧轮为摘要）。
    history_policy = HistoryPolicy(
        max_messages=settings.history_max_messages, max_chars=settings.history_max_chars
    )

    # —— M8 附件：pro_attachment 引用块的展开投影 + 上传附件入库 ——
    from functools import partial

    from cognition.attachments import MinioDownloader, expand_attachment_blocks, supports_vision
    from cognition.providers.router import _resolve_role

    exec_provider, _ = _resolve_role("executor", settings)
    vision = (not settings.fake_model) and supports_vision(exec_provider)
    expander = partial(
        expand_attachment_blocks, downloader=MinioDownloader(settings), vision=vision
    )
    ingest_fn = None
    if settings.rag_enabled:
        from cognition.attachments import build_ingestor

        ingest_fn = build_ingestor(settings)
    logger.info("attachments: vision=%s(provider=%s) auto_ingest=%s", vision, exec_provider, bool(ingest_fn))

    react_graph = build_react_graph(
        react_model, react_tools, checkpointer=checkpointer,
        max_steps=settings.max_steps, history_policy=history_policy, expander=expander,
        format_prompts=settings.output_format_prompts,
        max_tool_calls=settings.max_tool_calls_per_branch,
    )

    # Plan-Execute：executor 复用一套 ReAct 子图（无 checkpointer，分支级 thread 隔离）。
    # format_prompts 同样注入——executor 分支经 metadata spread 拿到 output_format。
    # max_tool_calls 对每个分支独立生效（分支=一次子图执行，ToolMessage 计数从零起）。
    executor_subgraph = build_react_graph(
        executor_model, tools, max_steps=settings.max_steps,
        history_policy=history_policy, expander=expander,
        format_prompts=settings.output_format_prompts,
        max_tool_calls=settings.max_tool_calls_per_branch,
    )
    plan_graph = build_plan_execute_graph(
        planner_model,
        executor_subgraph,
        tools,
        max_steps=settings.planner_max_steps,
        max_parallel=settings.max_parallel_tasks,
        sop_store=default_sop_store(),
        branch_timeout=settings.branch_timeout_seconds,
        react_recursion_limit=2 * settings.max_steps + 5,
        checkpointer=checkpointer,
        format_prompts=settings.output_format_prompts,
        max_total_branches=settings.max_total_branches,
    )
    # deep_research：同拓扑第二编译图（研究提示词 + 更高轮次预算，executor 子图共享）。
    from cognition.graphs.plan_execute import RESEARCH_PLANNER_SYSTEM

    research_graph = build_plan_execute_graph(
        planner_model,
        executor_subgraph,
        tools,
        max_steps=settings.research_max_steps,
        max_parallel=settings.max_parallel_tasks,
        sop_store=default_sop_store(),
        branch_timeout=settings.branch_timeout_seconds,
        react_recursion_limit=2 * settings.max_steps + 5,
        checkpointer=checkpointer,
        planner_system=RESEARCH_PLANNER_SYSTEM,
        format_prompts=settings.output_format_prompts,
        max_total_branches=settings.max_total_branches,
    )

    # 仅 otel_enabled 且 SDK 可用时挂 aio server 拦截器（从 metadata 提取 traceparent
    # 建 server span 覆盖 servicer.Run）；否则为空列表，等价于无拦截器、零行为变化。
    server = grpc.aio.server(interceptors=otel_seam.build_server_interceptors(settings))
    agent_pb2_grpc.add_CognitionServiceServicer_to_server(
        CognitionServicer(
            react_graph, settings, plan_graph=plan_graph, research_graph=research_graph,
            tool_providers=provider_map, ingest_attachments_fn=ingest_fn,
        ),
        server,
    )
    # 标准 gRPC 健康检查：图装配完成后翻 SERVING，供 Go /healthz 探"业务就绪"。
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    listen = f"{settings.grpc_host}:{settings.grpc_port}"
    server.add_insecure_port(listen)

    await server.start()
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    logger.info("cognition gRPC server listening on %s", listen)

    # 优雅停机
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):
            # 某些平台/非主线程不支持信号处理。
            pass

    try:
        await stop.wait()
    finally:
        await health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
        await server.stop(grace=5.0)
        for close in tool_closers:  # 关闭 MCP worker task / 子进程
            try:
                await close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("tool 资源关闭失败: %s", exc)
        if aclose is not None:
            await aclose()
        logger.info("cognition gRPC server stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
