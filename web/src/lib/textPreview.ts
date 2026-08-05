// 文本产物预览的展示层截断策略。
//
// 背景（线上缺陷）：此前在 fetch 层硬截 200KB——大 HTML（如内联 base64 图的 site.html
// 1.7MB）被截在图片中间，预览只剩页头、「代码」视图与「复制」全是残缺内容，
// 用户会误判「任务没完成」（产物本身是完整的）。
//
// 原则：**获取、复制、iframe 预览永远全量**；只有 DOM 重的展示（<pre>/Markdown
// 源码视图）超限时做展示层截断，并用醒目横幅明确告知「这只是展示截断」。
export const DISPLAY_SLICE_LIMIT = 300_000;

export interface TextDisplay {
  shown: string;
  truncated: boolean;
  totalChars: number;
}

export function sliceForDisplay(text: string, limit: number = DISPLAY_SLICE_LIMIT): TextDisplay {
  if (text.length <= limit) return { shown: text, truncated: false, totalChars: text.length };
  return { shown: text.slice(0, limit), truncated: true, totalChars: text.length };
}

/** 字符数的人类可读格式（展示横幅用）：1234 → "1.2K"，2_345_678 → "2.3M"。 */
export function formatChars(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}K`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
}
