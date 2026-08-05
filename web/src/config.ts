// 前端常量。端点全用相对路径（Vite proxy 转发）。

// 三档模式（M9）：value 是后端 agent_type，**不可改**（sessions 持久化按值恢复）；
// label 面向用户（快速=ReAct / 深度思考=Plan-Execute / 深度研究=研究变体图）。
export const AGENT_TYPES = [
  { value: "react", label: "Quick" },
  { value: "plan_solve", label: "Deep Think" },
  { value: "deep_research", label: "Deep Research" },
] as const;

// 输出格式选择器（M9）：value 经 startRun.outputFormat → 认知面 metadata；
// 仅深度思考/深度研究可选（快速模式 disabled，对齐原项目）。空串=自由格式。
export const OUTPUT_FORMATS = [
  { value: "", label: "Freeform" },
  { value: "docs", label: "Document" },
  { value: "table", label: "Spreadsheet" },
  { value: "ppt", label: "PPT" },
  { value: "html", label: "Web Page" },
] as const;

export const HEALTH_POLL_MS = 30_000;

// 首屏建议问题：每条映射一项真实能力（深度研究检索报告 / GitHub 调研 / 图表 / 生图 / 文档），
// 展示态（截图）与日常演示共用。e2e 不再依赖此列表（helpers.sendMessage 直接输入）。
export const SAMPLE_QUESTIONS = [
  "Write a research report on AI agent adoption in enterprise software for 2026, with cited sources",
  "Analyze the architecture of langchain-ai/langgraph on GitHub and produce a web-based report",
  "Search for EV market penetration data over the past five years and visualize it with charts",
  "Generate a watercolor-style poster of a Jiangnan water town",
  "Plan a three-day itinerary for Tokyo and output it as a document",
];
