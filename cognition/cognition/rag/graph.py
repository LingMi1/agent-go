"""Agentic RAG 子图（编排）：route→expand→hybrid_retrieve→reflect(loop)→rerank→generate。

编译成 CompiledStateGraph，经 knowledge_search 工具 `ainvoke` 调用；其内部 LLM 调用**不**外泄事件
（外层只见一次 tool_call/tool_result）。provider 与 model 可注入，便于用 fake + `:memory:` 端到端测试。
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from cognition.rag import citation, expand as expand_mod, prompts, reflect as reflect_mod
from cognition.rag.rerank import order_by_score
from cognition.rag.retriever import Retriever
from cognition.rag.types import RagState


def _text(model: Any, prompt: str) -> str:
    """调模型取纯文本（兼容 langchain ChatModel 与 fake 的 .invoke）。"""
    resp = model.invoke(prompt)
    return resp.content if hasattr(resp, "content") else str(resp)


def build_rag_subgraph(
    settings: Any,
    *,
    model: Any = None,
    retriever: Optional[Retriever] = None,
    reranker: Any = None,
    reflection_limit: Optional[int] = None,
    subquery_max: Optional[int] = None,
    top_k: Optional[int] = None,
    rerank_top_k: Optional[int] = None,
    rerank_threshold: Optional[float] = None,
):
    """编译 RAG 子图。缺省从 settings + factory 构建 I/O（生产）；测试注入 fake。"""
    if model is None:
        if getattr(settings, "fake_model", False):
            from cognition.providers.fake import build_fake_rag_model

            model = build_fake_rag_model()
        else:
            from cognition.providers.router import select_model

            model = select_model("summary", settings=settings)
    if retriever is None:
        from cognition.rag.factory import build_embedder, build_sparse, build_store

        retriever = Retriever(
            build_store(settings), build_embedder(settings), build_sparse(settings),
            top_k=int(getattr(settings, "rag_top_k", 10)),
        )
    if reranker is None:
        from cognition.rag.factory import build_reranker

        reranker = build_reranker(settings)

    limit = reflection_limit if reflection_limit is not None else int(getattr(settings, "rag_reflection_limit", 2))
    sub_max = subquery_max if subquery_max is not None else int(getattr(settings, "rag_subquery_max", 3))
    k = top_k if top_k is not None else int(getattr(settings, "rag_top_k", 10))
    rk = rerank_top_k if rerank_top_k is not None else int(getattr(settings, "rag_rerank_top_k", 5))
    rerank_on = bool(getattr(settings, "rerank_enabled", False))
    threshold = rerank_threshold if rerank_threshold is not None else float(getattr(settings, "rerank_threshold", 0.0))

    def route(state: RagState) -> dict:
        resp = _text(model, prompts.ROUTE_PROMPT.format(query=state["query"]))
        is_simple = "YES" not in resp.upper()
        return {"is_simple": is_simple, "loop": 0, "kb_id": state.get("kb_id", ""), "docs": []}

    def expand(state: RagState) -> dict:
        cur = state.get("current_query") or state["query"]
        raw = _text(model, prompts.EXPAND_PROMPT.format(query=cur, max=sub_max))
        subs = expand_mod.parse_subquestions(raw, limit=sub_max) or [cur]
        return {"subquestions": subs}

    def hybrid_retrieve(state: RagState) -> dict:
        new = retriever.retrieve(state.get("subquestions", []), kb_id=state.get("kb_id", ""), top_k=k)
        from cognition.rag.fusion import dedup_docs

        merged = dedup_docs(list(state.get("docs", [])) + new)
        return {"docs": merged}

    def reflect(state: RagState) -> dict:
        evidence = citation.build_ref_context(state.get("docs", []))
        raw = _text(model, prompts.REFLECT_PROMPT.format(query=state["query"], evidence=evidence))
        is_answer, rewrite = reflect_mod.parse_reflection(raw)
        return {
            "loop": int(state.get("loop", 0)) + 1,
            "is_answer": is_answer,
            "current_query": rewrite or state.get("current_query") or state["query"],
        }

    def rerank_node(state: RagState) -> dict:
        docs = list(state.get("docs", []))
        if not docs:
            return {"reranked": [], "sources": []}
        if rerank_on:
            scores = reranker.score(state["query"], [d.get("text", "") for d in docs])
            ranked = order_by_score(docs, scores, threshold=threshold, top_k=rk)
        else:
            ranked = docs[:rk]
        return {"reranked": ranked, "sources": ranked}

    def generate(state: RagState) -> dict:
        docs = state.get("reranked", [])
        if state.get("is_simple") or not docs:
            answer = _text(model, prompts.DIRECT_PROMPT.format(query=state["query"]))
            return {"answer": answer, "sources": []}
        context = citation.build_ref_context(docs)
        answer = _text(model, prompts.ANSWER_PROMPT.format(context=context, query=state["query"]))
        return {"answer": answer, "sources": docs}

    def after_route(state: RagState) -> str:
        return "generate" if state.get("is_simple") else "expand"

    def after_reflect(state: RagState) -> str:
        if reflect_mod.should_stop(int(state.get("loop", 0)), limit, bool(state.get("is_answer"))):
            return "rerank"
        return "expand"

    g = StateGraph(RagState)
    g.add_node("route", route)
    g.add_node("expand", expand)
    g.add_node("hybrid_retrieve", hybrid_retrieve)
    g.add_node("reflect", reflect)
    g.add_node("rerank", rerank_node)
    g.add_node("generate", generate)

    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, {"expand": "expand", "generate": "generate"})
    g.add_edge("expand", "hybrid_retrieve")
    g.add_edge("hybrid_retrieve", "reflect")
    g.add_conditional_edges("reflect", after_reflect, {"expand": "expand", "rerank": "rerank"})
    g.add_edge("rerank", "generate")
    g.add_edge("generate", END)
    return g.compile()
