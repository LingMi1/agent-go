"""Agentic RAG 子图端到端（fake LLM + Fake providers + :memory:，全离线确定性）。

验证：route 简单问题走直答；复杂问题走完整链并带〔n〕引用；反思循环在 reflection_limit 停。
"""

from __future__ import annotations

from types import SimpleNamespace

from qdrant_client import QdrantClient

from cognition.config import Settings
from cognition.rag.embeddings import FakeEmbedder
from cognition.rag.graph import build_rag_subgraph
from cognition.rag.ingest import ingest
from cognition.rag.reranker import FakeReranker
from cognition.rag.retriever import Retriever
from cognition.rag.sparse import FakeSparseEmbedder
from cognition.rag.store import QdrantStore

DIM = 64
_GREETINGS = ("你好", "谢谢", "hello", "hi")


class FakeChatModel:
    """按提示词关键字派生响应的确定性 fake。reflect_answer 控制反思是否判"足够"。"""

    def __init__(self, reflect_answer: bool = True) -> None:
        self.reflect_answer = reflect_answer
        self.calls: list[str] = []

    def invoke(self, prompt: str):
        self.calls.append(prompt)
        if "只回答 YES 或 NO" in prompt:  # route
            simple = any(g in prompt for g in _GREETINGS)
            return SimpleNamespace(content="NO" if simple else "YES")
        if "拆解成" in prompt:  # expand
            return SimpleNamespace(content="混合检索是什么\nRRF 是什么")
        if "是否足够" in prompt:  # reflect
            body = '{"is_answer": true, "rewrite_query": ""}' if self.reflect_answer else '{"is_answer": false, "rewrite_query": "混合检索 融合"}'
            return SimpleNamespace(content=body)
        if "直接" in prompt:  # direct（simple 路径）
            return SimpleNamespace(content="你好！有什么可以帮你？")
        # answer
        return SimpleNamespace(content="混合检索结合稠密与稀疏向量并用 RRF 融合〔1〕。")


def _retriever():
    store = QdrantStore(QdrantClient(location=":memory:"), "docs", DIM)
    emb, sp = FakeEmbedder(DIM), FakeSparseEmbedder()
    ingest(
        [
            {"text": "混合检索同时用稠密向量与稀疏向量，再用 RRF 融合两路排序。", "file_name": "hybrid.md"},
            {"text": "RRF 按文档在各路结果中的排名倒数累加分数，实现稳健秩融合。", "file_name": "rrf.md"},
            {"text": "今天的天气和烹饪食谱与检索无关。", "file_name": "noise.md"},
        ],
        kb_id="kb1", store=store, embedder=emb, sparse=sp,
    )
    return Retriever(store, emb, sp, top_k=5)


def _settings():
    return Settings(rag_enabled=True, rerank_enabled=True, rerank_threshold=0.0,
                    rag_reflection_limit=2, rag_subquery_max=2, embedding_dimension=DIM)


def test_complex_query_full_chain_with_citation():
    g = build_rag_subgraph(_settings(), model=FakeChatModel(), retriever=_retriever(), reranker=FakeReranker())
    out = g.invoke({"query": "什么是混合检索和 RRF", "kb_id": "kb1"})
    assert out.get("is_simple") is False
    assert "〔1〕" in out["answer"]           # 带引用生成
    assert out["sources"]                     # 有来源
    assert out["sources"][0]["file_name"] in {"hybrid.md", "rrf.md"}  # 相关文档排在最前


def test_simple_query_direct_answer():
    g = build_rag_subgraph(_settings(), model=FakeChatModel(), retriever=_retriever(), reranker=FakeReranker())
    out = g.invoke({"query": "你好", "kb_id": "kb1"})
    assert out.get("is_simple") is True
    assert out["sources"] == []               # 直答不检索
    assert "你好" in out["answer"]


def test_reflection_loops_until_limit():
    model = FakeChatModel(reflect_answer=False)  # 永不判"足够"
    g = build_rag_subgraph(_settings(), model=model, retriever=_retriever(), reranker=FakeReranker())
    out = g.invoke({"query": "什么是混合检索", "kb_id": "kb1"})
    assert out["loop"] == 2                    # 在 reflection_limit 停
    assert out["answer"]                       # 仍产出答案
