import { useEffect, useState } from "react";
import { getUsageStats, type UsageReport } from "../lib/api/client";
import { AGENT_TYPES } from "../config";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function agentLabel(v: string): string {
  return AGENT_TYPES.find((a) => a.value === v)?.label ?? v;
}

// 成本与用量面板（M11）：runs 账本聚合的纯读投影；纯 CSS 条形，无图表库。
// token 数为模型上报的 usage_metadata 累计；换算金额留给用户按各家单价自行估（多 provider 单价不一）。
export function UsageDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const [report, setReport] = useState<UsageReport | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setReport(null);
    setFailed(false);
    getUsageStats(30)
      .then((r) => alive && setReport(r))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [open]);

  const maxDay = Math.max(1, ...(report?.daily.map((d) => d.inputTokens + d.outputTokens) ?? [1]));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>Usage &amp; Cost (last 30 days)</DialogTitle>
        <DialogDescription>
          Read-only projection from the event ledger; tokens are cumulative values reported by the model (interrupted/timed-out runs excluded).
        </DialogDescription>
        {failed && <div className="text-sm text-destructive">Failed to load. Please try again.</div>}
        {!report && !failed && (
          <div className="space-y-2">
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}
        {report && (
          <div className="space-y-4">
            <div className="grid grid-cols-4 gap-2 text-center">
              {[
                ["Runs", fmt(report.totals.runs)],
                ["Input Tokens", fmt(report.totals.inputTokens)],
                ["Output Tokens", fmt(report.totals.outputTokens)],
                ["Model Calls", fmt(report.totals.modelCalls)],
              ].map(([label, value]) => (
                <div key={label} className="rounded-lg border bg-card px-2 py-3">
                  <div className="font-display text-lg font-semibold text-foreground">{value}</div>
                  <div className="text-[10px] text-muted-foreground/70">{label}</div>
                </div>
              ))}
            </div>

            {report.daily.length > 0 && (
              <div>
                <div className="mb-1.5 text-xs text-muted-foreground/70">Daily Tokens (Input+Output)</div>
                <div className="flex h-20 items-end gap-1">
                  {report.daily.map((d) => (
                    <div
                      key={d.date}
                      title={`${d.date}: ${fmt(d.inputTokens)} in / ${fmt(d.outputTokens)} out (${d.runs} runs)`}
                      className="min-w-2 flex-1 rounded-t bg-primary/70 transition-colors hover:bg-primary"
                      style={{ height: `${Math.max(6, ((d.inputTokens + d.outputTokens) / maxDay) * 100)}%` }}
                    />
                  ))}
                </div>
              </div>
            )}

            {report.byAgent.length > 0 && (
              <div className="space-y-1">
                <div className="text-xs text-muted-foreground/70">By Mode</div>
                {report.byAgent.map((a) => (
                  <div key={a.agentType} className="flex items-center gap-2 text-sm">
                    <span className="w-20 shrink-0 text-foreground/90">{agentLabel(a.agentType)}</span>
                    <span className="text-xs text-muted-foreground/70">
                      {a.runs} runs · {fmt(a.inputTokens)} in / {fmt(a.outputTokens)} out
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
