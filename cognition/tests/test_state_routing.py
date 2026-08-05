"""route_after_agent 纯函数：无工具→END / 有工具且未超步→tools / 超步→END。"""

from langchain_core.messages import AIMessage
from langgraph.graph import END

from cognition.graphs.state import route_after_agent


def _state(last_msg, step):
    return {
        "messages": [last_msg],
        "request_id": "r1",
        "session_id": "s1",
        "query": "q",
        "product_files": [],
        "is_stream": True,
        "step": step,
    }


def _ai_with_tool():
    return AIMessage(
        content="",
        tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "c1"}],
    )


def test_no_tool_call_goes_to_end():
    state = _state(AIMessage(content="final answer"), step=1)
    assert route_after_agent(state, max_steps=40) == END


def test_tool_call_under_max_goes_to_tools():
    state = _state(_ai_with_tool(), step=1)
    assert route_after_agent(state, max_steps=40) == "tools"


def test_tool_call_at_or_over_max_goes_to_end():
    # step >= max_steps → 即便有 tool_calls 也终止
    state = _state(_ai_with_tool(), step=40)
    assert route_after_agent(state, max_steps=40) == END

    state2 = _state(_ai_with_tool(), step=41)
    assert route_after_agent(state2, max_steps=40) == END


def test_empty_messages_goes_to_end():
    state = _state(AIMessage(content="x"), step=0)
    state["messages"] = []
    assert route_after_agent(state, max_steps=40) == END
