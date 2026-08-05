"""文本切分（纯逻辑）。"""

from __future__ import annotations

from cognition.rag.chunking import split_text


def test_empty_returns_empty():
    assert split_text("") == []
    assert split_text("   ") == []


def test_short_text_single_chunk():
    assert split_text("你好世界。", size=500) == ["你好世界。"]


def test_splits_by_size_with_overlap():
    text = "".join(f"第{i}句话。" for i in range(200))  # 远超 size
    chunks = split_text(text, size=100, overlap=20, hard_max=8000)
    assert len(chunks) > 1
    assert all(len(c) <= 8000 for c in chunks)
    # 相邻块有重叠：后块开头能在前块结尾找到
    assert chunks[1][:10] in chunks[0]


def test_hard_max_enforced_on_long_sentence():
    text = "x" * 5000  # 单句无标点
    chunks = split_text(text, size=500, overlap=50, hard_max=1000)
    assert all(len(c) <= 1000 for c in chunks)
    # overlap 会跨块复制少量字符，故 >=5000（无数据丢失），不会 <5000
    assert "".join(chunks).count("x") >= 5000


def test_chinese_punctuation_not_broken_midword():
    text = "人工智能是什么？它能做很多事情！机器学习是其分支。"
    chunks = split_text(text, size=12, overlap=0)
    # 每块以句末标点收尾（未把句子从中间劈开）
    assert all(c.strip()[-1] in "。！？!?；;" for c in chunks if c.strip())
