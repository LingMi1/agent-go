"""灌库/索引流程（编排）：chunk → dense+sparse embed → upsert 到 Qdrant。

文档可为纯字符串或 {"text","file_name","source_id"}。每块生成 dense+sparse 向量与 payload。
默认 FakeEmbedder + QdrantClient(":memory:") 可离线跑通；生产切真实 provider + Qdrant。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any, Union

from qdrant_client import models

from cognition.rag.chunking import split_text
from cognition.rag.embeddings import EmbeddingProvider
from cognition.rag.sparse import SparseProvider
from cognition.rag.store import DENSE_VECTOR, SPARSE_VECTOR, QdrantStore

Doc = Union[str, dict[str, Any]]

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub("", text).lower()


def _dedup_key(text: str) -> str:
    return hashlib.md5(_norm(text).encode("utf-8")).hexdigest()


# stable_ids 的 uuid5 命名空间（固定值，保证跨进程/跨次运行的确定性）。
_STABLE_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def ingest(
    docs: list[Doc],
    kb_id: str,
    *,
    store: QdrantStore,
    embedder: EmbeddingProvider,
    sparse: SparseProvider,
    chunk_size: int = 500,
    overlap: int = 100,
    stable_ids: bool = False,
) -> int:
    """把 docs 切块+向量化+写入 Qdrant，返回写入的块数。

    stable_ids=True（M8 附件 run 前自动入库用）：point id = uuid5(NS, "{kb_id}|{dedup_key}")
    ——**内容寻址幂等**：同附件重发/失败重试/续聊重跑变原地 upsert，不重复入库。
    幂等键必须是内容哈希而非 source_id（上传 key 带随机前缀，同文件两次上传 source_id
    不同）。代价：跨文件完全相同的 chunk 归并为一点（file_name last-write-wins），
    可接受且顺带减少 top-k 重复。默认 False 保持既有随机 id 语义（脚本灌库/测试不变）。
    """
    store.ensure_collection()

    chunks: list[str] = []
    metas: list[dict[str, Any]] = []
    for i, doc in enumerate(docs):
        if isinstance(doc, str):
            text, file_name, source_id = doc, f"doc-{i}", f"doc-{i}"
            dedup_seed = ""
        if not isinstance(doc, str):
            text = str(doc.get("text", ""))
            file_name = str(doc.get("file_name", f"doc-{i}"))
            source_id = str(doc.get("source_id", file_name))
            # 内容不确定的来源（如图片 OCR：同图每次转写文本会变）显式给稳定幂等种子
            # （由调用方按源字节内容寻址），否则用 chunk 文本哈希——见 point_id 计算处。
            dedup_seed = str(doc.get("dedup_seed", ""))
        for ci, ch in enumerate(split_text(text, size=chunk_size, overlap=overlap)):
            chunks.append(ch)
            metas.append({"file_name": file_name, "source_id": source_id, "chunk_index": ci,
                          "dedup_seed": dedup_seed})

    if not chunks:
        return 0

    dense_vecs = embedder.embed(chunks)
    sparse_vecs = sparse.embed(chunks)

    now = int(time.time())
    points: list[models.PointStruct] = []
    for ch, meta, dv, (s_idx, s_val) in zip(chunks, metas, dense_vecs, sparse_vecs):
        # 有稳定种子（图片 OCR 等内容不确定来源）用 种子:chunk 号；否则用 chunk 文本哈希。
        seed = meta.get("dedup_seed") or ""
        dk = f"{seed}:{meta['chunk_index']}" if seed else _dedup_key(ch)
        point_id = str(uuid.uuid5(_STABLE_NS, f"{kb_id}|{dk}")) if stable_ids else uuid.uuid4().hex
        points.append(
            models.PointStruct(
                id=point_id,
                vector={
                    DENSE_VECTOR: dv,
                    SPARSE_VECTOR: models.SparseVector(indices=s_idx, values=s_val),
                },
                payload={
                    "kb_id": kb_id,
                    "text": ch,
                    "source_id": meta["source_id"],
                    "file_name": meta["file_name"],
                    "chunk_index": meta["chunk_index"],
                    "dedup_key": dk,
                    "chunk_type": "text",
                    "image_url": None,
                    "created": now,
                },
            )
        )
    store.upsert(points)
    return len(points)
