import { describe, expect, it } from "vitest";
import {
  MASK_PREVIEW_COLOR,
  displayReplayOps,
  exportReplayOps,
  toCanvasPoint,
  type Stroke,
} from "./maskStrokes";

const brush: Stroke = { points: [{ x: 1, y: 2 }, { x: 3, y: 4 }], size: 24, erase: false };
const eraser: Stroke = { points: [{ x: 5, y: 6 }], size: 12, erase: true };

describe("displayReplayOps（红色半透明预览）", () => {
  it("笔刷=source-over 红迹，橡皮=destination-out 抠红迹，粗细/点位透传", () => {
    const ops = displayReplayOps([brush, eraser]);
    expect(ops).toHaveLength(2);
    expect(ops[0]).toMatchObject({
      composite: "source-over",
      color: MASK_PREVIEW_COLOR,
      lineWidth: 24,
      points: brush.points,
    });
    expect(ops[1]).toMatchObject({ composite: "destination-out", lineWidth: 12 });
  });
});

describe("exportReplayOps（OpenAI mask：黑底打透明洞）", () => {
  it("笔刷=destination-out 打洞（alpha=0=重绘区），橡皮=source-over 回填不透明黑", () => {
    const ops = exportReplayOps([brush, eraser]);
    expect(ops[0].composite).toBe("destination-out");
    expect(ops[1].composite).toBe("source-over");
    expect(ops[1].color).toBe("rgba(0, 0, 0, 1)");
  });
  it("重放顺序与笔迹栈一致（撤销=弹栈重放的前提）", () => {
    const ops = exportReplayOps([brush, eraser, brush]);
    expect(ops.map((o) => o.composite)).toEqual(["destination-out", "source-over", "destination-out"]);
  });
});

describe("toCanvasPoint（CSS 缩放显示 → naturalSize 逻辑坐标）", () => {
  const rect = { left: 10, top: 20, width: 200, height: 100 };
  it("按显示矩形与逻辑尺寸的比值换算", () => {
    // 显示 200x100，逻辑 400x200 → 缩放系数 2。
    expect(toCanvasPoint(110, 70, rect, 400, 200)).toEqual({ x: 200, y: 100 });
    expect(toCanvasPoint(10, 20, rect, 400, 200)).toEqual({ x: 0, y: 0 });
  });
  it("越界坐标夹取进画布", () => {
    expect(toCanvasPoint(0, 0, rect, 400, 200)).toEqual({ x: 0, y: 0 });
    expect(toCanvasPoint(9999, 9999, rect, 400, 200)).toEqual({ x: 400, y: 200 });
  });
  it("零尺寸矩形不除零（回退系数 1）", () => {
    const p = toCanvasPoint(15, 25, { left: 10, top: 20, width: 0, height: 0 }, 400, 200);
    expect(p).toEqual({ x: 5, y: 5 });
  });
});
