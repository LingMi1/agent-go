"""悬空 tool_calls 修复（repair_dangling_tool_calls）：纯函数 + 图级接入回归。

背景（线上缺陷）：工具执行崩溃后，checkpoint 已提交 think 节点的 AIMessage(tool_calls)
但 tools 节点未提交 ToolMessage → 会话线程留下悬空 tool_calls。此后该 session 每一轮
都被 provider 拒绝（DeepSeek/OpenAI 400: "An assistant message with 'tool_calls' must be
followed by tool messages…"；Anthropic 同类 orphan 校验）——线程被永久污染。

修复策略：入模型前对消息序列做只读修复投影（不回写 state/checkpoint）：
- 悬空的 tool_call 紧随其组补一条合成 error ToolMessage（模型可见"执行被中断"）；
- 无前置 tool_call 的孤儿 ToolMessage 丢弃。
接入点：react think 节点、plan_execute planner（其 planner_messages 里的 planning
tool_calls 从不跟 ToolMessage，对真实 provider 是同类缺陷）。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cognition.graphs.history import repair_dangling_tool_calls
from cognition.graphs.react import build_react_graph
from cognition.providers.fake import MessageDrivenChatModel, build_fake_executor_model
from cognition.tools.calculator import calculator


def _ai_with_calls(*ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "calculator", "args": {"expression": "1"}, "id": i} for i in ids],
    )


def _assert_pairs_valid(messages) -> None:
    """provider 合法性：每个 tool_call 有紧随的 ToolMessage 应答；无孤儿 ToolMessage。"""
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            want = {str(tc.get("id")) for tc in m.tool_calls}
            j = i + 1
            got = set()
            while j < n and isinstance(messages[j], ToolMessage):
                got.add(str(messages[j].tool_call_id))
                j += 1
            missing = want - got
            assert not missing, f"悬空 tool_calls: {missing}"
            i = j
        else:
            assert not isinstance(m, ToolMessage), f"孤儿 ToolMessage @{i}: {m.tool_call_id}"
            i += 1


def test_healthy_sequence_unchanged():
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="q"),
        _ai_with_calls("a"),
        ToolMessage(content="4", tool_call_id="a"),
        AIMessage(content="答案"),
    ]
    out = repair_dangling_tool_calls(msgs)
    assert out == msgs  # 健康序列原样返回（同对象同序）


def test_dangling_call_gets_synthetic_error_toolmessage():
    msgs = [HumanMessage(content="q"), _ai_with_calls("a"), HumanMessage(content="下一问")]
    out = repair_dangling_tool_calls(msgs)
    _assert_pairs_valid(out)
    # 合成消息紧随其组、status=error、tool_call_id 对上。
    assert isinstance(out[2], ToolMessage)
    assert out[2].tool_call_id == "a" and out[2].status == "error"
    assert out[3].content == "下一问"


def test_partially_answered_group_only_fills_missing():
    msgs = [
        HumanMessage(content="q"),
        _ai_with_calls("a", "b"),
        ToolMessage(content="ok", tool_call_id="a"),
    ]
    out = repair_dangling_tool_calls(msgs)
    _assert_pairs_valid(out)
    synth = [m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == "b"]
    assert len(synth) == 1 and synth[0].status == "error"
    # 已有应答保持原样。
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "a" and m.content == "ok" for m in out)


def test_orphan_toolmessage_dropped():
    msgs = [HumanMessage(content="q"), ToolMessage(content="孤儿", tool_call_id="ghost"), AIMessage(content="答")]
    out = repair_dangling_tool_calls(msgs)
    _assert_pairs_valid(out)
    assert not any(isinstance(m, ToolMessage) for m in out)


def test_react_think_node_heals_poisoned_thread():
    """被污染线程（悬空 tool_calls）续聊：think 节点入模型前自动修复，不再炸 provider。"""
    seen: list[list] = []

    def decide(messages):
        seen.append(list(messages))
        _assert_pairs_valid(messages)  # 严格 provider 视角校验
        return AIMessage(content="OK")

    model = MessageDrivenChatModel(decide=decide)
    graph = build_react_graph(model, [calculator])
    # 模拟 checkpoint 恢复出的污染历史 + 新一轮 Human。
    poisoned = [
        HumanMessage(content="写报告"),
        _ai_with_calls("tc-dead"),  # 工具崩溃当轮：无 ToolMessage
        HumanMessage(content="上一个失败的任务是什么"),
    ]
    state = {
        "messages": poisoned,
        "request_id": "r",
        "session_id": "s",
        "query": "上一个失败的任务是什么",
        "product_files": [],
        "is_stream": True,
        "step": 0,
    }
    out = asyncio.run(graph.ainvoke(state, {"metadata": {"request_id": "r"}}))
    assert seen, "模型应被调用"
    assert out["messages"][-1].content == "OK"


def test_planner_history_repaired_before_model_call():
    """plan_execute planner：planner_messages 里的 planning tool_calls 无应答 → 入模型前修复。"""
    from cognition.graphs.plan_execute import build_plan_execute_graph

    def planner_decide(messages):
        _assert_pairs_valid(messages)
        # 已有历史计划 → 走 replan 分支（只给思考文本，plan-lifecycle 自动推进收尾）。
        return AIMessage(content="继续推进。")

    planner = MessageDrivenChatModel(decide=planner_decide)
    executor = build_react_graph(build_fake_executor_model(), [calculator])
    graph = build_plan_execute_graph(planner, executor, [calculator], max_steps=3, max_parallel=2)

    from cognition.graphs.plan_lifecycle import create

    # 首轮已产生的污染历史：planning 调用从不跟 ToolMessage（真实 provider 会 400）。
    poisoned_history = [
        HumanMessage(content="用户任务：算数"),
        AIMessage(
            content="拆解任务。",
            tool_calls=[{"name": "planning", "args": {"command": "create", "title": "T", "steps": ["算 1+1"]}, "id": "plan-1"}],
        ),
    ]
    state = {
        "query": "算数",
        "request_id": "r2",
        "session_id": "s2",
        "plan": create("T", ["算 1+1"]),
        "round": 0,
        "step": 0,
        "planner_messages": poisoned_history,
        "sub_results": [{"round": 0, "branch_id": "b0", "task": "算 1+1", "result": "2", "observations": [], "status": "finished"}],
    }
    result = asyncio.run(graph.ainvoke(state, {"metadata": {"request_id": "r2"}}))
    assert result.get("plan") is not None  # 正常走完（planner 校验未抛）


# —— invalid_tool_calls（JSON 参数损坏）：线上 DeepSeek 深研 400 的根因回归 ——
# langchain-openai 序列化出站 assistant 消息时 tool_calls ∪ invalid_tool_calls 都会带上，
# 只按 .tool_calls 判断会漏应答 invalid 项 → 每轮 400、线程永久污染。


def test_invalid_tool_call_with_id_gets_synthetic_answer():
    ai = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "planning", "args": "{broken", "id": "call_bad", "error": "bad json", "type": "invalid_tool_call"}
        ],
    )
    out = repair_dangling_tool_calls([HumanMessage(content="q"), ai])
    tails = [m for m in out if isinstance(m, ToolMessage)]
    assert [t.tool_call_id for t in tails] == ["call_bad"]
    assert tails[0].status == "error"


def test_invalid_tool_call_without_id_is_stripped():
    ai = AIMessage(
        content="",
        invalid_tool_calls=[{"name": "planning", "args": "{broken", "id": None, "error": "e", "type": "invalid_tool_call"}],
        additional_kwargs={"tool_calls": [{"raw": "chunk"}], "other": "keep"},
    )
    out = repair_dangling_tool_calls([HumanMessage(content="q"), ai])
    fixed = out[1]
    assert isinstance(fixed, AIMessage)
    assert fixed.invalid_tool_calls == []
    assert "tool_calls" not in fixed.additional_kwargs  # 防序列化兜底路径复原
    assert fixed.additional_kwargs.get("other") == "keep"
    assert not any(isinstance(m, ToolMessage) for m in out)


def test_mixed_valid_and_invalid_calls_all_answered():
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "planning", "args": {"title": "t"}, "id": "call_ok"}],
        invalid_tool_calls=[{"name": "planning", "args": "{x", "id": "call_bad", "error": "e", "type": "invalid_tool_call"}],
    )
    out = repair_dangling_tool_calls([ai, ToolMessage(content="计划已登记", tool_call_id="call_ok")])
    answered = {m.tool_call_id for m in out if isinstance(m, ToolMessage)}
    assert answered == {"call_ok", "call_bad"}


def test_planner_acks_cover_invalid_and_foreign_calls():
    from cognition.graphs.plan_execute import _planning_acks

    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "planning", "args": {"title": "t"}, "id": "c1"},
            {"name": "web_search", "args": {"query": "幻觉调用"}, "id": "c2"},
        ],
        invalid_tool_calls=[{"name": "planning", "args": "{x", "id": "c3", "error": "e", "type": "invalid_tool_call"}],
    )
    acks = _planning_acks(ai)
    assert [a.tool_call_id for a in acks] == ["c1", "c2", "c3"]
    assert acks[2].status == "error"
