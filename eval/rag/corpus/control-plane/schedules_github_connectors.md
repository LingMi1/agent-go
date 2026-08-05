---
document_id: control_plane_schedules_github_connectors
title: 定时任务与 GitHub Proactive 连接器
module: control-plane
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - control-plane/internal/scheduler/scheduler.go
  - control-plane/internal/poller/poller.go
  - control-plane/internal/connector/github.go
  - control-plane/internal/store/migrations/0009_connectors.sql
---

# 定时任务与 GitHub Proactive 连接器

## 业务目标

除用户主动提问外，控制面可按时间或 GitHub 外部事件启动同一套 Agent run，并复用现有准入、账本、checkpoint 和 HITL 基础设施。

## 执行流程

scheduler 周期扫描到期 schedule，原子推进 next_run_at，并以固定 session 启动 headless run。可选 poller 在启用且主密钥有效时轮询 GitHub notifications，解密 PAT、推进 cursor，把匹配 trigger 的事件渲染为 query 后调用 Dispatcher。两者都受并发槽、重叠和超时保护。

## 关键数据结构

schedules 保存 owner、固定 session、query、agent type、间隔、下次执行时间和 last run。connectors 保存 kind、AES-GCM 密文 PAT、cursor、轮询间隔和状态；triggers 保存事件类型、repo/label filter、query 模板、agent type 与 needs approval。

## 失败场景

调度认领冲突、同任务重叠、并发槽不足或系统 busy 会跳拍并计入指标。主密钥缺失或非法时连接器 API 降级 503、poller 不启动。GitHub 请求、解密或模板处理失败会记录错误并等待后续轮询。

## 限制与消歧

当前外部连接器只有 GitHub PAT 轮询，不是 webhook，也没有 Gmail、飞书或 OAuth。`needs_approval` 只能引导已配置的 ReAct 工具审批，不能让 Plan 模式自动获得 HITL。NATS 未参与触发链，主动事件直接由 Go poller 调用 Dispatcher。
