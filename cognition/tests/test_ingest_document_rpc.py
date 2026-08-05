"""IngestDocument RPC（UX-1 Files 面板"上传即入库"）。

不变量：kb 恒由服务端从 owner_id 推导（客户端不可指定）；复用 Run 前置入库同一条
管线（内容寻址幂等 → 两种入口不产生重复向量）；失败以 message 上浮不抛 gRPC 错。
"""

from __future__ import annotations

import asyncio

from cognition._genproto import agent_pb2
from cognition.config import Settings
from cognition.server.servicer import CognitionServicer


def _req(owner="u1", key="uploads/u1/s/aa-doc.txt", name="doc.txt"):
    return agent_pb2.IngestDocumentRequest(
        owner_id=owner,
        attachment=agent_pb2.Attachment(
            resource_key=key, file_name=name, mime_type="text/plain", size=6
        ),
    )


def _servicer(ingest_fn):
    return CognitionServicer(react_graph=object(), settings=Settings(), ingest_attachments_fn=ingest_fn)


def test_success_derives_owner_kb_and_passes_normalized_attachment():
    calls = []

    def fn(atts, kb_id):
        calls.append((atts, kb_id))
        return ["doc.txt"]

    resp = asyncio.run(_servicer(fn).IngestDocument(_req(), None))
    assert resp.ok and resp.kb_id == "owner:u1" and resp.message == ""
    atts, kb = calls[0]
    assert kb == "owner:u1"
    assert atts[0]["resource_key"] == "uploads/u1/s/aa-doc.txt" and atts[0]["file_name"] == "doc.txt"


def test_guards():
    # 缺 owner → 拒绝（绝不落入无隔离 kb）。
    resp = asyncio.run(_servicer(lambda a, k: ["x"]).IngestDocument(_req(owner=""), None))
    assert not resp.ok and "owner" in resp.message
    # RAG 未启用（fn None）→ 明确 message。
    resp2 = asyncio.run(_servicer(None).IngestDocument(_req(), None))
    assert not resp2.ok and "RAG" in resp2.message
    # 无可入库文本（如图片）→ ok False 带原因。
    resp3 = asyncio.run(_servicer(lambda a, k: []).IngestDocument(_req(), None))
    assert not resp3.ok and "文本" in resp3.message
    # 管线异常 → message 上浮不抛。
    def boom(a, k):
        raise RuntimeError("qdrant down")

    resp4 = asyncio.run(_servicer(boom).IngestDocument(_req(), None))
    assert not resp4.ok and "入库失败" in resp4.message
