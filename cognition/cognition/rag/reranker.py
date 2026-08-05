"""rerank provider 抽象（I/O）+ 确定性 FakeReranker + 惰性 API reranker。

- FakeReranker：query 与 doc 的 token 重叠比例（Jaccard 近似）打分，确定性、无依赖（测试用）。
- ApiReranker：OpenAI/SiliconFlow 兼容 /rerank（bge-reranker-v2-m3）。惰性 httpx（生产；不测试）。
打分只负责给分，排序/阈值/截断在 rag/rerank.py 的纯逻辑里做。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from cognition.rag.embeddings import tokenize


class RerankProvider(ABC):
    @abstractmethod
    def score(self, query: str, texts: list[str]) -> list[float]:
        ...


class FakeReranker(RerankProvider):
    """确定性词重叠打分（∈[0,1]）。"""

    def score(self, query: str, texts: list[str]) -> list[float]:
        q = set(tokenize(query))
        if not q:
            return [0.0 for _ in texts]
        out: list[float] = []
        for t in texts:
            d = set(tokenize(t))
            inter = len(q & d)
            union = len(q | d) or 1
            out.append(inter / union)
        return out


class ApiReranker(RerankProvider):
    """OpenAI/SiliconFlow 兼容 rerank API。惰性 httpx。"""

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def score(self, query: str, texts: list[str]) -> list[float]:
        import httpx

        resp = httpx.post(
            f"{self.base_url}/rerank",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "query": query, "documents": texts},
            timeout=30.0,
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        scores = [0.0] * len(texts)
        for r in results:
            scores[int(r["index"])] = float(r["relevance_score"])
        return scores
