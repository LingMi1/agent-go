"""Retriever 多子问题检索 + 跨子问题去重（:memory: + Fake）。"""

from __future__ import annotations

from qdrant_client import QdrantClient

from cognition.rag.embeddings import FakeEmbedder
from cognition.rag.ingest import ingest
from cognition.rag.retriever import Retriever
from cognition.rag.sparse import FakeSparseEmbedder
from cognition.rag.store import QdrantStore

DIM = 64


def _retriever():
    store = QdrantStore(QdrantClient(location=":memory:"), "docs", DIM)
    emb, sp = FakeEmbedder(DIM), FakeSparseEmbedder()
    ingest(
        [
            {"text": "机器学习是人工智能的分支，用数据训练模型。", "file_name": "ml.md"},
            {"text": "深度学习使用神经网络处理复杂模式。", "file_name": "dl.md"},
        ],
        kb_id="kb1",
        store=store,
        embedder=emb,
        sparse=sp,
    )
    return Retriever(store, emb, sp, top_k=5)


def test_multi_subquestion_dedup():
    r = _retriever()
    # 两个子问题都会命中 ml.md → 去重后不重复
    docs = r.retrieve(["什么是机器学习", "机器学习用什么训练"], kb_id="kb1")
    keys = [d["dedup_key"] for d in docs]
    assert len(keys) == len(set(keys))  # 无重复
    assert any("机器学习" in d["text"] for d in docs)


def test_empty_queries():
    r = _retriever()
    assert r.retrieve([], kb_id="kb1") == []
    assert r.retrieve(["  "], kb_id="kb1") == []
