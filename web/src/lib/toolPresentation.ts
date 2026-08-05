// 工具展示元数据（纯函数，可测）：toolName/provider → 图标 + 动词短语 + 目标摘要。
// ToolRow 据此把每次工具调用渲染成紧凑状态行（不再每个占一张大卡）。
// input 可能是对象也可能是 JSON 串（账本原样回放），一律安全解析、失败降级为无目标。

import type { LucideIcon } from "lucide-react";
import {
  BookOpen,
  Calculator,
  FileText,
  Globe,
  ImagePlus,
  LibraryBig,
  Plug,
  Search,
  SquareTerminal,
  Wrench,
} from "lucide-react";
import { parseWebSearchResult } from "./sse/toolPayloads";

export type ToolKind = "generic" | "search" | "image";

export interface ToolMeta {
  icon: LucideIcon;
  kind: ToolKind;
  runningVerb(input?: unknown): string;
  doneVerb(input?: unknown, resultText?: string): string;
  target?(input?: unknown): string | undefined;
}

/** input 是 JSON 串或对象都可能：安全归一为 Record；解析失败/非对象返回 null。 */
function asRecord(input: unknown): Record<string, unknown> | null {
  let v = input;
  if (typeof v === "string") {
    try {
      v = JSON.parse(v);
    } catch {
      return null;
    }
  }
  if (typeof v === "object" && v !== null && !Array.isArray(v)) return v as Record<string, unknown>;
  return null;
}

function strField(input: unknown, key: string): string | undefined {
  const rec = asRecord(input);
  const v = rec?.[key];
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
}

function truncate(s: string, max = 48): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** input.url → hostname（去 www.）；缺失/非法返回 undefined。 */
function hostOf(input: unknown): string | undefined {
  const url = strField(input, "url");
  if (!url) return undefined;
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return undefined;
  }
}

/** knowledge_search 观察串里的〔n〕引用标记数（去重后的引用段数）；数不出返回 0。 */
export function countCitations(resultText: string | undefined): number {
  if (!resultText) return 0;
  const seen = new Set<string>();
  for (const m of resultText.matchAll(/〔(\d+)〕/g)) seen.add(m[1]);
  return seen.size;
}

const BY_NAME: Record<string, () => ToolMeta> = {
  calculator: () => ({
    icon: Calculator,
    kind: "generic",
    runningVerb: () => "正在计算…",
    doneVerb: () => "计算完成",
    target: (input) => {
      const expr = strField(input, "expression");
      return expr ? truncate(expr) : undefined;
    },
  }),
  write_report: () => ({
    icon: FileText,
    kind: "generic",
    runningVerb: () => "正在撰写报告…",
    doneVerb: () => "报告已生成",
  }),
  knowledge_search: () => ({
    icon: LibraryBig,
    kind: "generic",
    runningVerb: () => "Searching knowledge base…",
    doneVerb: (_input, resultText) => {
      const n = countCitations(resultText);
      return n > 0 ? `检索完成 · ${n} 段引用` : "检索完成";
    },
  }),
  image_generate: () => ({
    icon: ImagePlus,
    kind: "image",
    runningVerb: () => "正在生成图片…",
    doneVerb: () => "已生成图片",
  }),
  web_fetch: () => ({
    icon: Globe,
    kind: "generic",
    runningVerb: (input) => {
      const host = hostOf(input);
      return host ? `正在抓取 ${host}…` : "正在抓取…";
    },
    doneVerb: (input) => {
      const host = hostOf(input);
      return host ? `抓取完成 · ${host}` : "抓取完成";
    },
  }),
  code_interpreter: () => ({
    icon: SquareTerminal,
    kind: "generic",
    runningVerb: () => "正在执行代码…",
    doneVerb: () => "代码执行完成",
  }),
  script_runner: () => ({
    icon: Wrench,
    kind: "generic",
    runningVerb: (input) => {
      const skill = strField(input, "skill");
      return skill ? `正在运行技能 ${skill}…` : "正在运行技能…";
    },
    doneVerb: (input) => {
      const skill = strField(input, "skill");
      if (skill === "chart-visualization") return "图表已生成";
      return skill ? `技能 ${skill} 完成` : "技能完成";
    },
  }),
  web_search: () => ({
    icon: Search,
    kind: "search",
    runningVerb: (input) => {
      const query = strField(input, "query");
      return query ? `正在搜索「${truncate(query, 30)}」…` : "正在搜索…";
    },
    doneVerb: (_input, resultText) => {
      const payload = parseWebSearchResult(resultText);
      return payload ? `搜索完成 · ${payload.results.length} 条来源` : "搜索完成";
    },
  }),
};

const SKILL_DOC_TOOLS = new Set(["skill", "skill_read", "skill_list", "skill_glob", "skill_grep"]);

export function toolMeta(toolName: string, provider: string): ToolMeta {
  const named = BY_NAME[toolName];
  if (named) return named();
  if (SKILL_DOC_TOOLS.has(toolName)) {
    return {
      icon: BookOpen,
      kind: "generic",
      runningVerb: () => "正在查阅技能文档…",
      doneVerb: () => "已查阅技能文档",
    };
  }
  if (provider === "mcp") {
    const name = toolName || "MCP 工具";
    return {
      icon: Plug,
      kind: "generic",
      runningVerb: () => `正在调用 ${name}…`,
      doneVerb: () => `${name} 完成`,
    };
  }
  const name = toolName || "工具";
  return {
    icon: Wrench,
    kind: "generic",
    runningVerb: () => `正在运行 ${name}…`,
    doneVerb: () => `${name} 完成`,
  };
}
