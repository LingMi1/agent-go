"""跨子问题并集去重（纯逻辑）。"""

from __future__ import annotations

from cognition.rag.fusion import dedup_docs


def _d(id_, key, score, text="t"):
    return {"id": id_, "dedup_key": key, "score": score, "text": text}


def test_dedup_keeps_higher_score():
    out = dedup_docs([_d("1", "k1", 0.3), _d("2", "k1", 0.9), _d("3", "k2", 0.5)])
    assert len(out) == 2
    k1 = next(d for d in out if d["dedup_key"] == "k1")
    assert k1["score"] == 0.9  # 同键保留高分


def test_order_stable_by_first_appearance():
    out = dedup_docs([_d("a", "kb", 0.1), _d("b", "ka", 0.1), _d("c", "kb", 0.2)])
    assert [d["dedup_key"] for d in out] == ["kb", "ka"]  # 首次出现定序


def test_empty():
    assert dedup_docs([]) == []


def test_missing_dedup_key_falls_back_to_id():
    out = dedup_docs([{"id": "x", "score": 0.1, "text": "t"}, {"id": "x", "score": 0.5, "text": "t"}])
    assert len(out) == 1 and out[0]["score"] == 0.5
