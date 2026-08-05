"""跨子问题并集去重（纯逻辑）。

Qdrant 已在单次查询内做 dense+sparse 的 RRF 融合；这里合并"多个子问题各自的检索结果"：
按 dedup_key 去重，同键保留分数更高者，输出顺序稳定（按首次出现）。
"""

from __future__ import annotations

from cognition.rag.types import RetrievedDoc


def dedup_docs(docs: list[RetrievedDoc], *, key: str = "dedup_key") -> list[RetrievedDoc]:
    """并集去重：同 key 保留高分项；无 key 的用 id 兜底；顺序按首次出现稳定。"""
    order: list[str] = []
    best: dict[str, RetrievedDoc] = {}
    for d in docs:
        k = str(d.get(key) or d.get("id") or id(d))
        if k not in best:
            order.append(k)
            best[k] = d
        elif float(d.get("score", 0.0)) > float(best[k].get("score", 0.0)):
            best[k] = d
    return [best[k] for k in order]
