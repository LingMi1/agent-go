"""手搓 ReAct 图（think⇄act 环）。

刻意不使用 `create_react_agent`：自掌事件 schema 映射、maxSteps 优雅终止、工具错误→failed
映射；但复用 prebuilt `ToolNode` 执行工具（不重造工具执行）。

拓扑：START → agent →(条件)→ {tools, END}；tools → agent。
"""

from __future__ import annotations

from typing import Optional, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from cognition.graphs.history import HistoryPolicy
from cognition.graphs.nodes import make_think_node
from cognition.graphs.state import AgentState, route_after_agent


def _tool_error_message(exc: Exception) -> str:
    """工具异常 → error ToolMessage 的文案（模型据此决定重试/绕过/收尾）。

    ToolNode 默认只兜 ToolInvocationError（参数校验），运行期异常会 re-raise 炸穿整个
    run，并在 checkpoint 留下悬空 tool_calls 污染会话线程（后续每轮 provider 400）。
    这里显式兜住全部异常：配对保住、单工具失败不拖垮 run（与并行分支错误隔离同哲学）。
    """
    return f"工具执行失败：{type(exc).__name__}: {exc}"


def build_react_graph(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    checkpointer: Optional[BaseCheckpointSaver] = None,
    *,
    max_steps: int = 40,
    history_policy: Optional["HistoryPolicy"] = None,
    expander=None,
    format_prompts: Optional[dict[str, str]] = None,
    max_tool_calls: int = 0,
) -> CompiledStateGraph:
    """装配并编译 ReAct 图。

    Args:
        model: 已（或将）绑定工具的 chat 模型。
        tools: 本地工具列表（用于 ToolNode）。
        checkpointer: 可选 Postgres checkpointer（None 则不持久化，便于测试）。
        max_steps: ReAct 循环上限（注入路由纯函数）。
        history_policy: 可选记忆投影预算（None 则不裁剪，行为同 M1）。
        expander: 可选附件引用块展开投影（attachments.expand_attachment_blocks 闭包）。
        format_prompts: 输出格式模板表（think 按 config.metadata.output_format 临时前置）。
    """
    graph = StateGraph(AgentState)
    graph.add_node(
        "agent",
        make_think_node(
            model,
            history_policy=history_policy,
            expander=expander,
            format_prompts=format_prompts,
            max_tool_calls=max_tool_calls,
        ),
    )
    graph.add_node("tools", ToolNode(list(tools), handle_tool_errors=_tool_error_message))

    graph.add_edge(START, "agent")

    def _route(state: AgentState):
        return route_after_agent(state, max_steps)

    graph.add_conditional_edges("agent", _route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)
