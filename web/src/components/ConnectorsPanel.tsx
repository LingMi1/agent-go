import { useCallback, useEffect, useState } from "react";
import { Loader2, Plug, Plus, RotateCw, ShieldAlert, Trash2, Zap } from "lucide-react";
import { toast } from "sonner";
import {
  createConnector,
  createTrigger,
  deleteConnector,
  deleteTrigger,
  listConnectors,
  listTriggers,
  toggleConnector,
  toggleTrigger,
  type ConnectorItem,
  type TriggerItem,
} from "../lib/api/client";
import {
  canCreateConnector,
  canCreateTrigger,
  EVENT_TYPES,
  eventTypeLabel,
  POLL_INTERVALS,
  pollIntervalLabel,
  triggerFilter,
  triggersByConnector,
} from "../lib/connectorForm";
import { AGENT_TYPES } from "../config";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

// Proactive 连接器面板（docs/16）：授权 GitHub PAT → 建触发规则 → 平台按间隔轮询，
// 命中事件自动起 run（高危动作走 HITL 审批）。降级：SECRET_MASTER_KEY 未配置时后端 503，
// 面板显示未启用提示。产出走既有 run/会话链路（trig-* 固定会话）。
export function ConnectorsPanel({ onOpenSession }: { onOpenSession?: (sessionId: string) => void }) {
  const [connectors, setConnectors] = useState<ConnectorItem[] | null>(null);
  const [triggers, setTriggers] = useState<TriggerItem[]>([]);
  const [disabled, setDisabled] = useState(false); // 后端 503（未配置主密钥）
  const [creatingConn, setCreatingConn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);

  // 连接器创建表单。
  const [pat, setPat] = useState("");
  const [interval, setIntervalS] = useState(3600);

  // 触发规则创建表单（针对某连接器）。
  const [trigForConn, setTrigForConn] = useState<string | null>(null);
  const [eventType, setEventType] = useState("issue");
  const [repo, setRepo] = useState("");
  const [template, setTemplate] = useState("");
  const [trigAgent, setTrigAgent] = useState("react");
  const [needsApproval, setNeedsApproval] = useState(true);

  const refresh = useCallback(async () => {
    // #15：两侧独立取、独立处理错误——一侧失败绝不清空另一侧已加载数据（避免 triggers
    // 单侧 500 把连接器列表抹成空、误显示"还没有连接器"）。
    const [connRes, trigRes] = await Promise.allSettled([listConnectors(), listTriggers()]);
    // 连接器侧管连接器列表 + disabled：503=未配置主密钥（展示启用引导，非报错）；其它=加载失败。
    let featureDisabled = false;
    if (connRes.status === "fulfilled") {
      setConnectors(connRes.value);
      setDisabled(false);
    } else {
      featureDisabled = String(connRes.reason).includes("503");
      setConnectors([]);
      setDisabled(featureDisabled);
      if (!featureDisabled) toast.error("Failed to load connectors");
    }
    // 触发规则侧独立：成功才更新；失败仅提示、保留已加载的连接器与旧触发规则（功能未启用时不重复报错）。
    if (trigRes.status === "fulfilled") {
      setTriggers(trigRes.value);
    } else if (!featureDisabled) {
      toast.error("Failed to load triggers");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onCreateConnector = async () => {
    if (!canCreateConnector(pat)) return;
    setBusy(true);
    try {
      await createConnector({ kind: "github", pat: pat.trim(), pollIntervalS: interval });
      toast.success("Connector created");
      setPat("");
      setCreatingConn(false);
      await refresh();
    } catch {
      toast.error("Creation failed (invalid PAT or master key not configured)");
    } finally {
      setBusy(false);
    }
  };

  const onCreateTrigger = async (connectorId: string) => {
    if (!canCreateTrigger(connectorId, template)) return;
    setBusy(true);
    try {
      await createTrigger({
        connectorId,
        eventType,
        filter: triggerFilter(repo),
        queryTemplate: template.trim(),
        agentType: trigAgent,
        needsApproval,
      });
      toast.success("Trigger rule created");
      setTemplate("");
      setRepo("");
      setTrigForConn(null);
      await refresh();
    } catch {
      toast.error("Failed to create rule");
    } finally {
      setBusy(false);
    }
  };

  const twoStepDelete = (id: string, del: () => Promise<void>) => {
    if (confirming !== id) {
      setConfirming(id);
      setTimeout(() => setConfirming((c) => (c === id ? null : c)), 3000);
      return;
    }
    setConfirming(null);
    void del().then(refresh).catch(() => toast.error("Failed to delete"));
  };

  const byConn = triggersByConnector(triggers);

  return (
    <div className="flex h-full flex-col" data-testid="connectors-panel">
      <div className="flex items-center gap-1.5 border-b px-3 py-2">
        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground/70">Connectors (Proactive · GitHub)</span>
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setCreatingConn((v) => !v)}
          aria-label="New connector"
          data-testid="connector-new"
          className="size-7 text-muted-foreground hover:text-foreground"
        >
          <Plus />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          onClick={() => void refresh()}
          aria-label="Refresh"
          className="size-7 text-muted-foreground hover:text-foreground"
        >
          <RotateCw />
        </Button>
      </div>

      {creatingConn && (
        <div className="space-y-2 border-b p-3">
          <Input
            type="password"
            value={pat}
            onChange={(e) => setPat(e.target.value)}
            placeholder="GitHub Personal Access Token (notifications read scope)"
            data-testid="connector-pat"
            className="text-sm"
          />
          <div className="flex items-center gap-1.5">
            <Select value={String(interval)} onValueChange={(v) => setIntervalS(Number(v))}>
              <SelectTrigger size="sm" className="flex-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {POLL_INTERVALS.map((i) => (
                  <SelectItem key={i.value} value={String(i.value)}>
                    {i.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              size="sm"
              onClick={() => void onCreateConnector()}
              disabled={busy || !canCreateConnector(pat)}
              data-testid="connector-submit"
              className="bg-primary text-primary-foreground hover:bg-primary/85"
            >
              {busy ? <Loader2 className="animate-spin" /> : "Authorize"}
            </Button>
          </div>
          <p className="text-[10px] leading-relaxed text-muted-foreground/60">
            PAT is encrypted with AES-GCM at rest; plaintext is never logged or stored. Used only to fetch your GitHub notifications.
          </p>
        </div>
      )}

      <ScrollArea className="min-h-0 flex-1">
        <div className="p-2">
          {connectors === null && <div className="p-3 text-xs text-muted-foreground/70">Loading…</div>}
          {disabled && (
            <div className="p-4 text-center text-xs leading-relaxed text-muted-foreground/70">
              Connectors are not enabled.
              <br />Configure SECRET_MASTER_KEY (32-byte base64) in the control plane and restart.
            </div>
          )}
          {connectors?.length === 0 && !disabled && !creatingConn && (
            <div className="p-4 text-center text-xs leading-relaxed text-muted-foreground/70">
              No connectors yet.
              <br />Click + to authorize GitHub PAT: the platform will poll your notifications at the set interval and auto-trigger runs on matching rules.
            </div>
          )}

          {connectors?.map((c) => {
            const trigs = byConn.get(c.connectorId) ?? [];
            return (
              <div
                key={c.connectorId}
                data-testid="connector-row"
                className="group mb-2 rounded-lg border border-border/60 p-2"
              >
                <div className="flex items-center gap-2">
                  <Plug className={`size-4 shrink-0 ${c.enabled ? "text-primary" : "text-muted-foreground/60"}`} />
                  <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                    github · {pollIntervalLabel(c.pollIntervalS)}
                  </span>
                  <button
                    onClick={() =>
                      void toggleConnector(c.connectorId, !c.enabled).then(refresh).catch(() => toast.error("Failed to update"))
                    }
                    className="shrink-0 text-[10px] text-muted-foreground/70 hover:text-foreground"
                  >
                    {c.enabled ? "Polling (pause)" : "Paused (enable)"}
                  </button>
                  <button
                    onClick={() => twoStepDelete(c.connectorId, () => deleteConnector(c.connectorId))}
                    title={confirming === c.connectorId ? "Click again to confirm delete" : "Delete connector"}
                    className={`shrink-0 rounded p-1 transition-colors ${
                      confirming === c.connectorId
                        ? "bg-destructive/20 text-destructive"
                        : "text-muted-foreground/70 opacity-0 hover:text-destructive group-hover:opacity-100"
                    }`}
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                </div>

                {/* 触发规则子表 */}
                <div className="mt-1.5 space-y-1 pl-6">
                  {trigs.map((t) => (
                    <div key={t.triggerId} data-testid="trigger-row" className="flex items-center gap-2 text-[11px]">
                      <Zap className={`size-3 shrink-0 ${t.enabled ? "text-warning" : "text-muted-foreground/60"}`} />
                      <span className="shrink-0 rounded bg-accent/60 px-1 text-foreground/90">{eventTypeLabel(t.eventType)}</span>
                      {t.filter?.repo && <span className="shrink-0 text-muted-foreground/70">{t.filter.repo}</span>}
                      <button
                        onClick={() => onOpenSession?.(`trig-${t.triggerId.slice(0, 8)}`)}
                        title="Open rule session"
                        className="min-w-0 flex-1 truncate text-left text-muted-foreground hover:text-primary"
                      >
                        {t.queryTemplate}
                      </button>
                      {t.needsApproval && (
                        <ShieldAlert className="size-3 shrink-0 text-muted-foreground/70" aria-label="High-risk action requires approval" />
                      )}
                      <button
                        onClick={() =>
                          void toggleTrigger(t.triggerId, !t.enabled).then(refresh).catch(() => toast.error("Failed to update"))
                        }
                        className="shrink-0 text-muted-foreground/70 hover:text-foreground"
                      >
                        {t.enabled ? "ON" : "OFF"}
                      </button>
                      <button
                        onClick={() => twoStepDelete(t.triggerId, () => deleteTrigger(t.triggerId))}
                        title={confirming === t.triggerId ? "Click again to confirm delete" : "Delete rule"}
                        className={`shrink-0 rounded p-0.5 ${
                          confirming === t.triggerId ? "text-destructive" : "text-muted-foreground/60 hover:text-destructive"
                        }`}
                      >
                        <Trash2 className="size-3" />
                      </button>
                    </div>
                  ))}

                  {trigForConn === c.connectorId ? (
                    <div className="space-y-1.5 rounded-md border border-border/60 p-2">
                      <div className="flex items-center gap-1.5">
                        <Select value={eventType} onValueChange={setEventType}>
                          <SelectTrigger size="sm" className="flex-1">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {EVENT_TYPES.map((e) => (
                              <SelectItem key={e.value} value={e.value}>
                                {e.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        <Input
                          value={repo}
                          onChange={(e) => setRepo(e.target.value)}
                          placeholder="Repo filter owner/name (optional)"
                          data-testid="trigger-repo"
                          className="h-8 flex-1 text-xs"
                        />
                      </div>
                      <Textarea
                        value={template}
                        onChange={(e) => setTemplate(e.target.value)}
                        rows={2}
                        placeholder='Query template: use {{title}} {{url}} {{repo}}, e.g.: Draft a reply for "{{title}}"'
                        data-testid="trigger-template"
                        className="min-h-0 resize-none text-xs"
                      />
                      <div className="flex items-center gap-1.5">
                        <Select value={trigAgent} onValueChange={setTrigAgent}>
                          <SelectTrigger size="sm" className="flex-1">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {AGENT_TYPES.map((a) => (
                              <SelectItem key={a.value} value={a.value}>
                                {a.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        <label className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
                          <input
                            type="checkbox"
                            checked={needsApproval}
                            onChange={(e) => setNeedsApproval(e.target.checked)}
                            data-testid="trigger-approval"
                          />
                          Approval required
                        </label>
                        <Button
                          size="sm"
                          onClick={() => void onCreateTrigger(c.connectorId)}
                          disabled={busy || !canCreateTrigger(c.connectorId, template)}
                          data-testid="trigger-submit"
                          className="bg-primary text-primary-foreground hover:bg-primary/85"
                        >
                          {busy ? <Loader2 className="animate-spin" /> : "Add"}
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <button
                      onClick={() => {
                        setTrigForConn(c.connectorId);
                        setTemplate("");
                        setRepo("");
                      }}
                      data-testid="trigger-new"
                      className="flex items-center gap-1 text-[11px] text-muted-foreground/70 hover:text-primary"
                    >
                      <Plus className="size-3" /> Add trigger rule
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </ScrollArea>
      <div className="border-t p-2 text-[10px] leading-relaxed text-muted-foreground/60">
        Matching events auto-trigger runs (into rule-specific sessions); rules marked for approval will pause high-risk actions for your review in chat.
      </div>
    </div>
  );
}
