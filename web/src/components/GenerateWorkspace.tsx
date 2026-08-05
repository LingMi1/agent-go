import { useEffect, useMemo, useRef, useState } from "react";
import { Brush, ImagePlus, Loader2, Paperclip, Sparkles, X } from "lucide-react";
import {
  listArtifacts,
  startRun,
  uploadFileWithProgress,
  type AttachmentRef,
  type OwnerArtifact,
} from "../lib/api/client";
import { iterFrames } from "../lib/api/stream";
import { getUserId } from "../lib/identity";
import { ArtifactWorkspace, useAuthedObjectUrl } from "./ArtifactWorkspace";
import { MaskEditor } from "./MaskEditor";
import type { ArtifactRef } from "../lib/sse/frameTypes";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";

// 生图工作区（B.5 + docs/12 inpaint）：输入提示词批量生图（文生图 / 上传底图做图生图 /
// 画布蒙版局部重绘），下方是生成历史网格；历史图可"编辑这张"直接作新底图（canvas 闭环）。
// 自包含——不复用 chat 的 useRunStream：自己起一个 run（专用会话）并把 SSE 读空到终态，
// 完成后刷新历史网格。蒙版走 /uploads 附件通道，与底图同 session 上传，由模型把文件名
// 抄进 image_generate 的 mask 参数（query 模板 + IMAGE_GEN_INSTRUCTION 驱动，见 docs/12 §4.1）。

const SIZES = [
  { value: "1024x1024", label: "方形 1:1" },
  { value: "1536x1024", label: "横向 3:2" },
  { value: "1024x1536", label: "纵向 2:3" },
];
// 每次生成用一个**唯一**会话（前缀 generate:），避免固定 thread 让 checkpoint 无界累积、
// 且"猫"的历史污染下一次"狗"的生成。前缀让侧栏把它们过滤出普通对话列表（见 sessions.ts）。
const GEN_SESSION_PREFIX = "generate:";
const newGenSession = () =>
  GEN_SESSION_PREFIX + Math.random().toString(36).slice(2) + Date.now().toString(36);
const PAGE = 40;

function toRef(a: OwnerArtifact): ArtifactRef {
  return {
    resourceKey: a.resourceKey,
    name: a.name || a.fileName,
    fileName: a.fileName || a.name,
    previewUrl: `/artifacts/${a.resourceKey}`,
    downloadUrl: `/artifacts/${a.resourceKey}`,
    mimeType: a.mimeType,
    size: a.size,
    missing: false,
  };
}

function HistThumb({ art, onClick }: { art: OwnerArtifact; onClick: () => void }) {
  const objUrl = useAuthedObjectUrl(`/artifacts/${art.resourceKey}`, true);
  return (
    <button onClick={onClick} className="overflow-hidden rounded-lg border border-border transition-colors hover:border-primary/50">
      {objUrl ? (
        <img src={objUrl} alt={art.fileName} className="aspect-square w-full object-cover" />
      ) : (
        <Skeleton className="aspect-square w-full" />
      )}
    </button>
  );
}

export function GenerateWorkspace() {
  const [prompt, setPrompt] = useState("");
  const [size, setSize] = useState("1024x1024");
  const [count, setCount] = useState("1");
  const [source, setSource] = useState<AttachmentRef | null>(null);
  const [sourcePreview, setSourcePreview] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [images, setImages] = useState<OwnerArtifact[] | null>(null);
  const [selected, setSelected] = useState<OwnerArtifact | null>(null);
  // 蒙版（docs/12 inpaint）：确认后的 PNG blob + 缩略图 objectURL + 编辑器开关。
  const [maskBlob, setMaskBlob] = useState<Blob | null>(null);
  const [maskPreview, setMaskPreview] = useState<string | null>(null);
  const [maskOpen, setMaskOpen] = useState(false);
  const [settingSource, setSettingSource] = useState(false); // "编辑这张"取图中
  const fileRef = useRef<HTMLInputElement>(null);
  const previewRef = useRef<string | null>(null);
  previewRef.current = sourcePreview;
  const maskPreviewRef = useRef<string | null>(null);
  maskPreviewRef.current = maskPreview;
  // 蒙版与底图必须同 session 上传（uploads/{owner}/{session}/ 同前缀，f874e15 隔离沿用）。
  const sourceSessionRef = useRef<string>("");
  useEffect(() => () => {
    // objectURL 必回收（f874e15 修过泄漏）。
    if (previewRef.current) URL.revokeObjectURL(previewRef.current);
    if (maskPreviewRef.current) URL.revokeObjectURL(maskPreviewRef.current);
  }, []);

  const refresh = () => {
    void listArtifacts(PAGE, undefined, "image/") // 服务端只取图片（防单页客户端过滤漏更旧的图）
      .then(setImages)
      .catch(() => setImages([]));
  };
  useEffect(refresh, []);

  const clearMask = () => {
    if (maskPreviewRef.current) URL.revokeObjectURL(maskPreviewRef.current);
    setMaskBlob(null);
    setMaskPreview(null);
  };

  // 上传入口与"编辑这张"共用：设底图 = 回收旧预览 + 旧蒙版作废（尺寸随底图）+ 新 session 上传。
  const setSourceFromFile = (f: File) => {
    setUploading(true);
    if (previewRef.current) URL.revokeObjectURL(previewRef.current); // 重选先回收旧预览（#17）
    const url = URL.createObjectURL(f);
    setSourcePreview(url);
    setSource(null);
    clearMask();
    sourceSessionRef.current = newGenSession();
    void uploadFileWithProgress(f, sourceSessionRef.current, () => {})
      .then((ref) => setSource(ref))
      .catch(() => {
        // 失败也必须回收本次 objectURL（对照成功/卸载路径的 revoke）：catch 只清 state 的话
        // previewRef 随渲染同步为 null，之后换底图/卸载都触达不到它 → 泄漏（评审 D3）。
        // 用局部 url 直接 revoke 不依赖 ref 时序；函数式更新加相等性守卫，避免误清
        // 并发新一次 setSourceFromFile 已设好的预览。
        toast.error("底图上传失败");
        URL.revokeObjectURL(url);
        setSourcePreview((cur) => (cur === url ? null : cur));
      })
      .finally(() => setUploading(false));
  };

  const onPickSource = (files: FileList | null) => {
    const f = files?.[0];
    if (fileRef.current) fileRef.current.value = "";
    if (!f) return;
    setSourceFromFile(f);
  };

  const clearSource = () => {
    if (sourcePreview) URL.revokeObjectURL(sourcePreview);
    setSource(null);
    setSourcePreview(null);
    clearMask(); // 蒙版依附底图，一起清
  };

  // 历史图作底图（canvas 闭环）：/artifacts 恒需 X-User-Id（裸 img src 必 403），
  // 带头 fetch 成 blob → File → 重走上传通道（附件白名单只认 uploads/ 前缀，docs/12 §4.5）。
  const useAsSource = async (art: OwnerArtifact) => {
    if (settingSource || uploading) return;
    setSettingSource(true);
    try {
      const res = await fetch(`/artifacts/${art.resourceKey}`, {
        headers: { "X-User-Id": getUserId() || "anonymous" },
      });
      if (!res.ok) throw new Error(String(res.status));
      const blob = await res.blob();
      const name = art.fileName || art.name || "source.png";
      setSourceFromFile(new File([blob], name, { type: blob.type || art.mimeType || "image/png" }));
      setSelected(null);
      toast.success("已设为底图，可继续画蒙版做局部重绘");
    } catch {
      toast.error("设为底图失败");
    } finally {
      setSettingSource(false);
    }
  };

  const onMaskConfirm = (blob: Blob) => {
    clearMask(); // 回收旧蒙版预览
    setMaskBlob(blob);
    setMaskPreview(URL.createObjectURL(blob));
    setMaskOpen(false);
  };

  const generate = async () => {
    const p = prompt.trim();
    if (!p || generating || uploading) return;
    setGenerating(true);
    const n = Number(count) || 1;
    try {
      // 蒙版随生成才上传（可能反复重画）：与底图同 session，文件名随机防白名单重名歧义。
      let maskRef: AttachmentRef | null = null;
      if (source && maskBlob) {
        const maskName = `mask-${Math.random().toString(36).slice(2, 8)}.png`;
        maskRef = await uploadFileWithProgress(
          new File([maskBlob], maskName, { type: "image/png" }),
          sourceSessionRef.current || newGenSession(),
          () => {},
        );
      }
      // 组合 query：显式指示生图 + 尺寸/张数提示；source 走图生图；蒙版文件名写得
      // 非常显眼——模型要把它抄进 image_generate 的 mask 参数（docs/12 §4.1）。
      const query =
        `生成图片（尺寸 ${size}，共 ${n} 张）：${p}` +
        (source ? ". Use my uploaded image as the base for modification (image-to-image)." : "") +
        (maskRef
          ? `使用蒙版文件 ${maskRef.fileName} 对底图做局部重绘（inpaint），蒙版透明区域=需要重绘的区域。`
          : "");
      const { reader } = await startRun({
        query,
        sessionId: newGenSession(), // 每次生成一个新 thread，互不污染（评审#1）
        agentType: "react",
        imageGen: true,
        attachments: source ? (maskRef ? [source, maskRef] : [source]) : undefined,
      });
      // 把流读空到终态（工作区不渲染对话，只需知道何时完成）。
      for await (const _ of iterFrames(reader)) {
        /* drain */
      }
      refresh();
    } catch {
      toast.error("生成失败，请重试");
    } finally {
      setGenerating(false);
    }
  };

  const hist = useMemo(() => images ?? [], [images]);

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b px-6 py-4">
          <Sparkles className="size-5 text-primary" />
          <h1 className="text-xl font-semibold tracking-tight">Image Generation</h1>
          <span className="text-xs text-muted-foreground/70">Text-to-image / Image-to-image / Inpaint (gpt-image-2 · low)</span>
        </div>

        <ScrollArea className="min-h-0 flex-1">
          <div className="mx-auto max-w-3xl px-6 py-5">
            {/* 生成表单 */}
            <div className="rounded-lg border bg-card p-3 shadow-sm">
              <Textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={3}
                data-testid="generate-prompt"
                placeholder="描述你想要的图片：主体 / 风格 / 构图 / 光影…"
                className="resize-none border-0 bg-transparent shadow-none focus-visible:ring-0 dark:bg-transparent"
              />
              {/* 底图（图生图）+ 蒙版（inpaint） */}
              {sourcePreview && (
                <div className="mb-2 flex flex-col gap-1.5 px-1">
                  <div className="flex items-center gap-2">
                    <img src={sourcePreview} alt="底图" className="size-12 rounded-md object-cover" />
                    <span className="text-xs text-muted-foreground">底图（将按提示词修改）</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      data-testid="edit-mask"
                      disabled={uploading}
                      onClick={() => setMaskOpen(true)}
                      className="gap-1 text-muted-foreground hover:text-foreground"
                    >
                      <Brush className="size-3.5" />
                      {maskBlob ? "重画蒙版" : "编辑蒙版"}
                    </Button>
                    <button onClick={clearSource} aria-label="移除底图" className="ml-auto text-muted-foreground/70 hover:text-foreground">
                      <X className="size-4" />
                    </button>
                  </div>
                  {maskPreview && (
                    <div className="flex items-center gap-2">
                      {/* 蒙版是"黑底+透明洞"，衬白底让洞可见。 */}
                      <img src={maskPreview} alt="蒙版" data-testid="mask-thumb" className="size-12 rounded-md border bg-white object-cover" />
                      <span className="text-xs text-muted-foreground">蒙版（透明区域=将被重绘）</span>
                      <button onClick={clearMask} aria-label="移除蒙版" data-testid="mask-remove" className="ml-auto text-muted-foreground/70 hover:text-foreground">
                        <X className="size-4" />
                      </button>
                    </div>
                  )}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-2 px-1">
                <input ref={fileRef} type="file" accept=".png,.jpg,.jpeg,.webp" className="hidden" onChange={(e) => onPickSource(e.target.files)} />
                <Button variant="ghost" size="sm" disabled={uploading} onClick={() => fileRef.current?.click()} className="gap-1.5 text-muted-foreground hover:text-foreground">
                  {uploading ? <Loader2 className="size-4 animate-spin" /> : <Paperclip className="size-4" />}
                  上传底图
                </Button>
                <Select value={size} onValueChange={setSize}>
                  <SelectTrigger size="sm" className="w-28"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {SIZES.map((s) => <SelectItem key={s.value} value={s.value}>{s.label}</SelectItem>)}
                  </SelectContent>
                </Select>
                <Select value={count} onValueChange={setCount}>
                  <SelectTrigger size="sm" className="w-20"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {["1", "2", "3", "4"].map((n) => <SelectItem key={n} value={n}>{n} 张</SelectItem>)}
                  </SelectContent>
                </Select>
                <Button data-testid="generate-submit" onClick={() => void generate()} disabled={!prompt.trim() || generating || uploading} className="ml-auto gap-1.5 bg-primary text-primary-foreground hover:bg-primary/85">
                  {generating ? <Loader2 className="size-4 animate-spin" /> : <ImagePlus className="size-4" />}
                  {generating ? "生成中…" : "生成"}
                </Button>
              </div>
            </div>

            {/* 历史网格 */}
            <div className="mt-6">
              <div className="mb-2 text-xs font-medium tracking-wide text-muted-foreground/70 uppercase">生成历史</div>
              {images === null ? (
                <div className="grid grid-cols-3 gap-3 sm:grid-cols-4">
                  {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="aspect-square rounded-lg" />)}
                </div>
              ) : hist.length === 0 ? (
                <div className="py-12 text-center text-sm text-muted-foreground/70">还没有生成过图片。在上面写提示词，点“生成”。</div>
              ) : (
                <div className="grid grid-cols-3 gap-3 sm:grid-cols-4" data-testid="generate-history">
                  {hist.map((a) => <HistThumb key={a.resourceKey} art={a} onClick={() => setSelected(a)} />)}
                </div>
              )}
            </div>
          </div>
        </ScrollArea>
      </div>

      {/* 详情预览（复用工作区）+ "编辑这张"闭环入口 */}
      {selected && (
        <div className="flex w-[420px] shrink-0 flex-col border-l">
          <div className="flex items-center gap-2 border-b px-3 py-2">
            <span className="min-w-0 flex-1 truncate text-sm text-foreground">{selected.fileName || selected.name}</span>
            <Button
              variant="ghost"
              size="sm"
              data-testid="use-as-source"
              disabled={settingSource || uploading}
              onClick={() => void useAsSource(selected)}
              className="gap-1 text-muted-foreground hover:text-foreground"
            >
              {settingSource ? <Loader2 className="size-3.5 animate-spin" /> : <Brush className="size-3.5" />}
              编辑这张
            </Button>
            <Button variant="ghost" size="icon" onClick={() => setSelected(null)} aria-label="关闭" className="size-7 text-muted-foreground hover:text-foreground">
              <X />
            </Button>
          </div>
          <div className="min-h-0 flex-1">
            <ArtifactWorkspace artifacts={[toRef(selected)]} />
          </div>
        </div>
      )}

      {/* 蒙版画布编辑器（imageUrl 为本地 objectURL，天然免鉴权头） */}
      {maskOpen && sourcePreview && (
        <MaskEditor imageUrl={sourcePreview} onConfirm={onMaskConfirm} onClose={() => setMaskOpen(false)} />
      )}
    </div>
  );
}
