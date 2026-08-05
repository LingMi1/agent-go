// ECharts 产物（echarts-option.json / *chart*.json）交互预览：
// - 文本获取与 ArtifactWorkspace.useArtifactText 同款（带 X-User-Id 头 fetch；不 export 故本地小实现，
//   避免与 ArtifactWorkspace ↔ workspace/ 形成循环 import）。
// - echarts 懒加载（vite manualChunks 单独分包），init 用 claude-light/claude-dark 主题（useTheme 驱动，
//   主题变化 dispose 重建）；ResizeObserver → resize；卸载 dispose。
// - 顶部「图表 | JSON」小切换；解析/渲染失败回退 JSON 文本 + 提示查看同批 chart.png。

import { useEffect, useMemo, useRef, useState } from "react";
import type { EChartsType } from "echarts/core";
import type { ArtifactRef } from "../../lib/sse/frameTypes";
import { getUserId } from "../../lib/identity";
import { sliceForDisplay } from "../../lib/textPreview";
import { useTheme } from "@/lib/theme";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

function useArtifactText(url: string, enabled: boolean) {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setText(null);
    // 全量获取，不在 fetch 层截断——大数据集的 echarts-option.json 截断会直接 JSON 解析失败
    //（与 ArtifactWorkspace 同一缺陷根源）；JSON 兜底视图的展示截断在渲染处做。
    void fetch(url, { headers: { "X-User-Id": getUserId() || "anonymous" } })
      .then((r) => r.text())
      .then((t) => alive && setText(t))
      .catch(() => alive && setText("（预览失败）"));
    return () => {
      alive = false;
    };
  }, [url, enabled]);
  return text;
}

type Parsed = { ok: true; option: Record<string, unknown> } | { ok: false };

function parseOption(text: string): Parsed {
  try {
    const o = JSON.parse(text) as unknown;
    if (o && typeof o === "object" && !Array.isArray(o) && (o as Record<string, unknown>).series) {
      return { ok: true, option: o as Record<string, unknown> };
    }
  } catch {
    // fallthrough
  }
  return { ok: false };
}

export function EChartsPreview({ art }: { art: ArtifactRef }) {
  const theme = useTheme();
  const [mode, setMode] = useState<"chart" | "json">("chart");
  const [renderError, setRenderError] = useState(false);
  const text = useArtifactText(`/artifacts/${art.resourceKey}`, !art.missing);
  const parsed = useMemo(() => (text == null ? null : parseOption(text)), [text]);
  const elRef = useRef<HTMLDivElement>(null);

  // 新文本到达重置渲染错误
  useEffect(() => setRenderError(false), [text]);

  const showChart = mode === "chart" && parsed?.ok === true && !renderError;

  useEffect(() => {
    if (!showChart || !parsed?.ok) return;
    const el = elRef.current;
    if (!el) return;
    let disposed = false;
    let chart: EChartsType | undefined;
    let ro: ResizeObserver | undefined;
    void import("@/lib/echartsSetup").then(({ echarts }) => {
      if (disposed) return;
      try {
        chart = echarts.init(el, theme === "dark" ? "claude-dark" : "claude-light");
        chart.setOption(parsed.option, { notMerge: true });
      } catch {
        chart?.dispose();
        chart = undefined;
        setRenderError(true);
        return;
      }
      ro = new ResizeObserver(() => {
        if (chart && !chart.isDisposed()) chart.resize();
      });
      ro.observe(el);
    });
    return () => {
      disposed = true;
      ro?.disconnect();
      chart?.dispose();
    };
  }, [showChart, parsed, theme]);

  if (art.missing) return <div className="p-4 text-sm text-muted-foreground">文件已删除或不可用</div>;
  if (text === null || parsed === null)
    return (
      <div className="space-y-2 p-3">
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-40 w-full" />
      </div>
    );

  const broken = !parsed.ok || renderError;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 border-b px-2 py-1">
        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
          {broken ? "图表数据无法渲染，已回退显示 JSON；可查看同批生成的 chart.png" : "交互图表"}
        </span>
        {!broken && (
          <div className="flex shrink-0 items-center gap-0.5 rounded-md bg-muted p-0.5">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setMode("chart")}
              className={`h-5 rounded px-1.5 text-[11px] ${
                mode === "chart" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground"
              }`}
            >
              图表
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setMode("json")}
              className={`h-5 rounded px-1.5 text-[11px] ${
                mode === "json" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground"
              }`}
            >
              JSON
            </Button>
          </div>
        )}
      </div>
      {showChart ? (
        <div className="min-h-0 flex-1 p-2">
          <div ref={elRef} className="h-full min-h-[240px] w-full" />
        </div>
      ) : (
        <pre className="min-h-0 flex-1 overflow-auto rounded-lg bg-code-bg p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-foreground/90">
          {sliceForDisplay(text).shown}
        </pre>
      )}
    </div>
  );
}
