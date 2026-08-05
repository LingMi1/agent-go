// 会话本地草稿缓存与合并（M7 起服务端 GET /sessions 是权威来源；localStorage 降级为
// 缓存，只存"尚未落库的新会话"草稿——新建但还没发出第一条 run 的会话；按用户隔离，
// 服务端知晓某会话后即从本地修剪，避免无界膨胀）。纯逻辑核心可单测。

import { getUserId } from "./identity";

export interface SessionMeta {
  id: string;
  title: string;
  agentType: string;
  createdAt: number;
}

const keyFor = (userId: string) => `my-agent.sessions.${userId || "anonymous"}`;

function read(): SessionMeta[] {
  try {
    const raw = localStorage.getItem(keyFor(getUserId()));
    return raw ? (JSON.parse(raw) as SessionMeta[]) : [];
  } catch {
    return [];
  }
}

function write(list: SessionMeta[]): void {
  try {
    localStorage.setItem(keyFor(getUserId()), JSON.stringify(list));
  } catch {
    /* ignore */
  }
}

export function listSessions(): SessionMeta[] {
  return read().sort((a, b) => b.createdAt - a.createdAt);
}

// 纯函数核心（便于单测）：把新会话插到列表头。
export function addSessionTo(list: SessionMeta[], s: SessionMeta): SessionMeta[] {
  return [s, ...list.filter((x) => x.id !== s.id)];
}

// 纯函数核心：从草稿中剔除服务端已知晓的会话（run 已落库 → 服务端列表接管）。
export function pruneSessionsFrom(list: SessionMeta[], serverIds: Iterable<string>): SessionMeta[] {
  const known = new Set(serverIds);
  return list.filter((s) => !known.has(s.id));
}

export function createSession(title: string, agentType: string): SessionMeta {
  const s: SessionMeta = {
    id: cryptoRandomId(),
    title: title || "New Session",
    agentType,
    createdAt: Date.now(),
  };
  write(addSessionTo(read(), s));
  return s;
}

// 修剪本地草稿并返回修剪后的列表（refreshSessions 拿到服务端列表后调用）。
export function pruneSessions(serverIds: Iterable<string>): SessionMeta[] {
  const pruned = pruneSessionsFrom(read(), serverIds);
  write(pruned);
  return pruned.sort((a, b) => b.createdAt - a.createdAt);
}

// 删除一个本地草稿并返回剩余列表（删除会话时同步清本地缓存，按当前 user 隔离）。
export function removeLocalSession(id: string): SessionMeta[] {
  const left = read().filter((s) => s.id !== id);
  write(left);
  return left.sort((a, b) => b.createdAt - a.createdAt);
}

// 服务端会话行的结构形状（与 client.ts 的 ServerSession 对齐；结构类型避免依赖倒置）。
export interface ServerSessionLike {
  sessionId: string;
  title: string;
  entryAgent: string;
  runCount: number;
  createdAt: string; // ISO 8601
  lastActiveAt: string; // ISO 8601
  forkedFrom?: string; // docs/14：父会话 id（分叉会话才有）
}

// 侧栏渲染用的统一视图：服务端行 + 本地未落库草稿会话。
export interface SessionView {
  id: string;
  title: string;
  agentType: string;
  runCount: number;
  createdAt: number; // epoch ms
  lastActiveAt: number; // epoch ms
  pendingLocal: boolean; // true = 仅存在于本地缓存（尚无 run 落库）
  forkedFrom?: string; // docs/14：分叉会话的父会话 id（侧栏画分叉标记）
}

// 纯函数核心：合并服务端列表（权威）与本地草稿（补充），按 lastActiveAt 降序。
// 同 id 以服务端为准——一旦会话有 run 落库，本地草稿条目即被覆盖。
export function mergeSessions(server: ServerSessionLike[], local: SessionMeta[]): SessionView[] {
  // 生图工作区的一次性会话（generate: 前缀）不进对话侧栏——它们只是承载生图 run 的容器，
  // 产物仍在画廊可见。
  server = server.filter((s) => !s.sessionId.startsWith("generate:"));
  const views: SessionView[] = server.map((s) => ({
    id: s.sessionId,
    title: s.title,
    agentType: s.entryAgent,
    runCount: s.runCount,
    createdAt: Date.parse(s.createdAt) || 0,
    lastActiveAt: Date.parse(s.lastActiveAt) || 0,
    pendingLocal: false,
    forkedFrom: s.forkedFrom,
  }));
  const known = new Set(views.map((v) => v.id));
  for (const l of local) {
    if (known.has(l.id)) continue;
    views.push({
      id: l.id,
      title: l.title,
      agentType: l.agentType,
      runCount: 0,
      createdAt: l.createdAt,
      lastActiveAt: l.createdAt,
      pendingLocal: true,
    });
  }
  return views.sort((a, b) => b.lastActiveAt - a.lastActiveAt);
}

function cryptoRandomId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return "s-" + Math.random().toString(36).slice(2);
}
