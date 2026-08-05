package api_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/store"
)

// —— 内存 fake（供 api 层 auth/admin 测试，无需 DB；照 schedules_test.go 的 fake 范式） ——

type fakeUsers struct {
	mu     sync.Mutex
	byName map[string]store.User
	counts map[string]int64 // ListUsers 的 run 计数
}

func newFakeUsers() *fakeUsers {
	return &fakeUsers{byName: map[string]store.User{}, counts: map[string]int64{}}
}

func (f *fakeUsers) CreateUser(_ context.Context, u store.User) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.byName[u.Username]; ok {
		return store.ErrUserExists
	}
	if u.Role == "" {
		u.Role = store.RoleUser
	}
	u.CreatedAt = time.Now()
	f.byName[u.Username] = u
	return nil
}

func (f *fakeUsers) GetUserByName(_ context.Context, name string) (store.User, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	u, ok := f.byName[name]
	if !ok {
		return store.User{}, store.ErrUserNotFound
	}
	return u, nil
}

func (f *fakeUsers) ListUsers(_ context.Context) ([]store.User, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := []store.User{}
	for _, u := range f.byName {
		u.RunCount = f.counts[u.UserID]
		out = append(out, u)
	}
	return out, nil
}

func (f *fakeUsers) SetRole(_ context.Context, userID, role string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for name, u := range f.byName {
		if u.UserID == userID {
			u.Role = role
			f.byName[name] = u
			return nil
		}
	}
	return store.ErrUserNotFound
}

type fakeTokenRec struct {
	userID  string
	expires time.Time
}

type fakeTokens struct {
	mu    sync.Mutex
	m     map[string]fakeTokenRec
	users *fakeUsers // 解析角色
}

func newFakeTokens(users *fakeUsers) *fakeTokens {
	return &fakeTokens{m: map[string]fakeTokenRec{}, users: users}
}

func (f *fakeTokens) CreateToken(_ context.Context, token, userID string, expiresAt time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.m[token] = fakeTokenRec{userID: userID, expires: expiresAt}
	return nil
}

func (f *fakeTokens) LookupToken(_ context.Context, token string) (string, string, error) {
	f.mu.Lock()
	rec, ok := f.m[token]
	f.mu.Unlock()
	if !ok || time.Now().After(rec.expires) {
		return "", "", store.ErrTokenNotFound
	}
	role := store.RoleUser
	for _, u := range f.users.byName {
		if u.UserID == rec.userID {
			role = u.Role
			break
		}
	}
	return rec.userID, role, nil
}

func (f *fakeTokens) DeleteToken(_ context.Context, token string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.m, token)
	return nil
}

// authRouter 装配一个只有 auth/admin 相关依赖的路由（其余 nil）。
func authRouter(users store.UserRepository, tokens store.SessionTokenRepository) http.Handler {
	return api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, users, tokens, nil, nil, time.Minute, "", discardLogger())
}

func doJSON(t *testing.T, router http.Handler, method, path, body, bearer, xuser string) *httptest.ResponseRecorder {
	t.Helper()
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	if bearer != "" {
		r.Header.Set("Authorization", "Bearer "+bearer)
	}
	if xuser != "" {
		r.Header.Set("X-User-Id", xuser)
	}
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, r)
	return rec
}

// 注册 → 登录 → me → logout 全链路 + 唯一冲突 + 密码错误。
func TestAuthRegisterLoginLogoutMe(t *testing.T) {
	users := newFakeUsers()
	tokens := newFakeTokens(users)
	router := authRouter(users, tokens)

	// 注册。
	rec := doJSON(t, router, http.MethodPost, "/auth/register", `{"username":"alice","password":"secret123"}`, "", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("register: %d %s", rec.Code, rec.Body.String())
	}
	var reg struct{ UserID, Role, Token string }
	_ = json.Unmarshal(rec.Body.Bytes(), &reg)
	if reg.UserID != "alice" || reg.Role != store.RoleUser || reg.Token == "" {
		t.Fatalf("register 响应不对: %+v", reg)
	}

	// 重复用户名 → 409。
	if rec := doJSON(t, router, http.MethodPost, "/auth/register", `{"username":"alice","password":"secret123"}`, "", ""); rec.Code != http.StatusConflict {
		t.Fatalf("重复注册应 409: %d", rec.Code)
	}
	// 短密码 → 400。
	if rec := doJSON(t, router, http.MethodPost, "/auth/register", `{"username":"bob","password":"x"}`, "", ""); rec.Code != http.StatusBadRequest {
		t.Fatalf("短密码应 400: %d", rec.Code)
	}

	// 登录成功。
	rec = doJSON(t, router, http.MethodPost, "/auth/login", `{"username":"alice","password":"secret123"}`, "", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("login: %d %s", rec.Code, rec.Body.String())
	}
	var lg struct{ Token, Role string }
	_ = json.Unmarshal(rec.Body.Bytes(), &lg)
	if lg.Token == "" {
		t.Fatalf("login 无 token")
	}
	// 密码错 → 401；未知用户 → 401。
	if rec := doJSON(t, router, http.MethodPost, "/auth/login", `{"username":"alice","password":"wrong"}`, "", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("密码错应 401: %d", rec.Code)
	}
	if rec := doJSON(t, router, http.MethodPost, "/auth/login", `{"username":"ghost","password":"secret123"}`, "", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("未知用户应 401: %d", rec.Code)
	}

	// me（带 token）→ 200 身份。
	rec = doJSON(t, router, http.MethodGet, "/auth/me", "", lg.Token, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("me: %d", rec.Code)
	}
	var me struct{ UserID, Role string }
	_ = json.Unmarshal(rec.Body.Bytes(), &me)
	if me.UserID != "alice" || me.Role != store.RoleUser {
		t.Fatalf("me 身份不对: %+v", me)
	}
	// me（无 token）→ 401。
	if rec := doJSON(t, router, http.MethodGet, "/auth/me", "", "", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("无 token me 应 401: %d", rec.Code)
	}

	// logout 后 token 失效 → me 401。
	if rec := doJSON(t, router, http.MethodPost, "/auth/logout", "", lg.Token, ""); rec.Code != http.StatusOK {
		t.Fatalf("logout: %d", rec.Code)
	}
	if rec := doJSON(t, router, http.MethodGet, "/auth/me", "", lg.Token, ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("logout 后 me 应 401: %d", rec.Code)
	}
}

// nil repo → auth 端点 503 降级。
func TestAuthDegradesWhenNil(t *testing.T) {
	router := authRouter(nil, nil)
	if rec := doJSON(t, router, http.MethodPost, "/auth/register", `{"username":"a","password":"secret123"}`, "", ""); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil users 注册应 503: %d", rec.Code)
	}
	if rec := doJSON(t, router, http.MethodPost, "/auth/login", `{"username":"a","password":"secret123"}`, "", ""); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil users 登录应 503: %d", rec.Code)
	}
}

// resolveIdentity：token→身份（me 反映）；requireAdmin：普通用户 403、admin 200、无 token 403。
func TestRequireAdmin(t *testing.T) {
	users := newFakeUsers()
	tokens := newFakeTokens(users)
	_ = users.CreateUser(context.Background(), store.User{UserID: "root", Username: "root", Role: store.RoleAdmin})
	_ = users.CreateUser(context.Background(), store.User{UserID: "bob", Username: "bob", Role: store.RoleUser})
	_ = tokens.CreateToken(context.Background(), "t-root", "root", time.Now().Add(time.Hour))
	_ = tokens.CreateToken(context.Background(), "t-bob", "bob", time.Now().Add(time.Hour))
	router := authRouter(users, tokens)

	if rec := doJSON(t, router, http.MethodGet, "/admin/users", "", "t-bob", ""); rec.Code != http.StatusForbidden {
		t.Fatalf("普通用户访问 admin 应 403: %d", rec.Code)
	}
	if rec := doJSON(t, router, http.MethodGet, "/admin/users", "", "", ""); rec.Code != http.StatusForbidden {
		t.Fatalf("无 token 访问 admin 应 403: %d", rec.Code)
	}
	if rec := doJSON(t, router, http.MethodGet, "/admin/users", "", "t-root", ""); rec.Code != http.StatusOK {
		t.Fatalf("admin 访问应 200: %d %s", rec.Code, rec.Body.String())
	}
}

// AUTH_REQUIRED 开启：受保护 API 无有效 token → 401；带 token 放行；公共/静态路径免 token。
// 反向白名单确保 D2 后加的 owner 域端点（/connectors、/triggers）默认即受保护——防跨租户越权。
// 文档一律用 AUTH_REQUIRED=1 作启用值，故 "true" 与 "1" 两种取值都必须真正启用鉴权。
func TestAuthRequiredEnforcement(t *testing.T) {
	for _, enabled := range []string{"true", "1"} {
		t.Run("AUTH_REQUIRED="+enabled, func(t *testing.T) {
			t.Setenv("AUTH_REQUIRED", enabled)
			users := newFakeUsers()
			tokens := newFakeTokens(users)
			_ = users.CreateUser(context.Background(), store.User{UserID: "alice", Username: "alice", Role: store.RoleUser})
			_ = tokens.CreateToken(context.Background(), "t-alice", "alice", time.Now().Add(time.Hour))
			router := authRouter(users, tokens)

			// 受保护 API 无 token → 401（X-User-Id 头被忽略）。逐一覆盖既有端点 + D2 新增
			// owner 域端点（GET/POST/DELETE /connectors、/triggers 及其子路由）+ admin 分组路由。
			protected := []struct{ method, path string }{
				{http.MethodGet, "/sessions"},
				{http.MethodPost, "/runs"},
				{http.MethodGet, "/connectors"},
				{http.MethodPost, "/connectors"},
				{http.MethodDelete, "/connectors/c1"},
				{http.MethodPost, "/connectors/c1/toggle"},
				{http.MethodGet, "/triggers"},
				{http.MethodPost, "/triggers"},
				{http.MethodDelete, "/triggers/t1"},
				{http.MethodPost, "/triggers/t1/toggle"},
				{http.MethodGet, "/admin/users"},
			}
			for _, tc := range protected {
				body := ""
				if tc.method == http.MethodPost {
					body = "{}"
				}
				// 仅带可伪造的 X-User-Id 头、无 Authorization → 必须 401（头被忽略，杜绝冒充越权）。
				if rec := doJSON(t, router, tc.method, tc.path, body, "", "attacker"); rec.Code != http.StatusUnauthorized {
					t.Fatalf("AUTH_REQUIRED 下 %s %s 无 token 应 401（防跨租户越权）: %d %s", tc.method, tc.path, rec.Code, rec.Body.String())
				}
			}

			// 带有效 token → 过中间件（sessions repo nil → 503，但绝不是 401）。
			if rec := doJSON(t, router, http.MethodGet, "/sessions", "", "t-alice", ""); rec.Code == http.StatusUnauthorized {
				t.Fatalf("带 token 不应 401: %d", rec.Code)
			}
			// 放行清单免 token：/auth/register 无 token 也能过中间件（200 证明未被 401）。
			if rec := doJSON(t, router, http.MethodPost, "/auth/register", `{"username":"newbie","password":"secret123"}`, "", ""); rec.Code != http.StatusOK {
				t.Fatalf("/auth/register 不应被 AUTH_REQUIRED 拦截: %d %s", rec.Code, rec.Body.String())
			}
			// 注册后即可登录（200 证明 /auth/login 也免 token）。
			if rec := doJSON(t, router, http.MethodPost, "/auth/login", `{"username":"newbie","password":"secret123"}`, "", ""); rec.Code != http.StatusOK {
				t.Fatalf("/auth/login 不应被 AUTH_REQUIRED 拦截: %d", rec.Code)
			}
			// /healthz、/metrics 属放行清单：无 token 亦不得 401（否则探针/指标抓取失效）。
			for _, p := range []string{"/healthz", "/metrics"} {
				if rec := doJSON(t, router, http.MethodGet, p, "", "", ""); rec.Code == http.StatusUnauthorized {
					t.Fatalf("%s 不应被 AUTH_REQUIRED 401: %d", p, rec.Code)
				}
			}
		})
	}
}
