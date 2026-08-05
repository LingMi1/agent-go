// 把 fetch 响应体的 ReadableStream 转成 SseFrame 异步生成器（实时与回放共用）。

import { parseChunk } from "../sse/parseSSE";
import type { SseFrame } from "../sse/frameTypes";

export async function* iterFrames(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): AsyncGenerator<SseFrame> {
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (value) {
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = parseChunk(buffer);
      buffer = rest;
      for (const frame of frames) yield frame;
    }
    if (done) {
      // 冲刷残余缓冲（末帧无 trailing \n\n 的情况）
      buffer += decoder.decode();
      const { frames } = parseChunk(buffer + "\n\n");
      for (const frame of frames) yield frame;
      return;
    }
  }
}
