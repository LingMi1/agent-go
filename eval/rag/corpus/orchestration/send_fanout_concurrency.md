---
document_id: orchestration_send_fanout_concurrency
title: Send 子任务扇出与有界并发
module: orchestration
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/graphs/plan_execute.py
  - cognition/tests/test_plan_routing.py
  - cognition/cognition/config.py
---

# Send 子任务扇出与有界并发

## 业务目标

一个计划步骤可包含互不依赖的子任务。系统使用 LangGraph `Send` 并行分发这些任务，同时限制实际执行宽度、隔离单分支故障，并保证合并结果不受到达顺序影响。

## 执行流程

planner 将当前步骤按字面量 `<sep>` 切分，路由函数为每段构造一个 `Send("executor", state)`，branch ID 依次为 `b0`、`b1`。executor 在 event loop 对应的 `asyncio.Semaphore(max_parallel)` 中运行 ReAct 子图，并通过 `asyncio.wait_for` 应用单分支超时。全部分支结果经 reducer 回到 planner。

## 关键数据结构

`SubResult` 包含 request ID、round、branch ID、task、result、observations 和 status。`merge_sub_results` 以 `(request_id, round, branch_id)` 去重并规范排序，设计为可交换、可结合。子图 thread ID 包含 run、branch 和 round，tool-call 事件 metadata 也带 branch ID。

## 失败场景

单分支超时产生 `status=error` 和明确超时文本；其他异常也被转换为 error 结果，不直接取消兄弟分支。父状态归约优先级为 ERROR、IDLE、FINISHED；出现 ERROR 后 planner 路由到 summary 终止任务。

## 限制与消歧

信号量限制的是 executor 同时运行数量，不限制 `Send` 对象的生成数量，也不是 Go 控制面的请求级并发上限。默认 `max_parallel_tasks=2`、分支超时 300 秒。测试证明并发峰值不超过上限，但仓库没有 20 组耗时基准，因此不能声称具体百分比提速。
