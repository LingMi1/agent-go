import { useCallback, useEffect, useState } from "react";
import {
  clearRole,
  clearToken,
  clearUserId,
  getRole,
  getToken,
  getUserId,
  setRole,
  setToken,
  setUserId,
} from "../lib/identity";
import {
  login as apiLogin,
  logout as apiLogout,
  me as apiMe,
  register as apiRegister,
  isAuthExpiredError,
} from "../lib/api/client";

// D3（docs/17）：登录换发 token。login/register 改 async → 调后端 → 存 token+userId+role。
// 启动若已有 token → GET /auth/me 校验（仅 401 失效才清本地；网络/5xx 保留 token 重试，#14）。isAdmin 仅驱动前端入口显隐。
// 兼容：仅有 userId 无 token（AUTH_REQUIRED 关的 X-User-Id 老路径 / e2e 预置）仍视为已登录——
// 不触发 me() 校验、不登出，既有 Playwright 预置 localStorage 的用例零回归。
export function useAuth() {
  const [userId, setId] = useState<string>(() => getUserId());
  const [role, setRoleState] = useState<string>(() => getRole());

  const clearLocal = useCallback(() => {
    clearUserId();
    clearToken();
    clearRole();
    setId("");
    setRoleState("");
  }, []);

  const applyAuth = useCallback((uid: string, r: string, token: string) => {
    setToken(token);
    setUserId(uid);
    setRole(r);
    setId(uid);
    setRoleState(r);
  }, []);

  // 启动校验：有 token 才打 /auth/me（无 token 的 X-User-Id 老路径不校验，保持兼容）。
  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    void apiMe()
      .then((info) => {
        if (cancelled) return;
        setUserId(info.userId);
        setRole(info.role);
        setId(info.userId);
        setRoleState(info.role);
      })
      .catch((err: unknown) => {
        // #14：只在明确 401（token 失效/过期）时清本地回登录页；网络错误/5xx/超时保留
        // token（本地 userId/role 仍作已登录种子，下次刷新自会重试），避免网络抖动/后端
        // 滚动重启把有效登录态误清。
        if (!cancelled && isAuthExpiredError(err)) clearLocal();
      });
    return () => {
      cancelled = true;
    };
  }, [clearLocal]);

  const login = useCallback(
    async (username: string, password: string) => {
      const info = await apiLogin(username, password); // 失败抛错，交 LoginView 展示
      applyAuth(info.userId, info.role, info.token);
    },
    [applyAuth],
  );

  const register = useCallback(
    async (username: string, password: string) => {
      const info = await apiRegister(username, password);
      applyAuth(info.userId, info.role, info.token);
    },
    [applyAuth],
  );

  const logout = useCallback(() => {
    void apiLogout(); // 通知后端删 token（失败无妨，本地清即退出）
    clearLocal();
  }, [clearLocal]);

  return { userId, role, isAuthed: !!userId, isAdmin: role === "admin", login, register, logout };
}
