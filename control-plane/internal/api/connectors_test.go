package api_test

import (
	"bytes"
	"context"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/secret"
	"my-agent/control-plane/internal/store"
)

// —— fakes —— //
type fakeConnectors struct {
	created []store.Connector
	byOwner map[string][]store.Connector
}

func (f *fakeConnectors) Create(_ context.Context, c store.Connector) error {
	f.created = append(f.created, c)
	f.byOwner[c.OwnerID] = append(f.byOwner[c.OwnerID], c)
	return nil
}
func (f *fakeConnectors) ListByOwner(_ context.Context, o string) ([]store.Connector, error) {
	return f.byOwner[o], nil
}
func (f *fakeConnectors) Delete(_ context.Context, o, id string) error {
	if o != "u1" || id != "c1" {
		return store.ErrRunNotFound
	}
	return nil
}
func (f *fakeConnectors) SetEnabled(_ context.Context, o, _ string, _ bool) error {
	if o != "u1" {
		return store.ErrRunNotFound
	}
	return nil
}
func (f *fakeConnectors) ListDue(context.Context, int) ([]store.Connector, error) { return nil, nil }
func (f *fakeConnectors) Claim(context.Context, string) (bool, error)             { return false, nil }
func (f *fakeConnectors) UpdateCursor(context.Context, string, string, string) error {
	return nil
}

type fakeTriggers struct {
	created []store.Trigger
	byOwner map[string][]store.Trigger
}

func (f *fakeTriggers) Create(_ context.Context, t store.Trigger) error {
	f.created = append(f.created, t)
	return nil
}
func (f *fakeTriggers) ListByOwner(_ context.Context, o string) ([]store.Trigger, error) {
	return f.byOwner[o], nil
}
func (f *fakeTriggers) ListByConnector(context.Context, string) ([]store.Trigger, error) {
	return nil, nil
}
func (f *fakeTriggers) Delete(_ context.Context, o, id string) error {
	if o != "u1" || id != "t1" {
		return store.ErrRunNotFound
	}
	return nil
}
func (f *fakeTriggers) SetEnabled(_ context.Context, o, _ string, _ bool) error {
	if o != "u1" {
		return store.ErrRunNotFound
	}
	return nil
}

func testMasterKeyB64() string {
	return base64.StdEncoding.EncodeToString(make([]byte, secret.KeySize))
}

func do(router http.Handler, method, path, body, user string) *httptest.ResponseRecorder {
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, path, strings.NewReader(body))
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	r.Header.Set("X-User-Id", user)
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, r)
	return rec
}

func TestConnectorsEndpoints(t *testing.T) {
	t.Setenv("SECRET_MASTER_KEY", testMasterKeyB64())
	fc := &fakeConnectors{byOwner: map[string][]store.Connector{}}
	ft := &fakeTriggers{byOwner: map[string][]store.Trigger{}}
	router := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, fc, ft, time.Minute, "", discardLogger())

	// 创建：PAT 被 Seal（密文非空且不含明文），Kind 兜底 github、Enabled、owner 归属。
	rec := do(router, http.MethodPost, "/connectors", `{"kind":"github","pat":"ghp_secret123","pollIntervalS":3600}`, "u1")
	if rec.Code != http.StatusOK || len(fc.created) != 1 {
		t.Fatalf("create: %d %s", rec.Code, rec.Body.String())
	}
	c := fc.created[0]
	if c.OwnerID != "u1" || c.Kind != "github" || !c.Enabled || len(c.TokenCiphertext) == 0 {
		t.Fatalf("字段不对: %+v", c)
	}
	if bytes.Contains(c.TokenCiphertext, []byte("ghp_secret123")) {
		t.Fatal("密文包含明文 PAT")
	}
	// 响应体绝不回传 PAT/密文。
	if strings.Contains(rec.Body.String(), "ghp_secret123") || strings.Contains(rec.Body.String(), "tokenCiphertext") {
		t.Fatalf("响应泄露 PAT/密文: %s", rec.Body.String())
	}

	// 校验：间隔<60 / 空 pat / 非 github kind → 400。
	if r := do(router, http.MethodPost, "/connectors", `{"pat":"x","pollIntervalS":10}`, "u1"); r.Code != http.StatusBadRequest {
		t.Fatalf("间隔<60 应 400: %d", r.Code)
	}
	if r := do(router, http.MethodPost, "/connectors", `{"pat":"","pollIntervalS":3600}`, "u1"); r.Code != http.StatusBadRequest {
		t.Fatalf("空 pat 应 400: %d", r.Code)
	}
	if r := do(router, http.MethodPost, "/connectors", `{"kind":"gitlab","pat":"x","pollIntervalS":3600}`, "u1"); r.Code != http.StatusBadRequest {
		t.Fatalf("非 github 应 400: %d", r.Code)
	}

	// 列表（owner 域）。
	if r := do(router, http.MethodGet, "/connectors", "", "u1"); r.Code != http.StatusOK || !strings.Contains(r.Body.String(), "connectors") {
		t.Fatalf("list: %d %s", r.Code, r.Body.String())
	}

	// 删除/开关 owner 隔离。
	if r := do(router, http.MethodDelete, "/connectors/c1", "", "u1"); r.Code != http.StatusOK {
		t.Fatalf("本人删除: %d", r.Code)
	}
	if r := do(router, http.MethodDelete, "/connectors/c1", "", "intruder"); r.Code != http.StatusNotFound {
		t.Fatalf("他人删除应 404: %d", r.Code)
	}
	if r := do(router, http.MethodPost, "/connectors/c1/toggle", `{"enabled":false}`, "u1"); r.Code != http.StatusOK {
		t.Fatalf("toggle: %d", r.Code)
	}
}

func TestTriggersEndpoints(t *testing.T) {
	t.Setenv("SECRET_MASTER_KEY", testMasterKeyB64())
	// 连接器 c1 属 u1（触发规则 connectorId 归属校验依赖它）。
	fc := &fakeConnectors{byOwner: map[string][]store.Connector{"u1": {{ConnectorID: "c1", OwnerID: "u1"}}}}
	ft := &fakeTriggers{byOwner: map[string][]store.Trigger{}}
	router := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, fc, ft, time.Minute, "", discardLogger())

	// 创建：agentType 采纳、needsApproval、filter 落库。
	body := `{"connectorId":"c1","eventType":"issue","queryTemplate":"回复 {{title}}","filter":{"repo":"o/r"},"agentType":"plan_solve","needsApproval":true}`
	if r := do(router, http.MethodPost, "/triggers", body, "u1"); r.Code != http.StatusOK || len(ft.created) != 1 {
		t.Fatalf("create: %d %s", r.Code, r.Body.String())
	}
	tg := ft.created[0]
	if tg.OwnerID != "u1" || tg.AgentType != "plan_solve" || !tg.NeedsApproval || len(tg.Filter) == 0 || !tg.Enabled {
		t.Fatalf("字段不对: %+v", tg)
	}

	// connectorId 非本人 → 400（归属校验）。
	if r := do(router, http.MethodPost, "/triggers", `{"connectorId":"c-notmine","eventType":"issue","queryTemplate":"x"}`, "u1"); r.Code != http.StatusBadRequest {
		t.Fatalf("他人连接器应 400: %d", r.Code)
	}
	// 缺 queryTemplate → 400。
	if r := do(router, http.MethodPost, "/triggers", `{"connectorId":"c1","eventType":"issue"}`, "u1"); r.Code != http.StatusBadRequest {
		t.Fatalf("缺模板应 400: %d", r.Code)
	}

	if r := do(router, http.MethodGet, "/triggers", "", "u1"); r.Code != http.StatusOK {
		t.Fatalf("list: %d", r.Code)
	}
	if r := do(router, http.MethodDelete, "/triggers/t1", "", "u1"); r.Code != http.StatusOK {
		t.Fatalf("本人删除: %d", r.Code)
	}
	if r := do(router, http.MethodDelete, "/triggers/t1", "", "intruder"); r.Code != http.StatusNotFound {
		t.Fatalf("他人删除应 404: %d", r.Code)
	}
	if r := do(router, http.MethodPost, "/triggers/t1/toggle", `{"enabled":false}`, "u1"); r.Code != http.StatusOK {
		t.Fatalf("toggle: %d", r.Code)
	}
}

// 降级：仓库 nil → 503；仓库在但 SECRET_MASTER_KEY 空 → 503（PAT 无从加密）。
func TestConnectorsDowngrade(t *testing.T) {
	// nil 仓库（无 key）→ 503。
	bare := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	if r := do(bare, http.MethodGet, "/connectors", "", "u1"); r.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil 连接器应 503: %d", r.Code)
	}
	if r := do(bare, http.MethodGet, "/triggers", "", "u1"); r.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil 触发规则应 503: %d", r.Code)
	}

	// 仓库已装配但 key 空 → 仍 503（secret master key 空也降级）。
	t.Setenv("SECRET_MASTER_KEY", "")
	fc := &fakeConnectors{byOwner: map[string][]store.Connector{}}
	ft := &fakeTriggers{byOwner: map[string][]store.Trigger{}}
	noKey := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, fc, ft, time.Minute, "", discardLogger())
	if r := do(noKey, http.MethodGet, "/connectors", "", "u1"); r.Code != http.StatusServiceUnavailable {
		t.Fatalf("无 key 连接器应 503: %d", r.Code)
	}
	if r := do(noKey, http.MethodPost, "/connectors", `{"pat":"x","pollIntervalS":3600}`, "u1"); r.Code != http.StatusServiceUnavailable {
		t.Fatalf("无 key 创建应 503: %d", r.Code)
	}
	if r := do(noKey, http.MethodGet, "/triggers", "", "u1"); r.Code != http.StatusServiceUnavailable {
		t.Fatalf("无 key 触发规则应 503: %d", r.Code)
	}
}
