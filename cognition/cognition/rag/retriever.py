"""检索编排：多子问题各做一次混合检索，跨子问题并集去重。"""

from __future__ import annotations

from cognition.rag.embeddings import EmbeddingProvider
from cognition.rag.fusion import dedup_docs
from cognition.rag.sparse import SparseProvider
from cognition.rag.store import QdrantStore
from cognition.rag.types import RetrievedDoc


class Retriever:
    def __init__(
        self,
        store: QdrantStore,
        embedder: EmbeddingProvider,
        sparse: SparseProvider,
        *,
        top_k: int = 10,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._sparse = sparse
        self._top_k = top_k

    def retrieve(self, queries: list[str], *, kb_id: str, top_k: int | None = None) -> list[RetrievedDoc]:
        """对每个子问题混合检索，合并去重。"""
        queries = [q for q in queries if q.strip()]
        if not queries:
            return []
        k = top_k or self._top_k
        dense_vecs = self._embedder.embed(queries)
        sparse_vecs = self._sparse.embed(queries)
        docs: list[RetrievedDoc] = []
        for dv, sv in zip(dense_vecs, sparse_vecs):
            docs.extend(self._store.hybrid_query(dv, sv, kb_id=kb_id, limit=k))
        return dedup_docs(docs)
