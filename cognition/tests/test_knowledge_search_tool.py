"""knowledge_search 工具形状 + 经 EventMapper 映射为 artifact_refs（坐实 proto/Go 零改）。"""

from __future__ import annotations

from types import SimpleNamespace

from qdrant_client import QdrantClient

from cognition.config import Settings
from cognition.events.mapper import EventMapper
from cognition.events.schema import EventType
from cognition.rag.embeddings import FakeEmbedder
from cognition.rag.graph import build_rag_subgraph
from cognition.rag.ingest import ingest
from cognition.rag.reranker import FakeReranker
from cognition.rag.retriever import Retriever
from cognition.rag.sparse import FakeSparseEmbedder
from cognition.rag.store import QdrantStore
from cognition.tools.knowledge_search import build_knowledge_search_tool

DIM = 64


class _FakeModel:
    def invoke(self, prompt: str):
        if "只回答 YES 或 NO" in prompt:
            return SimpleNamespace(content="YES")
        if "拆解成" in prompt:
            return SimpleNamespace(content="混合检索")
        if "是否足够" in prompt:
            return SimpleNamespace(content='{"is_answer": true, "rewrite_query": ""}')
        return SimpleNamespace(content="混合检索结合稠密与稀疏向量〔1〕。")


def _tool(**settings_kw):
    store = QdrantStore(QdrantClient(location=":memory:"), "docs", DIM)
    emb, sp = FakeEmbedder(DIM), FakeSparseEmbedder()
    ingest([{"text": "混合检索用稠密+稀疏向量并 RRF 融合。", "file_name": "h.md"}],
           kb_id="kb1", store=store, embedder=emb, sparse=sp)
    settings = Settings(rag_enabled=True, rerank_enabled=True, embedding_dimension=DIM, **settings_kw)
    sub = build_rag_subgraph(settings, model=_FakeModel(),
                             retriever=Retriever(store, emb, sp, top_k=5), reranker=FakeReranker())
    return build_knowledge_search_tool(sub, settings)


async def test_tool_silent_by_default_registers_when_enabled():
    """M9 降噪：默认不登记产物（过程≠交付物，一轮 N 次检索会刷屏同名文件）；
    search_artifact_enabled=True 恢复旧行为。"""
    tool = _tool()
    msg = await tool.ainvoke(
        {"args": {"query": "什么是混合检索", "kb_id": "kb1"},
         "id": "call-1", "name": "knowledge_search", "type": "tool_call"}
    )
    assert "混合检索" in msg.content and "来源" in msg.content  # 内联引用与来源计数保留
    assert msg.artifact is None
    assert tool.metadata["provider"] == "local"

    tool_on = _tool(search_artifact_enabled=True)
    msg2 = await tool_on.ainvoke(
        {"args": {"query": "什么是混合检索", "kb_id": "kb1"},
         "id": "call-1", "name": "knowledge_search", "type": "tool_call"}
    )
    art = msg2.artifact
    assert art["file_name"] == "search-results.md"
    assert art["download_url"] == "/artifacts/run/call-1/search-results.md"
    assert art["mime_type"] == "text/markdown"
    assert art["size"] > 0 and art["missing"] is False


async def test_deep_research_registers_with_unique_name():
    """deep_research 例外：检索证据属于研究交付物——登记且按 tcid 唯一命名不刷屏。"""
    tool = _tool()
    msg = await tool.ainvoke(
        {"args": {"query": "什么是混合检索", "kb_id": "kb1"},
         "id": "abcdef99", "name": "knowledge_search", "type": "tool_call"},
        config={"metadata": {"agent_type": "deep_research", "request_id": "r1"}},
    )
    assert msg.artifact is not None
    # 尾 6 位：DeepSeek id 前缀恒 call_0，取头部必撞名（实测）。
    assert msg.artifact["file_name"] == "search-results-cdef99.md"

    # 非研究模式带 config 也保持静默。
    msg2 = await tool.ainvoke(
        {"args": {"query": "什么是混合检索", "kb_id": "kb1"},
         "id": "c3", "name": "knowledge_search", "type": "tool_call"},
        config={"metadata": {"agent_type": "react", "request_id": "r1"}},
    )
    assert msg2.artifact is None


async def test_tool_result_maps_to_artifact_refs():
    tool = _tool(search_artifact_enabled=True)
    msg = await tool.ainvoke(
        {"args": {"query": "什么是混合检索", "kb_id": "kb1"},
         "id": "c9", "name": "knowledge_search", "type": "tool_call"}
    )
    # 喂给 EventMapper._on_tool_end（复用现有 tool_result 通路，无需新事件类型）
    mapper = EventMapper("run-x")
    out = SimpleNamespace(tool_call_id="c9", name="knowledge_search",
                          content=msg.content, status="success", artifact=msg.artifact)
    events = mapper._on_tool_end({"data": {"output": out}}, "")
    tool_results = [e for e in events if e.type is EventType.TOOL_RESULT]
    assert tool_results
    refs = tool_results[0].tool_result.artifact_refs
    assert refs and refs[0].file_name == "search-results.md"
    assert tool_results[0].tool_result.tool_provider == "local"


# —— M8：kb 归属线程化（config 优先，防 LLM 注入/幻觉跨 owner 检索）——
class _SpySubgraph:
    """记录 ainvoke 入参的假子图（直测 kb 解析，不跑 RAG 机器）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ainvoke(self, state: dict) -> dict:
        self.calls.append(dict(state))
        return {"answer": "ok", "sources": []}


async def test_kb_id_config_priority_over_llm_arg():
    """config metadata 的 kb_id 必须压过 LLM 填的入参——kb_id 是模型可见参数，
    被提示注入/幻觉填成别人的 kb 也不能越权检索。"""
    spy = _SpySubgraph()
    tool = build_knowledge_search_tool(spy, Settings(embedding_dimension=DIM))
    await tool.ainvoke(
        {"args": {"query": "q", "kb_id": "owner:别人"},
         "id": "c1", "name": "knowledge_search", "type": "tool_call"},
        config={"metadata": {"kb_id": "owner:我"}},
    )
    assert spy.calls[0]["kb_id"] == "owner:我"


async def test_kb_id_falls_back_to_arg_without_config():
    spy = _SpySubgraph()
    tool = build_knowledge_search_tool(spy, Settings(embedding_dimension=DIM))
    await tool.ainvoke(
        {"args": {"query": "q", "kb_id": "kb-manual"},
         "id": "c2", "name": "knowledge_search", "type": "tool_call"}
    )
    assert spy.calls[0]["kb_id"] == "kb-manual"


def test_resolve_kb_id():
    from cognition.server.servicer import resolve_kb_id

    req = SimpleNamespace(metadata={"owner_id": "u1"}, session_id="s1", run_id="r1")
    assert resolve_kb_id(req) == "owner:u1"
    # 无 owner（旧 Go/直连 gRPC）→ 回退会话级，绝不返回 ""（空串=全库无隔离）。
    req2 = SimpleNamespace(metadata={}, session_id="s1", run_id="r1")
    assert resolve_kb_id(req2) == "sess:s1"
    req3 = SimpleNamespace(metadata=None, session_id="", run_id="r9")
    assert resolve_kb_id(req3) == "sess:r9"
