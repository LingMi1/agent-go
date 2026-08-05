"""rerank 后处理（纯逻辑）：用打分给文档排序 + 阈值过滤 + 取 top_k。

打分由 RerankProvider（I/O）产出，这里只做确定性的排序/过滤/截断，便于单测。
"""

from __future__ import annotations

from cognition.rag.types import RetrievedDoc


def order_by_score(
    docs: list[RetrievedDoc],
    scores: list[float],
    *,
    threshold: float = 0.0,
    top_k: int = 5,
) -> list[RetrievedDoc]:
    """把 scores 覆盖到 docs.score，按分降序，过滤 score>threshold，取前 top_k。

    docs 与 scores 长度不一致时按较短对齐（防御）。稳定排序：同分保留原相对序。
    """
    n = min(len(docs), len(scores))
    scored: list[tuple[int, RetrievedDoc, float]] = []
    for i in range(n):
        d = dict(docs[i])
        s = float(scores[i])
        d["score"] = s
        if s > threshold:
            scored.append((i, d, s))  # type: ignore[arg-type]
    scored.sort(key=lambda t: (-t[2], t[0]))  # 分降序，稳定
    return [d for _, d, _ in scored[: max(top_k, 0)]]
