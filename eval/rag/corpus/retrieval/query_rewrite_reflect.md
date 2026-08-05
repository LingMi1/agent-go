---
document_id: retrieval_query_rewrite_reflect
title: 子问题扩展、Reflect 与重检索
module: retrieval
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/rag/graph.py
  - cognition/cognition/rag/expand.py
  - cognition/cognition/rag/reflect.py
  - cognition/tests/test_rag_reflect.py
---

# 子问题扩展、Reflect 与重检索

## 业务目标

复杂问题可能包含多个事实需求，也可能因首轮措辞不佳而召回不足。系统先把查询拆成多个子问题，再让模型审视累计证据并决定是否用新措辞重检索。

## 执行流程

expand 节点对 `current_query` 或原始 query 调用扩展提示词，解析去编号、去重且有数量上限的子问题。retrieve 后，reflect 用原始用户问题和累计 evidence 要求模型输出 `is_answer` 与 `rewrite_query` JSON。若不足，rewrite 写入 `current_query`，下一轮 expand 基于它再次拆解和检索。

## 关键数据结构

`loop` 每次 reflect 加一；`current_query` 只作为重检索入口，最终 rerank 和答案生成仍以原始 query 为准。反思解析器从文本中抽取 JSON，缺字段或非法 JSON默认返回不足且无 rewrite。

## 失败场景

非法反思输出会保持当前查询继续循环，直到上限；这避免无限循环，但可能重复同一检索。扩展输出为空时回退到当前查询。`rag_reflection_limit=0` 时首轮 reflect 后立即停止。

## 限制与消歧

query expansion 是每轮检索前生成多个子问题；query rewrite 是 Reflect 判断证据不足后改变下一轮检索意图。Reflect 不会直接修改已生成的最终答案，因为答案只在停止后生成；所谓“答案修正”在当前实现中是补证据后重新生成，而非对已有回答做编辑 diff。
