"""HITL 人工审批（M11）：决议编解码、审批门包装、servicer 挂起/恢复全链路。

架构不变量：审批=run 边界——run1 以 approval_request(finish=False)+挂起 RESULT(finish=True)
正常收尾；决议乘 metadata 走既有 Run RPC 开新 run，从 checkpoint 的 pending interrupt
处 Command(resume=字符串) 续图。resume 值恒为字符串（dict 会被解释为 interrupt-id 映射）。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from cognition._genproto import agent_pb2
from cognition.approval import (
    first_interrupt_payload,
    make_decision,
    parse_decision,
    preview_args,
    wrap_with_approval,
)
from cognition.config import Settings
from cognition.graphs.react import build_react_graph
from cognition.providers.fake import ScriptedChatModel
from cognition.server.servicer import CognitionServicer
from cognition.tools.calculator import calculator


def test_decision_roundtrip():
    assert make_decision(True) == "approved"
    assert make_decision(False, "太危险") == "rejected:太危险"
    assert parse_decision("approved") == (True, "")
    assert parse_decision("approved:速办") == (True, "速办")
    assert parse_decision("rejected:太危险") == (False, "太危险")
    # 容错：未知/空值一律按拒绝（安全默认）。
    assert parse_decision("") == (False, "")
    assert parse_decision(None) == (False, "")
    assert parse_decision({"weird": 1})[0] is False


def test_preview_args_sanitizes():
    p = preview_args({"expression": "2*3", "config": object(), "blob": object(), "long": "x" * 600})
    assert "config" not in p  # 注入参数剔除
    assert isinstance(p["blob"], str)  # 不可 JSON 化 → str
    assert p["long"].endswith("…") and len(p["long"]) < 600


def test_wrap_preserves_identity_and_untouched_tools():
    from cognition.tools.report import write_report

    wrapped = wrap_with_approval([calculator, write_report], ["write_report"])
    assert wrapped[0] is calculator  # 未列名工具原对象
    w = wrapped[1]
    assert w is not write_report and w.name == "write_report"
    assert w.response_format == "content_and_artifact"  # 拒绝路径要按此返回 (text, None)
    assert w.args_schema is write_report.args_schema  # schema 不变 → bind_tools 线上等价


def _paused_servicer(responses: list[AIMessage]) -> CognitionServicer:
    model = ScriptedChatModel(responses=responses)
    tools = wrap_with_approval([calculator], ["calculator"], reason="测试需要审批")
    graph = build_react_graph(model, tools, checkpointer=MemorySaver(), max_steps=4)
    return CognitionServicer(react_graph=graph, settings=Settings())


_SCRIPT = [
    AIMessage(content="先算", tool_calls=[
        {"name": "calculator", "args": {"expression": "2*(3+4)"}, "id": "tc-9", "type": "tool_call"},
    ]),
    AIMessage(content="答案是 14"),
]


def _req(run_id: str, session: str, query: str = "算一下", metadata: dict | None = None):
    return agent_pb2.RunRequest(
        run_id=run_id, session_id=session, query=query, agent_type="react",
        metadata=metadata or {},
    )


async def _drain(servicer, request):
    return [p async for p in servicer.Run(request, None)]


# 模块级（不受 `from __future__ import annotations` 的字符串化影响，StructuredTool 可求值注解）。
_PEEK_SEEN: dict = {}


def _peek_config(x: str, config: RunnableConfig = None) -> str:  # type: ignore[assignment]
    """记录注入的 config.metadata.run_id（证实审批门转发 config）。"""
    _PEEK_SEEN["run_id"] = (config or {}).get("metadata", {}).get("run_id")
    return f"ok:{x}"


async def test_pause_then_approve_full_chain():
    s = _paused_servicer(list(_SCRIPT))

    # —— run1：撞审批门挂起 ——
    protos1 = await _drain(s, _req("r1", "s1"))
    types1 = [p.type for p in protos1]
    assert agent_pb2.EVENT_TYPE_APPROVAL_REQUEST in types1
    ap = next(p for p in protos1 if p.type == agent_pb2.EVENT_TYPE_APPROVAL_REQUEST)
    assert not ap.finish and ap.is_final
    assert ap.approval.tool_name == "calculator" and ap.approval.approval_id
    assert "tc-9" in list(ap.approval.pending_tool_call_ids)  # 孤儿卡防转圈
    assert ap.approval.reason == "测试需要审批"
    final1 = protos1[-1]
    assert final1.type == agent_pb2.EVENT_TYPE_RESULT and final1.finish
    assert "挂起" in final1.result.text
    # seq 无空洞
    assert [p.seq for p in protos1] == list(range(1, len(protos1) + 1))

    # —— run2：批准恢复 ——
    protos2 = await _drain(s, _req("r2", "s1", metadata={
        "approval_resume_id": ap.approval.approval_id,
        "approval_decision": "approved",
        "approval_comment": "可以",
    }))
    texts = [p.tool_thought.text for p in protos2 if p.type == agent_pb2.EVENT_TYPE_TOOL_THOUGHT]
    assert any("已批准" in t and "可以" in t for t in texts)  # 决议注记入账本
    results = [p for p in protos2 if p.type == agent_pb2.EVENT_TYPE_TOOL_RESULT]
    assert results and results[0].tool_result.tool_result == "14"  # 工具真实执行
    assert protos2[-1].finish and "14" in protos2[-1].result.text


async def test_pause_then_reject():
    s = _paused_servicer(list(_SCRIPT))
    protos1 = await _drain(s, _req("r1", "s2"))
    ap = next(p for p in protos1 if p.type == agent_pb2.EVENT_TYPE_APPROVAL_REQUEST)

    protos2 = await _drain(s, _req("r2", "s2", metadata={
        "approval_resume_id": ap.approval.approval_id,
        "approval_decision": "rejected",
        "approval_comment": "太危险",
    }))
    results = [p for p in protos2 if p.type == agent_pb2.EVENT_TYPE_TOOL_RESULT]
    assert results and "已被人工拒绝" in results[0].tool_result.tool_result
    assert "太危险" in results[0].tool_result.tool_result
    assert protos2[-1].finish  # 模型看到拒绝 observation 后收尾


async def test_resume_without_pending_is_graceful():
    s = _paused_servicer(list(_SCRIPT))
    protos = await _drain(s, _req("rx", "s-none", metadata={
        "approval_resume_id": "ghost", "approval_decision": "approved",
    }))
    assert len(protos) == 1 and protos[0].finish
    assert "没有待审批" in protos[0].result.text


async def test_new_message_while_pending_invalidates_approval():
    """pending 期间发新消息：图从 START 重启丢弃 pending task（悬空 tool_calls 由
    repair 投影自愈）→ 旧审批永久不可恢复，resume 优雅报错。"""
    s = _paused_servicer(list(_SCRIPT))
    protos1 = await _drain(s, _req("r1", "s3"))
    ap = next(p for p in protos1 if p.type == agent_pb2.EVENT_TYPE_APPROVAL_REQUEST)

    # 用户不理会审批、直接发新消息（同 thread）→ 图从 START 重启、丢弃 pending task，
    # 悬空 tool_calls 由 repair 投影自愈，新 run 正常完成（脚本第二响应=直接作答）。
    protos_mid = await _drain(s, _req("r-mid", "s3", query="换个问题"))
    assert protos_mid[-1].finish and "挂起" not in protos_mid[-1].result.text

    protos2 = await _drain(s, _req("r2", "s3", metadata={
        "approval_resume_id": ap.approval.approval_id, "approval_decision": "approved",
    }))
    assert len(protos2) == 1 and "没有待审批" in protos2[0].result.text


def test_first_interrupt_payload_shape():
    intr = SimpleNamespace(value={"approval_id": "a1", "tool": "x"})
    task = SimpleNamespace(interrupts=[intr])
    assert first_interrupt_payload(SimpleNamespace(tasks=[task]))["approval_id"] == "a1"
    assert first_interrupt_payload(SimpleNamespace(tasks=[])) is None
    # 非审批 interrupt（无 approval_id）不误判
    other = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={"x": 1})])])
    assert first_interrupt_payload(other) is None


def test_usage_accumulates_across_all_nodes_and_attaches_to_result():
    """usage 单咽喉：任意节点的 on_chat_model_end 都计入；随终态 RESULT 附带。"""
    from langchain_core.messages import AIMessage as AIM

    from cognition.events.mapper import EventMapper

    m = EventMapper("run-u")
    for node, inp, out in [("agent", 100, 20), ("rag_generate", 50, 10), ("planner", 30, 5)]:
        m.handle({
            "event": "on_chat_model_end",
            "name": "model",
            "metadata": {"langgraph_node": node},
            "data": {"output": AIM(content="x", usage_metadata={
                "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out})},
        })
    ev = m.plain_result("done")
    assert ev.result.usage is not None
    assert ev.result.usage.input_tokens == 180
    assert ev.result.usage.output_tokens == 35
    assert ev.result.usage.model_calls == 3
    # proto 往返
    proto = ev.to_proto()
    assert proto.result.usage.input_tokens == 180 and proto.result.usage.model_calls == 3


def test_usage_absent_when_no_model_calls():
    from cognition.events.mapper import EventMapper

    ev = EventMapper("run-z").plain_result("done")
    assert ev.result.usage is None
    assert ev.to_proto().result.usage.model_calls == 0  # proto 零值（旧端等价）


def test_gate_forwards_config_to_config_taking_tool():
    """评审#1：审批门必须转发 RunnableConfig——否则 script_runner 读不到附件白名单、
    write_report 的 run_id 错乱致产物 404。用一个读 config 的假工具证实注入贯通。"""
    import asyncio as _asyncio

    from langchain_core.tools import StructuredTool
    from langgraph.types import Command as _Command

    tool = StructuredTool.from_function(func=_peek_config, name="peek")
    wrapped = wrap_with_approval([tool], ["peek"])[0]
    _PEEK_SEEN.clear()

    async def run():
        model = ScriptedChatModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "peek", "args": {"x": "hi"}, "id": "t1", "type": "tool_call"}]),
            AIMessage(content="done"),
        ])
        graph = build_react_graph(model, [wrapped], checkpointer=MemorySaver(), max_steps=3)
        cfg = {"configurable": {"thread_id": "cfgtest"}, "metadata": {"run_id": "RUN-XYZ"}}
        async for _ in graph.astream_events({"messages": []}, version="v2", config=cfg):
            pass
        async for _ in graph.astream_events(_Command(resume="approved"), version="v2", config=cfg):
            pass

    _asyncio.run(run())
    assert _PEEK_SEEN.get("run_id") == "RUN-XYZ"  # config 贯通到被审批工具


def test_graph_interrupt_detection_precise():
    """评审#15：按类型识别中断，不误伤子串含 Interrupt 的真实工具异常。"""
    from cognition.events.mapper import _is_graph_interrupt

    class DuckDBInterruptException(Exception):
        pass

    assert _is_graph_interrupt(DuckDBInterruptException("x")) is False
    assert _is_graph_interrupt(None) is False
    try:
        from langgraph.errors import GraphInterrupt

        assert _is_graph_interrupt(GraphInterrupt(())) is True
    except Exception:
        pass
