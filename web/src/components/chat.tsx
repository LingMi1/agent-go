import { memo, useState, type ReactNode } from "react";
import {
  Brain,
  Check,
  Circle,
  CircleCheck,
  Copy,
  Hand,
  ListTodo,
  LoaderCircle,
  Paperclip,
  Puzzle,
} from "lucide-react";
import type { AttachmentRef } from "../lib/api/client";
import type {
  ApprovalView,
  ArtifactRef,
  PlanView as PlanViewT,
  RunState,
  ThoughtView,
} from "../lib/sse/frameTypes";
import { Collapsible, Markdown } from "./common";
import { ToolRow } from "./ToolRow";
import { useAuthedObjectUrl } from "./ArtifactWorkspace";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

function ThinkingDots() {
  return (
    <span className="thinking-dots">
      <span />
      <span />
      <span />
    </span>
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-secondary px-4 py-2 text-sm text-foreground">
        {text}
      </div>
    </div>
  );
}

// firstAt/lastAt → 思考秒数；时间戳缺失/非法/倒挂一律 null（折叠行就不显示秒数）。
function thoughtSeconds(t: ThoughtView): number | null {
  if (!t.firstAt || !t.lastAt) return null;
  const a = Date.parse(t.firstAt);
  const b = Date.parse(t.lastAt);
  if (!Number.isFinite(a) || !Number.isFinite(b) || b < a) return null;
  return Math.max(1, Math.round((b - a) / 1000));
}

function ThoughtBlock({ thought, running }: { thought: ThoughtView; running?: boolean }) {
  const text = thought.text;
  if (!text.trim()) return null;
  // 过程可见但不喧宾夺主：流式期间展开跟读，run 结束折叠为一行摘要
  //（折叠/展开由 MessageList 的 key 带 running 标志触发重挂载实现）。
  const excerpt = text.trim().split("\n")[0].slice(0, 42);
  const secs = thoughtSeconds(thought);
  return (
    <Collapsible
      title={
        <span className="flex min-w-0 items-center gap-1.5 text-muted-foreground">
          <Brain className="size-3.5 shrink-0" />
          {running ? (
            <span>
              思考中
              <ThinkingDots />
            </span>
          ) : (
            <span className="min-w-0 truncate">
              已思考{secs != null ? ` ${secs} 秒` : ""}
              <span className="ml-1 text-xs text-muted-foreground/70">· {excerpt}…</span>
            </span>
          )}
        </span>
      }
      defaultOpen={!!running}
      ghost
    >
      <div className="whitespace-pre-wrap text-muted-foreground">{text}</div>
    </Collapsible>
  );
}

function PlanCard({ plan }: { plan: PlanViewT }) {
  const done = plan.stepStatus.filter((s) => s === "completed").length;
  const pct = plan.steps.length ? Math.round((done / plan.steps.length) * 100) : 0;
  return (
    <Card className="gap-0 border-plan/30 bg-plan/[0.06] py-3">
      <CardContent className="px-3">
        <div className="mb-2 flex items-center gap-2">
          <span className="flex items-center gap-1.5 text-plan">
            <ListTodo className="size-4 shrink-0" />
            {plan.title || "计划"}
          </span>
          <span className="text-xs text-muted-foreground">
            {done}/{plan.steps.length}
          </span>
          <Progress
            value={pct}
            className="ml-auto h-1.5 w-24 bg-border [&>[data-slot=progress-indicator]]:bg-plan"
          />
        </div>
        <ol className="space-y-1">
          {plan.steps.map((step, i) => {
            const st = plan.stepStatus[i] ?? "not_started";
            const icon =
              st === "completed" ? (
                <CircleCheck className="size-4 shrink-0 text-success" />
              ) : st === "in_progress" ? (
                <LoaderCircle className="size-4 shrink-0 animate-spin text-plan" />
              ) : (
                <Circle className="size-4 shrink-0 text-muted-foreground/40" />
              );
            return (
              <li
                key={i}
                className={`flex items-start gap-2 text-sm ${st === "in_progress" ? "text-plan" : "text-foreground/85"}`}
              >
                <span className="mt-0.5">{icon}</span>
                <span className="min-w-0 flex-1">{step}</span>
              </li>
            );
          })}
        </ol>
      </CardContent>
    </Card>
  );
}

function TaskChip({ text }: { text: string }) {
  return (
    <Badge
      variant="outline"
      className="gap-2 rounded-lg border-warning/25 bg-warning/[0.06] px-3 py-1.5 text-sm font-normal text-warning"
    >
      <span className="flex items-center gap-1">
        <Puzzle className="size-3.5 shrink-0" />
        子任务
      </span>
      <span className="text-foreground/85">{text}</span>
    </Badge>
  );
}

function Conclusion({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板被拒绝（非 https 等）：静默 */
    }
  };
  return (
    <Card className="gap-0 rounded-2xl rounded-bl-sm border bg-card px-4 py-3">
      {/* 「结论」二字为 e2e 锚点，原样保留 */}
      <div className="mb-1 flex items-center gap-1.5 text-xs text-muted-foreground">
        <CircleCheck className="size-3.5 shrink-0 text-success" />
        结论
      </div>
      <Markdown>{text || "（空）"}</Markdown>
      <div className="mt-2 flex justify-end border-t pt-1.5">
        <button
          onClick={() => void copy()}
          title="复制结论"
          className="flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          {copied ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
    </Card>
  );
}

// M11 HITL：人工审批卡。live/最后一轮可操作（onDecide 传入且 pending）；
// 其余（历史回放/已决议）只读展示。决议后按钮即禁用（本地补丁 + 后端幂等校验双保险）。
// docs/14 fork：分叉会话继承轮的 pending 卡恒只读（inherited），并提示去源会话处理——
// 决议 API 按 run 归属会话恢复，从分叉视图操作会把决议 run 落回父会话时间线。
export function ApprovalCard({
  approval,
  onDecide,
  inherited,
}: {
  approval: ApprovalView;
  // 返回决议是否成功——失败（网络/429/无 pending）时重置 busy 让卡可重试。
  onDecide?: (approvalId: string, approved: boolean, comment?: string) => Promise<boolean> | void;
  inherited?: boolean;
}) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = approval.status === "pending";
  const actionable = pending && !!onDecide && !busy;
  const decide = async (approved: boolean) => {
    if (!onDecide) return;
    setBusy(true);
    const ok = await onDecide(approval.approvalId, approved, comment.trim() || undefined);
    if (ok === false) setBusy(false); // 失败复位（成功时本轮已归档/替换，busy 随卸载消失）
  };
  return (
    <Card className="gap-0 border-warning/30 bg-warning/[0.06] px-4 py-3">
      <div className="mb-1 flex items-center gap-2 text-sm">
        <span className="flex items-center gap-1.5 text-warning">
          <Hand className="size-4 shrink-0" />
          需要人工审批
        </span>
        <span className="text-foreground/85">{approval.toolName}</span>
        {approval.status === "approved" && <Badge className="bg-success/15 text-success">已批准</Badge>}
        {approval.status === "rejected" && (
          <Badge className="bg-destructive/15 text-destructive">已拒绝</Badge>
        )}
        {pending && !onDecide && (
          <Badge className="bg-warning/15 text-warning">{inherited ? "属于源会话" : "等待决议"}</Badge>
        )}
      </div>
      {pending && inherited && (
        <div className="mb-2 text-xs text-muted-foreground" data-testid="approval-inherited-hint">
          此审批属于源会话，请在源会话中处理。
        </div>
      )}
      {approval.reason && <div className="mb-2 text-xs text-muted-foreground">{approval.reason}</div>}
      {approval.input != null && (
        <pre className="mb-2 overflow-auto rounded-md bg-code-bg p-2 font-mono text-xs">
          {JSON.stringify(approval.input, null, 2)}
        </pre>
      )}
      {actionable && (
        <div className="flex items-center gap-2">
          <input
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="备注（可选）"
            className="min-w-0 flex-1 rounded-lg border bg-transparent px-2 py-1 text-xs text-foreground outline-none focus:border-ring"
          />
          <button
            onClick={() => void decide(true)}
            className="rounded-lg bg-success px-3 py-1 text-xs text-white transition-opacity hover:opacity-85"
          >
            批准
          </button>
          <button
            onClick={() => void decide(false)}
            className="rounded-lg bg-destructive px-3 py-1 text-xs text-white transition-opacity hover:opacity-85"
          >
            拒绝
          </button>
        </div>
      )}
      {busy && (
        <div className="flex items-center gap-1 text-xs text-muted-foreground">
          <LoaderCircle className="size-3 animate-spin" />
          正在提交决议…
        </div>
      )}
    </Card>
  );
}

// 图片附件的缩略图卡：/artifacts/<resourceKey> 带头 fetch 成 blob（裸 <img src> 无法带
// X-User-Id，登入用户必 403），点击交给工作区 focus（onOpenArtifact 与工具产物同一回调）。
// 单独成组件：useAuthedObjectUrl 是 hook，不能在 AttachmentRow 的 map 里直接调。
function AttachmentThumb({
  att,
  onOpen,
}: {
  att: AttachmentRef;
  onOpen?: (resourceKey: string) => void;
}) {
  const objUrl = useAuthedObjectUrl(`/artifacts/${att.resourceKey}`, true);
  return (
    <button
      type="button"
      title={att.fileName}
      onClick={() => onOpen?.(att.resourceKey)}
      className="group overflow-hidden rounded-lg border transition-shadow hover:shadow-md"
    >
      {objUrl ? (
        <img
          src={objUrl}
          alt={att.fileName}
          className="h-20 w-auto max-w-40 object-cover transition-transform duration-200 group-hover:scale-[1.04]"
        />
      ) : (
        <span className="flex h-20 w-24 items-center justify-center bg-secondary">
          <Paperclip className="size-4 text-muted-foreground" />
        </span>
      )}
    </button>
  );
}

// 发送轮的附件行（挂在用户气泡下方；实时轮与历史回放轮同源渲染——附件元数据已随 run 落库返还）。
// image/* 升级为缩略图卡，其余保持文件名 chip。
function AttachmentRow({
  attachments,
  onOpenArtifact,
}: {
  attachments?: AttachmentRef[];
  onOpenArtifact?: (resourceKey: string) => void;
}) {
  if (!attachments?.length) return null;
  return (
    <div className="flex flex-wrap justify-end gap-1.5">
      {attachments.map((a) =>
        a.mimeType?.startsWith("image/") ? (
          <AttachmentThumb key={a.resourceKey} att={a} onOpen={onOpenArtifact} />
        ) : (
          <Badge key={a.resourceKey} variant="outline" className="gap-1 font-normal text-muted-foreground">
            <Paperclip className="size-3" />
            <span className="max-w-40 truncate">{a.fileName}</span>
          </Badge>
        ),
      )}
    </div>
  );
}

// memo：多轮 timeline 里历史轮的 props 引用稳定，流式期间只有 live 轮重渲，
// 每帧渲染成本不随会话长度增长。
export const MessageList = memo(function MessageList({
  state,
  query,
  attachments,
  running,
  inherited,
  onApprovalDecision,
  onOpenSources,
  onOpenArtifact,
}: {
  state: RunState;
  query?: string;
  attachments?: AttachmentRef[];
  running?: boolean;
  // docs/14 fork：本轮是否为继承轮（审批卡据此渲染"去源会话处理"只读提示）。
  inherited?: boolean;
  // M11：仅 live 轮/最后一轮传入（历史回放只读）；带上本轮 runId（恢复不依赖 liveRef）。
  onApprovalDecision?: (
    runId: string,
    approvalId: string,
    approved: boolean,
    comment?: string,
  ) => Promise<boolean> | void;
  // 工具状态行透传：搜索"查看全部来源" / 图片缩略图打开工作区。
  onOpenSources?: (toolCallId: string) => void;
  onOpenArtifact?: (resourceKey: string) => void;
}) {
  const resultByCall = new Map(state.toolResults.map((r) => [r.toolCallId, r.text]));
  const artifactByKey = new Map(state.artifacts.map((a) => [a.resourceKey, a]));
  const artifactsFor = (toolCallId: string): ArtifactRef[] | undefined => {
    const keys = state.artifactsByCall?.[toolCallId];
    if (!keys?.length) return undefined;
    const refs = keys.map((k) => artifactByKey.get(k)).filter((a): a is ArtifactRef => !!a);
    return refs.length ? refs : undefined;
  };

  const renderEntry = (kind: string, key: string, k: string): ReactNode => {
    switch (kind) {
      case "thought": {
        const t = state.thoughts[key];
        // key 带 running：run 结束时重挂载 → 未受控 Collapsible 从展开态收拢为摘要行。
        return t ? <ThoughtBlock key={`${k}:${running ? "r" : "d"}`} thought={t} running={running} /> : null;
      }
      case "plan": {
        const round = state.plannerRounds.find((r) => (r.plannerRoundId ?? "plan") === key) ?? state.plan;
        return round ? <PlanCard key={k} plan={round} /> : null;
      }
      case "task":
        return <TaskChip key={k} text={state.tasks[Number(key)]?.text ?? ""} />;
      case "toolCall": {
        const call = state.toolCalls[key];
        // key 带成功标志：工具成功那一帧重挂载 → 行回到收起态（运行中/失败保持既有开合）。
        return call ? (
          <ToolRow
            key={`${k}:${call.status === "success" ? "s" : "r"}`}
            call={call}
            resultText={resultByCall.get(call.toolCallId)}
            artifacts={artifactsFor(call.toolCallId)}
            onOpenSources={onOpenSources}
            onOpenArtifact={onOpenArtifact}
          />
        ) : null;
      }
      case "approval": {
        const a = state.approvals[key];
        if (!a) return null;
        const onDecide = onApprovalDecision
          ? (aid: string, ap: boolean, c?: string) => onApprovalDecision(state.runId, aid, ap, c)
          : undefined;
        return <ApprovalCard key={k} approval={a} onDecide={onDecide} inherited={inherited} />;
      }
      case "result":
        return state.result ? <Conclusion key={k} text={state.result.text} /> : null;
      case "toolResult":
        return null; // 观察结果已并入对应 ToolRow
      default:
        return null;
    }
  };

  // 连续的 toolCall/task（含穿插其间的 toolResult，渲染为 null 不断轨）聚成一条步骤轨：
  // 左侧细线 + 紧凑行距，多工具 run 呈 Claude 风时间线而非卡片墙。
  const nodes: ReactNode[] = [];
  let group: ReactNode[] = [];
  let groupKey = "";
  const flushGroup = () => {
    if (group.length > 0) {
      nodes.push(
        <div key={`grp:${groupKey}`} className="ml-1 space-y-0.5 border-l border-border pl-3">
          {group}
        </div>,
      );
    }
    group = [];
    groupKey = "";
  };
  state.order.forEach((entry, idx) => {
    const k = `${entry.kind}:${entry.key}:${idx}`;
    const groupable = entry.kind === "toolCall" || entry.kind === "task" || entry.kind === "toolResult";
    const node = renderEntry(entry.kind, entry.key, k);
    if (groupable) {
      if (!groupKey) groupKey = k;
      if (node) group.push(node);
    } else {
      flushGroup();
      if (node) nodes.push(node);
    }
  });
  flushGroup();

  return (
    <div className="space-y-3">
      {query && <UserBubble text={query} />}
      <AttachmentRow attachments={attachments} onOpenArtifact={onOpenArtifact} />
      {nodes}
      {state.unknown.length > 0 && (
        <div className="text-xs text-muted-foreground">
          （{state.unknown.length} 条暂不支持的事件，已忽略）
        </div>
      )}
    </div>
  );
});
