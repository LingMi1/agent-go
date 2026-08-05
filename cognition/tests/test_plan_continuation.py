"""plan_solve 规划链路与同会话续聊修复回归（真实模型走查暴露的三类缺陷）。

线上现象（tmp/为什么这个对话无法正常产出文件内容？.md）：
1. planner 模型从未绑定 planning 工具（grpc_server 装配缺 tools 参数）→ 真实模型把计划
   JSON 写进正文 → `_parse_planning_call` 恒 None → 永远退化"单步计划=原句"（新旧对话
   都受影响，fake planner 直接产 tool_calls 掩盖了缺陷）；
2. 同会话第二次 plan run：`merge_sub_results` 按 (round, branch) 去重且"取首个"→ 新
   run 的执行结果被旧 run 同键结果顶掉，planner/summary 读到的全是上一次的旧内容；
   reduced_state 残留同理（旧 run ERROR 会把新 run 直接路由去 summary）；
3. executor 分支按设计无会话上下文，"把上面内容整理成报告"类指代型任务必失败
   （executor 自述"新对话无上下文"）。

修复：planning 工具绑定 + 正文 JSON 兜底解析 + planning 调用落历史即补 ack ToolMessage；
sub_results 键与过滤加 request_id、servicer 重置 reduced_state、executor 子线程按 run 隔离；
build_context_digest 会话背景随 Send 注入 executor 提示词 + planner 步骤自包含约束。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from cognition.graphs.plan_execute import (
    _parse_planning_text,
    build_context_digest,
    build_plan_execute_graph,
    merge_sub_results,
    results_for_round,
)
from cognition.graphs.react import build_react_graph
from cognition.providers.fake import MessageDrivenChatModel
from cognition.tools.calculator import calculator


# ——————————————————————————————————————————————————————————————
# 1) 正文 JSON 兜底解析
# ——————————————————————————————————————————————————————————————
def test_parse_planning_text_fenced_json():
    text = '思考过程……\n```json\n{"title": "T", "steps": ["步骤一", "步骤二<sep>步骤三"]}\n```\n完成。'
    draft = _parse_planning_text(text)
    assert draft and draft["title"] == "T" and len(draft["steps"]) == 2


def test_parse_planning_text_bare_json():
    text = '直接给出：{"command": "update", "title": "X", "steps": ["a"]} 以上。'
    draft = _parse_planning_text(text)
    assert draft and draft["command"] == "update" and draft["steps"] == ["a"]


def test_parse_planning_text_none_cases():
    assert _parse_planning_text("") is None
    assert _parse_planning_text("没有任何 JSON") is None
    # steps 缺失/为空不算计划。
    assert _parse_planning_text('{"title": "T"}') is None
    assert _parse_planning_text('{"title": "T", "steps": []}') is None


# ——————————————————————————————————————————————————————————————
# 2) 跨 run 的 sub_results 隔离
# ——————————————————————————————————————————————————————————————
def _sub(request_id: str, rnd: int, branch: str, result: str) -> dict:
    return {"request_id": request_id, "round": rnd, "branch_id": branch,
            "task": "t", "result": result, "observations": [], "status": "finished"}


def test_merge_sub_results_keeps_same_key_across_runs():
    old = [_sub("run-1", 0, "b0", "旧结果")]
    new = [_sub("run-2", 0, "b0", "新结果")]
    merged = merge_sub_results(old, new)
    # 不同 run 的同 (round, branch) 都保留——不再互相顶掉。
    assert len(merged) == 2
    assert {m["result"] for m in merged} == {"旧结果", "新结果"}


def test_results_for_round_filters_by_request():
    subs = [_sub("run-1", 0, "b0", "旧"), _sub("run-2", 0, "b0", "新"), _sub("run-2", 1, "b0", "下一轮")]
    got = results_for_round(subs, "run-2", 0)
    assert [r["result"] for r in got] == ["新"]
    # 旧数据（无 request_id 字段）不会泄入新 run。
    legacy = [{"round": 0, "branch_id": "b0", "result": "遗留", "status": "finished"}]
    assert results_for_round(legacy, "run-2", 0) == []


def test_servicer_resets_reduced_state():
    from cognition.config import Settings
    from cognition.server.servicer import CognitionServicer

    servicer = CognitionServicer(react_graph=object(), settings=Settings(), plan_graph=object())
    req = SimpleNamespace(run_id="r9", session_id="s9", query="q", agent_type="plan_solve", max_steps=0)
    _, state, _ = servicer._build(req)
    # 旧 run 的 ERROR 残留不得把新 run 直接路由去 summary。
    assert state["reduced_state"] == ""
    assert state["plan"] is None and state["round"] == 0


# ——————————————————————————————————————————————————————————————
# 3) 会话背景摘要
# ——————————————————————————————————————————————————————————————
def test_build_context_digest_recent_first_budget_and_order():
    msgs = [
        HumanMessage(content="问题一"),
        AIMessage(content="回答一"),
        ToolMessage(content="工具输出不该出现", tool_call_id="x"),
        HumanMessage(content="问题二"),
        AIMessage(content="回答二"),
    ]
    d = build_context_digest(msgs, max_chars=10_000)
    assert "工具输出" not in d
    # 时间顺序保持（问题一在回答二之前）。
    assert d.index("问题一") < d.index("回答二")
    # 预算收紧时近期优先：只留得下最后的条目。
    d2 = build_context_digest(msgs, max_chars=20)
    assert "回答二" in d2 and "问题一" not in d2
    assert build_context_digest([], max_chars=100) == ""


# ——————————————————————————————————————————————————————————————
# 4) 图级集成：planning ack 配对 + 第二次 run 用自己的结果
# ——————————————————————————————————————————————————————————————
def _planner_model() -> MessageDrivenChatModel:
    """create 轮：按用户任务产出 planning 调用（步骤=原任务）；replan 轮：仅思考。"""

    def decide(messages):
        last = str(messages[-1].content)
        if last.startswith("用户任务："):
            q = last.split("：", 1)[1].split("\n", 1)[0]
            return AIMessage(
                content="拆解任务。",
                tool_calls=[{"name": "planning",
                             "args": {"command": "create", "title": "T", "steps": [q]},
                             "id": f"plan-{q}"}],
            )
        return AIMessage(content="推进计划。")

    return MessageDrivenChatModel(decide=decide)


def _executor_graph():
    """executor 回显任务行（DONE::你的任务是：<task>…），便于断言 note 归属。"""

    def decide(messages):
        first_line = str(messages[-1].content).split("\n", 1)[0]
        return AIMessage(content=f"DONE::{first_line}")

    return build_react_graph(MessageDrivenChatModel(decide=decide), [calculator])


def _plan_state(request_id: str, query: str) -> dict:
    return {
        "query": query, "request_id": request_id, "session_id": "sess-cont",
        "plan": None, "round": 0, "step": 0, "reduced_state": "",
        "planner_messages": [], "sub_results": [],
    }


def test_planning_ack_keeps_pairs_and_second_run_uses_own_results():
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_plan_execute_graph(
        _planner_model(), _executor_graph(), [calculator],
        max_steps=3, max_parallel=2, checkpointer=MemorySaver(),
    )
    cfg = lambda rid: {"configurable": {"thread_id": "sess-cont"}, "metadata": {"request_id": rid}}  # noqa: E731

    out1 = asyncio.run(graph.ainvoke(_plan_state("run-1", "任务甲"), cfg("run-1")))
    # planning 调用在历史里必须有 ack ToolMessage 紧随（源头配对合法，真实 provider 不 400）。
    pm = out1["planner_messages"]
    ai_idx = next(i for i, m in enumerate(pm) if isinstance(m, AIMessage) and m.tool_calls)
    assert isinstance(pm[ai_idx + 1], ToolMessage) and pm[ai_idx + 1].tool_call_id == pm[ai_idx].tool_calls[0]["id"]
    assert out1["plan"] is not None and "任务甲" in (out1["plan"].notes[0] or "")

    # 同一 thread 第二次 run：结果必须来自本次执行，不被 run-1 的旧结果顶掉。
    out2 = asyncio.run(graph.ainvoke(_plan_state("run-2", "任务乙"), cfg("run-2")))
    note = out2["plan"].notes[0] or ""
    assert "任务乙" in note, f"第二次 run 的 note 应含本次任务：{note}"
    assert "任务甲" not in note, f"旧 run 结果泄入新 run：{note}"
    # 两个 run 的 sub_results 并存（键含 request_id，不互顶）。
    rids = {r.get("request_id") for r in out2["sub_results"]}
    assert rids == {"run-1", "run-2"}


def test_executor_receives_context_digest():
    """续聊场景：planner 历史里的旧回答要作为会话背景进入 executor 提示词。"""
    from langgraph.checkpoint.memory import MemorySaver

    seen_prompts: list[str] = []

    def exec_decide(messages):
        seen_prompts.append(str(messages[-1].content))
        return AIMessage(content="DONE")

    executor = build_react_graph(MessageDrivenChatModel(decide=exec_decide), [calculator])
    graph = build_plan_execute_graph(
        _planner_model(), executor, [calculator], max_steps=3, checkpointer=MemorySaver(),
    )
    cfg = {"configurable": {"thread_id": "sess-digest"}, "metadata": {"request_id": "run-a"}}
    state = _plan_state("run-a", "把上面内容整理成报告")
    # 模拟已有会话历史（上一轮 planner 记录的问答）。
    state["planner_messages"] = [
        HumanMessage(content="用户任务：介绍多智能体平台"),
        AIMessage(content="这是关于多智能体平台的长篇回答……要点 ABC。"),
    ]
    asyncio.run(graph.ainvoke(state, cfg))
    assert seen_prompts, "executor 应被调用"
    assert "会话背景" in seen_prompts[0] and "要点 ABC" in seen_prompts[0]
