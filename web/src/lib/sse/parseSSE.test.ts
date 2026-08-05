import { describe, expect, it } from "vitest";
import { parseChunk } from "./parseSSE";

const msg = (obj: object) => `event: message\ndata: ${JSON.stringify(obj)}\n\n`;

describe("parseChunk", () => {
  it("parses a complete frame", () => {
    const { frames, rest } = parseChunk(msg({ messageType: "result", result: "hi", seq: 1 }));
    expect(frames).toHaveLength(1);
    expect(frames[0].messageType).toBe("result");
    expect(rest).toBe("");
  });

  it("parses multiple frames in one chunk", () => {
    const buf = msg({ messageType: "tool_thought", toolThought: "a" }) + msg({ messageType: "result", result: "b" });
    expect(parseChunk(buf).frames).toHaveLength(2);
  });

  it("keeps a half frame in rest", () => {
    const buf = msg({ messageType: "result", result: "done" }) + "event: message\ndata: {\"messageType\":\"res";
    const { frames, rest } = parseChunk(buf);
    expect(frames).toHaveLength(1);
    expect(rest).toContain("\"messageType\":\"res");
  });

  it("skips heartbeat blocks", () => {
    const buf = "event: heartbeat\ndata: {}\n\n" + msg({ messageType: "result", result: "x" });
    const { frames } = parseChunk(buf);
    expect(frames).toHaveLength(1);
    expect(frames[0].messageType).toBe("result");
  });

  it("filters messageType heartbeat defensively", () => {
    expect(parseChunk(msg({ messageType: "heartbeat" })).frames).toHaveLength(0);
  });

  it("does not throw on bad JSON", () => {
    expect(() => parseChunk("event: message\ndata: {bad\n\n")).not.toThrow();
    expect(parseChunk("event: message\ndata: {bad\n\n").frames).toHaveLength(0);
  });
});
