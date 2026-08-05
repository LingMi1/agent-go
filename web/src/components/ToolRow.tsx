// 紧凑工具状态行：每次工具调用一行（图标 + 动词短语 + 状态），点击展开入参/观察结果。
// 取代旧 ToolCard 的"每个工具一张大卡"——多工具 run 的时间线从卡片墙变成 Claude 风步骤轨。
// web_search 成功另渲染来源迷你卡、image_generate 成功渲染缩略图条。

import { useState } from "react";
import {
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Globe,
  Hand,
  Loader2,
} from "lucide-react";
import type { ArtifactRef, ToolCallView } from "../lib/sse/frameTypes";
import { parseWebSearchResult, sourceHostname, stripWebSearchJson } from "../lib/sse/toolPayloads";
import { toolMeta } from "../lib/toolPresentation";
import { ProviderTag } from "./common";
import { useAuthedObjectUrl } from "./ArtifactWorkspace";

function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case "running":
      return <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" />;
    case "success":
      return <CircleCheck className="size-3.5 shrink-0 text-success" />;
    case "failed":
      return <CircleAlert className="size-3.5 shrink-0 text-destructive" />;
    case "awaiting_approval":
      return <Hand className="size-3.5 shrink-0 text-warning" />;
    default:
      return <Loader2 className="size-3.5 shrink-0 text-muted-foreground" />;
  }
}

function ThinkingDots() {
  return (
    <span className="thinking-dots">
      <span />
      <span />
      <span />
    </span>
  );
}

// 图片缩略图（hook 不能进循环体 → 独立子组件）。/artifacts 需 X-User-Id 头，走 blob URL。
function ArtifactThumb({ art, onOpen }: { art: ArtifactRef; onOpen?: (resourceKey: string) => void }) {
  const objUrl = useAuthedObjectUrl(`/artifacts/${art.resourceKey}`, !art.missing);
  if (art.missing) return null;
  return (
    <button
      type="button"
      title={art.fileName || art.name}
      onClick={() => onOpen?.(art.resourceKey)}
      className="h-16 shrink-0 overflow-hidden rounded-md border transition-opacity hover:opacity-80"
    >
      {objUrl ? (
        <img src={objUrl} alt={art.fileName || art.name} className="h-full w-auto object-cover" />
      ) : (
        <div className="h-full w-24 animate-pulse bg-secondary" />
      )}
    </button>
  );
}

export function ToolRow({
  call,
  resultText,
  artifacts,
  onOpenSources,
  onOpenArtifact,
  defaultOpen,
}: {
  call: ToolCallView;
  resultText?: string;
  artifacts?: ArtifactRef[];
  onOpenSources?: (toolCallId: string) => void;
  onOpenArtifact?: (resourceKey: string) => void;
  defaultOpen?: boolean;
}) {
  // failed 默认展开（错误必须一眼可见），其余默认收起。
  const [open, setOpen] = useState(defaultOpen ?? call.status === "failed");
  const meta = toolMeta(call.toolName, call.toolProvider);
  const Icon = meta.icon;
  const running = call.status === "running" || call.status === "awaiting_approval";
  const verb =
    call.status === "failed"
      ? `${call.toolName || "工具"} 调用失败`
      : running
        ? meta.runningVerb(call.input)
        : meta.doneVerb(call.input, resultText);
  const target = meta.target?.(call.input);

  const search = meta.kind === "search" && call.status === "success" ? parseWebSearchResult(resultText) : null;
  const images =
    meta.kind === "image" && call.status === "success"
      ? (artifacts ?? []).filter((a) => a.mimeType.startsWith("image/"))
      : [];
  const displayResult =
    resultText != null && meta.kind === "search" ? stripWebSearchJson(resultText) : resultText;

  return (
    <div data-testid="tool-row">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="group flex min-h-8 w-full items-center gap-2 rounded-md px-1 py-0.5 text-left transition-colors hover:bg-accent/40"
      >
        <StatusIcon status={call.status} />
        <Icon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 shrink truncate text-sm text-foreground/85">
          {verb}
          {running && <ThinkingDots />}
        </span>
        {target && <span className="min-w-0 shrink truncate text-sm text-muted-foreground">{target}</span>}
        {/* mono 小工具名 chip：内容恒为 toolName 原文（e2e 定位锚点） */}
        <span className="shrink-0 font-mono text-[11px] text-muted-foreground/60">{call.toolName}</span>
        {(call.toolProvider === "mcp" || call.toolProvider === "skill") && (
          <ProviderTag provider={call.toolProvider} />
        )}
        <ChevronRight
          className={`ml-auto size-3.5 shrink-0 text-muted-foreground opacity-0 transition-all group-hover:opacity-100 ${open ? "rotate-90" : ""}`}
        />
      </button>

      {open && (
        <div className="ml-[1.4rem] space-y-2 border-l border-border py-1 pl-3 text-sm">
          {call.summary && <div className="text-xs text-muted-foreground">{call.summary}</div>}
          {call.input != null && (
            <div>
              <div className="mb-1 text-xs text-muted-foreground">入参</div>
              <pre className="max-h-64 overflow-auto rounded-md bg-code-bg p-2 font-mono text-xs">
                {typeof call.input === "string" ? call.input : JSON.stringify(call.input, null, 2)}
              </pre>
            </div>
          )}
          {call.errorMsg && <div className="text-xs text-destructive">错误：{call.errorMsg}</div>}
          {displayResult != null && displayResult !== "" && (
            <div>
              <div className="mb-1 text-xs text-muted-foreground">观察结果</div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-code-bg p-2 font-mono text-xs">
                {displayResult}
              </pre>
            </div>
          )}
        </div>
      )}

      {/* 搜索迷你卡：query + 来源 chips（最多 5 个）+ 查看全部来源 */}
      {search && (
        <div className="ml-[1.4rem] mt-0.5 border-l border-border pl-3">
          {search.query && <div className="mb-1 text-xs text-muted-foreground">「{search.query}」</div>}
          <div className="flex flex-wrap items-center gap-1.5">
            {search.results.slice(0, 5).map((r, i) => (
              <a
                key={`${r.url}:${i}`}
                href={r.url}
                target="_blank"
                rel="noreferrer noopener"
                title={r.title}
                className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs text-foreground/80 transition-colors hover:bg-accent"
              >
                <Globe className="size-3 shrink-0 text-muted-foreground" />
                <span className="max-w-36 truncate">{sourceHostname(r.url)}</span>
              </a>
            ))}
            {onOpenSources && search.results.length > 0 && (
              <button
                type="button"
                onClick={() => onOpenSources(call.toolCallId)}
                className="rounded-full px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                查看全部来源
              </button>
            )}
          </div>
        </div>
      )}

      {/* 图片缩略图条 */}
      {images.length > 0 && (
        <div className="ml-[1.4rem] mt-0.5 flex gap-2 overflow-x-auto border-l border-border pl-3">
          {images.map((a) => (
            <ArtifactThumb key={a.resourceKey} art={a} onOpen={onOpenArtifact} />
          ))}
        </div>
      )}
    </div>
  );
}
