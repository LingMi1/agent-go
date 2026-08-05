"""分支工具调用硬预算（make_think_node max_tool_calls）。

背景（实测）：DeepSeek 每轮并行发大量工具调用×研究分支数，单 run 曾 961 次 web_fetch——
拖满 RUN_TIMEOUT_S、烧穿 Tavily 免费额度与模型余额；提示词软约束收敛有限。
预算语义：已执行 ToolMessage 数达上限 → think 前置「预算已尽」指令并以
tool_choice="none" 调用（模型只能收口）；0=不限（默认关不影响既有行为的测试基线）。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cognition.graphs.nodes import make_think_node


class _CaptureModel:
    """记录 invoke 入参的桩模型（think 只用 .invoke，鸭子类型即可）。"""

    def __init__(self) -> None:
        self.messages = None
        self.kwargs: dict = {}

    def invoke(self, messages, **kwargs):  # noqa: ANN001
        self.messages = list(messages)
        self.kwargs = kwargs
        return AIMessage(content="收口结论")


def _ai_with_call(cid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "web_fetch", "args": {"url": "https://x"}, "id": cid}])


def _state(n_tool_msgs: int) -> dict:
    msgs = [HumanMessage(content="任务")]
    for i in range(n_tool_msgs):
        msgs += [_ai_with_call(f"c{i}"), ToolMessage(content="ok", tool_call_id=f"c{i}")]
    return {"messages": msgs, "step": n_tool_msgs}


def test_budget_not_reached_no_forcing():
    m = _CaptureModel()
    think = make_think_node(m, max_tool_calls=5)  # type: ignore[arg-type]
    think(_state(4))
    assert "tool_choice" not in m.kwargs
    assert not any("预算已用尽" in str(getattr(x, "content", "")) for x in m.messages)


def test_budget_reached_forces_tool_choice_none_with_note():
    m = _CaptureModel()
    think = make_think_node(m, max_tool_calls=3)  # type: ignore[arg-type]
    out = think(_state(3))
    assert m.kwargs.get("tool_choice") == "none"
    head = m.messages[0]
    assert isinstance(head, SystemMessage) and "预算已用尽" in str(head.content) and "3 次" in str(head.content)
    assert out["messages"][0].content == "收口结论"


def test_budget_zero_means_unlimited():
    m = _CaptureModel()
    think = make_think_node(m, max_tool_calls=0)  # type: ignore[arg-type]
    think(_state(50))
    assert "tool_choice" not in m.kwargs
