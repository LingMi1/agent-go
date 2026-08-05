"""Qdrant 混合检索存储（I/O）：dense+sparse 命名向量 + 原生 RRF 融合。

关键约束（亲测）：kb 隔离过滤**必须写进每路 `Prefetch(filter=...)`**——顶层 `query_filter`
在 FusionQuery 下不生效，会跨库漏数据。`QdrantClient(":memory:")` 完整支持本文件的调用，供契约测试。
"""

from __future__ import annotations

from typing import Any, Optional

from qdrant_client import QdrantClient, models

from cognition.rag.types import RetrievedDoc

DENSE_VECTOR = "dense_vector"
SPARSE_VECTOR = "sparse_vector"


class QdrantStore:
    """单集合命名向量存储。dense 用 COSINE，sparse 用点积。"""

    def __init__(
        self,
        client: QdrantClient,
        collection: str,
        dimension: int,
        *,
        prefetch_limit: int = 20,
    ) -> None:
        self._c = client
        self._col = collection
        self._dim = dimension
        self._prefetch = prefetch_limit

    @classmethod
    def from_settings(cls, settings: Any) -> "QdrantStore":
        url = getattr(settings, "qdrant_url", "") or ""
        # 约定：url 为 ":memory:" 或空且显式要求时用本地内存模式（测试/离线）。
        client = QdrantClient(location=":memory:") if url in (":memory:", "") else QdrantClient(url=url)
        return cls(
            client,
            getattr(settings, "qdrant_collection", "cognition_docs"),
            int(getattr(settings, "embedding_dimension", 64)),
            prefetch_limit=int(getattr(settings, "rag_prefetch_limit", 20)),
        )

    def ensure_collection(self) -> None:
        """幂等建集合（dense 命名向量 + sparse 命名向量）。"""
        if self._c.collection_exists(self._col):
            return
        self._c.create_collection(
            self._col,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(size=self._dim, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams()},
        )

    def upsert(self, points: list[models.PointStruct]) -> None:
        if points:
            self._c.upsert(self._col, points=points, wait=True)

    def hybrid_query(
        self,
        dense: list[float],
        sparse: tuple[list[int], list[float]],
        *,
        kb_id: str,
        limit: int,
    ) -> list[RetrievedDoc]:
        """dense + sparse 双路 Prefetch → RRF 融合。kb 过滤写进每路 Prefetch。"""
        flt: Optional[models.Filter] = None
        if kb_id:
            flt = models.Filter(
                must=[models.FieldCondition(key="kb_id", match=models.MatchValue(value=kb_id))]
            )
        s_idx, s_val = sparse
        res = self._c.query_points(
            self._col,
            prefetch=[
                models.Prefetch(query=dense, using=DENSE_VECTOR, limit=self._prefetch, filter=flt),
                models.Prefetch(
                    query=models.SparseVector(indices=s_idx, values=s_val),
                    using=SPARSE_VECTOR,
                    limit=self._prefetch,
                    filter=flt,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
        return [self._to_doc(p) for p in res.points]

    def delete_by_kb_id(self, kb_id: str) -> None:
        """按 kb_id 全量删除（重灌前用；文件级增量留 seam）。"""
        self._c.delete(
            self._col,
            points_selector=models.Filter(
                must=[models.FieldCondition(key="kb_id", match=models.MatchValue(value=kb_id))]
            ),
        )

    @staticmethod
    def _to_doc(point: Any) -> RetrievedDoc:
        payload = point.payload or {}
        return RetrievedDoc(
            id=str(point.id),
            text=str(payload.get("text", "")),
            score=float(getattr(point, "score", 0.0) or 0.0),
            dedup_key=str(payload.get("dedup_key", payload.get("text", ""))),
            source_id=str(payload.get("source_id", "")),
            file_name=str(payload.get("file_name", "")),
            chunk_type=str(payload.get("chunk_type", "text")),
            image_url=payload.get("image_url"),
        )
