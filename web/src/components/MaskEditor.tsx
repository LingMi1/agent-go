import { useCallback, useEffect, useRef, useState } from "react";
import { Brush, Check, Eraser, Trash2, Undo2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  applyReplayOps,
  displayReplayOps,
  exportReplayOps,
  toCanvasPoint,
  type Stroke,
} from "../lib/maskStrokes";

// 蒙版画布编辑器（docs/12 inpaint）：在底图上涂抹要重绘的区域（红色半透明预览），
// 确认时导出与底图 naturalWidth/Height **逐像素同尺寸**的 RGBA PNG——不透明黑底 +
// destination-out 回放笔迹打透明洞（OpenAI mask 契约：alpha=0=要重绘的区域）。
// 显示只做 CSS 缩放，逻辑坐标全部按 naturalSize 换算；撤销=笔迹栈弹栈重放。
// imageUrl 必须是本地 objectURL（/artifacts 需带鉴权头，裸 URL 会 403，由调用方保证）。

const MIN_BRUSH = 8;
const MAX_BRUSH = 128;

export function MaskEditor({
  imageUrl,
  onConfirm,
  onClose,
}: {
  imageUrl: string;
  onConfirm: (blob: Blob) => void;
  onClose: () => void;
}) {
  const [strokes, setStrokes] = useState<Stroke[]>([]);
  const [tool, setTool] = useState<"brush" | "erase">("brush");
  const [brushSize, setBrushSize] = useState(40);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [exporting, setExporting] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  // 进行中的一笔（提笔才入栈）。pointerId 记录起笔指针：单指针绘制语义——第二根手指/
  // 手掌误触的 down/move/up 一律忽略，否则第二个 pointerdown 覆盖进行中笔迹、两个触点的
  // move 交替混入同一 points 产生横穿图面的锯齿涂抹（评审 D4）。
  const drawingRef = useRef<(Stroke & { pointerId: number }) | null>(null);

  // 加载底图取 naturalSize（objectURL 同源，无需鉴权头）。
  useEffect(() => {
    const img = new Image();
    img.onload = () => {
      imgRef.current = img;
      setNatural({ w: img.naturalWidth, h: img.naturalHeight });
    };
    img.onerror = () => {
      toast.error("底图加载失败");
      onClose();
    };
    img.src = imageUrl;
    return () => {
      imgRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imageUrl]);

  // 整帧重绘：底图 + 全量笔迹重放（含进行中的一笔）。O(笔迹数)，画布 MB 级可承受，
  // 换来的是橡皮/撤销语义天然正确（无需增量擦除的边界处理）。
  const redraw = useCallback(() => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img || !natural) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, natural.w, natural.h);
    ctx.drawImage(img, 0, 0, natural.w, natural.h);
    // 红迹画在独立标记层再叠到底图上：橡皮 destination-out 只抠红迹、不抠底图。
    const marks = document.createElement("canvas");
    marks.width = natural.w;
    marks.height = natural.h;
    const mctx = marks.getContext("2d");
    if (!mctx) return;
    const all = drawingRef.current ? [...strokes, drawingRef.current] : strokes;
    applyReplayOps(mctx, displayReplayOps(all));
    ctx.drawImage(marks, 0, 0);
  }, [strokes, natural]);

  useEffect(redraw, [redraw]);

  const pointOf = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = e.currentTarget;
    return toCanvasPoint(e.clientX, e.clientY, canvas.getBoundingClientRect(), canvas.width, canvas.height);
  };

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (e.button !== 0) return;
    if (drawingRef.current) return; // 已有进行中笔迹：忽略次要触点（多指/手掌误触）
    e.currentTarget.setPointerCapture(e.pointerId);
    drawingRef.current = { pointerId: e.pointerId, points: [pointOf(e)], size: brushSize, erase: tool === "erase" };
    redraw();
  };
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drawingRef.current || e.pointerId !== drawingRef.current.pointerId) return;
    drawingRef.current.points.push(pointOf(e));
    redraw();
  };
  const finishStroke = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const s = drawingRef.current;
    if (!s || e.pointerId !== s.pointerId) return; // 非起笔指针的 up/cancel 不收笔
    drawingRef.current = null;
    const { pointerId: _pid, ...stroke } = s;
    setStrokes((prev) => [...prev, stroke]); // 入栈触发重绘
  };

  // 导出：黑底（全保留）→ destination-out 重放笔迹打透明洞 → PNG blob。
  const exportMask = () => {
    const img = imgRef.current;
    if (!img || !natural || strokes.length === 0) return;
    setExporting(true);
    const c = document.createElement("canvas");
    c.width = natural.w;
    c.height = natural.h;
    const ctx = c.getContext("2d");
    if (!ctx) {
      setExporting(false);
      return;
    }
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, natural.w, natural.h);
    applyReplayOps(ctx, exportReplayOps(strokes));
    c.toBlob((blob) => {
      setExporting(false);
      if (blob) onConfirm(blob);
      else toast.error("蒙版导出失败");
    }, "image/png");
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl" data-testid="mask-editor">
        <DialogTitle className="flex items-center gap-2">
          <Brush className="size-4 text-primary" />
          编辑蒙版（局部重绘）
        </DialogTitle>
        <DialogDescription>
          用笔刷涂抹<span className="text-foreground">要重绘的区域</span>（红色标记）；生成时仅这些区域按提示词重画，其余保留。
        </DialogDescription>

        {/* 工具条 */}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant={tool === "brush" ? "secondary" : "ghost"}
            size="sm"
            data-testid="mask-brush"
            onClick={() => setTool("brush")}
            className="gap-1.5"
          >
            <Brush className="size-4" />
            笔刷
          </Button>
          <Button
            variant={tool === "erase" ? "secondary" : "ghost"}
            size="sm"
            data-testid="mask-eraser"
            onClick={() => setTool("erase")}
            className="gap-1.5"
          >
            <Eraser className="size-4" />
            橡皮
          </Button>
          <label className="ml-2 flex items-center gap-2 text-xs text-muted-foreground/70">
            粗细
            <input
              type="range"
              min={MIN_BRUSH}
              max={MAX_BRUSH}
              value={brushSize}
              data-testid="mask-size"
              onChange={(e) => setBrushSize(Number(e.target.value))}
              className="w-28 accent-primary"
            />
            <span className="w-6 tabular-nums">{brushSize}</span>
          </label>
          <div className="ml-auto flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              data-testid="mask-undo"
              disabled={strokes.length === 0}
              onClick={() => setStrokes((prev) => prev.slice(0, -1))}
              className="gap-1.5 text-muted-foreground/70 hover:text-foreground"
            >
              <Undo2 className="size-4" />
              撤销
            </Button>
            <Button
              variant="ghost"
              size="sm"
              data-testid="mask-clear"
              disabled={strokes.length === 0}
              onClick={() => setStrokes([])}
              className="gap-1.5 text-muted-foreground/70 hover:text-foreground"
            >
              <Trash2 className="size-4" />
              清空
            </Button>
          </div>
        </div>

        {/* 画布：逻辑尺寸=naturalSize（保证 mask 与底图逐像素一致），显示 CSS 缩放。 */}
        <div className="flex max-h-[60vh] items-center justify-center overflow-hidden rounded-lg border bg-accent/50">
          {natural ? (
            <canvas
              ref={canvasRef}
              width={natural.w}
              height={natural.h}
              data-testid="mask-canvas"
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={finishStroke}
              onPointerCancel={finishStroke}
              className="max-h-[60vh] max-w-full cursor-crosshair"
              style={{ touchAction: "none" }}
            />
          ) : (
            <Skeleton className="h-64 w-full" />
          )}
        </div>

        <div className="flex items-center justify-end gap-2">
          <span className="mr-auto text-xs text-muted-foreground/70">
            {strokes.length === 0 ? "先涂抹至少一笔才能确认" : `已画 ${strokes.length} 笔`}
          </span>
          <Button variant="ghost" size="sm" onClick={onClose}>
            取消
          </Button>
          <Button
            size="sm"
            data-testid="mask-confirm"
            disabled={strokes.length === 0 || exporting || !natural}
            onClick={exportMask}
            className="gap-1.5 bg-primary text-primary-foreground hover:bg-primary/85"
          >
            <Check className="size-4" />
            {exporting ? "导出中…" : "确认蒙版"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
