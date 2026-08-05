"""provider 穿透事件：注入 provider_map 后 tool_call/tool_result 带正确 tool_provider。

唯一契约相关改动的验证——纯查表替换硬编码，用合成 astream 事件即可，不消耗模型、不触网。
"""

from __future__ import annotations

from types import SimpleNamespace

from cognition.events.mapper import EventMapper
from cognition.events.schema import EventType


def _chat_end(tool_calls):
    out = SimpleNamespace(tool_calls=tool_calls, content="")
    return {"event": "on_chat_model_end", "metadata": {"langgraph_node": "agent"}, "data": {"output": out}}


def _tool_end(tool_call_id, name, content="ok", status="success"):
    out = SimpleNamespace(tool_call_id=tool_call_id, name=name, content=content, status=status, artifact=None)
    return {"event": "on_tool_end", "metadata": {"langgraph_node": "tools"}, "data": {"output": out}}


def _drive(provider_map):
    m = EventMapper("run-1", tool_providers=provider_map)
    events = []
    events += m.handle({"event": "on_chat_model_start", "metadata": {"langgraph_node": "agent"}})
    events += m.handle(
        _chat_end(
            [
                {"id": "c1", "name": "mcp__web__fetch", "args": {"url": "x"}},
                {"id": "c2", "name": "skill", "args": {"name": "chart"}},
                {"id": "c3", "name": "calculator", "args": {"expr": "1+1"}},
            ]
        )
    )
    events += m.handle(_tool_end("c1", "mcp__web__fetch"))
    events += m.handle(_tool_end("c2", "skill"))
    events += m.handle(_tool_end("c3", "calculator"))
    return events


def test_provider_穿透_tool_call_and_result():
    events = _drive({"mcp__web__fetch": "mcp", "skill": "skill"})
    by_name = {}
    for e in events:
        payload = e.tool_call or e.tool_result
        if payload is None:
            continue
        by_name.setdefault(payload.tool_name, set()).add(payload.tool_provider)

    assert by_name["mcp__web__fetch"] == {"mcp"}   # RUNNING + SUCCESS + result 全为 mcp
    assert by_name["skill"] == {"skill"}
    assert by_name["calculator"] == {"local"}       # 未登记回落 local


def test_default_provider_local_when_no_map():
    events = _drive({})
    providers = {
        (e.tool_call or e.tool_result).tool_provider
        for e in events
        if e.tool_call or e.tool_result
    }
    assert providers == {"local"}  # 向后兼容：不注入即全 local


def test_seq_monotonic_no_finish():
    events = _drive({"mcp__web__fetch": "mcp"})
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(seqs) + 1))       # 单调无空洞从 1
    assert not any(e.type is EventType.RESULT for e in events)  # 无 result → 无 finish
