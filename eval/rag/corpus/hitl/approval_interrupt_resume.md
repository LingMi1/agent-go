---
document_id: hitl_approval_interrupt_resume
title: HITL 审批中断与恢复执行
module: hitl
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/approval.py
  - cognition/cognition/server/servicer.py
  - control-plane/internal/api/api.go
  - cognition/tests/test_approval.py
---

# HITL 审批中断与恢复执行

## 业务目标

配置为高风险的工具在产生副作用前暂停，让用户查看工具和参数后批准或拒绝。审批被设计为 run 边界，因此浏览器断开后仍可通过新的 run 提交决议并继续原图。

## 执行流程

服务装配时按工具名包装 ReAct 工具。gate 在调用原工具前执行 `interrupt`，载荷含 approval ID、工具名、参数预览和原因。流结束但没有 result 时，servicer 从 checkpoint 查 pending interrupt，发 approval request 和挂起 result。Go `POST /runs/{id}/approvals` 校验 owner 后，以新 run 的 metadata 提交决议；Python 用 `Command(resume=字符串)` 续图。

## 关键数据结构

决议字符串为 `approved[:comment]` 或 `rejected[:comment]`，未知值按拒绝。参数预览删除 config/callback 等注入字段并截断长字符串。ApprovalPayload 还携带 pending tool-call IDs，前端据此把原 RUNNING 工具卡显示为待审批。

## 失败场景

伪造、过期或已经失效的 approval ID 会优雅返回“没有待审批”，不会执行工具。拒绝时生成工具 observation，模型可继续收尾。恢复异常会返回错误 result；恢复后再次遇到保护工具可形成下一次审批。

## 限制与消歧

审批恢复继续的是 checkpoint 中的图，不是重放事件。审批请求自身会进入事件账本，决议在新 run 以 info 事件记录。所谓跨会话审批不准确：恢复使用原 session/thread，只是审批决议创建新的 run 边界。
