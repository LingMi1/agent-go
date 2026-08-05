"""工具执行链路回归（真实 ToolNode 路径，不触网）。

背景（线上缺陷）：write_report 声明了显式 args_schema=ReportArgs，但 schema 里没有
tool_call_id 字段——ToolNode 按 args_schema 判定可注入参数，看不到 InjectedToolCallId
注解就不注入 → TypeError 直接炸穿图（M2 的 fake ReAct 只调 calculator，从未走到这条路径）。
同时 ToolNode 默认错误处理只兜 ToolInvocationError，其余异常 re-raise → 整个 run 崩溃、
checkpoint 留下悬空 tool_calls 污染线程。

钉死三件事：
1. write_report 经 ToolNode 注入 tool_call_id 成功执行（LLM 可见 schema 不含注入参数）；
2. 任意工具异常 fail-soft：转 status=error 的 ToolMessage，run 继续走到终态；
3. EventMapper 对 on_tool_error 产出 tool_call(FAILED)+tool_result，事件流不留悬挂的 running。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from cognition.events.mapper import EventMapper
from cognition.events.schema import EventType, ToolCallStatus
from cognition.graphs.react import build_react_graph
from cognition.providers.fake import ScriptedChatModel
from cognition.tools.registry import get_local_tools
from cognition.tools.report import write_report


def _react_state(query: str = "写份报告") -> dict:
    return {
        "messages": [HumanMessage(content=query)],
        "request_id": "run-x",
        "session_id": "s-x",
        "query": query,
        "product_files": [],
        "is_stream": True,
        "step": 0,
    }


def _model_calling(tool_name: str, args: dict) -> ScriptedChatModel:
    return ScriptedChatModel(
        responses=[
            AIMessage(
                content="我来调用工具。",
                tool_calls=[{"name": tool_name, "args": args, "id": "tc-1"}],
            ),
            AIMessage(content="完成。"),
        ]
    )


def test_write_report_llm_schema_hides_injected_args():
    """LLM 面向的 schema 只有 title/content——注入参数绝不能暴露给模型。"""
    assert set(write_report.tool_call_schema.model_fields.keys()) == {"title", "content"}


def test_write_report_executes_via_toolnode():
    """write_report 经真实 ToolNode 执行：tool_call_id 注入 + artifact 归属 run/tool_call。"""
    model = _model_calling("write_report", {"title": "测试报告", "content": "正文"})
    graph = build_react_graph(model, get_local_tools())
    out = asyncio.run(graph.ainvoke(_react_state(), {"metadata": {"request_id": "run-x"}}))

    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    tm = tool_msgs[0]
    assert tm.status != "error", f"write_report 不应失败：{tm.content}"
    assert tm.artifact and tm.artifact["resource_key"] == "run-x/tc-1/测试报告.md"
    # run 正常走到最终答复（不炸穿图）。
    assert out["messages"][-1].content == "完成。"


@tool("explode")
def explode(x: str) -> str:
    """总是抛异常的工具（模拟运行期缺陷/下游故障）。"""
    raise RuntimeError(f"boom: {x}")


def test_tool_exception_is_fail_soft():
    """工具运行期异常 → error ToolMessage（配对保住、run 不崩），模型可继续收尾。"""
    model = _model_calling("explode", {"x": "1"})
    graph = build_react_graph(model, [explode])
    out = asyncio.run(graph.ainvoke(_react_state("炸一下"), {"metadata": {"request_id": "run-x"}}))

    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].status == "error"
    assert tool_msgs[0].tool_call_id == "tc-1"  # tool_use↔tool_result 配对完好
    assert "boom" in str(tool_msgs[0].content)
    assert out["messages"][-1].content == "完成。"


@pytest.mark.asyncio
async def test_mapper_emits_failed_pair_on_tool_error():
    """工具异常路径的事件流完整：tool_call 以 FAILED 封口 + tool_result，finish 恰一次。"""
    model = _model_calling("explode", {"x": "1"})
    graph = build_react_graph(model, [explode])
    mapper = EventMapper("run-x")

    events = []
    async for ev in graph.astream_events(
        _react_state("炸一下"), version="v2", config={"metadata": {"request_id": "run-x"}}
    ):
        events.extend(mapper.handle(ev))

    calls = [e for e in events if e.type == EventType.TOOL_CALL]
    assert calls, "应产出 tool_call 事件"
    assert calls[0].tool_call.status == ToolCallStatus.RUNNING
    assert calls[-1].tool_call.status == ToolCallStatus.FAILED
    assert "boom" in (calls[-1].tool_call.error_msg or "")

    results = [e for e in events if e.type == EventType.TOOL_RESULT]
    assert len(results) == 1 and results[0].tool_result.tool_call_id == calls[-1].tool_call.tool_call_id

    finishes = [e for e in events if e.finish]
    assert len(finishes) == 1 and finishes[0].type == EventType.RESULT
    # seq 单调无空洞。
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(seqs) + 1))
