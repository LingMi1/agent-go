import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, Check, GitBranch, ImagePlus, Loader2, PanelRight, Paperclip, RotateCw, Square, TriangleAlert, X } from "lucide-react";
import { MessageList } from "../components/chat";
import { Hero } from "../components/Hero";
import { WorkspacePanel, type WorkspaceFocus } from "../components/workspace/WorkspacePanel";
import { buildActivityFeed } from "../lib/workspaceFeed";
import type { ArtifactRef } from "../lib/sse/frameTypes";
import type { RunStatus, RunTurn } from "../hooks/useRunStream";
import { AGENT_TYPES, OUTPUT_FORMATS } from "../config";
import { uploadFileWithProgress, type AttachmentRef } from "../lib/api/client";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// 附件条目：选文件即上传（run body 只带引用），失败可重试。
interface PendingAttachment {
  id: string;
  file: File;
  status: "uploading" | "done" | "error";
  ref?: AttachmentRef;
  progress: number; // 0..1，上传进度（XHR onprogress 回填）
  previewUrl?: string; // 图片附件的本地 object URL（缩略图）
}

const ACCEPT = ".png,.jpg,.jpeg,.webp,.gif,.txt,.md,.markdown,.csv,.json,.xml,.yaml,.yml,.log,.pdf,.docx,.xlsx";

// 上传进度圆环：SVG 圆用 stroke-dashoffset 表示 0..1 逐渐占满（透明底 + 珊瑚橙进度）。
function ProgressRing({ pct }: { pct: number }) {
  const r = 9;
  const c = 2 * Math.PI * r;
  return (
    <svg viewBox="0 0 24 24" className="size-5 -rotate-90">
      <circle cx="12" cy="12" r={r} fill="none" strokeWidth="2.5" className="stroke-border" />
      <circle
        cx="12"
        cy="12"
        r={r}
        fill="none"
        strokeWidth="2.5"
        strokeLinecap="round"
        className="stroke-primary transition-[stroke-dashoffset] duration-150"
        strokeDasharray={c}
        strokeDashoffset={c * (1 - Math.max(0, Math.min(1, pct)))}
      />
    </svg>
  );
}

function AttachmentChips({
  items,
  onRemove,
  onRetry,
}: {
  items: PendingAttachment[];
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="mb-2 flex flex-wrap gap-2">
      {items.map((a) => {
        const err = a.status === "error";
        return (
          <div
            key={a.id}
            className={`group relative flex items-center gap-2 rounded-lg border py-1.5 pl-1.5 pr-7 ${
              err ? "border-destructive/40 bg-destructive/5" : "border-border bg-card"
            }`}
          >
            {/* 缩略图（图片）或文件角标；上传中盖一层进度圆环，完成盖绿对勾 */}
            <div className="relative flex size-9 shrink-0 items-center justify-center overflow-hidden rounded-md bg-accent/60">
              {a.previewUrl ? (
                <img src={a.previewUrl} alt={a.file.name} className="size-full object-cover" />
              ) : (
                <span className="text-[9px] uppercase text-muted-foreground">
                  {(a.file.name.split(".").pop() || "file").slice(0, 4)}
                </span>
              )}
              {a.status === "uploading" && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/45">
                  <ProgressRing pct={a.progress} />
                </div>
              )}
              {a.status === "done" && (
                <div className="absolute right-0 bottom-0 rounded-full bg-background/80 p-0.5">
                  <Check className="size-3 text-success" />
                </div>
              )}
            </div>
            <div className="min-w-0">
              <div className={`max-w-36 truncate text-xs ${err ? "text-destructive" : "text-foreground"}`}>
                {a.file.name}
              </div>
              <div className="text-[10px] text-muted-foreground/70">
                {a.status === "uploading"
                  ? `Uploading ${Math.round(a.progress * 100)}%`
                  : err
                    ? "Upload failed"
                    : "Ready"}
              </div>
            </div>
            {err && (
              <button
                onClick={() => onRetry(a.id)}
                title="Retry"
                className="absolute right-7 top-1/2 -translate-y-1/2 rounded p-1 text-destructive hover:text-destructive/75"
              >
                <RotateCw className="size-3" />
              </button>
            )}
            <button
              onClick={() => onRemove(a.id)}
              title="Remove"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground/70 hover:text-foreground"
            >
              <X className="size-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}

function Composer({
  disabled,
  running,
  onStop,
  placeholder,
  onSubmit,
  uploadSessionId,
  agentType,
  onAgentType,
}: {
  disabled: boolean;
  running: boolean;
  onStop: () => void;
  placeholder: string;
  onSubmit: (q: string, attachments?: AttachmentRef[], outputFormat?: string, imageGen?: boolean) => void;
  uploadSessionId: string;
  agentType: string;
  onAgentType: (v: string) => void;
}) {
  const [text, setText] = useState("");
  const [atts, setAtts] = useState<PendingAttachment[]>([]);
  const [format, setFormat] = useState("");
  const [imageGen, setImageGen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  // 卸载时回收所有未清的图片预览 object URL（避免切走 Composer 时泄漏）。
  const attsRef = useRef<PendingAttachment[]>([]);
  attsRef.current = atts;
  useEffect(() => () => attsRef.current.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl)), []);

  const doUpload = (item: PendingAttachment) => {
    setAtts((xs) => xs.map((x) => (x.id === item.id ? { ...x, status: "uploading", progress: 0 } : x)));
    uploadFileWithProgress(item.file, uploadSessionId, (pct) =>
      setAtts((xs) => xs.map((x) => (x.id === item.id ? { ...x, progress: pct } : x))),
    )
      .then((ref) =>
        setAtts((xs) => xs.map((x) => (x.id === item.id ? { ...x, status: "done", progress: 1, ref } : x))),
      )
      .catch(() => setAtts((xs) => xs.map((x) => (x.id === item.id ? { ...x, status: "error" } : x))));
  };

  const onPick = (files: FileList | null) => {
    for (const f of Array.from(files ?? [])) {
      // 图片给个本地缩略图（object URL 在移除/发送时回收，见 removeAtt/submit）。
      const previewUrl = f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined;
      const item: PendingAttachment = {
        id: `${Date.now()}-${f.name}`,
        file: f,
        status: "uploading",
        progress: 0,
        previewUrl,
      };
      setAtts((xs) => [...xs, item]);
      doUpload(item);
    }
    if (fileRef.current) fileRef.current.value = ""; // 允许再次选择同一文件
  };

  const uploading = atts.some((a) => a.status === "uploading");
  const submit = () => {
    const q = text.trim();
    if (!q || disabled || uploading) return;
    const refs = atts.filter((a) => a.status === "done" && a.ref).map((a) => a.ref!) ;
    // 格式仅在选择器可用时才随请求发送：快速模式且未开生图时格式选择器被禁用，
    // 但残留值不该继续透传（评审#11）。
    const fmtApplicable = agentType !== "react" || imageGen;
    onSubmit(q, refs.length ? refs : undefined, (fmtApplicable && format) || undefined, imageGen || undefined);
    setText("");
    atts.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
    setAtts([]);
  };
  return (
    <div className="px-4 pb-4">
      {/* UX-1 胶囊 Composer（对齐 Claude 官网）：圆角卡片内 输入区 + 底部控制行；
          模式/格式选择器内嵌于此（顶栏精简）。IME Enter 守卫原样保留。 */}
      <div className="mx-auto max-w-3xl rounded-3xl border bg-card shadow-sm transition-all focus-within:border-primary/40 focus-within:shadow-md">
        <div className="px-3 pt-2">
          <AttachmentChips
            items={atts}
            onRemove={(id) =>
              setAtts((xs) => {
                const t = xs.find((x) => x.id === id);
                if (t?.previewUrl) URL.revokeObjectURL(t.previewUrl);
                return xs.filter((x) => x.id !== id);
              })
            }
            onRetry={(id) => {
              const item = atts.find((x) => x.id === id);
              if (item) doUpload(item);
            }}
          />
        </div>
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              // 输入法组合期间的 Enter 只用于选词上屏（中文打英文单词等），不发送。
              // isComposing 为标准判定；keyCode 229 兜 Safari/旧 Chromium 在
              // compositionend 前后派发 keydown 的时序差（对齐 ChatGPT/Claude 行为）。
              if (e.nativeEvent.isComposing || e.keyCode === 229) return;
              e.preventDefault();
              submit();
            }
          }}
          rows={2}
          placeholder={placeholder}
          disabled={disabled}
          className="min-h-0 resize-none border-0 bg-transparent shadow-none focus-visible:ring-0 dark:bg-transparent"
        />
        <div className="flex items-center gap-1 px-2 pb-2">
          <input
            ref={fileRef}
            type="file"
            multiple
            accept={ACCEPT}
            className="hidden"
            onChange={(e) => onPick(e.target.files)}
          />
          <Button
            variant="ghost"
            size="icon"
            title="Upload attachments (images for multimodal; text/PDF auto-indexed into your knowledge base)"
            disabled={disabled}
            onClick={() => fileRef.current?.click()}
            className="text-muted-foreground hover:text-foreground"
          >
            <Paperclip />
          </Button>
          {/* 生图开关：置位则本轮走生图指令（上传图→图生图；配合下方格式把图嵌入网页/文档）。 */}
          <Button
            variant="ghost"
            size="icon"
            title={imageGen ? "Image gen enabled (click to disable)" : "Enable image generation for this turn"}
            aria-pressed={imageGen}
            onClick={() => setImageGen((v) => !v)}
            className={imageGen ? "text-primary hover:text-primary" : "text-muted-foreground hover:text-foreground"}
          >
            <ImagePlus />
          </Button>
          {/* 输出格式：非快速模式可选；**生图开启时全模式可选**（把图嵌入网页/文档）。 */}
          <Select value={format || "free"} onValueChange={(v) => setFormat(v === "free" ? "" : v)}>
            <SelectTrigger
              size="sm"
              disabled={agentType === "react" && !imageGen}
              title={agentType === "react" && !imageGen ? "Quick mode doesn't specify output format (available with image gen)" : "Output format"}
              className="w-fit shrink-0 border-0 bg-transparent text-muted-foreground shadow-none hover:text-foreground"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {OUTPUT_FORMATS.map((f) => (
                <SelectItem key={f.value || "free"} value={f.value || "free"}>
                  {f.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="ml-auto flex items-center gap-1.5">
            <Select value={agentType} onValueChange={onAgentType}>
              <SelectTrigger
                size="sm"
                title="Reasoning mode"
                className="w-fit border-0 bg-transparent text-muted-foreground shadow-none hover:text-foreground"
              >
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
            {running ? (
              <Button
                onClick={onStop}
                size="icon"
                title="Stop"
                className="rounded-full bg-foreground/85 text-background hover:bg-foreground/70"
              >
                <Square className="fill-current" />
              </Button>
            ) : (
              <Button
                onClick={submit}
                disabled={disabled || uploading}
                size="icon"
                title={uploading ? "Uploading…" : "Send (Enter)"}
                className="rounded-full bg-primary text-primary-foreground hover:bg-primary/85"
              >
                {uploading ? <Loader2 className="animate-spin" /> : <ArrowUp />}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// M7：单 RunState 视图升级为 timeline+live 多轮会话视图。
// 历史轮次只读堆叠展示；Composer 仅在「载入历史/运行中」时禁用——载入完即可继续对话。
export function ChatView({
  visible,
  timeline,
  live,
  status,
  loadingHistory,
  onSubmit,
  uploadSessionId,
  agentType,
  onAgentType,
  artifactsOpen,
  onArtifactsOpenChange,
  artifactsWidth,
  onArtifactsWidthChange,
  onApprovalDecision,
  onStop,
  onForkTurn,
}: {
  visible: boolean;
  timeline: RunTurn[];
  live: RunTurn | null;
  status: RunStatus;
  loadingHistory: boolean;
  onSubmit: (q: string, attachments?: AttachmentRef[], outputFormat?: string, imageGen?: boolean) => void;
  uploadSessionId: string;
  agentType: string;
  onAgentType: (v: string) => void;
  artifactsOpen: boolean;
  onArtifactsOpenChange: (open: boolean) => void;
  artifactsWidth: number;
  onArtifactsWidthChange: (w: number) => void;
  onApprovalDecision?: (
    runId: string,
    approvalId: string,
    approved: boolean,
    comment?: string,
  ) => Promise<boolean> | void;
  onStop: () => void;
  // docs/14 会话分叉：从某轮之后分叉（组合层调 forkSession → 刷列表 → 切入新会话）。
  onForkTurn?: (afterRunId: string) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!visible) return; // display:none 期间 scrollHeight=0，滚动无意义；切回可见(visible 变 true)时重滚
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [visible, timeline.length, live?.state.order.length, live?.state.result?.text, status]);

  const empty = timeline.length === 0 && !live && !loadingHistory;
  // artifact 工作区聚合整个会话（历史轮 + 当前轮）的产物。useMemo 依赖数组标识：
  // reducer 只在产物帧到达时才替换 artifacts 数组，token 帧不会触发重算/工作区重渲。
  const liveArtifacts = live?.state.artifacts;
  const artifacts: ArtifactRef[] = useMemo(
    () => [...timeline.flatMap((t) => t.state.artifacts), ...(liveArtifacts ?? [])],
    [timeline, liveArtifacts],
  );
  // 右 dock 的"上传内容(Content)"段：仅本会话上传的附件（timeline 各轮 + 当前 live 轮），按
  // resourceKey 去重。附件元数据仅当前会话实时轮携带（历史回放轮不含——M8 既有限制，
  // 载入旧会话时此段为空）。与产物同为"仅本对话"作用域，区别于侧栏"产物"跨会话画廊。
  const liveAttachments = live?.attachments;
  const uploads: AttachmentRef[] = useMemo(() => {
    const seen = new Set<string>();
    const out: AttachmentRef[] = [];
    for (const a of [...timeline.flatMap((t) => t.attachments ?? []), ...(liveAttachments ?? [])]) {
      if (a.resourceKey && !seen.has(a.resourceKey)) {
        seen.add(a.resourceKey);
        out.push(a);
      }
    }
    return out;
  }, [timeline, liveAttachments]);

  // 当前 run 产出新产物 → 自动展开 Files 面板。跟踪 live 轮产物数（而非聚合）：
  // 覆盖"产物随终帧 flush 与 status=done 同批到达"的情形（此时聚合+status 守卫会漏），
  // 且载入历史只填 timeline 不动 live → 不误触发。
  const liveCount = liveArtifacts?.length ?? 0;
  const prevLiveRef = useRef(0);
  useEffect(() => {
    if (liveCount > prevLiveRef.current && !artifactsOpen) {
      onArtifactsOpenChange(true);
    }
    prevLiveRef.current = liveCount;
  }, [liveCount, artifactsOpen, onArtifactsOpenChange]);

  // onClose 稳定引用：否则内联箭头每帧重建 → WorkspacePanel 的 memo 失效、流式期每帧重渲。
  const closeArtifacts = useCallback(() => onArtifactsOpenChange(false), [onArtifactsOpenChange]);

  // 智能体工作区「动态」feed：搜索来源组 + 产物时序。依赖数组标识（reducer 仅在
  // tool_result/产物帧替换 toolResults/artifacts 数组），token 帧不触发重算。
  const liveToolResults = live?.state.toolResults;
  const liveState = live?.state;
  const activity = useMemo(
    () => buildActivityFeed([...timeline.map((t) => t.state), ...(liveState ? [liveState] : [])]),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- liveState 每帧换标识，仅在关键数组换时重算
    [timeline, liveArtifacts, liveToolResults],
  );

  // ToolRow → 工作区联动：查看来源切「动态」tab、查看产物切「文件」tab（并确保 dock 打开）。
  const [wsFocus, setWsFocus] = useState<WorkspaceFocus | null>(null);
  const openSources = useCallback(
    (toolCallId: string) => {
      setWsFocus({ kind: "sources", toolCallId });
      onArtifactsOpenChange(true);
    },
    [onArtifactsOpenChange],
  );
  const openArtifact = useCallback(
    (resourceKey: string) => {
      setWsFocus({ kind: "artifact", resourceKey });
      onArtifactsOpenChange(true);
    },
    [onArtifactsOpenChange],
  );
  const consumeFocus = useCallback(() => setWsFocus(null), []);

  // 面板宽度拖拽（左缘把手）：pointer 捕获 + 实时回写（App 端 clamp+持久化）。
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);
  const onDragStart = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    dragRef.current = { startX: e.clientX, startW: artifactsWidth };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onDragMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    if (e.buttons === 0) {
      // 指针已松开却仍在移动（capture 被 pointercancel 抢走等）→ 结束，勿追踪裸悬停。
      dragRef.current = null;
      return;
    }
    onArtifactsWidthChange(d.startW + (d.startX - e.clientX)); // 向左拖=变宽
  };
  const onDragEnd = () => {
    dragRef.current = null;
  };

  const composerDisabled = loadingHistory || status === "running";
  const placeholder = loadingHistory
    ? "Loading session history…"
    : status === "running"
      ? "Task in progress… continue after completion"
      : "Ask anything, Enter to send, Shift+Enter for new line";

  const dockCount = artifacts.length + uploads.length;
  return (
    <div className="flex min-w-0 flex-1">
      <div className="relative flex min-w-0 flex-1 flex-col">
        {/* 右上角开关：dock 关闭时可再次打开"本对话 Artifacts 与上传内容"（对齐参考图；仅 lg+，dock 亦仅 lg+）。 */}
        {!artifactsOpen && (
          <div className="absolute right-2 top-2 z-10 hidden lg:block">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => onArtifactsOpenChange(true)}
                  aria-label="Open agent workspace"
                  className="relative size-8 text-muted-foreground hover:text-foreground"
                >
                  <PanelRight />
                  {dockCount > 0 && (
                    <span className="absolute -right-0.5 -top-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-primary px-1 text-[9px] font-medium text-primary-foreground">
                      {dockCount}
                    </span>
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>Agent Workspace</TooltipContent>
            </Tooltip>
          </div>
        )}
        <div ref={scrollRef} className="flex-1 overflow-auto p-4">
          {empty ? (
            <Hero onAsk={(q) => onSubmit(q)} />
          ) : (
            <div className="mx-auto max-w-3xl">
              {timeline.map((turn, i) => {
                // docs/14：分叉锚点只对**已终态轮**开放（RUNNING 快照未收口，服务端也会 409）。
                // 终态判据：回放轮看服务端 status（SUCCESS/FAILED/STOPPED/TIMEOUT 皆可分叉）；
                // 实时归档轮无 status，以"见过 finish 帧"兜底。
                const terminal = turn.state.finished || (!!turn.status && turn.status !== "RUNNING");
                return (
                  <div key={turn.runId} className="group/turn mb-6">
                    {turn.inherited && (
                      <div
                        className="mb-1 flex items-center gap-1 text-[10px] text-muted-foreground/70"
                        title="Inherited turns from parent session (read-only, preserved events)"
                      >
                        <GitBranch className="size-3" />
                        Inherited
                      </div>
                    )}
                    <MessageList
                      state={turn.state}
                      query={turn.query}
                      attachments={turn.attachments}
                      inherited={turn.inherited}
                      onOpenSources={openSources}
                      onOpenArtifact={openArtifact}
                      // docs/14 §4.4：审批决议属于父会话时间线。继承轮的 pending 审批卡若可
                      // 操作，决议 run 会落回父会话（分叉视图刷新后消失）——继承轮一律只读。
                      onApprovalDecision={i === timeline.length - 1 && !live && !turn.inherited ? onApprovalDecision : undefined}
                    />
                    {turn.failed && (
                      <div className="mt-1 flex items-center gap-1 text-xs text-warning">
                        <TriangleAlert className="size-3.5 shrink-0" />
                        This turn did not reach a terminal state (cancelled/error/still running). Only persisted events are shown.
                      </div>
                    )}
                    {/* hover 操作区：从此轮分叉（继承轮同样可为锚——"分叉的分叉"）。 */}
                    {onForkTurn && terminal && status !== "running" && (
                      <div className="mt-1 flex justify-end opacity-0 transition-opacity group-hover/turn:opacity-100">
                        <button
                          data-testid="fork-turn"
                          title="Fork from here"
                          onClick={() => onForkTurn(turn.runId)}
                          className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground/70 hover:bg-accent/60 hover:text-foreground"
                        >
                          <GitBranch className="size-3.5" />
                          Fork from here
                        </button>
                      </div>
                    )}
                    {/* 分叉分界线：最后一条继承轮与首条 own 轮之间（own 轮未产生时垫在时间线尾）。 */}
                    {turn.inherited && !timeline[i + 1]?.inherited && (
                      <div
                        data-testid="fork-divider"
                        className="mt-5 flex items-center gap-2 text-[11px] text-muted-foreground/70"
                      >
                        <div className="h-px flex-1 bg-border" />
                        <span className="flex items-center gap-1">
                          <GitBranch className="size-3" />
                          Fork from here
                        </span>
                        <div className="h-px flex-1 bg-border" />
                      </div>
                    )}
                  </div>
                );
              })}
              {live && (
                <MessageList
                  state={live.state}
                  query={live.query}
                  attachments={live.attachments}
                  running={status === "running"}
                  onApprovalDecision={onApprovalDecision}
                  onOpenSources={openSources}
                  onOpenArtifact={openArtifact}
                />
              )}
              {live?.failed && status !== "running" && (
                // 中断/未终态的当前轮：显式标记，否则冻结的部分回答与"完成"无从区分。
                <div className="mt-1 flex items-center gap-1 text-xs text-warning">
                  <TriangleAlert className="size-3.5 shrink-0" />
                  Stopped. This turn did not reach a terminal state.
                </div>
              )}
              {loadingHistory && (
                <div className="mt-3 space-y-2">
                  <div className="flex items-center gap-2 text-sm text-muted-foreground/70">
                    <span className="thinking-dots"><span /><span /><span /></span>
                    Loading session history…
                  </div>
                  <Skeleton className="h-16 w-3/4" />
                  <Skeleton className="h-10 w-1/2" />
                </div>
              )}
              {status === "running" && (
                <div data-testid="run-indicator" className="mt-3 flex items-center gap-2 text-sm text-muted-foreground/70">
                  <span className="thinking-dots"><span /><span /><span /></span>
                  Running…
                </div>
              )}
              {status === "error" && (
                <div className="mt-3 text-sm text-destructive">Run error. Check health status and try again.</div>
              )}
            </div>
          )}
        </div>
        <Composer
          disabled={composerDisabled}
          running={status === "running"}
          onStop={onStop}
          placeholder={placeholder}
          onSubmit={onSubmit}
          uploadSessionId={uploadSessionId}
          agentType={agentType}
          onAgentType={onAgentType}
        />
      </div>
      {artifactsOpen && (
        <>
          <div
            role="separator"
            aria-orientation="vertical"
            title="Drag to resize"
            onPointerDown={onDragStart}
            onPointerMove={onDragMove}
            onPointerUp={onDragEnd}
            onPointerCancel={onDragEnd}
            onLostPointerCapture={onDragEnd}
            className="hidden w-1 shrink-0 touch-none cursor-col-resize bg-border/40 transition-colors select-none hover:bg-primary/50 lg:block"
          />
          {/* max-w 兜底：极窄窗口下即使存量宽度值偏大，dock 也不越过视口（主修复是父链 min-w-0）。 */}
          <div style={{ width: artifactsWidth }} className="hidden max-w-[70vw] shrink-0 lg:block">
            <WorkspacePanel
              artifacts={artifacts}
              uploads={uploads}
              activity={activity}
              focus={wsFocus}
              onFocusConsumed={consumeFocus}
              onClose={closeArtifacts}
            />
          </div>
        </>
      )}
    </div>
  );
}
