// 蒙版笔迹的纯数据变换（MaskEditor 的可测内核，vitest node 环境可跑）。
//
// 显示层与导出层共用同一笔迹栈（stroke list：路径点+粗细+笔/橡皮），差别只在
// 合成模式与颜色：显示层把笔刷画成红色半透明预览、橡皮抠掉已画红迹；导出层在
// **不透明黑底**上以 destination-out 打透明洞——OpenAI mask 契约：RGBA PNG、
// alpha=0（全透明）区域=要重绘的区域，橡皮则回填不透明黑（恢复"保留"）。
// 撤销=弹栈后整栈重放，故所有绘制都表达为可重放的 ReplayOp 列表。

export interface StrokePoint {
  x: number;
  y: number;
}

export interface Stroke {
  points: StrokePoint[];
  size: number; // 逻辑像素（画布逻辑尺寸=底图 naturalWidth/Height）
  erase: boolean;
}

export interface ReplayOp {
  composite: GlobalCompositeOperation;
  color: string;
  lineWidth: number;
  points: StrokePoint[];
}

// 红色半透明预览（对齐 tailwind red-500）。
export const MASK_PREVIEW_COLOR = "rgba(239, 68, 68, 0.55)";
const OPAQUE_BLACK = "rgba(0, 0, 0, 1)";

// 显示层（标记画布）：笔刷=叠红迹，橡皮=抠掉已画红迹。
export function displayReplayOps(strokes: Stroke[]): ReplayOp[] {
  return strokes.map((s) => ({
    composite: s.erase ? ("destination-out" as const) : ("source-over" as const),
    color: s.erase ? OPAQUE_BLACK : MASK_PREVIEW_COLOR,
    lineWidth: s.size,
    points: s.points,
  }));
}

// 导出层（黑底蒙版）：笔刷=destination-out 打透明洞（alpha=0=重绘区），
// 橡皮=source-over 回填不透明黑。按笔迹顺序重放，语义与显示层逐笔对应。
export function exportReplayOps(strokes: Stroke[]): ReplayOp[] {
  return strokes.map((s) => ({
    composite: s.erase ? ("source-over" as const) : ("destination-out" as const),
    color: OPAQUE_BLACK,
    lineWidth: s.size,
    points: s.points,
  }));
}

// 显示坐标 → 画布逻辑坐标：画布逻辑尺寸=底图 naturalWidth/Height，显示用 CSS
// 缩放适配 Dialog，故指针坐标须按 rect 与逻辑尺寸的比值换算，并夹取进画布。
export function toCanvasPoint(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
  canvasW: number,
  canvasH: number,
): StrokePoint {
  const sx = rect.width > 0 ? canvasW / rect.width : 1;
  const sy = rect.height > 0 ? canvasH / rect.height : 1;
  const clamp = (v: number, max: number) => Math.min(Math.max(v, 0), max);
  return {
    x: clamp((clientX - rect.left) * sx, canvasW),
    y: clamp((clientY - rect.top) * sy, canvasH),
  };
}

// 把 ReplayOp 列表重放到 2D 上下文（canvas 副作用集中在此，不做单测）。
// 单点笔迹画圆点（radius=lineWidth/2），多点画圆头折线。
export function applyReplayOps(ctx: CanvasRenderingContext2D, ops: ReplayOp[]): void {
  for (const op of ops) {
    if (op.points.length === 0) continue;
    ctx.globalCompositeOperation = op.composite;
    ctx.strokeStyle = op.color;
    ctx.fillStyle = op.color;
    ctx.lineWidth = op.lineWidth;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    if (op.points.length === 1) {
      const p = op.points[0];
      ctx.beginPath();
      ctx.arc(p.x, p.y, op.lineWidth / 2, 0, Math.PI * 2);
      ctx.fill();
    } else {
      ctx.beginPath();
      ctx.moveTo(op.points[0].x, op.points[0].y);
      for (let i = 1; i < op.points.length; i++) ctx.lineTo(op.points[i].x, op.points[i].y);
      ctx.stroke();
    }
  }
  ctx.globalCompositeOperation = "source-over";
}
