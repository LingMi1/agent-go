"""my-agent 认知面（cognition）：LangGraph 编排 + 工具 + 模型路由 + 事件契约。

M1 骨架：ReAct 图（think⇄act）+ 1 个本地工具（calculator），通过 gRPC 流式
`CognitionService.Run(RunRequest) -> stream Event` 对 Go 控制面暴露。

公开稳定 API（Go 侧 lead 依赖，勿改名）：
- cognition.config.Settings
- cognition.graphs.state.AgentState / route_after_agent
- cognition.graphs.react.build_react_graph
- cognition.tools.registry.get_local_tools
- cognition.events.schema.Event (+ .to_proto())
- cognition.events.mapper.EventMapper
- cognition.providers.router.select_model
- cognition.checkpoint.postgres.build_checkpointer
- cognition.server.grpc_server（可运行入口）
"""

__version__ = "0.1.0"
