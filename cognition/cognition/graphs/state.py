"""贯穿 ReAct 图的类型化状态（= 原项目 AgentContext）与路由纯函数。

`messages` 是唯一的 reducer 通道（add_messages：按 id 追加/覆盖消息）。其余字段为普通
覆盖语义。`product_files` 是预留的产物列表 seam（M1 恒空）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import END
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """ReAct 图状态。"""

    # 唯一的 reducer 通道：think 产出的 AIMessage、tools 产出的 ToolMessage 都并入此处。
    messages: Annotated[list[AnyMessage], add_messages]

    request_id: str          # run 的规范身份（事件归属）
    session_id: str          # 会话/归属 + LangGraph thread 作用域
    query: str               # 用户输入
    product_files: list[Any]  # 产物列表（M1 恒空，seam）
    is_stream: bool          # 是否流式（M1 恒 True）
    step: int                # ReAct 步序（think 节点每轮 +1）


def route_after_agent(
    state: AgentState, max_steps: int
) -> Literal["tools", "__end__"]:
    """think 节点之后的条件路由（纯函数，便于单测）。

    规则：最后一条 AIMessage 带 tool_calls **且** step < max_steps → 走 "tools"；
    否则结束（END == "__end__"）。
    """
    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    has_tool_calls = bool(getattr(last, "tool_calls", None))
    if has_tool_calls and int(state.get("step", 0)) < int(max_steps):
        return "tools"
    return END
