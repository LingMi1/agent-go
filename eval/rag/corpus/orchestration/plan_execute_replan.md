---
document_id: orchestration_plan_execute_replan
title: Plan-Execute 计划生命周期与动态重规划
module: orchestration
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/graphs/plan_execute.py
  - cognition/cognition/graphs/plan_lifecycle.py
  - cognition/tests/test_plan_lifecycle.py
  - cognition/tests/test_plan_continuation.py
---

# Plan-Execute 计划生命周期与动态重规划

## 业务目标

Plan-Execute 面向需要拆解和多轮推进的复杂任务。规划模型负责结构化计划，executor 复用 ReAct 子图执行当前步骤，主图根据结果推进、更新剩余步骤或收尾。

## 执行流程

图从 SOP recall 进入 planner。首次规划优先解析 `planning` 工具调用，也可从模型正文 JSON 兜底；若均失败，则用原始 query 建单步计划。executor 结果回到 planner 后，确定性逻辑写入当前步骤 note 并标记完成。只有模型显式给出 `command=update`、非空 steps 且计划未完成时，才替换未完成后缀形成动态 replan。

## 关键数据结构

`Plan` 使用并行索引的 `steps`、`step_status`、`notes`，状态包含 `not_started`、`in_progress`、`completed`、`blocked`。生命周期函数均返回深拷贝，不原地修改。`PlanExecuteState` 还含 round、reduced_state、planner_messages 和由 reducer 管理的 sub_results。

## 失败场景

非法或缺失的 current step 会由 `ensure_executable` 尝试修复，无法修复则报错。外层 round 超过配置、计划完成或上一轮归约为 ERROR 时进入 summary。summary 会带出本 run 的具体分支失败原因，避免只返回泛化错误。

## 限制与消歧

`deep_research` 与 `plan_solve` 共用同一拓扑和 executor，只是规划提示词与最大轮次不同。它不是独立 research 图。计划是按顺序推进的步骤列表；只有单个当前步骤内部用 `<sep>` 表示可并行子任务。
