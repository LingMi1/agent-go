"""Qdrant 混合检索契约（QdrantClient(":memory:") + Fake，不触网/不下模型/不烧钱）。"""

from __future__ import annotations

from qdrant_client import QdrantClient

from cognition.rag.embeddings import FakeEmbedder
from cognition.rag.ingest import ingest
from cognition.rag.sparse import FakeSparseEmbedder
from cognition.rag.store import QdrantStore

DIM = 64


def _store():
    return QdrantStore(QdrantClient(location=":memory:"), "docs", DIM)


def _fixtures(store):
    ingest(
        [
            {"text": "人工智能是研究智能体的学科。机器学习是其核心分支。", "file_name": "ai.md"},
            {"text": "Python 是一门流行的编程语言，广泛用于数据科学。", "file_name": "py.md"},
        ],
        kb_id="kb1",
        store=store,
        embedder=FakeEmbedder(DIM),
        sparse=FakeSparseEmbedder(),
    )
    ingest(
        [{"text": "这是另一个知识库的内容，关于烹饪。", "file_name": "cook.md"}],
        kb_id="kb2",
        store=store,
        embedder=FakeEmbedder(DIM),
        sparse=FakeSparseEmbedder(),
    )


def test_ensure_collection_idempotent():
    store = _store()
    store.ensure_collection()
    store.ensure_collection()  # 再次不报错


def test_hybrid_query_returns_payload():
    store = _store()
    _fixtures(store)
    q = "机器学习"
    dv = FakeEmbedder(DIM).embed([q])[0]
    sv = FakeSparseEmbedder().embed([q])[0]
    docs = store.hybrid_query(dv, sv, kb_id="kb1", limit=5)
    assert docs
    top = docs[0]
    assert "机器学习" in top["text"] or "人工智能" in top["text"]
    assert top["file_name"] in {"ai.md", "py.md"}
    assert "score" in top and top["chunk_type"] == "text"


def test_kb_filter_isolation():
    """关键：kb 过滤写进每路 Prefetch，绝不能漏出其它 kb。"""
    store = _store()
    _fixtures(store)
    dv = FakeEmbedder(DIM).embed(["内容"])[0]
    sv = FakeSparseEmbedder().embed(["内容"])[0]
    docs = store.hybrid_query(dv, sv, kb_id="kb2", limit=10)
    assert docs
    assert all(d["file_name"] == "cook.md" for d in docs)  # 只见 kb2


def test_delete_by_kb_id():
    store = _store()
    _fixtures(store)
    store.delete_by_kb_id("kb1")
    dv = FakeEmbedder(DIM).embed(["机器学习"])[0]
    sv = FakeSparseEmbedder().embed(["机器学习"])[0]
    assert store.hybrid_query(dv, sv, kb_id="kb1", limit=5) == []
