"""会话历史投影（纯逻辑）：预算触发/近期优先/锚点/tool 配对保护/确定性摘要。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cognition.graphs.history import HistoryPolicy, plan_history_reduction

SYS = SystemMessage(content="你是助手")
H0 = HumanMessage(content="第一个问题")


def _tool_turn(i: int):
    """一轮工具调用：AIMessage(tool_calls) + 对应 ToolMessage。"""
    ai = AIMessage(content=f"我调用工具{i}", tool_calls=[{"name": "calc", "args": {}, "id": f"c{i}"}])
    tm = ToolMessage(content=f"结果{i}", tool_call_id=f"c{i}")
    return [ai, tm]


def _no_orphans(messages):
    """校验无 orphan：每个 ToolMessage 的 tool_call_id 都能在其前的 AIMessage.tool_calls 找到；
    每个被保留的 AIMessage.tool_calls 的 id 都有后续 ToolMessage。"""
    seen_ids: set[str] = set()
    produced: set[str] = set()
    consumed: set[str] = set()
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                produced.add(tc["id"])
                seen_ids.add(tc["id"])
        if isinstance(m, ToolMessage):
            consumed.add(m.tool_call_id)
            assert m.tool_call_id in seen_ids, f"orphan tool_result: {m.tool_call_id}"
    assert produced == consumed, f"tool_use/tool_result 不配对: {produced ^ consumed}"


def test_under_budget_returns_as_is():
    msgs = [SYS, H0, AIMessage(content="回答")]
    r = plan_history_reduction(msgs, HistoryPolicy(max_messages=40, max_chars=24000))
    assert r.summarized is False
    assert r.messages == msgs


def test_over_budget_summarizes_and_keeps_anchors_and_recent():
    # 系统 + 首Human + 10 轮工具对（22 条），低预算触发折叠
    msgs = [SYS, H0]
    for i in range(10):
        msgs += _tool_turn(i)
    msgs.append(AIMessage(content="最终答复"))
    r = plan_history_reduction(msgs, HistoryPolicy(max_messages=8, max_chars=100000))
    assert r.summarized is True
    # 锚点保留
    assert r.messages[0] is SYS and r.messages[1] is H0
    # 摘要紧随锚点
    assert isinstance(r.messages[2], SystemMessage) and "前情摘要" in r.messages[2].content
    # 最近的最终答复保留
    assert r.messages[-1].content == "最终答复"
    # 总数不超过 max_messages（锚点2 + 摘要1 + 近期）
    assert len(r.messages) <= 8
    _no_orphans(r.messages)


def test_tool_pairing_never_split():
    # 预算恰好落在某轮工具对中间：绝不能只留 ToolMessage 而丢其 AIMessage
    msgs = [SYS, H0]
    for i in range(6):
        msgs += _tool_turn(i)
    # 多个不同 max_messages 值都不能产生 orphan
    for mm in (4, 5, 6, 7, 9):
        r = plan_history_reduction(msgs, HistoryPolicy(max_messages=mm, max_chars=100000))
        _no_orphans(r.messages)


def test_char_budget_triggers():
    big = "x" * 5000
    msgs = [SYS, H0] + [AIMessage(content=big) for _ in range(10)]
    r = plan_history_reduction(msgs, HistoryPolicy(max_messages=100, max_chars=6000))
    assert r.summarized is True
    assert sum(len(m.content) for m in r.messages) <= 6000 + 5000  # 近期单条可超，但整体受控


def test_custom_summarize_fn_seam():
    msgs = [SYS, H0]
    for i in range(6):
        msgs += _tool_turn(i)
    r = plan_history_reduction(
        msgs, HistoryPolicy(max_messages=5, max_chars=100000),
        summarize_fn=lambda dropped: f"LLM摘要:{len(dropped)}组",
    )
    assert "LLM摘要:" in r.messages[2].content


def test_think_node_applies_projection():
    """接线验证：think 节点入模型前对累积 messages 做投影（fake 模型记录所见）。"""
    from cognition.graphs.nodes import make_think_node

    class _Recorder:
        def __init__(self):
            self.seen = None

        def invoke(self, messages):
            self.seen = list(messages)
            return AIMessage(content="ok")

    rec = _Recorder()
    node = make_think_node(rec, history_policy=HistoryPolicy(max_messages=6, max_chars=100000))
    long_msgs = [SYS, H0]
    for i in range(8):
        long_msgs += _tool_turn(i)
    out = node({"messages": long_msgs, "step": 0})
    assert out["step"] == 1
    assert rec.seen is not None
    assert len(rec.seen) < len(long_msgs)          # 确实被裁剪
    assert rec.seen[0] is SYS                        # 锚点在
    _no_orphans(rec.seen)                            # 无 orphan tool


def test_think_node_no_policy_passes_through():
    from cognition.graphs.nodes import make_think_node

    class _Recorder:
        def invoke(self, messages):
            self.seen = list(messages)
            return AIMessage(content="ok")

    rec = _Recorder()
    node = make_think_node(rec)  # 无 policy → 行为同 M1
    msgs = [SYS, H0, AIMessage(content="x")]
    node({"messages": msgs, "step": 0})
    assert rec.seen == msgs


# —— M8：多模态块内容的字符预算与摘要（base64 不得计入预算/漏进摘要）——
def test_block_content_text_and_image_placeholder():
    from cognition.graphs.history import IMAGE_CHAR_COST, _char_cost, _text

    fake_b64 = "x" * 200_000  # 模拟一张图的 base64
    msg = HumanMessage(content=[
        {"type": "text", "text": "看这张图"},
        {"type": "image", "source_type": "base64", "data": fake_b64, "mime_type": "image/png"},
    ])
    # 可见文本 = text join + [image] 占位，绝不包含 base64。
    assert _text(msg) == "看这张图[image]"
    # 字符预算 = 文本长度 + 每图固定估价（不是 len(base64)）。
    assert _char_cost(msg) == len("看这张图") + IMAGE_CHAR_COST


def test_block_content_budget_not_exploded_by_base64():
    fake_b64 = "y" * 100_000
    msgs = [
        SYS,
        H0,
        HumanMessage(content=[
            {"type": "text", "text": "短问题"},
            {"type": "image", "source_type": "base64", "data": fake_b64, "mime_type": "image/png"},
        ]),
        AIMessage(content="短回答"),
    ]
    # 预算远大于 文本+1600 但远小于 base64 长度：不得触发裁剪。
    out = plan_history_reduction(msgs, HistoryPolicy(max_messages=40, max_chars=10_000))
    assert out.summarized is False and out.messages == msgs


def test_summary_never_swallows_base64():
    fake_b64 = "z" * 50_000
    old_img = HumanMessage(content=[
        {"type": "text", "text": "旧图片消息"},
        {"type": "image", "source_type": "base64", "data": fake_b64, "mime_type": "image/png"},
    ])
    filler = []
    for i in range(30):
        filler.append(HumanMessage(content=f"问题{i}"))
        filler.append(AIMessage(content=f"回答{i}"))
    msgs = [SYS, H0, old_img] + filler
    out = plan_history_reduction(msgs, HistoryPolicy(max_messages=8, max_chars=2_000))
    assert out.summarized is True
    joined = "".join(str(getattr(m, "content", "")) for m in out.messages)
    assert "zzzz" not in joined, "base64 漏进了摘要/投影"
