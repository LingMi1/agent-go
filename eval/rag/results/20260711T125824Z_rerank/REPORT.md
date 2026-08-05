# Rerank 跟进实验报告（20260711T125824Z_rerank）

基线：`20260711T093300Z`（四配置，rerank 关闭=生产默认）。本实验验证开启生产已实现的 rerank（`BAAI/bge-reranker-v2-m3` via SiliconFlow）对检索质量的影响，**未修改任何生产代码**，仅改配置与评测脚本。指标口径、题目集、语料、embedding 与基线完全一致。

## 主结果（threshold=0.0，与 deploy/.env 一致：纯重排不过滤）

| 指标 | C 基线 hybrid_rrf | **C+rerank** | D 基线 agentic | **D+rerank(离线)** |
|---|---|---|---|---|
| Recall@1 | 19.5% | **71.7%** | 17.6% | **71.7%** |
| Recall@3 | 64.2% | **83.0%** | 50.3% | **86.8%** |
| Recall@5 | 77.4% | **84.9%** | 61.0% | **91.2%** |
| Recall@10 | 82.4% | **84.9%** | 76.7% | **94.3%** |
| MRR@10 | 0.426 | **0.772** | 0.366 | **0.793** |
| 多跳 Any Recall@5 | 90.0% | **90.0%** | 70.0% | **96.7%** |
| 多跳 All Recall@5 | 3.3% | **40.0%** | 6.7% | **50.0%** |
| 多跳 All Recall@10 | 40.0% | **40.0%** | 40.0% | **66.7%** |
| 不可回答误检率 | 100.0% | **100.0%** | 100.0% | **100.0%** |
| 平均延迟(s) | 0.004 | **0.937** | 53.040 | **54.158** |
| P95 延迟(s) | 0.005 | **1.106** | 88.258 | **89.380** |

- hybrid_rrf_rerank：3 次运行，结果哈希不一致（均值）；
- full_agentic_rag_rerank_offline：3 次运行，结果哈希不一致（均值）；
- D+rerank 延迟为估算口径：基线子图延迟 + 本次实测 rerank 调用延迟（rerank 单次均值 1.118s）。

**分题型 Recall@5**：

| 题型 | C 基线 | C+rerank | D 基线 | D+rerank |
|---|---|---|---|---|
| disambiguation | 87.5% | 87.5% | 58.3% | 87.5% |
| multi_hop | 90.0% | 90.0% | 70.0% | 96.7% |
| paraphrase | 45.5% | 81.8% | 33.3% | 84.8% |
| single_hop | 77.8% | 77.8% | 68.5% | 90.7% |
| troubleshooting | 100.0% | 100.0% | 77.8% | 100.0% |

## threshold=0.3 消融（config.py 注释建议值：过滤低分证据，观察拒答）

| 指标 | C+rerank t0.3 | D+rerank t0.3 |
|---|---|---|
| Recall@1 | 35.8% | 37.7% |
| Recall@3 | 35.8% | 37.7% |
| Recall@5 | 35.8% | 37.7% |
| Recall@10 | 35.8% | 37.7% |
| MRR@10 | 0.358 | 0.377 |
| 多跳 Any Recall@5 | 30.0% | 30.0% |
| 多跳 All Recall@5 | 0.0% | 0.0% |
| 多跳 All Recall@10 | 0.0% | 0.0% |
| 不可回答误检率 | 16.7% | 16.7% |
| 平均延迟(s) | 0.937 | 54.158 |
| P95 延迟(s) | 1.106 | 89.380 |

## 失败案例（run1，Recall@5 未命中）

### hybrid_rrf_rerank：未命中 8 题

- `q_006`（single_hop）：两种推理模式中，哪一种的高危工具带人工闸门？另一种为什么没有？
  - gold: `hitl_approval_scope_replay_limits::c00`
  - top-5: `architecture_dual_plane_system::c00`, `hitl_approval_interrupt_resume::c00`, `persistence_minio_artifacts_attachments::c01`, `architecture_dual_plane_system::c01`, `tools_unified_registry::c01`
- `q_014`（single_hop）：运行记录表和事件表分别用什么做主键？事件内容用什么类型存储？
  - gold: `persistence_postgres_business_and_events::c01`
  - top-5: `persistence_postgres_business_and_events::c00`, `architecture_event_contract_ledger_replay::c00`, `architecture_dual_plane_system::c01`, `persistence_minio_artifacts_attachments::c00`, `orchestration_react_graph::c01`
- `q_015`（single_hop）：人工做出的批准或拒绝以什么格式传回系统？遇到无法识别的值怎么处理？
  - gold: `hitl_approval_interrupt_resume::c01`
  - top-5: `hitl_approval_interrupt_resume::c00`, `hitl_approval_scope_replay_limits::c00`, `troubleshooting_failure_modes_known_limits::c01`, `architecture_dual_plane_system::c01`, `orchestration_react_graph::c01`

### full_agentic_rag_rerank_offline：未命中 5 题

- `q_001`（single_hop）：一条运行事件要推送给浏览器之前，必须先成功完成什么动作？
  - gold: `architecture_event_contract_ledger_replay::c00`
  - top-5: `architecture_dual_plane_system::c01`, `control_plane_grpc_sse_streaming::c00`, `orchestration_run_admission_cancellation::c00`, `hitl_approval_interrupt_resume::c00`, `architecture_event_contract_ledger_replay::c01`
- `q_006`（single_hop）：两种推理模式中，哪一种的高危工具带人工闸门？另一种为什么没有？
  - gold: `hitl_approval_scope_replay_limits::c00`
  - top-5: `architecture_dual_plane_system::c00`, `persistence_minio_artifacts_attachments::c01`, `orchestration_react_graph::c00`, `orchestration_react_graph::c01`, `tools_code_interpreter_sandbox_boundary::c00`
- `q_024`（paraphrase）：不用人开口、由外部代码托管平台的动静来唤醒智能体，目前支持哪家平台？是对方推送过来的吗？
  - gold: `control_plane_schedules_github_connectors::c00`, `control_plane_schedules_github_connectors::c01`
  - top-5: `architecture_dual_plane_system::c00`, `architecture_event_contract_ledger_replay::c00`, `tools_code_interpreter_sandbox_boundary::c00`, `deployment_docker_config_infrastructure::c01`, `persistence_minio_artifacts_attachments::c01`

## 结论与可安全写入简历的表述（更新版，替代基线报告 §9 中被禁写的条目）

- ✅ “混合检索 + bge-reranker-v2-m3 精排：Recall@5 从 77.4% 提升至 84.9%、MRR@10 从 0.426 提升至 0.772（自建 53 条可回答中文评测集，3 次运行均值）”
- ✅ “Agentic RAG（子查询扩展 + Reflect 重检索 + 精排）：Recall@5 从单轮混合检索的 77.4% 提升至 91.2%（评测集同上；对比未开精排的 agentic 61.0%，精排修复了扩展检索的排序损失）”
- ✅ “多跳 All Recall@5 从 6.7%（未精排 agentic）提升至 50.0%”（10 条多跳题×3 次运行；基线单轮混合检索为 3.3%）
- ✅ “rerank 阈值过滤（0.3）将不可回答问题误检率从 100% 降至 16.7%”（6 条不可回答题，附 threshold 对 Recall 的代价见消融表）

仍然禁写：任何不带“自建 59 题/58 chunk 评测集、3 次运行”限定的绝对数字；“大规模/生产流量验证”。基线报告 §8 的数据集局限全部适用。

