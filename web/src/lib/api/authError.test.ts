import { describe, expect, it } from "vitest";
import { ApiError, isAuthExpiredError } from "./client";

// #14：启动 me() 校验的错误分类——只有明确 401（token 失效/过期）才应清本地登录态；
// 5xx/超时/网络错误一律保留 token 下次重试，避免网络抖动/后端滚动重启误清有效登录。
describe("me() 启动校验错误分类（#14）", () => {
  it("明确 401（token 失效）→ 判定需清本地", () => {
    expect(isAuthExpiredError(new ApiError(401))).toBe(true);
    expect(isAuthExpiredError(new ApiError(401, "me failed: 401"))).toBe(true);
  });

  it("5xx / 403 / 其它 HTTP 状态 → 不清本地（保留 token 重试）", () => {
    expect(isAuthExpiredError(new ApiError(500))).toBe(false);
    expect(isAuthExpiredError(new ApiError(502))).toBe(false);
    expect(isAuthExpiredError(new ApiError(503))).toBe(false);
    expect(isAuthExpiredError(new ApiError(504))).toBe(false);
    expect(isAuthExpiredError(new ApiError(403))).toBe(false);
    expect(isAuthExpiredError(new ApiError(400))).toBe(false);
  });

  it("网络错误/超时（TypeError 等，无 status）→ 不清本地", () => {
    expect(isAuthExpiredError(new TypeError("Failed to fetch"))).toBe(false);
    expect(isAuthExpiredError(new Error("timeout"))).toBe(false);
    expect(isAuthExpiredError("me failed: 401")).toBe(false); // 字符串巧合含 401 也不误判
    expect(isAuthExpiredError(undefined)).toBe(false);
    expect(isAuthExpiredError(null)).toBe(false);
  });

  it("ApiError 携带 status 且是 Error 子类", () => {
    const e = new ApiError(401);
    expect(e).toBeInstanceOf(Error);
    expect(e.status).toBe(401);
    expect(e.name).toBe("ApiError");
  });
});
