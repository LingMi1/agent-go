"""Sparse 向量 provider 抽象（I/O）+ 确定性 FakeSparseEmbedder + 惰性 fastembed BM25。

sparse 向量以 (indices, values) 表示，直接喂 Qdrant SparseVector。
- FakeSparseEmbedder：token 哈希到大空间的索引 + 词频值，确定性、无依赖（测试用）。
- FastembedBm25Provider：惰性 fastembed 的 Qdrant/bm25（生产；不测试）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from cognition.rag.embeddings import _token_hash, tokenize

_SPARSE_DIM = 1 << 20  # 稀疏索引空间（哈希取模）


class SparseProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        ...


class FakeSparseEmbedder(SparseProvider):
    """确定性 hashing 稀疏向量：index=token_hash%dim，value=词频。"""

    def __init__(self, dim: int = _SPARSE_DIM) -> None:
        self.dim = dim

    def _one(self, text: str) -> tuple[list[int], list[float]]:
        counts: dict[int, float] = {}
        for tok in tokenize(text):
            idx = _token_hash(tok) % self.dim
            counts[idx] = counts.get(idx, 0.0) + 1.0
        if not counts:
            return [], []
        items = sorted(counts.items())  # 索引升序，稳定
        return [i for i, _ in items], [v for _, v in items]

    def embed(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        return [self._one(t) for t in texts]


class FastembedBm25Provider(SparseProvider):
    """fastembed 的 BM25 稀疏 embedding（惰性；首用会下模型）。"""

    def __init__(self, model: str = "Qdrant/bm25") -> None:
        self.model = model
        self._impl = None

    def _ensure(self):
        if self._impl is None:
            from fastembed import SparseTextEmbedding  # 惰性

            self._impl = SparseTextEmbedding(model_name=self.model)
        return self._impl

    def embed(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        out: list[tuple[list[int], list[float]]] = []
        for emb in self._ensure().embed(texts):
            out.append((list(map(int, emb.indices)), list(map(float, emb.values))))
        return out
