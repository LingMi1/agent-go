// 身份（D3 多用户 + RBAC，docs/17）：登录换发 token（server 端 auth_sessions）。
//   - userId：owner 归属（== 用户名），所有请求仍带 X-User-Id（AUTH_REQUIRED 关时的老路径兜底）。
//   - token：Authorization: Bearer 头（认证传播的全部表面，见 client.headers()）。
//   - role：user|admin，仅驱动前端 admin 入口显隐（真校验在后端 requireAdmin，前端非安全边界）。
// 三者独立 localStorage 键，同 `my-agent.` 前缀约定；容错读写（隐私模式存不进仅影响记忆）。

const KEY = "my-agent.userId";
const TOKEN_KEY = "my-agent.token";
const ROLE_KEY = "my-agent.role";

function read(key: string): string {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function remove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

export function getUserId(): string {
  return read(KEY);
}

export function setUserId(id: string): void {
  write(KEY, id);
}

export function clearUserId(): void {
  remove(KEY);
}

export function getToken(): string {
  return read(TOKEN_KEY);
}

export function setToken(token: string): void {
  write(TOKEN_KEY, token);
}

export function clearToken(): void {
  remove(TOKEN_KEY);
}

export function getRole(): string {
  return read(ROLE_KEY);
}

export function setRole(role: string): void {
  write(ROLE_KEY, role);
}

export function clearRole(): void {
  remove(ROLE_KEY);
}
