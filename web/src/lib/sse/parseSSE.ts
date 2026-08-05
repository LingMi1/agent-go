// SSE 帧解析（纯函数，无副作用、可单测）。
// 后端帧形如： "event: message\ndata: {json}\n\n"；心跳为 "event: heartbeat..."。
// 维护 buffer 按 \n\n 切完整帧；半包留在 rest 回填下次。

import type { SseFrame } from "./frameTypes";

export interface ParseResult {
  frames: SseFrame[];
  rest: string;
}

export function parseChunk(buffer: string): ParseResult {
  const frames: SseFrame[] = [];
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? ""; // 最后一段可能是半包，留回 buffer
  for (const block of parts) {
    const frame = parseBlock(block);
    if (frame) frames.push(frame);
  }
  return { frames, rest };
}

function parseBlock(block: string): SseFrame | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const raw of block.split("\n")) {
    const line = raw.trimEnd();
    if (line.startsWith(":")) continue; // 注释行
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (event === "heartbeat" || dataLines.length === 0) return null;
  try {
    const obj = JSON.parse(dataLines.join("\n")) as SseFrame;
    if (obj.messageType === "heartbeat") return null;
    return obj;
  } catch {
    return null; // 坏 JSON 不崩，丢弃该帧
  }
}
