package api_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/store"
)

type fakeSchedules struct {
	created []store.Schedule
	byOwner map[string][]store.Schedule
}

func (f *fakeSchedules) Create(_ context.Context, s store.Schedule) error {
	f.created = append(f.created, s)
	return nil
}
func (f *fakeSchedules) ListByOwner(_ context.Context, owner string) ([]store.Schedule, error) {
	return f.byOwner[owner], nil
}
func (f *fakeSchedules) Delete(_ context.Context, owner, id string) error {
	if owner != "u1" || id != "sc1" {
		return store.ErrRunNotFound
	}
	return nil
}
func (f *fakeSchedules) SetEnabled(_ context.Context, owner, id string, _ bool) error {
	if owner != "u1" {
		return store.ErrRunNotFound
	}
	return nil
}
func (f *fakeSchedules) ListDue(_ context.Context, _ int) ([]store.Schedule, error) { return nil, nil }
func (f *fakeSchedules) Claim(_ context.Context, _, _ string) (bool, error)         { return false, nil }

func TestSchedulesEndpoints(t *testing.T) {
	fs := &fakeSchedules{byOwner: map[string][]store.Schedule{}}
	router := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, fs, nil, nil, nil, nil, time.Minute, "", discardLogger())
	// 创建：默认会话 sched-*、agentType 兜底 react、间隔下限校验。
	req := httptest.NewRequest(http.MethodPost, "/schedules",
		strings.NewReader(`{"query":"每小时巡检","intervalSeconds":3600,"agentType":"weird"}`))
	req.Header.Set("X-User-Id", "u1")
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || len(fs.created) != 1 {
		t.Fatalf("create: %d %s", rec.Code, rec.Body.String())
	}
	c := fs.created[0]
	if c.OwnerID != "u1" || c.AgentType != "react" || !strings.HasPrefix(c.SessionID, "sched-") || !c.Enabled {
		t.Fatalf("创建字段不对: %+v", c)
	}
	// 客户端塞 sessionId 被忽略（评审#10：防写入他人 thread）。
	inj := httptest.NewRequest(http.MethodPost, "/schedules",
		strings.NewReader(`{"query":"注入","intervalSeconds":3600,"sessionId":"victim-session"}`))
	inj.Header.Set("X-User-Id", "u1")
	irec := httptest.NewRecorder()
	router.ServeHTTP(irec, inj)
	if fs.created[len(fs.created)-1].SessionID == "victim-session" {
		t.Fatal("客户端 sessionId 不应被采纳")
	}

	bad := httptest.NewRequest(http.MethodPost, "/schedules",
		strings.NewReader(`{"query":"x","intervalSeconds":10}`))
	bad.Header.Set("X-User-Id", "u1")
	brec := httptest.NewRecorder()
	router.ServeHTTP(brec, bad)
	if brec.Code != http.StatusBadRequest {
		t.Fatalf("间隔<60 应 400: %d", brec.Code)
	}

	// 删除/开关：owner 隔离（fake 内置 u1 语义）。
	del := httptest.NewRequest(http.MethodDelete, "/schedules/sc1", nil)
	del.Header.Set("X-User-Id", "u1")
	drec := httptest.NewRecorder()
	router.ServeHTTP(drec, del)
	if drec.Code != http.StatusOK {
		t.Fatalf("delete: %d", drec.Code)
	}
	del2 := httptest.NewRequest(http.MethodDelete, "/schedules/sc1", nil)
	del2.Header.Set("X-User-Id", "intruder")
	drec2 := httptest.NewRecorder()
	router.ServeHTTP(drec2, del2)
	if drec2.Code != http.StatusNotFound {
		t.Fatalf("他人删除应 404: %d", drec2.Code)
	}

	// nil 降级。
	bare := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	g := httptest.NewRequest(http.MethodGet, "/schedules", nil)
	grec := httptest.NewRecorder()
	bare.ServeHTTP(grec, g)
	if grec.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil schedules 应 503: %d", grec.Code)
	}
}
