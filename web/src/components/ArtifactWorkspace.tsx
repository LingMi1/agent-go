import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy, Download, TriangleAlert, X } from "lucide-react";
import { toast } from "sonner";
import type { ArtifactRef } from "../lib/sse/frameTypes";
import { downloadArtifact } from "../lib/api/client";
import { getUserId } from "../lib/identity";
import { DISPLAY_SLICE_LIMIT, formatChars, sliceForDisplay } from "../lib/textPreview";
import { isChartArtifact } from "../lib/workspaceFeed";
import { Markdown } from "./common";
import { EChartsPreview } from "./workspace/EChartsPreview";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

function human(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

// 同名文件分组序号（一次会话多次产出同名报告时，切换器可区分）。纯函数便于测试。
export function artifactLabels(artifacts: ArtifactRef[]): Map<string, string> {
  const total = new Map<string, number>();
  for (const a of artifacts) {
    const n = a.fileName || a.name;
    total.set(n, (total.get(n) ?? 0) + 1);
  }
  const seen = new Map<string, number>();
  const out = new Map<string, string>();
  for (const a of artifacts) {
    const n = a.fileName || a.name;
    const idx = (seen.get(n) ?? 0) + 1;
    seen.set(n, idx);
    out.set(a.resourceKey, (total.get(n) ?? 1) > 1 ? `${n}（第 ${idx} 份）` : n);
  }
  return out;
}

function isTextLike(mime: string, name: string) {
  return (
    mime.startsWith("text/") ||
    mime.includes("json") ||
    mime.includes("markdown") ||
    /\.(md|markdown|txt|csv|json|log|html)$/i.test(name)
  );
}

function isMarkdown(mime: string, name: string) {
  return mime.includes("markdown") || /\.(md|markdown)$/i.test(name);
}

function isHtml(mime: string, name: string) {
  return mime === "text/html" || /\.html?$/i.test(name);
}

// /artifacts 恒需 X-User-Id 头做 owner 校验（api.go），裸 <img src>/<iframe src> 无法携带 →
// 登入用户必 403。图片/PDF 预览改为带头 fetch 成 blob object URL。
export function useAuthedObjectUrl(url: string, enabled: boolean) {
  const [objUrl, setObjUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled || !url) {
      setObjUrl(null);
      return;
    }
    let alive = true;
    let created: string | null = null;
    setObjUrl(null);
    void fetch(url, { headers: { "X-User-Id": getUserId() || "anonymous" } })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
      .then((b) => {
        if (!alive) return;
        created = URL.createObjectURL(b);
        setObjUrl(created);
      })
      .catch(() => alive && setObjUrl(null));
    return () => {
      alive = false;
      if (created) URL.revokeObjectURL(created);
    };
  }, [url, enabled]);
  return objUrl;
}

function useArtifactText(url: string, enabled: boolean) {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setText(null);
    // 全量获取，绝不在 fetch 层截断——iframe 预览与「复制内容」必须拿到完整文本
    //（曾硬截 200KB：大 HTML 被截在内联 base64 图中间，预览/代码/复制全残缺）。
    // DOM 重的展示（<pre>/Markdown）由 FilePreview 做展示层截断并明确告知。
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

// 展示层截断横幅：告诉用户「只是没全部渲染出来」，产物/复制/下载都是完整的。
function TruncatedBanner({ totalChars }: { totalChars: number }) {
  return (
    <div className="mb-2 flex shrink-0 items-center gap-1.5 rounded-md border border-warning/25 bg-warning/10 px-2.5 py-1.5 text-xs text-warning">
      <TriangleAlert className="size-3.5 shrink-0" />
      内容过大，此处仅渲染前 {formatChars(DISPLAY_SLICE_LIMIT)} 字符（全文 {formatChars(totalChars)}
      ）；「复制内容 / 下载」均为完整文件。
    </div>
  );
}

function FilePreview({
  art,
  text,
  htmlMode = "preview",
}: {
  art: ArtifactRef;
  text: string | null;
  htmlMode?: "preview" | "code"; // ArtifactPreview 头部「代码 | 预览」切换驱动
}) {
  const url = `/artifacts/${art.resourceKey}`;
  const mime = art.mimeType || "";
  const name = art.fileName || art.name;
  const isImg = mime.startsWith("image/");
  const isPdf = mime === "application/pdf";
  // 带 X-User-Id 的 blob URL（img/pdf 无法在 src 上带头）。
  const objUrl = useAuthedObjectUrl(url, !art.missing && (isImg || isPdf));
  if (art.missing) return <div className="p-4 text-sm text-muted-foreground">文件已删除或不可用</div>;
  if (isImg)
    return (
      <div className="flex h-full items-start justify-center p-3">
        {objUrl ? (
          <img src={objUrl} alt={name} className="max-h-full max-w-full rounded-lg" />
        ) : (
          <Skeleton className="h-40 w-40" />
        )}
      </div>
    );
  if (isPdf)
    return objUrl ? (
      <iframe src={objUrl} title={name} className="h-full w-full rounded-lg bg-white" />
    ) : (
      <div className="p-3">
        <Skeleton className="h-96 w-full" />
      </div>
    );
  // ECharts 图表产物：交互渲染优先于 json 文本分支（内部自带懒加载/主题/回退）。
  if (isChartArtifact({ name, mimeType: mime })) return <EChartsPreview art={art} />;
  if (isTextLike(mime, name)) {
    if (text === null)
      return (
        <div className="space-y-2 p-3">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-5/6" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      );
    // 文档级预览美化：markdown 渲染成文档、html 进沙箱 iframe，而不是源码 dump。
    // 展示层截断只作用于 <pre>/Markdown 这类 DOM 重渲染；iframe srcDoc 恒用全量文本。
    const display = sliceForDisplay(text);
    if (isHtml(mime, name)) {
      if (htmlMode === "code")
        return (
          <div className="flex h-full min-h-0 flex-col">
            {display.truncated && <TruncatedBanner totalChars={display.totalChars} />}
            <pre className="min-h-0 flex-1 overflow-auto rounded-lg bg-code-bg p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-foreground/90">
              {display.shown}
            </pre>
          </div>
        );
      // allow-scripts：生成的交互网页/图表能跑；srcDoc 保持 opaque origin，
      // 且绝不加 allow-same-origin（同源组合=沙箱逃逸）；内部 fetch 无 X-User-Id 撞 403 墙。
      return <iframe srcDoc={text} title={name} sandbox="allow-scripts" className="h-full w-full rounded-lg bg-white" />;
    }
    if (isMarkdown(mime, name))
      return (
        <div className="flex h-full min-h-0 flex-col">
          {display.truncated && <div className="px-4 pt-3"><TruncatedBanner totalChars={display.totalChars} /></div>}
          <div className="min-h-0 flex-1 overflow-auto p-4">
            <Markdown>{display.shown}</Markdown>
          </div>
        </div>
      );
    return (
      <div className="flex h-full min-h-0 flex-col">
        {display.truncated && <TruncatedBanner totalChars={display.totalChars} />}
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap rounded-lg bg-code-bg p-3 text-xs leading-relaxed text-foreground/90">
          {display.shown}
        </pre>
      </div>
    );
  }
  return (
    <div className="p-4 text-sm text-muted-foreground">该类型（{mime || "未知"}）暂不支持内联预览，请下载查看。</div>
  );
}

// 单项预览（名称/大小 + 复制/下载 + 内联预览）。抽出供 dock 两段索引与画廊单项复用。
export function ArtifactPreview({ art }: { art: ArtifactRef }) {
  const [copied, setCopied] = useState(false);
  const [htmlMode, setHtmlMode] = useState<"preview" | "code">("preview");
  const mime = art.mimeType || "";
  const name = art.fileName || art.name;
  const textLike = !art.missing && isTextLike(mime, name);
  const htmlLike = !art.missing && isHtml(mime, name);
  const text = useArtifactText(textLike ? `/artifacts/${art.resourceKey}` : "", textLike);
  const copy = async () => {
    if (text == null) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("复制失败");
    }
  };
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b px-3 py-1.5 text-xs text-muted-foreground">
        <span className="min-w-0 flex-1 truncate">{name}</span>
        <span className="shrink-0">{human(art.size)}</span>
        {htmlLike && (
          <div data-testid="html-mode-toggle" className="flex shrink-0 items-center gap-0.5 rounded-md bg-muted p-0.5">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setHtmlMode("code")}
              className={`h-5 rounded px-1.5 text-[11px] ${
                htmlMode === "code" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground"
              }`}
            >
              代码
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setHtmlMode("preview")}
              className={`h-5 rounded px-1.5 text-[11px] ${
                htmlMode === "preview" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground"
              }`}
            >
              预览
            </Button>
          </div>
        )}
        {textLike && (
          <HeaderIcon label={copied ? "已复制" : "复制内容"} onClick={() => void copy()} disabled={text == null}>
            {copied ? <Check className="text-success" /> : <Copy />}
          </HeaderIcon>
        )}
        <HeaderIcon label="下载" onClick={() => void downloadArtifact(art.resourceKey, name)}>
          <Download />
        </HeaderIcon>
      </div>
      <div className="min-h-0 flex-1 p-2">
        <FilePreview art={art} text={textLike ? text : null} htmlMode={htmlMode} />
      </div>
    </div>
  );
}

function HeaderIcon({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          onClick={onClick}
          disabled={disabled}
          aria-label={label}
          className="size-7 text-muted-foreground hover:text-foreground"
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

// memo：ChatView 每个流式帧都会重渲，但 artifacts 数组只在产物变化时换引用，
// 工作区（含预览 iframe/图片）无关帧不重渲。onClose 由父组件 useCallback 稳定。
export const ArtifactWorkspace = memo(function ArtifactWorkspace({
  artifacts,
  onClose,
}: {
  artifacts: ArtifactRef[];
  onClose?: () => void; // FilesPanel 内嵌时由外层页签栏统一提供关闭
}) {
  const [active, setActive] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // 新产物到达即切到最新（"生成即所见"）：清掉手动选择，回落"最新一份"默认。
  const prevLenRef = useRef(artifacts.length);
  useEffect(() => {
    if (artifacts.length > prevLenRef.current) setActive(null);
    prevLenRef.current = artifacts.length;
  }, [artifacts.length]);
  const current = artifacts.find((a) => a.resourceKey === active) ?? artifacts[artifacts.length - 1];
  const labels = useMemo(() => artifactLabels(artifacts), [artifacts]);

  const mime = current?.mimeType || "";
  const name = current ? current.fileName || current.name : "";
  const textLike = current ? !current.missing && isTextLike(mime, name) : false;
  const text = useArtifactText(current ? `/artifacts/${current.resourceKey}` : "", textLike);

  const copy = async () => {
    if (text == null) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <div className="flex h-full flex-col bg-background">
      <div className="flex items-center gap-1.5 border-b px-2 py-2">
        {artifacts.length > 0 ? (
          <Select value={current?.resourceKey ?? ""} onValueChange={setActive}>
            <SelectTrigger size="sm" className="min-w-0 flex-1 border-0 bg-transparent shadow-none">
              <SelectValue placeholder="选择产物" />
            </SelectTrigger>
            <SelectContent>
              {artifacts.map((a) => (
                <SelectItem key={a.resourceKey} value={a.resourceKey}>
                  {labels.get(a.resourceKey)}
                  {a.missing ? "（不可用）" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <span className="flex-1 px-1 text-sm text-muted-foreground">产物</span>
        )}
        <span className="shrink-0 text-xs text-muted-foreground">
          {artifacts.length > 0 ? `${artifacts.length} 个` : ""}
        </span>
        {current && textLike && (
          <HeaderIcon label={copied ? "已复制" : "复制内容"} onClick={() => void copy()} disabled={text == null}>
            {copied ? <Check className="text-success" /> : <Copy />}
          </HeaderIcon>
        )}
        {current && (
          <HeaderIcon
            label="下载"
            onClick={() => void downloadArtifact(current.resourceKey, current.fileName || current.name)}
          >
            <Download />
          </HeaderIcon>
        )}
        {onClose && (
          <HeaderIcon label="关闭" onClick={onClose}>
            <X />
          </HeaderIcon>
        )}
      </div>
      {artifacts.length === 0 ? (
        <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">
          运行产出的文件（报告、图表等）
          <br />
          会出现在这里，可预览与下载。
        </div>
      ) : (
        current && (
          <>
            <div className="flex items-center gap-2 border-b px-3 py-1.5 text-xs text-muted-foreground">
              <span className="min-w-0 flex-1 truncate">{name}</span>
              <span>{human(current.size)}</span>
            </div>
            <div className="min-h-0 flex-1 p-2">
              <FilePreview art={current} text={textLike ? text : null} />
            </div>
          </>
        )
      )}
    </div>
  );
});
