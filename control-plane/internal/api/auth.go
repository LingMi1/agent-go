package api

// D3 多用户 + RBAC（docs/17）：身份解析单点改造 + 登录/注册/会话端点。
//
// 设计核心（docs/17 §3.2）：resolveIdentity 中间件读 `Authorization: Bearer <token>` → LookupToken
// →把 (userID, role) 塞进 request context；ownerOf 改成「先读 context，没有再回退 X-User-Id 头」。
// 于是 20 处 ownerOf 调用、所有 owner 过滤 SQL、kb_id、upload key、认知面 metadata **全部零改动**。
// AUTH_REQUIRED 默认关：token 有则用无则回退 X-User-Id（老路径完整保留，既有测试零回归）。

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"golang.org/x/crypto/bcrypt"

	"my-agent/control-plane/internal/store"
)

// authTokenTTL 是登录 token 的有效期（server 端可提前吊销：logout 删行）。
const authTokenTTL = 30 * 24 * time.Hour

// identityCtxKey 是 context 里身份的自定义 key type（避免与其他包的 key 串扰）。
type identityCtxKey struct{}

// identity 是 resolveIdentity 解析出的调用方身份（token 有效时才入 context）。
type identity struct {
	userID string
	role   string
}

// identityFrom 取 context 里的身份（token 无效/未登录时 ok=false）。
func identityFrom(r *http.Request) (identity, bool) {
	id, ok := r.Context().Value(identityCtxKey{}).(identity)
	return id, ok
}

// bearerToken 从 Authorization 头提取 Bearer token（无则空串）。
func bearerToken(r *http.Request) string {
	const p = "Bearer "
	h := r.Header.Get("Authorization")
	if strings.HasPrefix(h, p) {
		return strings.TrimSpace(h[len(p):])
	}
	return ""
}

// isPublicPath 是 AUTH_REQUIRED 开启时始终免 token 的 API 路径（docs/17 §3.2）：
// 登录/注册/登出/me（否则 prod 首屏无从登录）、健康检查、指标（否则探针/抓取被 401）。
func isPublicPath(p string) bool {
	return p == "/healthz" || p == "/metrics" || strings.HasPrefix(p, "/auth/")
}

// protectedRoute 判定「AUTH_REQUIRED 下该请求是否必须持有效 token」，采用 docs/17 §3.2
// 原始设计的**反向白名单**：除放行清单（isPublicPath）与「非 API 的 SPA/静态路径」外，
// 任何在路由树中命中的已注册 API 路由都受保护。
//
// 为什么用反向白名单而非维护一张正向前缀清单：D2 新增的 owner 域端点 /connectors、/triggers
// 曾因未被补进正向白名单，导致 AUTH_REQUIRED=true 下仍可无凭证、靠伪造 X-User-Id 头跨租户
// 越权读写（列举/删除他人连接器、以受害者身份建触发规则拉起 agent run）。改为「默认全拦、
// 只显式放行公共/静态」后，今后任何新增 API 路由自动受保护，杜绝「漏加白名单 → prod 越权」回归。
//
// SPA/静态资源经 NotFound(spaHandler) 兜底、在路由树中并无注册项 → Match 返回 false → 放行，
// 保住 prod 单端口托管时首屏与静态资源可无 token 加载（docs/17 §7 记的白屏问题不复发）。
func (h *handlers) protectedRoute(r *http.Request) bool {
	if isPublicPath(r.URL.Path) {
		return false
	}
	if h.mux == nil {
		return false // 理论上不发生（NewRouter 恒装配 h.mux）；缺路由引用时保守放行，绝不误锁首屏
	}
	return h.mux.Match(chi.NewRouteContext(), r.Method, r.URL.Path)
}

// resolveIdentity 是身份解析中间件（挂在 metrics/Recoverer 之后）：
//   - token repo 已装配且带有效 Bearer token → 把 (userID, role) 放进 context；
//   - token repo 为 nil（测试/降级）或无 token → 不设身份，ownerOf 回退 X-User-Id（老路径）；
//   - AUTH_REQUIRED=true 且访问受保护 API 却无有效身份 → 401（X-User-Id 头被忽略）。
func (h *handlers) resolveIdentity(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if h.authTokens != nil {
			if tok := bearerToken(r); tok != "" {
				if userID, role, err := h.authTokens.LookupToken(r.Context(), tok); err == nil {
					ctx := context.WithValue(r.Context(), identityCtxKey{}, identity{userID: userID, role: role})
					r = r.WithContext(ctx)
				}
			}
		}
		if h.authRequired {
			if _, ok := identityFrom(r); !ok && h.protectedRoute(r) {
				writeProblem(w, http.StatusUnauthorized, "unauthorized", "需要登录")
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

// requireAdmin 门控 admin 后台：context role != admin → 403（真校验在此，前端隐藏仅 UX）。
func (h *handlers) requireAdmin(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id, ok := identityFrom(r)
		if !ok || id.role != store.RoleAdmin {
			writeProblem(w, http.StatusForbidden, "forbidden", "需要管理员权限")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// authResponse 是 register/login 的返回体（前端存 token+userId+role）。
type authResponse struct {
	UserID    string    `json:"userId"`
	Username  string    `json:"username"`
	Role      string    `json:"role"`
	Token     string    `json:"token"`
	ExpiresAt time.Time `json:"expiresAt"`
}

type credentials struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

// issueToken 生成随机 token 并落库（附 TTL）。
func (h *handlers) issueToken(r *http.Request, userID string) (string, time.Time, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", time.Time{}, err
	}
	token := hex.EncodeToString(buf)
	exp := time.Now().Add(authTokenTTL)
	if err := h.authTokens.CreateToken(r.Context(), token, userID, exp); err != nil {
		return "", time.Time{}, err
	}
	return token, exp, nil
}

// register：POST /auth/register {username,password} → bcrypt 存 users(role=user) → 发 token。
// 用户名已存在 409。user_id == username（docs/17：历史 owner_id 零迁移归属）。
func (h *handlers) register(w http.ResponseWriter, r *http.Request) {
	if h.users == nil || h.authTokens == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_auth", "鉴权未启用")
		return
	}
	var body credentials
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad_request", "请求体解析失败")
		return
	}
	body.Username = strings.TrimSpace(body.Username)
	if body.Username == "" || len(body.Username) > 64 {
		writeProblem(w, http.StatusBadRequest, "bad_request", "用户名必填且不超过 64 字符")
		return
	}
	if len(body.Password) < 6 {
		writeProblem(w, http.StatusBadRequest, "bad_request", "密码至少 6 位")
		return
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(body.Password), bcrypt.DefaultCost)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "密码加密失败")
		return
	}
	err = h.users.CreateUser(r.Context(), store.User{
		UserID: body.Username, Username: body.Username, PasswordHash: string(hash), Role: store.RoleUser,
	})
	if errors.Is(err, store.ErrUserExists) {
		writeProblem(w, http.StatusConflict, "user_exists", "用户名已被占用")
		return
	}
	if err != nil {
		h.log.Error("register failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "注册失败")
		return
	}
	token, exp, err := h.issueToken(r, body.Username)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "发放 token 失败")
		return
	}
	writeJSON(w, http.StatusOK, authResponse{
		UserID: body.Username, Username: body.Username, Role: store.RoleUser, Token: token, ExpiresAt: exp,
	})
}

// login：POST /auth/login {username,password} → bcrypt 校验 → 新 token。
// 用户不存在或密码错都回同一句「用户名或密码错误」（不泄露用户名是否存在）。
func (h *handlers) login(w http.ResponseWriter, r *http.Request) {
	if h.users == nil || h.authTokens == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_auth", "鉴权未启用")
		return
	}
	var body credentials
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad_request", "请求体解析失败")
		return
	}
	u, err := h.users.GetUserByName(r.Context(), strings.TrimSpace(body.Username))
	if errors.Is(err, store.ErrUserNotFound) {
		writeProblem(w, http.StatusUnauthorized, "invalid_credentials", "用户名或密码错误")
		return
	}
	if err != nil {
		h.log.Error("login lookup failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "登录失败")
		return
	}
	if bcrypt.CompareHashAndPassword([]byte(u.PasswordHash), []byte(body.Password)) != nil {
		writeProblem(w, http.StatusUnauthorized, "invalid_credentials", "用户名或密码错误")
		return
	}
	token, exp, err := h.issueToken(r, u.UserID)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "发放 token 失败")
		return
	}
	writeJSON(w, http.StatusOK, authResponse{
		UserID: u.UserID, Username: u.Username, Role: u.Role, Token: token, ExpiresAt: exp,
	})
}

// logout：POST /auth/logout → 删当前 token（幂等：无 token/已失效也 200）。
func (h *handlers) logout(w http.ResponseWriter, r *http.Request) {
	if h.authTokens != nil {
		if tok := bearerToken(r); tok != "" {
			if err := h.authTokens.DeleteToken(r.Context(), tok); err != nil {
				h.log.Error("logout failed", "err", err)
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// me：GET /auth/me → 当前身份（前端 admin 门控 + 启动校验 token 有效性）。
// 无有效 token → 401（前端据此 logout 清本地失效 token）。
func (h *handlers) me(w http.ResponseWriter, r *http.Request) {
	id, ok := identityFrom(r)
	if !ok {
		writeProblem(w, http.StatusUnauthorized, "unauthorized", "未登录或 token 已失效")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{
		"userId": id.userID, "username": id.userID, "role": id.role,
	})
}
