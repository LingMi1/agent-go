// 连接器面板（ConnectorsPanel）的纯逻辑：与渲染解耦，便于 vitest（node 环境）测试。
import type { TriggerItem } from "./api/client";

export const POLL_INTERVALS = [
  { value: 300, label: "每 5 分钟" },
  { value: 900, label: "每 15 分钟" },
  { value: 3600, label: "每小时" },
  { value: 21600, label: "每 6 小时" },
] as const;

// GitHub 事件类型（对齐后端 Normalize：subject.type 归一后的值）。
export const EVENT_TYPES = [
  { value: "issue", label: "Issue" },
  { value: "pull_request", label: "Pull Request" },
] as const;

export function pollIntervalLabel(s: number): string {
  return POLL_INTERVALS.find((i) => i.value === s)?.label ?? `每 ${s} 秒`;
}

export function eventTypeLabel(t: string): string {
  return EVENT_TYPES.find((e) => e.value === t)?.label ?? t;
}

// repo 输入 → 触发规则 filter：非空 repo → { repo }，空 → undefined（不过滤）。
// repo 形如 "owner/name"，与后端事件 fields.repo（repository.full_name）对齐。
export function triggerFilter(repo: string): Record<string, string> | undefined {
  const r = repo.trim();
  return r ? { repo: r } : undefined;
}

// 触发规则按 connectorId 分组（面板在每个连接器下展示其规则子表）。
export function triggersByConnector(triggers: TriggerItem[]): Map<string, TriggerItem[]> {
  const m = new Map<string, TriggerItem[]>();
  for (const t of triggers) {
    const list = m.get(t.connectorId) ?? [];
    list.push(t);
    m.set(t.connectorId, list);
  }
  return m;
}

// 创建连接器可提交条件（PAT 非空）。
export function canCreateConnector(pat: string): boolean {
  return pat.trim().length > 0;
}

// 创建触发规则可提交条件（选了连接器 + 模板非空）。
export function canCreateTrigger(connectorId: string, queryTemplate: string): boolean {
  return connectorId.trim().length > 0 && queryTemplate.trim().length > 0;
}
