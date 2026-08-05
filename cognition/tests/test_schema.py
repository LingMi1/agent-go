"""事件 schema：pydantic 校验 + finish 仅 result 不变量。"""

import pytest
from pydantic import ValidationError

from cognition.events.schema import (
    Event,
    EventType,
    ResultPayload,
    ThoughtPayload,
    ToolCallStatus,
    ToolPayload,
)


def test_result_can_finish():
    e = Event(
        seq=1,
        run_id="r1",
        message_id="r1:result",
        type=EventType.RESULT,
        is_final=True,
        finish=True,
        result=ResultPayload(text="14"),
    )
    assert e.finish is True
    assert e.result.text == "14"


def test_finish_only_on_result():
    # 非 result 事件不允许 finish=True
    with pytest.raises(ValidationError):
        Event(
            seq=1,
            run_id="r1",
            message_id="m",
            type=EventType.TOOL_THOUGHT,
            finish=True,
            tool_thought=ThoughtPayload(text="x"),
        )


def test_payload_must_match_type():
    with pytest.raises(ValidationError):
        Event(
            seq=1,
            run_id="r1",
            message_id="m",
            type=EventType.TOOL_CALL,
            tool_thought=ThoughtPayload(text="oops"),  # 错误的载荷
        )


def test_seq_must_be_positive():
    with pytest.raises(ValidationError):
        Event(
            seq=0,
            run_id="r1",
            message_id="m",
            type=EventType.RESULT,
            result=ResultPayload(text=""),
        )


def test_tool_payload_defaults():
    p = ToolPayload(tool_call_id="c1", tool_name="calculator", status=ToolCallStatus.RUNNING)
    assert p.tool_provider == "local"
    assert p.input == {}
    assert p.artifact_refs == []


def test_tool_call_event_ok():
    e = Event(
        seq=2,
        run_id="r1",
        message_id="c1",
        type=EventType.TOOL_CALL,
        is_final=False,
        tool_call=ToolPayload(
            tool_call_id="c1",
            tool_name="calculator",
            status=ToolCallStatus.RUNNING,
            dispatch_index=1,
            input={"expression": "2*(3+4)"},
            summary="正在调用 calculator",
        ),
    )
    assert e.message_id == e.tool_call.tool_call_id == "c1"
    assert e.finish is False
