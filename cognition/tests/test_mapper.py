"""黄金契约：脚本化 fake 模型 → 真实 ReAct 图 → EventMapper → schema Event 序列。

用一个返回「先 calculator(2*(3+4)) 再给最终答案」的脚本化 chat 模型驱动真实图，
跑 astream_events(v2)，逐事件喂 EventMapper，断言契约：结构 / seq 顺序 /
tool_call_id 原位配对 / 状态迁移 / calculator 输入与结果 "14" / finish 恰好一次且在 result。

本测试不触碰真实 LLM key、不需要 PG、不需要 genproto（不调用 to_proto）。
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from cognition.events.mapper import EventMapper
from cognition.events.schema import Event, EventType, ToolCallStatus
from cognition.graphs.react import build_react_graph
from cognition.tools.registry import get_local_tools


class ScriptedChatModel(BaseChatModel):
    """按预设脚本逐轮返回消息的 fake 模型（支持 bind_tools 与流式）。"""

    responses: List[AIMessage] = []
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":  # noqa: ARG002
        return self

    def _next(self) -> AIMessage:
        msg = self.responses[self.idx]
        self.idx += 1
        return msg

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        msg = self._next()
        if msg.content:
            yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content))
        # 携带 tool_calls 的收口 chunk（无 tool_calls 时为空 chunk，被映射器跳过）。
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", tool_calls=list(msg.tool_calls or []))
        )


def _build_model() -> ScriptedChatModel:
    return ScriptedChatModel(
        responses=[
            AIMessage(
                content="Let me compute that.",
                tool_calls=[
                    {
                        "name": "calculator",
                        "args": {"expression": "2*(3+4)"},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(content="The answer is 14."),
        ]
    )


async def _run() -> List[Event]:
    model = _build_model()
    tools = get_local_tools()
    graph = build_react_graph(model, tools, max_steps=40)
    mapper = EventMapper(run_id="r1")

    state = {
        "messages": [("user", "What is 2*(3+4)?")],
        "request_id": "r1",
        "session_id": "s1",
        "query": "What is 2*(3+4)?",
        "product_files": [],
        "is_stream": True,
        "step": 0,
    }
    config = {"configurable": {"thread_id": "s1"}, "recursion_limit": 25}

    collected: List[Event] = []
    async for ev in graph.astream_events(state, version="v2", config=config):
        collected.extend(mapper.handle(ev))
    return collected


@pytest.fixture(scope="module")
def events() -> List[Event]:
    # 同步运行一次真实图，结果缓存供各断言复用（不依赖 async fixture）。
    return asyncio.run(_run())


def test_seq_monotonic_gapless_from_one(events: List[Event]):
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(events) + 1))


def test_finish_exactly_once_and_on_result(events: List[Event]):
    finished = [e for e in events if e.finish]
    assert len(finished) == 1
    assert finished[0].type is EventType.RESULT
    assert finished[0].is_final is True


def test_thought_opened_and_finalized(events: List[Event]):
    thoughts = [e for e in events if e.type is EventType.TOOL_THOUGHT]
    # 至少有一次开 (is_final=False) 与一次封口 (is_final=True)
    assert any(not t.is_final for t in thoughts)
    assert any(t.is_final for t in thoughts)
    # 思考文本出现在某个增量里
    assert any("Let me compute" in (t.tool_thought.text or "") for t in thoughts)
    # 同一 think 轮的 thought 共享 message_id
    first_mid = thoughts[0].message_id
    assert first_mid == "r1:think:1"


def test_tool_call_running_then_success_share_id(events: List[Event]):
    calls = [e for e in events if e.type is EventType.TOOL_CALL]
    running = [e for e in calls if e.tool_call.status is ToolCallStatus.RUNNING]
    success = [e for e in calls if e.tool_call.status is ToolCallStatus.SUCCESS]
    assert len(running) == 1 and len(success) == 1

    r, s = running[0], success[0]
    # 原位配对：message_id == tool_call_id，running 与 success 共享
    assert r.message_id == r.tool_call.tool_call_id == "call_1"
    assert s.message_id == s.tool_call.tool_call_id == "call_1"

    # running 字段
    assert r.tool_call.tool_name == "calculator"
    assert r.tool_call.tool_provider == "local"
    assert r.tool_call.dispatch_index == 1
    assert r.tool_call.input == {"expression": "2*(3+4)"}
    assert r.tool_call.summary == "正在调用 calculator"
    assert r.is_final is False and r.finish is False

    # success 字段
    assert s.tool_call.summary == "calculator 调用完成"
    assert s.is_final is True and s.finish is False

    # 状态迁移顺序：running 在 success 之前
    assert events.index(r) < events.index(s)


def test_tool_result_carries_observation(events: List[Event]):
    results = [e for e in events if e.type is EventType.TOOL_RESULT]
    assert len(results) == 1
    tr = results[0]
    assert tr.tool_result.tool_call_id == "call_1"
    assert tr.tool_result.tool_result == "14"
    assert tr.is_final is True and tr.finish is False


def test_final_result_contains_answer(events: List[Event]):
    results = [e for e in events if e.type is EventType.RESULT]
    assert len(results) == 1
    assert "14" in results[0].result.text


def test_overall_milestone_ordering(events: List[Event]):
    """里程碑相对顺序：thought封口 → running → success → tool_result → result。"""
    types_status = []
    for e in events:
        if e.type is EventType.TOOL_THOUGHT and e.is_final:
            types_status.append(("thought_final", e.seq))
        elif e.type is EventType.TOOL_CALL:
            types_status.append((f"call_{e.tool_call.status.value}", e.seq))
        elif e.type is EventType.TOOL_RESULT:
            types_status.append(("tool_result", e.seq))
        elif e.type is EventType.RESULT:
            types_status.append(("result", e.seq))

    order = [name for name, _ in types_status]
    # 关键里程碑按预期相对顺序出现
    assert order.index("thought_final") < order.index("call_running")
    assert order.index("call_running") < order.index("call_success")
    assert order.index("call_success") < order.index("tool_result")
    assert order.index("tool_result") < order.index("result")
