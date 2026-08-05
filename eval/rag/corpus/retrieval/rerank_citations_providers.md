---
document_id: retrieval_rerank_citations_providers
title: Rerank、引用生成与检索 Provider
module: retrieval
version: "1.0"
source_revision: c04ce65
status: partial
source_files:
  - cognition/cognition/rag/reranker.py
  - cognition/cognition/rag/rerank.py
  - cognition/cognition/rag/citation.py
  - cognition/cognition/rag/factory.py
---

# Rerank、引用生成与检索 Provider

## 业务目标

RRF 后的候选可用独立相关性模型精排，并通过阈值和 top-k 控制进入上下文的证据。引用层把候选编号为 `〔n〕`，使答案和来源产物能关联同一证据顺序。

## 执行流程

`rerank_enabled=true` 时，reranker 对原始 query 与候选文本打分，纯逻辑层按分数降序、阈值过滤并截取 `rag_rerank_top_k`；关闭时直接取 RRF 结果前 k 条。生成提示词接收编号 context，工具可把 query、答案和来源写成 Markdown 产物。

## 关键数据结构

Provider factory 支持 fake、fastembed 和 SiliconFlow/OpenAI-compatible 路径。Fake reranker 使用 query/doc token 集合的 Jaccard 近似；API reranker读取返回的 index 和 relevance score。rerank 后 `RetrievedDoc.score` 被新分数覆盖。

## 失败场景

API 缺 key、超时、非 2xx 或响应形状异常会抛错。打分数量少于文档数时，纯逻辑只处理可配对部分。阈值过高可能清空 sources，此时生成节点回退直接回答，不能保证答案来自知识库。

## 限制与消歧

fake provider 只用于离线确定性测试，不能作为真实质量结论。fastembed 与 SiliconFlow 路径有实现，但仓库没有已提交的真实相关性评测结果；Recall@5、MRR 或 rerank 提升均为 unknown。引用格式由提示词约束，代码没有逐句验证模型确实引用了每个论断。
