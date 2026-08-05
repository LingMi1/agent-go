"""Agentic RAG 子系统：检索路由→子问题扩展→混合检索(Qdrant dense+sparse RRF)→多轮反思→rerank→带引用生成。

编译成一个 LangGraph 子图，经 `knowledge_search` 工具暴露给外层 ReAct/Plan-Execute（proto/Go 零改）。
纯逻辑（chunking/expand/reflect/fusion/rerank/citation）与 I/O（embeddings/sparse/reranker/store）严格分层，
I/O 一律经 provider 抽象、测试注入确定性 Fake。
"""
