from __future__ import annotations

from langchain_core.messages import AIMessage

from cognition.graphs.plan_execute import (
    EVENT_BRANCH_END,
    EVENT_BRANCH_START,
    build_plan_execute_graph,
)
from cognition.graphs.react import build_react_graph
from cognition.providers.fake import MessageDrivenChatModel
from cognition.tools.calculator import calculator


def _planner(messages):
    has_plan = any(
        isinstance(m, AIMessage) and m.tool_calls
        for m in messages
    )
    if has_plan:
        return AIMessage(content="continue")
    return AIMessage(
        content="plan",
        tool_calls=[{
            "name": "planning",
            "args": {"command": "create", "title": "t", "steps": ["a<sep>b<sep>c"]},
            "id": "p1",
        }],
    )


async def test_graph_emits_branch_slot_events_without_changing_public_result():
    executor = build_react_graph(
        MessageDrivenChatModel(decide=lambda messages: AIMessage(content="done")),
        [calculator],
    )
    graph = build_plan_execute_graph(
        MessageDrivenChatModel(decide=_planner), executor, [calculator], max_parallel=2
    )
    state = {
        "query": "q", "request_id": "r", "session_id": "s", "plan": None,
        "round": 0, "step": 0, "reduced_state": "", "planner_messages": [],
        "sub_results": [], "output_format": "", "image_gen": False,
    }
    starts, ends, result = [], [], ""
    async for event in graph.astream_events(
        state, version="v2", config={"metadata": {"request_id": "r"}}
    ):
        if event.get("event") != "on_custom_event":
            continue
        if event.get("name") == EVENT_BRANCH_START:
            starts.append(event["data"])
        elif event.get("name") == EVENT_BRANCH_END:
            ends.append(event["data"])
        elif event.get("name") == "result":
            result = event["data"]["text"]
    assert len(starts) == len(ends) == 3
    assert {(x["round"], x["branch_id"]) for x in starts} == {
        (0, "b0"), (0, "b1"), (0, "b2")
    }
    assert all(x["perf_counter_ns"] > 0 for x in starts + ends)
    assert "任务已完成" in result
