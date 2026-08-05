import { describe, expect, it } from "vitest";
import {
  addSessionTo,
  mergeSessions,
  pruneSessionsFrom,
  type ServerSessionLike,
  type SessionMeta,
} from "./sessions";

const s = (id: string): SessionMeta => ({ id, title: id, agentType: "react", createdAt: 1 });

const srv = (id: string, lastActiveAt: string, extra?: Partial<ServerSessionLike>): ServerSessionLike => ({
  sessionId: id,
  title: `标题-${id}`,
  entryAgent: "react",
  runCount: 2,
  createdAt: "2026-07-01T10:00:00Z",
  lastActiveAt,
  ...extra,
});

describe("sessions pure logic", () => {
  it("addSessionTo prepends and dedupes by id", () => {
    const out = addSessionTo([s("a")], s("b"));
    expect(out.map((x) => x.id)).toEqual(["b", "a"]);
    expect(addSessionTo(out, s("b")).map((x) => x.id)).toEqual(["b", "a"]);
  });

  it("pruneSessionsFrom removes drafts the server already knows", () => {
    const out = pruneSessionsFrom([s("a"), s("b"), s("c")], ["b", "x"]);
    expect(out.map((x) => x.id)).toEqual(["a", "c"]);
    expect(pruneSessionsFrom([], ["a"])).toEqual([]);
  });
});

describe("mergeSessions（服务端为准，本地仅补未落库会话）", () => {
  it("服务端字段映射：ISO 时间转 epoch ms，pendingLocal=false", () => {
    const out = mergeSessions([srv("a", "2026-07-01T12:00:00Z")], []);
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      id: "a",
      title: "标题-a",
      agentType: "react",
      runCount: 2,
      createdAt: Date.parse("2026-07-01T10:00:00Z"),
      lastActiveAt: Date.parse("2026-07-01T12:00:00Z"),
      pendingLocal: false,
    });
  });

  it("同 id 以服务端为准（本地缓存不覆盖、不重复）", () => {
    const local: SessionMeta = { id: "a", title: "本地旧标题", agentType: "plan_solve", createdAt: 5 };
    const out = mergeSessions([srv("a", "2026-07-01T12:00:00Z")], [local]);
    expect(out).toHaveLength(1);
    expect(out[0].title).toBe("标题-a");
    expect(out[0].pendingLocal).toBe(false);
  });

  it("本地独有（尚未落库的新会话）追加为 pendingLocal，runCount=0", () => {
    const local: SessionMeta = { id: "draft", title: "New Session", agentType: "react", createdAt: 42 };
    const out = mergeSessions([], [local]);
    expect(out).toEqual([
      { id: "draft", title: "New Session", agentType: "react", runCount: 0, createdAt: 42, lastActiveAt: 42, pendingLocal: true },
    ]);
  });

  it("合并结果按 lastActiveAt 降序（服务端与本地交错排序）", () => {
    const local: SessionMeta = {
      id: "draft",
      title: "草稿",
      agentType: "react",
      createdAt: Date.parse("2026-07-01T11:00:00Z"),
    };
    const out = mergeSessions(
      [srv("old", "2026-07-01T09:00:00Z"), srv("new", "2026-07-01T12:00:00Z")],
      [local],
    );
    expect(out.map((x) => x.id)).toEqual(["new", "draft", "old"]);
  });

  it("空输入返回空数组", () => {
    expect(mergeSessions([], [])).toEqual([]);
  });
});
