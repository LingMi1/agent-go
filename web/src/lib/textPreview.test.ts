import { describe, expect, it } from "vitest";
import { DISPLAY_SLICE_LIMIT, formatChars, sliceForDisplay } from "./textPreview";

describe("sliceForDisplay", () => {
  it("未超限原样返回（同一引用，不复制）", () => {
    const t = "hello 世界";
    const d = sliceForDisplay(t);
    expect(d).toEqual({ shown: t, truncated: false, totalChars: t.length });
    expect(d.shown).toBe(t);
  });

  it("超限截断并保留总长（回归：1.7MB site.html 曾被 fetch 层硬截致预览残缺）", () => {
    const t = "x".repeat(DISPLAY_SLICE_LIMIT + 5);
    const d = sliceForDisplay(t);
    expect(d.truncated).toBe(true);
    expect(d.shown.length).toBe(DISPLAY_SLICE_LIMIT);
    expect(d.totalChars).toBe(DISPLAY_SLICE_LIMIT + 5);
  });

  it("自定义 limit", () => {
    expect(sliceForDisplay("abcdef", 3)).toEqual({ shown: "abc", truncated: true, totalChars: 6 });
  });
});

describe("formatChars", () => {
  it("K/M 分档，去掉多余 .0", () => {
    expect(formatChars(999)).toBe("999");
    expect(formatChars(1200)).toBe("1.2K");
    expect(formatChars(300_000)).toBe("300K");
    expect(formatChars(1_692_781)).toBe("1.7M");
  });
});
