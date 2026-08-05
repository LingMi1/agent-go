"""ingest 幂等（stable_ids）：同内容重复入库不翻倍（M8 附件 run 前自动入库的前提）。

背景：ingest 默认 point id=uuid4，run 时 lazy ingest 下同附件重发/失败重试/续聊重跑
都会重复写点。幂等键必须是**内容哈希**（dedup_key）而非 source_id——上传 key 带
{uuid8} 前缀，同一文件传两次 source_id 不同。stable_ids=True 时
point id = uuid5(NS, f"{kb_id}|{dedup_key}")，Qdrant upsert 语义下重复入库=原地覆盖。
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from cognition.rag.embeddings import FakeEmbedder
from cognition.rag.ingest import ingest
from cognition.rag.sparse import FakeSparseEmbedder
from cognition.rag.store import QdrantStore

DIM = 64


def _store() -> QdrantStore:
    return QdrantStore(QdrantClient(location=":memory:"), "docs", DIM)


def _count(store: QdrantStore) -> int:
    return store._c.count("docs").count  # noqa: SLF001 — 测试直查底层


def _ingest(store, docs, kb="kb1", **kw) -> int:
    return ingest(
        docs, kb, store=store, embedder=FakeEmbedder(DIM), sparse=FakeSparseEmbedder(), **kw
    )


def test_stable_ids_same_doc_twice_no_duplicates():
    store = _store()
    doc = {"text": "苹果是一种水果。香蕉也是水果。", "file_name": "a.txt", "source_id": "up1/a.txt"}
    n1 = _ingest(store, [doc], stable_ids=True)
    assert n1 > 0
    total1 = _count(store)
    # 同一文件再次上传：source_id 不同（新 uuid8 前缀）但内容相同 → 点数不变。
    doc2 = dict(doc, source_id="up2/a.txt")
    _ingest(store, [doc2], stable_ids=True)
    assert _count(store) == total1


def test_stable_ids_isolated_by_kb():
    store = _store()
    doc = {"text": "同一段内容", "file_name": "a.txt"}
    _ingest(store, [doc], kb="kb1", stable_ids=True)
    total1 = _count(store)
    # 不同 kb 的同内容是不同的点（幂等键含 kb_id，不互相覆盖）。
    _ingest(store, [doc], kb="kb2", stable_ids=True)
    assert _count(store) == total1 * 2


def test_default_path_unchanged_random_ids():
    store = _store()
    doc = {"text": "默认路径行为保持", "file_name": "a.txt"}
    _ingest(store, [doc])
    total1 = _count(store)
    _ingest(store, [doc])  # 默认 uuid4：重复入库确实翻倍（既有语义不变）
    assert _count(store) == total1 * 2
