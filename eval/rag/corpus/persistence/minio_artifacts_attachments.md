---
document_id: persistence_minio_artifacts_attachments
title: MinIO 上传对象与运行产物
module: persistence
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - control-plane/internal/artifact/store.go
  - control-plane/internal/api/upload.go
  - cognition/cognition/tools/report.py
  - cognition/cognition/attachments.py
---

# MinIO 上传对象与运行产物

## 业务目标

大文件字节不穿过 gRPC 事件流，而以对象 key 在双平面之间引用。用户上传、报告、图片和脚本产物统一落 MinIO，再由 Go 代理鉴权下载。

## 执行流程

`POST /uploads` 校验大小、MIME/扩展名和文件名，写入 `uploads/{owner}/{session}/{uuid8}-{file}`。RunRequest 只携带 Attachment 引用，认知面按 key 下载并做多模态展开或知识库入库。工具产物使用 `{run}/{tool_call}/{file}` key，Event 中返回 ArtifactRef。

## 关键数据结构

Attachment 只有 resource key、文件名、MIME 和大小；ArtifactRef 另含预览/下载 URL 与 missing 标记。默认单上传上限 20 MiB，图片进模型另有约 4.5 MiB 闸。控制面 MinIO Store提供 Put、Open 和 EnsureBucket。

## 失败场景

超限、类型不支持或附件 key 不属于 owner 会在 Go 层拒绝。MinIO 桶不可用时控制面告警，纯对话仍可运行，但上传和下载失败。认知面下载失败会把图片降级为文本占位，附件入库失败不阻断 run。

## 限制与消歧

对象存储保存原始字节，PostgreSQL 只保存事件中的引用和运行元数据，Qdrant 保存解析后的向量点。删除知识库文档不删除对象。`minio_upload_enabled=false` 时某些工具仍会构造 ArtifactRef，但实际对象可能不存在。
