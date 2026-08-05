# RAG 评测集独立审查报告

审查人：独立审查会话（未参与 corpus / questions 的生成）。
审查对象：`eval/rag/corpus/`（29 篇文档）、`chunks.jsonl`（58 chunk）、`questions.jsonl`（60 题）、`corpus_manifest.json`。
审查日期：2026-07-11。

## 结论摘要

| 项 | 数量 |
| --- | --- |
| 通过（原样接受） | 44 |
| 通过（修正后接受，主要为补标漏标 gold） | 15 |
| 通过但带警告 | 7（与上两类有重叠） |
| 拒绝 | 1（q_023） |
| 有效题目合计 | **59**（可回答 53 + 不可回答 6） |

产出文件：`questions_verified.jsonl`（59 题，新增 `gold_groups` / `review_status` / `review_note` / `review_warning` 字段）、`questions_rejected.jsonl`（1 题，含拒绝原因）。

## 审查方法

1. 通读全部 58 个 chunk 的完整文本与 60 题的题面/答案/evidence/notes；
2. 运行既有结构校验 `scripts/validate_questions.py`（通过；该脚本只做结构性检查：quote 逐字存在、gold 与 quote 所在 chunk 集合一致、题型分布、chunks 可复现重建）；
3. 用字符 3-gram 重叠脚本量化题面与语料的词面重合（辅助检查照抄与漏标线索）；
4. 逐题人工判定 9 项标准（唯一答案 / 证据充分 / 漏标 / 词面重叠 / 常识可答 / 多跳必要性 / 不可答验证 / 未实现功能依赖 / answer 与 evidence 一致）；
5. 所有修正由脚本落盘，每处补标必须携带 verbatim quote 并硬校验其确实存在于所加 chunk 文本中，防止审查本身引入错误标注。

## 主要发现

### 1. 系统性漏标（已修正，15 题）

两个成因：

- **overlap 接头**：分块规则在相邻 chunk 间保留前块尾部 100 字符。原标注规则「gold = 完整 quote 逐字出现的 chunk 集合」能处理 quote 完整落入接头的情况，但当接头**截断句首却保留完整语义**时会漏标（如 q_008 的 `retrieval_agentic_rag_graph::c01` 接头只截掉主语 "route"，事实完整可答）。
- **同一事实多处表述**：语料刻意在不同文档以不同措辞重述关键事实，原标注只标了 quote 所在篇。最明显的是 q_047（心跳不占 seq——`grpc_sse::c01` 与 `ledger_replay::c00/c01` 都完整陈述）和 q_049（断线语义——`troubleshooting::c01` 与 `ledger_replay::c01` 都完整陈述）。

漏标直接压低所有配置的测得 Recall（检索到等价证据却判 miss）。修正后的补标清单（每条在 `questions_verified.jsonl` 的 `review_note` 与新增 `evidence_spans` 中可核对）：

| 题 | 补标 chunk | 依据 |
| --- | --- | --- |
| q_003 | react::c00 | "小于 max_steps…否则结束" 独立可答 |
| q_007 | troubleshooting::c00 | "请求过载在 SSE 头写出前返回 429" 覆盖两问 |
| q_008 | rag_graph::c01 | overlap 接头完整携带事实 |
| q_012 | rerank_citations::c01 | overlap 接头完整携带三步流水线 |
| q_020 | run_admission::c00 | "失败立即返回 429" 足以回答 |
| q_024 | schedules::c00 | "轮询 GitHub notifications" 覆盖平台与方式 |
| q_029 | history_repair::c01 | 接头+本体段落完整覆盖修复机制 |
| q_030 | deployment::c00 | "唯一对外 control-plane / 内部 cognition" |
| q_042 | run_admission::c01 | "两级并发相互独立" 另一侧表述 |
| q_044 | query_rewrite::c00 | 业务目标句已区分两机制 |
| q_047 | ledger::c00 + c01 | 同一事实不同措辞，明显漏标 |
| q_049 | ledger::c01 | 完整覆盖取消+只能回放 |
| q_031 | mcp::c01、send_fanout::c01 | 多跳组内等价证据 |
| q_032 | run_admission::c00 | 多跳组内等价证据 |
| q_037 | postgres::c00 | FinishRun 写用量为等价证据 |

### 2. 多跳题 gold 语义缺陷（已修正：引入 `gold_groups`）

原 `gold_chunk_ids` 是扁平列表，但其中混有「同一事实的等价证据」（多为 overlap 接头复制，如 q_033 的 prometheus::c00/c01）。若按「全部 gold chunk 被召回」计 All-Recall，会要求配置同时召回两个内容重复的 chunk——语义错误。

修正：所有题增加 `gold_groups`（组间 = 必要事实 AND；组内 = 等价证据 OR）。多跳 All-Recall 的正确定义为**每组至少命中一个成员**；Any-Recall 为至少一组命中。单跳/其余题型为单组。10 道多跳题的组数：2 组 ×8、3 组 ×2（q_031、q_040），全部满足「必须跨 ≥2 篇文档取证」。

### 3. 拒绝题（1 题）

- **q_023**（paraphrase）：题面「让智能体替你跑一段代码时，那段代码的运行环境能联网吗？能随便写文件吗？」——语料明确存在 local / Docker 两条执行路径，且**默认 `skill_runner=local`（非沙箱）**；标注答案只描述 Docker 模式（断网/只读），在默认配置下不成立，答案不唯一且标签有误导性。选择拒绝而非改写题面，避免审查员改题引入偏置。

### 4. 保留但记录警告（7 题）

- q_001 / q_006 / q_041 / q_045：语料他处存在**部分**证据（隐含或只覆盖半问）的 chunk，按「证据必须充分」标准不计 gold；若检索器召回这些近似 chunk 会被判 miss，Recall 可能被轻微低估（这是从严方向的偏差，可接受）。
- q_019 / q_051：答案接近通用常识（SSE 单向、容器内 localhost 误配）。本评测只测**检索召回**（gold 命中），不测答案正确率，题面均为同义改写、检索难度真实，故保留；若未来复用本集做生成/拒答评测，需注意这两题可被常识作答。
- q_054：「不回写数据库」半个答案只在 c01，但按题型定义以任一 gold 计召回，对只召回 c00 的配置略宽松。

### 5. 逐项标准核查结果

1. **唯一答案**：除 q_023（拒绝）外均唯一；
2. **gold 证据充分性**：60 题 evidence quote 全部逐字复核存在且支撑答案；
3. **漏标**：15 题补标（见上）；
4. **词面重叠**：3-gram 重合度整体很低（最高 q_050≈0.18），无照抄题面；`validate_questions.py` 的 12 字连抄检测亦零命中；
5. **常识可答**：q_019、q_051 记录警告，因仅测检索保留；
6. **多跳必要性**：10 题全部核实需要跨 ≥2 篇文档、组间事实不可互推；
7. **不可回答题**：6 题逐条对照全语料核实——重试/退避策略、分片/副本、前端框架、模型路由分配、心跳间隔数值、kb 配额均未在任何 chunk 中出现；其中 q_058/q_059 为近失配陷阱（语料有相邻主题但无该事实），设计合理；
8. **未实现功能依赖**：无。partial 状态文档（code_interpreter、rerank、otel、deployment、troubleshooting）相关题目问的都是语料明确记载的事实或限制本身；
9. **answer 严格来自 evidence**：逐题核对通过；个别答案含轻度措辞归纳（如 q_032 "等待后续调度"），语义由证据蕴含，未见编造。

## 对下游评测的影响

- 有效可回答题 53（single_hop 18、paraphrase 11、multi_hop 10、disambiguation 8、troubleshooting 6），不可回答 6；
- 指标计算必须使用 `gold_groups` 语义（本目录评测脚本已实现）；
- 补标使 Recall 数字与修正前不可直接比较——所有配置一律使用 `questions_verified.jsonl`，不存在混用。

## 数据集局限（如实记录）

- 语料为**从源码整理的结构化中文文档**（每篇 2 chunk、约 280–660 字符），不是真实用户文档；chunk 边界与语义边界对齐，检索难度低于生产场景的杂乱长文档；
- 58 chunk 的库规模小，Recall 天花板偏高，数字外推到大库需谨慎；
- 每篇固定 2 chunk 且带 100 字符接头，相邻 chunk 语义相关性强，对 dense 检索相对友好；
- 题目由熟悉语料结构的一方生成（另一会话），虽经独立审查，仍非真实用户查询分布。
