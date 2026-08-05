"""思考链（reasoning_content）进入 thought 事件流的回归。

背景（线上现象）：真实 DeepSeek 下「💭 思考」与「结论」一模一样——因为 M1 映射把
**答案的 content token 流**标为 thought，终态又把同一 content 作为 result 发出；而模型
真正的思考链（deepseek-v4-pro 默认输出 reasoning_content）在两层被丢弃：
① langchain-openai 的 ChatOpenAI 不透传 reasoning_content（已换 ChatDeepSeek）；
② mapper 只抽 content（extract_reasoning_delta seam 一直未接）。

本测试钉死：流式 chunk 的 additional_kwargs.reasoning_content 会进入 thought 事件，
且在答案文本之前（推理先于作答）；结论仍只含 content。
"""

from __future__ import annotations

import asyncio
from typing import Any, List

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from cognition.events.mapper import EventMapper
from cognition.events.schema import EventType
from cognition.graphs.react import build_react_graph
from cognition.tools.calculator import calculator


class ReasoningScriptedModel(BaseChatModel):
    """模拟 DeepSeek 思考模型：先流出 reasoning_content 增量，再流出 content 增量。"""

    @property
    def _llm_type(self) -> str:
        return "reasoning-scripted-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ReasoningScriptedModel":  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="答案是 14。"))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        chunks: List[AIMessageChunk] = [
            AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先拆解问题，"}),
            AIMessageChunk(content="", additional_kwargs={"reasoning_content": "再计算。"}),
            AIMessageChunk(content="答案是"),
            AIMessageChunk(content=" 14。"),
        ]
        for c in chunks:
            yield ChatGenerationChunk(message=c)


def test_reasoning_content_streams_into_thought():
    graph = build_react_graph(ReasoningScriptedModel(), [calculator])
    mapper = EventMapper("run-r")
    state = {
        "messages": [],
        "request_id": "run-r",
        "session_id": "s",
        "query": "算一下",
        "product_files": [],
        "is_stream": True,
        "step": 0,
    }
    state["messages"] = [__import__("langchain_core.messages", fromlist=["HumanMessage"]).HumanMessage(content="算一下")]

    async def run():
        events = []
        async for ev in graph.astream_events(state, version="v2", config={"metadata": {"request_id": "run-r"}}):
            events.extend(mapper.handle(ev))
        return events

    events = asyncio.run(run())
    thought_text = "".join(
        e.tool_thought.text for e in events if e.type == EventType.TOOL_THOUGHT and e.tool_thought
    )
    # 思考流 = 真实思考链 + 作答流（推理在前）。
    assert "先拆解问题，再计算。" in thought_text
    assert thought_text.index("先拆解问题") < thought_text.index("答案是")

    results = [e for e in events if e.type == EventType.RESULT]
    assert len(results) == 1
    # 结论只含答案 content，不混入思考链。
    assert results[0].result.text == "答案是 14。"
    assert "先拆解问题" not in results[0].result.text
