"""上传附件 run 前自动入库（M8 C6）：离线全链路——上传 → owner kb → knowledge_search 命中。

全程 fake embedder + QdrantClient(":memory:")，不触网不烧钱。钉死：
- 文本类判定矩阵与 pdf 降级；
- 入库幂等（同文件重传不翻倍）；非文本跳过；超长截断；kb_id 空拒绝入库；
- 端到端：ingestor 入库后 knowledge_search（config 注入 kb）检索命中带引用。
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from cognition.attachments import build_ingestor, extract_text, is_pdf, is_text_like
from cognition.config import Settings
from cognition.rag.embeddings import FakeEmbedder
from cognition.rag.sparse import FakeSparseEmbedder
from cognition.rag.store import QdrantStore

DIM = 64


def test_text_like_and_pdf_matrix():
    assert is_text_like("text/plain") and is_text_like("text/markdown")
    assert is_text_like("application/json")
    assert is_text_like("", "notes.md") and is_text_like("application/octet-stream", "data.csv")
    assert not is_text_like("image/png", "a.png")
    assert is_pdf("application/pdf") and is_pdf("", "paper.PDF")
    assert not is_pdf("text/plain", "a.txt")


def test_extract_text_paths():
    assert extract_text("你好".encode(), "text/plain") == "你好"
    assert extract_text(b'{"k": 1}', "application/json") == '{"k": 1}'
    assert extract_text(b"\x89PNG", "image/png") is None  # 非文本
    assert extract_text(b"not a real pdf", "application/pdf", "x.pdf") is None  # 损坏 pdf 降级


def _harness():
    store = QdrantStore(QdrantClient(location=":memory:"), "docs", DIM)
    emb, sp = FakeEmbedder(DIM), FakeSparseEmbedder()
    objects = {
        "uploads/u1/s1/aa11-fruit.txt": "苹果是一种水果。火龙果富含花青素。".encode(),
        "uploads/u1/s2/bb22-fruit.txt": "苹果是一种水果。火龙果富含花青素。".encode(),  # 同内容再传
        "uploads/u1/s1/cc33-pic.png": b"\x89PNG-bytes",
    }
    ingestor = build_ingestor(
        Settings(embedding_dimension=DIM),
        downloader=lambda key: objects[key],
        store=store, embedder=emb, sparse=sp,
    )
    return store, emb, sp, ingestor


def _att(key: str, mime: str) -> dict:
    return {"resource_key": key, "file_name": key.rsplit("-", 1)[-1], "mime_type": mime, "size": 1}


def test_ingest_idempotent_and_skips_non_text():
    store, _, _, ingestor = _harness()
    names = ingestor(
        [_att("uploads/u1/s1/aa11-fruit.txt", "text/plain"), _att("uploads/u1/s1/cc33-pic.png", "image/png")],
        "owner:u1",
    )
    assert names == ["fruit.txt"]  # 图片被跳过
    total1 = store._c.count("docs").count  # noqa: SLF001
    assert total1 > 0
    # 同内容文件再次上传（不同 resource_key）→ 内容寻址幂等，点数不变。
    ingestor([_att("uploads/u1/s2/bb22-fruit.txt", "text/plain")], "owner:u1")
    assert store._c.count("docs").count == total1  # noqa: SLF001


def test_empty_kb_refused():
    _, _, _, ingestor = _harness()
    assert ingestor([_att("uploads/u1/s1/aa11-fruit.txt", "text/plain")], "") == []


def test_truncation_over_limit(monkeypatch):
    import cognition.attachments as att_mod

    monkeypatch.setattr(att_mod, "MAX_INGEST_CHARS", 10)
    store = QdrantStore(QdrantClient(location=":memory:"), "docs", DIM)
    ingestor = build_ingestor(
        Settings(embedding_dimension=DIM),
        downloader=lambda key: ("字" * 100).encode(),
        store=store, embedder=FakeEmbedder(DIM), sparse=FakeSparseEmbedder(),
    )
    names = ingestor([_att("uploads/u1/s1/dd44-long.txt", "text/plain")], "owner:u1")
    assert names == ["long.txt"]
    pts, _ = store._c.scroll("docs", limit=10, with_payload=True)  # noqa: SLF001
    assert all("超长截断" in p.payload["text"] or len(p.payload["text"]) < 50 for p in pts)


async def test_uploaded_doc_retrievable_via_knowledge_search():
    """闭环：入库后 knowledge_search（config 注入 owner kb）检索命中、答案带引用。"""
    from types import SimpleNamespace

    from cognition.rag.graph import build_rag_subgraph
    from cognition.rag.reranker import FakeReranker
    from cognition.rag.retriever import Retriever
    from cognition.tools.knowledge_search import build_knowledge_search_tool

    store, emb, sp, ingestor = _harness()
    ingestor([_att("uploads/u1/s1/aa11-fruit.txt", "text/plain")], "owner:u1")

    class _FakeModel:
        def invoke(self, prompt: str):
            if "只回答 YES 或 NO" in prompt:
                return SimpleNamespace(content="YES")
            if "拆解成" in prompt:
                return SimpleNamespace(content="火龙果")
            if "是否足够" in prompt:
                return SimpleNamespace(content='{"is_answer": true, "rewrite_query": ""}')
            return SimpleNamespace(content="火龙果富含花青素〔1〕。")

    settings = Settings(rag_enabled=True, rerank_enabled=True, embedding_dimension=DIM)
    sub = build_rag_subgraph(settings, model=_FakeModel(),
                             retriever=Retriever(store, emb, sp, top_k=5), reranker=FakeReranker())
    tool = build_knowledge_search_tool(sub, settings)
    msg = await tool.ainvoke(
        {"args": {"query": "火龙果有什么营养？", "kb_id": ""},
         "id": "c1", "name": "knowledge_search", "type": "tool_call"},
        config={"metadata": {"kb_id": "owner:u1", "request_id": "r1"}},
    )
    assert "花青素" in msg.content
    assert "来源" in msg.content  # 命中了入库内容（sources 非空才有该注脚）
    # 错误的 kb 检索不到（owner 隔离）。
    msg2 = await tool.ainvoke(
        {"args": {"query": "火龙果有什么营养？", "kb_id": ""},
         "id": "c2", "name": "knowledge_search", "type": "tool_call"},
        config={"metadata": {"kb_id": "owner:别人", "request_id": "r1"}},
    )
    assert "来源" not in msg2.content


async def test_image_gen_run_skips_attachment_auto_ingest():
    """评审#23：生图 run（metadata.image_gen 置位）的附件是生成素材（底图/蒙版）而非
    用户知识——Run 前置入库步整体跳过（不烧 vision OCR、不向知识库堆蒙版垃圾文档）；
    普通 run 的附件入库行为不变（对照组）。"""
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver

    from cognition._genproto import agent_pb2
    from cognition.graphs.react import build_react_graph
    from cognition.providers.fake import MessageDrivenChatModel
    from cognition.server.servicer import CognitionServicer
    from cognition.tools.calculator import calculator

    calls: list[tuple] = []

    def recorder(atts, kb_id):
        calls.append((tuple(a["file_name"] for a in atts), kb_id))
        return [a["file_name"] for a in atts]

    model = MessageDrivenChatModel(decide=lambda messages: AIMessage(content="好的"))
    graph = build_react_graph(model, [calculator], checkpointer=MemorySaver(), max_steps=4)
    servicer = CognitionServicer(react_graph=graph, settings=Settings(), ingest_attachments_fn=recorder)
    att = agent_pb2.Attachment(
        resource_key="uploads/u/g/aa-mask-1.png", file_name="mask-1.png",
        mime_type="image/png", size=8,
    )

    def req(run_id: str, session: str, meta: dict):
        return agent_pb2.RunRequest(
            run_id=run_id, session_id=session, query="用蒙版局部重绘", agent_type="react",
            metadata=meta, attachments=[att],
        )

    # 生图 run：入库预步整体跳过（底图/蒙版不进知识库、不触发 vision 转写）。
    protos = [p async for p in servicer.Run(req("rg", "generate:s1", {"image_gen": "true"}), None)]
    assert protos[-1].finish
    assert calls == []

    # 对照组：普通 run（无 image_gen）照常入库。
    protos2 = [p async for p in servicer.Run(req("rn", "s2", {}), None)]
    assert protos2[-1].finish
    assert len(calls) == 1 and calls[0][0] == ("mask-1.png",)
