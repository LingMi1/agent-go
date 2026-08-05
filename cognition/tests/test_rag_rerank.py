"""rerank 排序 + 阈值 + top_k（纯逻辑）。"""

from __future__ import annotations

from cognition.rag.rerank import order_by_score


def _docs(n):
    return [{"id": str(i), "text": f"d{i}"} for i in range(n)]


def test_sorts_desc_and_overrides_score():
    out = order_by_score(_docs(3), [0.2, 0.9, 0.5], threshold=0.0, top_k=3)
    assert [d["id"] for d in out] == ["1", "2", "0"]
    assert out[0]["score"] == 0.9


def test_threshold_filters():
    out = order_by_score(_docs(3), [0.1, 0.4, 0.2], threshold=0.3, top_k=5)
    assert [d["id"] for d in out] == ["1"]  # 只有 0.4 > 0.3


def test_top_k_truncates():
    out = order_by_score(_docs(4), [0.9, 0.8, 0.7, 0.6], threshold=0.0, top_k=2)
    assert len(out) == 2 and [d["id"] for d in out] == ["0", "1"]


def test_length_mismatch_defensive():
    out = order_by_score(_docs(3), [0.9], threshold=0.0, top_k=5)
    assert len(out) == 1 and out[0]["id"] == "0"  # 按较短对齐


def test_stable_on_ties():
    out = order_by_score(_docs(3), [0.5, 0.5, 0.5], threshold=0.0, top_k=3)
    assert [d["id"] for d in out] == ["0", "1", "2"]  # 同分保留原序
