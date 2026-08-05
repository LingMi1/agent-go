---
document_id: retrieval_agentic_rag_graph
title: Agentic RAG 子图拓扑
module: retrieval
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/rag/graph.py
  - cognition/cognition/rag/types.py
  - cognition/tests/test_rag_graph_fake.py
---

# Agentic RAG 子图拓扑

## 业务目标

知识检索被封装为可独立调用的 LangGraph 子图，使外层 ReAct 或 Plan executor 能把它当作一个普通工具，同时在子图内部自主判断是否需要检索、是否需要补充证据以及何时生成答案。

## 执行流程

完整拓扑为 `route -> expand -> hybrid_retrieve -> reflect -> rerank -> generate`。route 判定简单问题时直接进入 generate；复杂问题先扩展子问题并做混合检索。Reflect 判断证据不足时改写当前查询并回到 expand，证据足够或达到循环上限后进入 rerank 和带引用生成。

## 关键数据结构

`RagState` 保存 query、kb ID、简单问题标记、subquestions、loop、累计 docs、reranked、answer 和 sources。检索轮次之间的 docs 会先与新结果合并去重，不会每轮清空。子图在服务装配期编译一次。

## 失败场景

无检索结果时 generate 走 direct prompt 并返回空 sources。route 模型输出未包含 `YES` 会被判为简单问题。模型、embedding、Qdrant 或 reranker 的 I/O 异常没有在子图节点内统一降级，会向外层工具错误处理路径传播。

## 限制与消歧

RAG 子图内部事件不映射到跨面 proto，外层只看到一次 `knowledge_search` 的 tool call/result。该图不是 `deep_research` 编排图；后者只是 Plan-Execute 变体，可在 executor 中多次调用本工具。
