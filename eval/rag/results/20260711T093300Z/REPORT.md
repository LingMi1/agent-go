# Pro-Agent RAG 检索评测报告（20260711T093300Z）

所有指标由 `scripts/eval_metrics.py`（含单测）计算、本报告由 `scripts/render_report.py` 从 `metrics.json` 程序化渲染；逐题原始结果见 `per_question.jsonl`，失败清单见 `failures.jsonl`，完整运行环境见 `config.json`。

## 1. 语料规模与有效问题数

- 语料：58 个固定 chunk（29 篇结构化中文文档，构建于 `chunks.jsonl`，sha256 前缀 `187ae20a1f459b78`），一次性入库独立 collection `eval_rag_bench_v1`（kb_id=`eval_rag_v1`），四配置共用同一份索引；
- 问题：`questions_verified.jsonl`（sha256 前缀 `ea6434add4ca7869`）共 59 题 = 可回答 53 + 不可回答 6；四配置使用完全相同的题目集合，无逐配置删题；
- 题型分布：disambiguation=8、multi_hop=10、paraphrase=11、single_hop=18、troubleshooting=6、unanswerable=6。

## 2. 独立审查：被拒与修正

评测集经独立审查（见 `eval/rag/review_report.md`）：原始 60 题中拒绝 1 题、修正 15 题（补标漏标的等价证据 chunk、为多跳题引入 gold_groups）。被拒题目：
- **q_023**（paraphrase）：题面问“那段代码的运行环境能联网吗？能随便写文件吗？”，但语料明确存在 local/Docker 两条执行路径且默认 skill_runner=local（非沙箱）；标注答案只描述 Docker 模式，答案不唯一且标签在默认配置下不成立。审查选择拒绝而非改写题面，避免审查员改题引入偏置。

## 3. 指标定义

- **单跳/同义改写/消歧/故障排查**：Recall@k = top-k 是否包含任一 gold chunk；
- **多跳 Any Recall@k**：至少一个证据组被命中；**多跳 All Recall@k**：全部证据组均被命中（组内任一等价证据 chunk 命中即该组命中；组间为回答所需的不同必要事实）；
- 汇总 Recall@k 对全部可回答题按“gold 并集任一命中”口径计算（多跳题该口径等价于 Any）；
- **MRR@10**：gold 并集首个命中名次的倒数（>10 或未命中计 0）；
- **不可回答误检率**：不可回答题中最终证据集非空的比例（A/B/C 为 top-5 非空——纯检索器无拒答机制，恒为 100%，列出仅作对照；D 为进入生成的 sources 非空）；
- 延迟：A/B/C 为 embed+查询端到端；D 为整个子图（含 LLM 调用），另附去除答案生成后的检索侧延迟。

## 4. 各配置结果

确定性验证：dense_only：3 次运行结果哈希完全一致（确定性成立，表中为单次值）；sparse_only：3 次运行结果哈希完全一致（确定性成立，表中为单次值）；hybrid_rrf：3 次运行结果哈希不一致（确定性不成立，表中为均值）。full_agentic_rag 含 LLM（temperature=0 仍非确定），运行 3 次取均值±标准差。

| 指标 | A. dense_only | B. sparse_only | C. hybrid_rrf | D. full_agentic_rag |
|---|---|---|---|---|
| Recall@1（可回答 53 题） | 43.4% | 9.4% | 19.5% | 17.6% ±4.4 |
| Recall@3（可回答 53 题） | 69.8% | 22.6% | 64.2% | 50.3% ±6.1 |
| Recall@5（可回答 53 题） | 77.4% | 30.2% | 77.4% | 61.0% ±8.9 |
| Recall@10（可回答 53 题） | 79.2% | 37.7% | 82.4% | 76.7% ±2.9 |
| MRR@10 | 0.577 | 0.178 | 0.426 | 0.366 ±0.045 |
| 多跳 Any Recall@5 | 100.0% | 50.0% | 90.0% | 70.0% |
| 多跳 All Recall@5 | 30.0% | 10.0% | 3.3% | 6.7% ±5.8 |
| 多跳 All Recall@10 | 60.0% | 20.0% | 40.0% | 40.0% |
| 不可回答误检率 | 100.0% | 100.0% | 100.0% | 100.0% |
| 平均检索延迟 | 0.003 | 0.001 | 0.004 | 53.040 ±10.669 |
| P50 延迟 | 0.003 | 0.001 | 0.004 | 43.410 ±7.235 |
| P95 延迟 | 0.005 | 0.002 | 0.005 | 88.258 ±21.935 |

**full_agentic_rag 行为统计**（3 次运行合计，59 题/次）：

- query 扩展（expand）调用 354 次、Reflect 反思 354 次、其中给出改写查询（rewrite 触发重检索）74 次；
- route 判为简单问题（跳过检索）0 次；LLM 瞬时错误重试 0 次（无丢题）；
- 检索侧延迟（去除答案生成调用）：均值 44.73s；总延迟中其余为 route/expand/reflect/generate 的 LLM 调用耗时。

**分题型 Recall@5（gold 并集任一命中口径，均值）**：

| 题型 | A. dense_only | B. sparse_only | C. hybrid_rrf | D. full_agentic_rag |
|---|---|---|---|---|
| disambiguation | 62.5% | 37.5% | 87.5% | 58.3% |
| multi_hop | 100.0% | 50.0% | 90.0% | 70.0% |
| paraphrase | 63.6% | 18.2% | 45.5% | 33.3% |
| single_hop | 77.8% | 16.7% | 77.8% | 68.5% |
| troubleshooting | 83.3% | 50.0% | 100.0% | 77.8% |

## 5. 失败案例分析

### A. dense_only：Recall@5 未命中 12 题（另有不可回答误检 6 题）

按题型：disambiguation×3、paraphrase×4、single_hop×4、troubleshooting×1。示例：

- `q_006`（single_hop）：两种推理模式中，哪一种的高危工具带人工闸门？另一种为什么没有？
  - gold: `hitl_approval_scope_replay_limits::c00`
  - 实际 top-5: `control_plane_schedules_github_connectors::c01`, `persistence_minio_artifacts_attachments::c01`, `deployment_docker_config_infrastructure::c01`, `control_plane_schedules_github_connectors::c00`, `architecture_dual_plane_system::c00`
- `q_014`（single_hop）：运行记录表和事件表分别用什么做主键？事件内容用什么类型存储？
  - gold: `persistence_postgres_business_and_events::c01`
  - 实际 top-5: `architecture_event_contract_ledger_replay::c00`, `persistence_minio_artifacts_attachments::c00`, `persistence_postgres_business_and_events::c00`, `tools_skill_disclosure_runner::c00`, `tools_unified_registry::c00`
- `q_015`（single_hop）：人工做出的批准或拒绝以什么格式传回系统？遇到无法识别的值怎么处理？
  - gold: `hitl_approval_interrupt_resume::c01`
  - 实际 top-5: `tools_code_interpreter_sandbox_boundary::c01`, `hitl_approval_scope_replay_limits::c00`, `retrieval_kb_isolation_and_management::c00`, `hitl_approval_interrupt_resume::c00`, `control_plane_http_api_auth_rbac::c00`

### B. sparse_only：Recall@5 未命中 37 题（另有不可回答误检 6 题）

按题型：disambiguation×5、multi_hop×5、paraphrase×9、single_hop×15、troubleshooting×3。示例：

- `q_001`（single_hop）：一条运行事件要推送给浏览器之前，必须先成功完成什么动作？
  - gold: `architecture_event_contract_ledger_replay::c00`
  - 实际 top-5: `retrieval_query_rewrite_reflect::c00`, `tools_mcp_transports_lifecycle::c01`, `tools_code_interpreter_sandbox_boundary::c00`, `orchestration_react_graph::c01`, `tools_tool_call_history_repair::c01`
- `q_002`（single_hop）：查看一次已结束运行的过程记录时，服务端按什么依据返回事件？编码逻辑是单独实现的一套吗？
  - gold: `architecture_event_contract_ledger_replay::c00`, `architecture_event_contract_ledger_replay::c01`
  - 实际 top-5: `retrieval_query_rewrite_reflect::c00`, `tools_mcp_transports_lifecycle::c01`, `tools_code_interpreter_sandbox_boundary::c00`, `orchestration_react_graph::c01`, `tools_tool_call_history_repair::c01`
- `q_004`（single_hop）：执行中的任务计划，什么条件同时满足时才会替换尚未完成的步骤？
  - gold: `orchestration_plan_execute_replan::c00`, `orchestration_plan_execute_replan::c01`
  - 实际 top-5: `retrieval_query_rewrite_reflect::c00`, `tools_mcp_transports_lifecycle::c01`, `tools_code_interpreter_sandbox_boundary::c00`, `orchestration_react_graph::c01`, `tools_tool_call_history_repair::c01`

### C. hybrid_rrf：Recall@5 未命中 12 题（另有不可回答误检 6 题）

按题型：disambiguation×1、multi_hop×1、paraphrase×6、single_hop×4。示例：

- `q_006`（single_hop）：两种推理模式中，哪一种的高危工具带人工闸门？另一种为什么没有？
  - gold: `hitl_approval_scope_replay_limits::c00`
  - 实际 top-5: `control_plane_schedules_github_connectors::c01`, `retrieval_query_rewrite_reflect::c00`, `persistence_minio_artifacts_attachments::c01`, `tools_code_interpreter_sandbox_boundary::c00`, `tools_mcp_transports_lifecycle::c01`
- `q_014`（single_hop）：运行记录表和事件表分别用什么做主键？事件内容用什么类型存储？
  - gold: `persistence_postgres_business_and_events::c01`
  - 实际 top-5: `retrieval_query_rewrite_reflect::c00`, `architecture_event_contract_ledger_replay::c00`, `tools_code_interpreter_sandbox_boundary::c00`, `tools_mcp_transports_lifecycle::c01`, `persistence_minio_artifacts_attachments::c00`
- `q_015`（single_hop）：人工做出的批准或拒绝以什么格式传回系统？遇到无法识别的值怎么处理？
  - gold: `hitl_approval_interrupt_resume::c01`
  - 实际 top-5: `retrieval_query_rewrite_reflect::c00`, `tools_code_interpreter_sandbox_boundary::c01`, `hitl_approval_scope_replay_limits::c00`, `tools_mcp_transports_lifecycle::c01`, `orchestration_react_graph::c01`

### D. full_agentic_rag：Recall@5 未命中 17 题（另有不可回答误检 6 题）

按题型：disambiguation×2、multi_hop×3、paraphrase×7、single_hop×4、troubleshooting×1。示例：

- `q_006`（single_hop）：两种推理模式中，哪一种的高危工具带人工闸门？另一种为什么没有？
  - gold: `hitl_approval_scope_replay_limits::c00`
  - 实际 top-5: `tools_code_interpreter_sandbox_boundary::c00`, `retrieval_query_rewrite_reflect::c00`, `control_plane_schedules_github_connectors::c01`, `tools_mcp_transports_lifecycle::c01`, `control_plane_schedules_github_connectors::c00`
- `q_009`（single_hop）：语义向量与词法两路各自默认预取多少候选？最终返回条数由哪个配置项决定？
  - gold: `retrieval_dense_sparse_rrf::c01`
  - 实际 top-5: `retrieval_query_rewrite_reflect::c00`, `retrieval_dense_sparse_rrf::c00`, `retrieval_rerank_citations_providers::c00`, `tools_mcp_transports_lifecycle::c01`, `retrieval_ingestion_chunking_idempotency::c00`
- `q_011`（single_hop）：知识检索到底查哪个库，听模型传的参数还是听服务端注入的值？没有用户归属时退到什么？
  - gold: `retrieval_kb_isolation_and_management::c00`
  - 实际 top-5: `retrieval_query_rewrite_reflect::c00`, `retrieval_ingestion_chunking_idempotency::c00`, `retrieval_rerank_citations_providers::c00`, `tools_mcp_transports_lifecycle::c01`, `orchestration_react_graph::c00`

**失败归因拆分（gold 在返回全列表任意位置的命中率 vs Recall@5，均值）**：

| 配置 | 全列表命中 | Recall@5 | 差值=排序损失 | 平均返回条数 |
|---|---|---|---|---|
| A. dense_only | 79.2% | 77.4% | 1.9% | 10.0 |
| B. sparse_only | 37.7% | 30.2% | 7.5% | 10.0 |
| C. hybrid_rrf | 82.4% | 77.4% | 5.0% | 10.0 |
| D. full_agentic_rag | 94.3% | 61.0% | 33.3% | 18.5 |

full_agentic_rag 的“全列表命中”高于其 Recall@5 的部分，来自扩展子查询确实召回了 gold 但生产的“首次出现序”累积排序把噪声排在了前面——属于排序/融合问题而非检索覆盖问题。

## 6. 配置对比与 trade-off（是否优于 baseline 的如实说明）

- Recall@5：dense_only=0.774、sparse_only=0.302、hybrid_rrf=0.774、full_agentic_rag=0.610
- Recall@10：dense_only=0.792、sparse_only=0.377、hybrid_rrf=0.824、full_agentic_rag=0.767
- MRR@10：dense_only=0.577、sparse_only=0.178、hybrid_rrf=0.426、full_agentic_rag=0.366

- hybrid_rrf vs dense_only：Recall@5 差 +0.0 个百分点、MRR@10 差 -0.151 → Recall@5 口径 hybrid_rrf 持平 dense_only。
- hybrid_rrf vs sparse_only：Recall@5 差 +47.2 个百分点、MRR@10 差 +0.248 → Recall@5 口径 hybrid_rrf 优于 sparse_only。
- full_agentic_rag vs hybrid_rrf：Recall@5 差 -16.4 个百分点、MRR@10 差 -0.060 → Recall@5 口径 full_agentic_rag **未优于** hybrid_rrf。

- 延迟代价：full_agentic_rag 平均延迟约为 hybrid_rrf 的 **14472 倍**（53.0s vs 4ms），代价来自 route/expand/reflect/generate 的多次 LLM 调用；且在本评测集上未换来任何召回口径的收益（见上方对比）。

## 7. 运行环境与可复现性

- git commit：`8a5f78037d121e34a8f746a7f981db9cd42271d4`（tracked 脏工作区：False）；
- embedding：fastembed `BAAI/bge-small-zh-v1.5`（dim=512）；sparse：fastembed `Qdrant/bm25`；融合：Qdrant native FusionQuery(Fusion.RRF), prefetch=20/路；top_k=10；
- LLM（仅 D）：`deepseek-v4-pro` @ https://api.deepseek.com，temperature=0，reflection_limit=2，subquery_max=3；
- 运行时间：2026-07-11T09:33:00.529466+00:00 → 2026-07-11T12:30:15.312418+00:00（10634.8s）；Python 3.13.5，PYTHONHASHSEED=0；
- 依赖版本：qdrant-client=1.18.0、fastembed=0.8.0、langgraph=1.2.7、langchain-core=1.4.8、langchain-deepseek=1.1.0；Qdrant server 1.12.4；
- dense_only/sparse_only 为消融配置：单路 query_points，其余参数与生产一致
- full_agentic_rag 的 ranked 取子图累计去重 docs 的生产顺序（首次出现序），reranked/sources 为进入生成的 top-k
- qdrant-client 1.18 对 server 1.12 有版本告警，功能已验证正常
- 指标全部由 eval_metrics.py 计算（含单测），报告由 render_report.py 从 metrics.json 渲染

## 8. 数据集局限

- 语料是**从源码整理的结构化中文文档**（29 篇 × 2 chunk、280–660 字符），chunk 边界与语义边界对齐，检索难度低于生产环境的杂乱长文档，绝对数值外推需谨慎；
- 库规模小（58 chunk），Recall 天花板偏高；干扰项主要来自刻意设计的相似概念文档（三种并发限制、多个大小上限等）；
- 可回答题 53 条、不可回答 6 条，样本量支持配置间的相对比较，单点百分比的置信区间较宽（±1 题 ≈ ±1.9 个百分点）；
- 题目由熟悉语料的会话生成、另一会话独立审查（拒 1 修 15），非真实用户查询分布；
- fastembed `Qdrant/bm25` 的默认分词按空白/标点切分，中文长串成为单 token，sparse 路在纯中文改写题上几乎只能靠英文标识符命中——这是生产实现的真实行为，评测如实反映；
- D 配置的延迟依赖外部 LLM API 的当日网络状况，绝对值参考意义有限，量级对比有效。

## 9. 可安全写入简历的结论（含禁写清单）

以下表述均由本目录数据直接支撑（评测集 59 题、其中可回答 53 题；语料 58 chunk；配置与逐题结果可复现），数字为多次运行均值，措辞遵循“基线+样本量+相对改进”原则：

- ✅ “混合检索 Recall@5 与单路最优持平（77.4%），但显著优于 sparse 单路（30.2%）”——只能这样写，不能写“混合检索提升 Recall@5”
- ✅ “混合检索将 Recall@10 从纯 dense 的 79.2% 提升至 82.4%”——差距仅 3.1 个百分点（≈1.7 题），样本量下证据较弱，如写必须带评测集规模，不建议作为主亮点
- ✅ “自建 59 题中文知识库检索评测集（六种题型、gold 分组标注、独立审查拒 1 修 15）与四配置消融评测 harness（复用生产检索代码，12 项指标代码计算、3 次运行、可复现），定位出 BM25 中文分词失效与子查询语义漂移两类瓶颈”——评测体系与失败归因本身即是有效亮点（Eval/L3），不依赖提升数字
- ✅ “单轮混合检索 Recall@5 77.4%、Recall@10 82.4%、检索延迟 P95 5ms”——写绝对值时必须同时给出评测集规模（59 题/58 chunk 自建集）

**禁写清单**（当前数据不支持，写了会在面试追问中翻车）：

- ❌ “Agentic RAG 提升整体 Recall@5”——实测 61.0% vs hybrid 的 77.4%，未优于基线，必须如实说明
- ❌ “Agentic RAG 改善排序质量（MRR）”——实测 MRR@10 0.366 vs 0.426
- ❌ “混合检索优于纯 dense”——本集上 Recall@5 hybrid=77.4% vs dense=77.4%（中文 BM25 分词限制所致，见 §8）
- ❌ 任何“召回率 99%/大规模语料验证/生产流量验证”表述——本评测是 58 chunk 自建集
- ❌ “Recall 提升 X%”却不注明基线配置、评测集规模与运行次数

面试追问自检：分块策略（标题优先、560/100，理由见 scripts/build_chunks.py 头注释）、Recall 口径（gold 并集任一命中 / 多跳组语义，§3）、评测集标注质量（独立审查拒 1 修 15，review_report.md）、为什么 sparse 路弱（BM25 中文分词，§8）、Agentic 的延迟代价（§6）——以上均有文档与数据可答。

