// 工作区「动态」页签：顶部产物翻页器（驱动 ArtifactPreview）+ 下方时序 feed
// （artifact 项 / web_search 来源组，图片项带缩略）。focus(sources) 由父层传 scrollTo 滚到对应组。
// 预览/feed 之间为可拖拽分隔条（比例持久化 uiPrefs.workspaceSplit）；
// 双击分隔条 = 预览最大化（feed 收起成摘要行，可点回）。

import { useEffect, useMemo, useRef, useState } from "react";
import { BarChart3, ChevronLeft, ChevronRight, FileText, Globe, Image as ImageIcon } from "lucide-react";
import type { ArtifactRef } from "../../lib/sse/frameTypes";
import { sourceHostname } from "../../lib/sse/toolPayloads";
import { clampWorkspaceSplit, loadUiPrefs, saveUiPrefs } from "../../lib/uiPrefs";
import { isChartArtifact, type ActivityItem } from "../../lib/workspaceFeed";
import { ArtifactPreview, useAuthedObjectUrl } from "../ArtifactWorkspace";
import { ScrollArea } from "@/components/ui/scroll-area";

function human(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

// feed 里的 artifact 行：类型图标（图表/图片/文件）+ 文件名 + 大小；图片带缩略；点击切 pager。
function ArtifactRow({
  art,
  toolName,
  active,
  onClick,
}: {
  art: ArtifactRef;
  toolName?: string;
  active: boolean;
  onClick: () => void;
}) {
  const name = art.fileName || art.name;
  const mime = art.mimeType || "";
  const isImg = mime.startsWith("image/");
  const thumb = useAuthedObjectUrl(`/artifacts/${art.resourceKey}`, isImg && !art.missing);
  const Icon = isChartArtifact({ name, mimeType: mime }) ? BarChart3 : isImg ? ImageIcon : FileText;
  return (
    <button
      onClick={onClick}
      title={name}
      className={`flex w-full items-center gap-2 rounded-lg border px-2 py-1.5 text-left transition-colors ${
        active ? "border-primary/50 bg-primary/10" : "border-border/60 hover:border-border hover:bg-accent/50"
      }`}
    >
      <span className="flex size-8 shrink-0 items-center justify-center overflow-hidden rounded bg-muted">
        {isImg && thumb ? (
          <img src={thumb} alt="" className="size-full object-cover" />
        ) : (
          <Icon className="size-4 text-muted-foreground" />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs text-foreground">{name}</span>
        <span className="block truncate text-[10px] text-muted-foreground">
          {human(art.size)}
          {toolName ? ` · ${toolName}` : ""}
        </span>
      </span>
    </button>
  );
}

function SourcesGroup({ item }: { item: Extract<ActivityItem, { kind: "sources" }> }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
        <Globe className="size-3.5 shrink-0" />
        <span className="min-w-0 truncate">{item.query || "网页搜索"}</span>
        <span className="shrink-0 text-[10px] text-muted-foreground/70">{item.sources.length} 个来源</span>
      </div>
      {item.sources.map((s, i) => (
        <a
          key={`${s.url}-${i}`}
          href={s.url}
          target="_blank"
          rel="noopener noreferrer"
          className="block rounded-lg border border-border/60 px-2 py-1.5 transition-colors hover:border-border hover:bg-accent/50"
        >
          <span className="block truncate text-xs text-foreground">{s.title}</span>
          <span className="block truncate text-[10px] text-muted-foreground">{sourceHostname(s.url)}</span>
          {s.snippet && (
            <span className="mt-0.5 line-clamp-2 block text-[11px] leading-snug text-muted-foreground/80">
              {s.snippet}
            </span>
          )}
        </a>
      ))}
    </div>
  );
}

export function ActivityTab({
  artifacts,
  activity,
  scrollTo,
  onScrolled,
}: {
  artifacts: ArtifactRef[];
  activity: ActivityItem[];
  scrollTo: string | null; // sources 组的 toolCallId
  onScrolled: () => void;
}) {
  // 翻页器：null=跟随最新（"生成即所见"），新产物到达自动回落最新。
  const [idx, setIdx] = useState<number | null>(null);
  const prevLen = useRef(artifacts.length);
  useEffect(() => {
    if (artifacts.length > prevLen.current) setIdx(null);
    prevLen.current = artifacts.length;
  }, [artifacts.length]);
  const current = idx == null ? artifacts.length - 1 : Math.min(idx, artifacts.length - 1);
  const indexByKey = useMemo(
    () => new Map(artifacts.map((a, i) => [a.resourceKey, i])),
    [artifacts],
  );

  // focus(sources)：滚到对应来源组
  const groupRefs = useRef(new Map<string, HTMLDivElement>());
  useEffect(() => {
    if (!scrollTo) return;
    groupRefs.current.get(scrollTo)?.scrollIntoView({ behavior: "smooth", block: "start" });
    onScrolled();
  }, [scrollTo, onScrolled]);

  // 预览/feed 分隔：split=预览占比（uiPrefs 持久化）；maximized=预览最大化（feed 收起成摘要行）。
  const [split, setSplit] = useState(() => clampWorkspaceSplit(loadUiPrefs().workspaceSplit));
  const [maximized, setMaximized] = useState(false);
  const splitRef = useRef(split);
  splitRef.current = split;
  const containerRef = useRef<HTMLDivElement>(null);
  // 拖拽：仿 ChatView 右 dock 宽度拖拽的 pointer 捕获模式，纵向变体（Δy/容器高 → 比例增量）。
  const dragRef = useRef<{ startY: number; startSplit: number; height: number } | null>(null);
  const onDragStart = (e: React.PointerEvent<HTMLDivElement>) => {
    if (maximized) return; // 最大化时无 feed 可分，仅响应双击恢复
    e.preventDefault();
    const height = containerRef.current?.getBoundingClientRect().height ?? 0;
    if (height <= 0) return;
    dragRef.current = { startY: e.clientY, startSplit: splitRef.current, height };
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
    setSplit(clampWorkspaceSplit(d.startSplit + (e.clientY - d.startY) / d.height)); // 向下拖=预览变高
  };
  const onDragEnd = () => {
    if (!dragRef.current) return;
    dragRef.current = null;
    saveUiPrefs({ ...loadUiPrefs(), workspaceSplit: splitRef.current });
  };

  if (artifacts.length === 0 && activity.length === 0)
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-sm leading-relaxed text-muted-foreground">
        Artifacts and search results appear here in real time
      </div>
    );

  const hasPreview = artifacts.length > 0;
  return (
    <div ref={containerRef} className="flex h-full min-h-0 flex-col">
      {hasPreview && (
        <div
          className="flex min-h-0 flex-col"
          style={{ flexGrow: maximized ? 1 : split, flexBasis: 0 }}
        >
          <div
            data-testid="artifact-pager"
            className="flex items-center justify-center gap-1 border-b px-2 py-1"
          >
            <button
              onClick={() => setIdx(Math.max(0, current - 1))}
              disabled={current <= 0}
              aria-label="Previous artifact"
              className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
            >
              <ChevronLeft className="size-4" />
            </button>
            <span className="text-xs text-muted-foreground tabular-nums">
              {current + 1}/{artifacts.length}
            </span>
            <button
              onClick={() => setIdx(Math.min(artifacts.length - 1, current + 1))}
              disabled={current >= artifacts.length - 1}
              aria-label="Next artifact"
              className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
            >
              <ChevronRight className="size-4" />
            </button>
          </div>
          <div className="min-h-0 flex-1">
            {artifacts[current] && <ArtifactPreview art={artifacts[current]} />}
          </div>
        </div>
      )}
      {hasPreview && (
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label="调整预览与动态高度（双击最大化预览）"
          onPointerDown={onDragStart}
          onPointerMove={onDragMove}
          onPointerUp={onDragEnd}
          onPointerCancel={onDragEnd}
          onLostPointerCapture={onDragEnd}
          onDoubleClick={() => setMaximized((m) => !m)}
          className="h-1 shrink-0 touch-none cursor-row-resize bg-border/40 transition-colors select-none hover:bg-primary/50"
        />
      )}
      {hasPreview && maximized ? (
        <button
          onClick={() => setMaximized(false)}
          className="flex shrink-0 items-center gap-1 px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
        >
          {activity.length} 条动态 ▸
        </button>
      ) : (
        <ScrollArea
          className="min-h-0"
          style={hasPreview ? { flexGrow: 1 - split, flexBasis: 0 } : { flexGrow: 1, flexBasis: 0 }}
        >
          <div className="space-y-2 p-2">
            {activity.map((item) =>
              item.kind === "artifact" ? (
                <ArtifactRow
                  key={item.art.resourceKey}
                  art={item.art}
                  toolName={item.toolName}
                  active={indexByKey.get(item.art.resourceKey) === current}
                  onClick={() => {
                    const i = indexByKey.get(item.art.resourceKey);
                    if (i != null) setIdx(i);
                  }}
                />
              ) : (
                <div
                  key={item.toolCallId}
                  ref={(el) => {
                    if (el) groupRefs.current.set(item.toolCallId, el);
                    else groupRefs.current.delete(item.toolCallId);
                  }}
                >
                  <SourcesGroup item={item} />
                </div>
              ),
            )}
          </div>
        </ScrollArea>
      )}
    </div>
  );
}
