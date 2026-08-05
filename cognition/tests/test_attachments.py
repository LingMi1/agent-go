"""附件消息构造与展开（M8 C5）：checkpoint 防膨胀 + vision 门控 + 全量替换不变量。

关键不变量：
1. state/checkpoint 里只有 pro_attachment 引用块（无 base64）；
2. expand 之后**不得残留任何 pro_attachment 块**（provider 对未知块类型 400）；
3. 非 vision provider / 下载失败 / 超尺寸 → 文本占位降级，不炸 run；
4. 同 thread 两轮续聊（第一轮带附件）：第二轮 repair→裁剪→expand 三道投影不崩。
"""

from __future__ import annotations

import asyncio
import base64

from langchain_core.messages import AIMessage, HumanMessage

from cognition.attachments import (
    ATTACHMENT_BLOCK_TYPE,
    attachment_note,
    build_attachment_message,
    expand_attachment_blocks,
    normalize_attachments,
    supports_vision,
)
from cognition.graphs.react import build_react_graph
from cognition.providers.fake import MessageDrivenChatModel
from cognition.tools.calculator import calculator

PNG = {"resource_key": "uploads/u1/s1/ab12-cat.png", "file_name": "cat.png", "mime_type": "image/png", "size": 1024}
TXT = {"resource_key": "uploads/u1/s1/cd34-note.txt", "file_name": "note.txt", "mime_type": "text/plain", "size": 64}


def _no_ref_blocks(messages) -> bool:
    for m in messages:
        c = getattr(m, "content", "")
        if isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == ATTACHMENT_BLOCK_TYPE for b in c
        ):
            return False
    return True


def test_supports_vision_table():
    assert supports_vision("anthropic") and supports_vision("Anthropic")
    assert not supports_vision("deepseek") and not supports_vision("")


def test_normalize_attachments_from_obj_and_dict():
    from types import SimpleNamespace

    objs = [SimpleNamespace(resource_key="uploads/u/s/x-a.png", file_name="", mime_type="image/png", size=9)]
    out = normalize_attachments(objs + [dict(TXT)])
    assert out[0]["file_name"] == "x-a.png"  # 缺省取 key 尾段
    assert out[1] == TXT
    assert normalize_attachments([{"resource_key": ""}]) == []  # 无 key 丢弃


def test_build_message_blocks_only_for_images():
    # 仅文本附件：退化为纯字符串 content（不走块路径），注记在场。
    m1 = build_attachment_message("总结一下", [TXT], ingested_names=["note.txt"])
    assert isinstance(m1.content, str)
    assert "note.txt" in m1.content and "knowledge_search" in m1.content
    # 含图片：text 块 + pro_attachment 引用块，绝无 base64。
    m2 = build_attachment_message("看图", [PNG, TXT])
    assert isinstance(m2.content, list)
    types = [b.get("type") for b in m2.content]
    assert types == ["text", ATTACHMENT_BLOCK_TYPE]
    assert m2.content[1]["resource_key"] == PNG["resource_key"]


def test_expand_vision_true_replaces_all_ref_blocks():
    msg = build_attachment_message("看图", [PNG])
    raw = b"\x89PNG-fake-bytes"
    out = expand_attachment_blocks([msg], downloader=lambda key: raw, vision=True)
    assert _no_ref_blocks(out)
    img = [b for b in out[0].content if isinstance(b, dict) and b.get("type") == "image"]
    assert len(img) == 1
    assert img[0]["source_type"] == "base64"
    assert img[0]["mime_type"] == "image/png"
    assert base64.b64decode(img[0]["data"]) == raw
    # 原消息不被改动（只读投影）。
    assert any(b.get("type") == ATTACHMENT_BLOCK_TYPE for b in msg.content if isinstance(b, dict))


def test_expand_vision_false_degrades_to_text():
    msg = build_attachment_message("看图", [PNG])
    out = expand_attachment_blocks([msg], downloader=lambda key: b"x", vision=False)
    assert _no_ref_blocks(out)
    texts = [b["text"] for b in out[0].content if isinstance(b, dict) and b.get("type") == "text"]
    assert any("不支持图像理解" in t for t in texts)


def test_expand_oversize_and_download_failure_degrade():
    msg = build_attachment_message("看图", [PNG])
    big = b"b" * (5 * 1024 * 1024)
    out1 = expand_attachment_blocks([msg], downloader=lambda key: big, vision=True)
    assert _no_ref_blocks(out1)
    assert any("上限" in str(b.get("text", "")) for b in out1[0].content if isinstance(b, dict))

    def boom(key: str) -> bytes:
        raise RuntimeError("minio down")

    out2 = expand_attachment_blocks([msg], downloader=boom, vision=True)
    assert _no_ref_blocks(out2)
    assert any("读取失败" in str(b.get("text", "")) for b in out2[0].content if isinstance(b, dict))


def test_plain_messages_pass_through_unchanged():
    msgs = [HumanMessage(content="纯文本"), AIMessage(content="回答")]
    out = expand_attachment_blocks(msgs, downloader=lambda key: b"", vision=True)
    assert out == msgs


def test_two_turn_continuation_with_attachment_memory_saver():
    """同 thread 两轮（第一轮带图片附件）：模型两轮看到的消息都无 ref 块、无异常。"""
    from langgraph.checkpoint.memory import MemorySaver

    seen: list[list] = []

    def decide(messages):
        seen.append(list(messages))
        assert _no_ref_blocks(messages), "pro_attachment 泄漏给 provider"
        return AIMessage(content="OK")

    graph = build_react_graph(
        MessageDrivenChatModel(decide=decide),
        [calculator],
        checkpointer=MemorySaver(),
        expander=lambda msgs: expand_attachment_blocks(
            msgs, downloader=lambda key: b"img-bytes", vision=True
        ),
    )
    cfg = {"configurable": {"thread_id": "sess-att"}, "metadata": {"request_id": "r1"}}
    state1 = {
        "messages": [build_attachment_message("这图里有什么？", [PNG])],
        "request_id": "r1", "session_id": "sess-att", "query": "这图里有什么？",
        "product_files": [PNG], "is_stream": True, "step": 0,
    }
    asyncio.run(graph.ainvoke(state1, cfg))
    # 第二轮：普通提问，checkpoint 恢复的历史里仍是 ref 块 → 展开投影必须再次生效。
    state2 = {
        "messages": [HumanMessage(content="继续")],
        "request_id": "r2", "session_id": "sess-att", "query": "继续",
        "product_files": [], "is_stream": True, "step": 0,
    }
    asyncio.run(graph.ainvoke(state2, {"configurable": {"thread_id": "sess-att"}, "metadata": {"request_id": "r2"}}))
    assert len(seen) >= 2
    # checkpoint 中的原始消息仍是引用块（防膨胀）：直接查 checkpointer 状态。
    snap = graph.get_state({"configurable": {"thread_id": "sess-att"}})
    stored = snap.values["messages"]
    stored_first = stored[0]
    assert isinstance(stored_first.content, list)
    assert any(
        isinstance(b, dict) and b.get("type") == ATTACHMENT_BLOCK_TYPE for b in stored_first.content
    ), "checkpoint 里应保存引用块而非 base64"
    joined = str(stored)
    assert base64.b64encode(b"img-bytes").decode() not in joined, "base64 泄漏进 checkpoint"


def test_attachment_note_shapes():
    assert attachment_note([]) == ""
    note = attachment_note([PNG, TXT], ingested_names=["note.txt"])
    assert "cat.png" in note and "note.txt" in note and "knowledge_search" in note
