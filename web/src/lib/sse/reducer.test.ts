import { describe, expect, it } from "vitest";
import { applyFrame, reduceFrames } from "./reducer";
import { emptyRunState, type SseFrame } from "./frameTypes";

const f = (o: Partial<SseFrame>): SseFrame =>
  ({ requestId: "r", messageId: "m", seq: 1, messageType: "result", messageTime: "0", isFinal: false, finish: false, ...o }) as SseFrame;

describe("applyFrame", () => {
  it("accumulates thought text on same messageId", () => {
    let s = emptyRunState();
    s = applyFrame(s, f({ messageType: "tool_thought", messageId: "t1", toolThought: "Hel" }));
    s = applyFrame(s, f({ messageType: "tool_thought", messageId: "t1", toolThought: "lo" }));
    expect(s.thoughts["t1"].text).toBe("Hello");
    expect(s.order.filter((e) => e.kind === "thought")).toHaveLength(1);
  });

  it("overwrites tool_call on same messageId (running -> success)", () => {
    let s = emptyRunState();
    s = applyFrame(s, f({ messageType: "tool_call", messageId: "c1", resultMap: { status: "running", toolName: "calc", toolCallId: "c1" } }));
    s = applyFrame(s, f({ messageType: "tool_call", messageId: "c1", resultMap: { status: "success", toolName: "calc", toolCallId: "c1" } }));
    expect(Object.keys(s.toolCalls)).toHaveLength(1);
    expect(s.toolCalls["c1"].status).toBe("success");
    expect(s.order.filter((e) => e.kind === "toolCall")).toHaveLength(1);
  });

  it("appends tool_result and links toolCallId + merges artifacts", () => {
    let s = emptyRunState();
    s = applyFrame(s, f({
      messageType: "tool_result", messageId: "c1:result",
      toolResult: { toolName: "report", toolResult: "ok", toolCallId: "c1" },
      artifactRefs: [{ resourceKey: "r/c1/a.md", name: "a", previewUrl: "", downloadUrl: "/artifacts/r/c1/a.md", fileName: "a.md", mimeType: "text/markdown", size: 3, missing: false }],
    }));
    expect(s.toolResults).toHaveLength(1);
    expect(s.toolResults[0].toolCallId).toBe("c1");
    expect(s.artifacts).toHaveLength(1);
  });

  it("replaces plan snapshot and groups planner rounds", () => {
    let s = emptyRunState();
    s = applyFrame(s, f({ messageType: "plan", plan: { title: "P", steps: ["a", "b"], stepStatus: ["in_progress", "not_started"], notes: [] }, resultMap: { plannerRoundId: "r1" } }));
    s = applyFrame(s, f({ messageType: "plan", plan: { title: "P", steps: ["a", "b"], stepStatus: ["completed", "in_progress"], notes: [] }, resultMap: { plannerRoundId: "r1" } }));
    expect(s.plan?.stepStatus[0]).toBe("completed"); // 快照替换
    expect(s.plannerRounds).toHaveLength(1); // 同轮替换
    s = applyFrame(s, f({ messageType: "plan", plan: { title: "P", steps: ["c"], stepStatus: ["in_progress"], notes: [] }, resultMap: { plannerRoundId: "r2" } }));
    expect(s.plannerRounds).toHaveLength(2); // 跨轮追加
  });

  it("adds tasks", () => {
    let s = emptyRunState();
    s = applyFrame(s, f({ messageType: "task", task: "子任务A" }));
    s = applyFrame(s, f({ messageType: "task", task: "子任务B" }));
    expect(s.tasks.map((t) => t.text)).toEqual(["子任务A", "子任务B"]);
  });

  it("sets result and finished on finish", () => {
    let s = emptyRunState();
    s = applyFrame(s, f({ messageType: "result", result: "答案", finish: true }));
    expect(s.result?.text).toBe("答案");
    expect(s.finished).toBe(true);
  });

  it("dedupes artifacts by resourceKey", () => {
    const art = (k: string) => ({ resourceKey: k, name: k, previewUrl: "", downloadUrl: "", fileName: k, mimeType: "", size: 1, missing: false });
    let s = emptyRunState();
    s = applyFrame(s, f({ messageType: "tool_result", toolResult: { toolName: "t", toolResult: "", toolCallId: "c" }, artifactRefs: [art("k1")] }));
    s = applyFrame(s, f({ messageType: "result", result: "x", finish: true, artifactRefs: [art("k1"), art("k2")] }));
    expect(s.artifacts.map((a) => a.resourceKey).sort()).toEqual(["k1", "k2"]);
  });

  it("does not throw on unknown messageType", () => {
    const s = applyFrame(emptyRunState(), f({ messageType: "external_tool_result" as never }));
    expect(s.unknown).toHaveLength(1);
  });

  it("ignores heartbeat", () => {
    const s0 = emptyRunState();
    expect(applyFrame(s0, f({ messageType: "heartbeat" }))).toBe(s0);
  });

  it("reduceFrames replays a full run deterministically", () => {
    const frames = [
      f({ messageType: "tool_thought", messageId: "t", toolThought: "think" }),
      f({ messageType: "tool_call", messageId: "c", resultMap: { status: "success", toolName: "calc", toolCallId: "c" } }),
      f({ messageType: "result", result: "done", finish: true }),
    ];
    const s = reduceFrames(frames);
    expect(s.finished).toBe(true);
    expect(s.order.map((e) => e.kind)).toEqual(["thought", "toolCall", "result"]);
  });
});

describe("approval_request（M11 HITL）", () => {
  const ap = (o: Partial<SseFrame> = {}): SseFrame =>
    ({
      requestId: "r1",
      messageId: "r1:approval:ap-1",
      seq: 3,
      messageType: "approval_request",
      messageTime: "t",
      isFinal: true,
      finish: false,
      approval: { approvalId: "ap-1", toolName: "calculator", reason: "高危", pendingToolCallIds: ["tc1"] },
      ...o,
    }) as SseFrame;

  it("登记 pending 审批并把 RUNNING 工具卡翻待审批", () => {
    let s = reduceFrames([
      {
        requestId: "r1", messageId: "tc1", seq: 1, messageType: "tool_call", messageTime: "t",
        isFinal: false, finish: false,
        resultMap: { toolCallId: "tc1", toolName: "calculator", status: "running" },
      } as SseFrame,
    ]);
    s = applyFrame(s, ap());
    expect(s.approvals["ap-1"].status).toBe("pending");
    expect(s.approvals["ap-1"].toolName).toBe("calculator");
    expect(s.toolCalls["tc1"].status).toBe("awaiting_approval");
    expect(s.order.filter((e) => e.kind === "approval")).toHaveLength(1);
    expect(s.unknown).toHaveLength(0); // 不再落 unknown
  });

  it("重复帧幂等（order/记录不重复）", () => {
    let s = applyFrame(emptyRunState(), ap());
    s = applyFrame(s, ap());
    expect(Object.keys(s.approvals)).toHaveLength(1);
    expect(s.order.filter((e) => e.kind === "approval")).toHaveLength(1);
  });

  it("已完成的工具卡不被回翻", () => {
    let s = reduceFrames([
      {
        requestId: "r1", messageId: "tc1", seq: 1, messageType: "tool_call", messageTime: "t",
        isFinal: true, finish: false,
        resultMap: { toolCallId: "tc1", toolName: "calculator", status: "success" },
      } as SseFrame,
    ]);
    s = applyFrame(s, ap());
    expect(s.toolCalls["tc1"].status).toBe("success");
  });
});

describe("additive 字段（紧凑工具状态行）", () => {
  it("thought 记录 firstAt（首帧）/lastAt（每帧更新）", () => {
    let s = emptyRunState();
    s = applyFrame(s, f({ messageType: "tool_thought", messageId: "t1", toolThought: "a", messageTime: "2026-07-20T00:00:00Z" }));
    s = applyFrame(s, f({ messageType: "tool_thought", messageId: "t1", toolThought: "b", messageTime: "2026-07-20T00:00:03Z" }));
    expect(s.thoughts["t1"].firstAt).toBe("2026-07-20T00:00:00Z");
    expect(s.thoughts["t1"].lastAt).toBe("2026-07-20T00:00:03Z");
    expect(s.thoughts["t1"].text).toBe("ab"); // 累加语义不变
  });

  it("plan_thought 同样记录时间戳", () => {
    let s = emptyRunState();
    s = applyFrame(s, f({ messageType: "plan_thought", messageId: "p1", planThought: "x", messageTime: "T1" }));
    s = applyFrame(s, f({ messageType: "plan_thought", messageId: "p1", planThought: "y", messageTime: "T2" }));
    expect(s.thoughts["p1"].firstAt).toBe("T1");
    expect(s.thoughts["p1"].lastAt).toBe("T2");
  });

  it("tool_result 把 artifact resourceKey 归属进 artifactsByCall（去重、按调用分组）", () => {
    const art = (k: string) => ({ resourceKey: k, name: k, previewUrl: "", downloadUrl: "", fileName: k, mimeType: "image/png", size: 1, missing: false });
    let s = emptyRunState();
    s = applyFrame(s, f({
      messageType: "tool_result",
      toolResult: { toolName: "image_generate", toolResult: "ok", toolCallId: "c1" },
      resultMap: { toolCallId: "c1" },
      artifactRefs: [art("k1"), art("k2")],
    }));
    s = applyFrame(s, f({
      messageType: "tool_result",
      toolResult: { toolName: "image_generate", toolResult: "ok", toolCallId: "c1" },
      resultMap: { toolCallId: "c1" },
      artifactRefs: [art("k2")], // 重复 key 去重
    }));
    s = applyFrame(s, f({
      messageType: "tool_result",
      toolResult: { toolName: "write_report", toolResult: "ok", toolCallId: "c2" },
      artifactRefs: [art("k3")], // 无 resultMap：toolResult.toolCallId 兜底
    }));
    expect(s.artifactsByCall?.["c1"]).toEqual(["k1", "k2"]);
    expect(s.artifactsByCall?.["c2"]).toEqual(["k3"]);
  });

  it("无 artifactRefs 的 tool_result 不建 artifactsByCall 条目", () => {
    const s = applyFrame(emptyRunState(), f({
      messageType: "tool_result",
      toolResult: { toolName: "calc", toolResult: "2", toolCallId: "c1" },
    }));
    expect(s.artifactsByCall?.["c1"]).toBeUndefined();
  });
});
