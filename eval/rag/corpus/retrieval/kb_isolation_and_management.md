---
document_id: retrieval_kb_isolation_and_management
title: 用户知识库隔离与管理接口
module: retrieval
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/server/servicer.py
  - cognition/cognition/tools/knowledge_search.py
  - cognition/cognition/rag/store.py
  - control-plane/internal/kb/kb.go
---

# 用户知识库隔离与管理接口

## 业务目标

上传文档按用户隔离并可跨该用户的不同会话检索，同时防止模型通过伪造 kb ID 查询或删除其他用户数据。管理面可列出和删除知识库文档。

## 执行流程

servicer 优先从 Go 注入的 owner ID 推导 `owner:{owner}`，无 owner 才回退 `sess:{session}`，且永不返回空 ID。该值通过 RunnableConfig metadata 注入工具，优先级高于模型填写的 `kb_id` 参数。Qdrant dense 与 sparse 两路 Prefetch 都应用相同 kb filter。

## 关键数据结构

Go 的 Qdrant 管理客户端用 scroll 拉取指定 kb 的 payload，并按 source ID 聚合成文件、chunk 数和创建时间。删除请求同时过滤 kb ID 与 source ID。上传来源可生成对象下载 URL，脚本灌库来源可能没有下载对象。

## 失败场景

Qdrant collection 尚未创建时列表返回空而不是故障；其他 HTTP 错误上浮。删除不存在的 collection 视为成功。若知识搜索没有服务端 metadata，只有离线脚本直调才会使用调用参数中的 kb ID。

## 限制与消歧

删除知识库文档只删除 Qdrant 向量点，不删除 MinIO 上传对象，以保留历史附件展开和下载。kb 隔离是应用层 filter，不是 Qdrant collection-per-user。空 kb ID 在检索层意味着全库，因此 servicer 的非空回退是安全要求。
