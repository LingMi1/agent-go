"""按 Settings 构建 RAG 的 I/O provider（embedding / sparse / rerank / store）。

集中 provider 选择：fake（测试/离线）| fastembed（本地）| siliconflow（国产 API）。
这样 graph.py 与 scripts/ingest.py 都从这里取，切换只改 env。
"""

from __future__ import annotations

from typing import Any

from cognition.rag.embeddings import (
    EmbeddingProvider,
    FakeEmbedder,
    FastembedEmbedder,
    OpenAICompatEmbedder,
)
from cognition.rag.reranker import ApiReranker, FakeReranker, RerankProvider
from cognition.rag.sparse import FakeSparseEmbedder, FastembedBm25Provider, SparseProvider
from cognition.rag.store import QdrantStore


def build_embedder(settings: Any) -> EmbeddingProvider:
    provider = getattr(settings, "embedding_provider", "fake")
    dim = int(getattr(settings, "embedding_dimension", 64))
    if provider == "fastembed":
        return FastembedEmbedder(getattr(settings, "embedding_model", "BAAI/bge-small-zh-v1.5"), dim)
    if provider == "siliconflow":
        return OpenAICompatEmbedder(
            base_url=getattr(settings, "embedding_base_url", ""),
            api_key=getattr(settings, "embedding_api_key", "") or "",
            model=getattr(settings, "embedding_model", ""),
            dimension=dim,
        )
    return FakeEmbedder(dim)


def build_sparse(settings: Any) -> SparseProvider:
    if getattr(settings, "sparse_provider", "fake") == "fastembed":
        return FastembedBm25Provider()
    return FakeSparseEmbedder()


def build_reranker(settings: Any) -> RerankProvider:
    if getattr(settings, "rerank_provider", "fake") == "siliconflow":
        return ApiReranker(
            base_url=getattr(settings, "rerank_base_url", ""),
            api_key=getattr(settings, "rerank_api_key", "") or "",
            model=getattr(settings, "rerank_model", ""),
        )
    return FakeReranker()


def build_store(settings: Any) -> QdrantStore:
    return QdrantStore.from_settings(settings)
