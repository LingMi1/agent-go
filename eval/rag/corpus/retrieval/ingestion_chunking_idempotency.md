---
document_id: retrieval_ingestion_chunking_idempotency
title: 文档分块、向量化与幂等入库
module: retrieval
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/rag/ingest.py
  - cognition/cognition/rag/chunking.py
  - cognition/cognition/attachments.py
  - cognition/tests/test_rag_ingest_idempotent.py
---

# 文档分块、向量化与幂等入库

## 业务目标

上传或脚本语料需要转换为可检索的块，并在重试、续聊或重复上传时避免无限产生重复向量点。附件解析和索引在 Python 认知面完成，Go 不参与 embedding。

## 执行流程

文本先按默认 500 字符、100 字符 overlap 分块，再批量生成 dense 与 sparse 向量并 upsert Qdrant。普通脚本灌库默认使用随机 UUID；附件自动入库使用 `stable_ids=True`，以 `uuid5(namespace, kb_id|dedup_key)` 生成点 ID。同内容重试会覆盖原点。

## 关键数据结构

payload 保存 kb ID、chunk 文本、source ID、文件名、chunk index、dedup key、chunk type、image URL 和创建时间。普通文本 dedup key 是去空白小写后的 MD5；OCR 等不稳定文本可传入基于源字节的稳定 seed，再附加 chunk index。

## 失败场景

空文档或无法提取文本时返回 0。附件下载、解析或 OCR 失败在上层通常被记录并跳过，不阻断对话 run。向量 provider 或 Qdrant upsert 失败会使该次入库失败。相同文本出现在不同 kb 时因 ID 包含 kb ID 而保持隔离。

## 限制与消歧

内容寻址会把不同文件中完全相同的 chunk 合并为一个点，后写文件名覆盖先前 payload。文件删除按 source ID 删除向量，但不会删除 MinIO 原对象。扫描 PDF OCR 依赖 vision 模型和密钥，不是纯本地 OCR。
