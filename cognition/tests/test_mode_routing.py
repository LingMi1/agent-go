"""三档模式路由（M9 B5）：deep_research=plan 图的配置化变体。

不变量：
- plan_graphs 路由表按 agent_type 选图；deep_research 缺研究图时回退 plan 图（可用性优先）。
- recursion 预算随 agent_type 取各自轮次上限（硬绑 planner_max_steps 会让研究后期爆
  GraphRecursionError——晚且贵，单测必须钉死）。
- RESEARCH_PLANNER_SYSTEM 与默认提示词不同且保留 {{sop}} 槽与自包含约束。
"""

from __future__ import annotations

from cognition._genproto import agent_pb2
from cognition.config import Settings
from cognition.graphs.plan_execute import PLANNER_SYSTEM, RESEARCH_PLANNER_SYSTEM
from cognition.server.servicer import CognitionServicer


def _servicer(**kw) -> CognitionServicer:
    return CognitionServicer(react_graph=object(), settings=Settings(), **kw)


def _req(agent_type: str) -> agent_pb2.RunRequest:
    return agent_pb2.RunRequest(query="研究一下", session_id="s1", agent_type=agent_type)


def test_research_prompt_is_distinct_variant():
    assert RESEARCH_PLANNER_SYSTEM != PLANNER_SYSTEM
    assert "{{sop}}" in RESEARCH_PLANNER_SYSTEM
    assert "自包含" in RESEARCH_PLANNER_SYSTEM  # 执行者无对话历史的硬约束保留
    assert "引用" in RESEARCH_PLANNER_SYSTEM


def test_routing_and_recursion_budget_per_type():
    plan, research = object(), object()
    s = _servicer(plan_graph=plan, research_graph=research)
    g1, _, r1 = s._build(_req("plan_solve"), ())
    g2, _, r2 = s._build(_req("deep_research"), ())
    assert g1 is plan and g2 is research
    st = Settings()
    assert r1 == 4 * st.planner_max_steps + 25
    assert r2 == 4 * st.research_max_steps + 25
    assert r2 > r1  # 研究模式预算更高


def test_deep_research_falls_back_to_plan_graph():
    plan = object()
    s = _servicer(plan_graph=plan, research_graph=None)
    g, _, _ = s._build(_req("deep_research"), ())
    assert g is plan  # 缺研究图不拒答（提示词非研究版但链路可用）


def test_unknown_agent_type_routes_react():
    s = _servicer(plan_graph=object(), research_graph=object())
    g, state, _ = s._build(_req("whatever"), ())
    assert g is s.react_graph
    assert state["messages"]  # react 态


def test_build_accepts_planner_system_param():
    """build_plan_execute_graph(planner_system=...) 可编译（配置化变体非新图）。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from cognition.graphs.plan_execute import build_plan_execute_graph
    from cognition.graphs.react import build_react_graph

    model = FakeListChatModel(responses=["ok"])
    sub = build_react_graph(model, [], max_steps=2)
    g = build_plan_execute_graph(
        model, sub, [], max_steps=8, planner_system=RESEARCH_PLANNER_SYSTEM
    )
    assert g is not None
