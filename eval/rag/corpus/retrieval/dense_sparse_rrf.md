---
document_id: retrieval_dense_sparse_rrf
title: Qdrant Dense 与 Sparse 混合检索及 RRF
module: retrieval
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/rag/store.py
  - cognition/cognition/rag/retriever.py
  - cognition/tests/test_rag_store_memory.py
  - cognition/tests/test_rag_retriever.py
---

# Qdrant Dense 与 Sparse 混合检索及 RRF

## 业务目标

混合检索同时利用语义相似性和词项匹配，降低仅靠 dense 向量漏掉专有词、编号或精确短语的风险。RRF 以排名而非不可比的原始分数融合两路结果。

## 执行流程

每个子问题分别生成 dense 向量和 sparse 向量。Qdrant 查询对 `dense_vector` 与 `sparse_vector` 各建立一个 Prefetch，再使用原生 `FusionQuery(Fusion.RRF)` 得到统一排名。多个子问题的结果汇总后按 `dedup_key` 去重。

## 关键数据结构

单个 Qdrant collection 配置一个 COSINE dense 命名向量和一个 sparse 命名向量。查询的两路 Prefetch 默认各取 20 条，RRF 最终返回数量由 `rag_top_k` 控制。`RetrievedDoc.score` 初始保存 Qdrant 融合分。

## 失败场景

集合未创建、向量维度与配置不一致或 Qdrant 不可达会导致查询失败。空子问题列表直接返回空结果。多个 query 的 embedding 与 sparse 输出通过 `zip` 配对，provider 返回长度异常可能静默丢失尾部查询。

## 限制与消歧

RRF 是 dense 与 sparse 两路在单个子问题内的秩融合；跨子问题阶段只做并集去重，不再次计算 RRF。内存 Qdrant 契约测试证明查询和隔离行为，但不等于真实中文语料上的 Recall@5 已提升。
