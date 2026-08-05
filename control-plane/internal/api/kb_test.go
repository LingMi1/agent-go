package api_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/cognition"
	"my-agent/control-plane/internal/kb"
)

// fakeKB 记录调用入参（断言 kb_id 恒由请求者身份推导）。
type fakeKB struct {
	lastListKb   string
	lastDelKb    string
	lastDelSrc   string
	docs         []kb.DocInfo
	deleteCalled bool
}

func (f *fakeKB) ListDocs(_ context.Context, kbID string) ([]kb.DocInfo, error) {
	f.lastListKb = kbID
	return f.docs, nil
}

func (f *fakeKB) DeleteDoc(_ context.Context, kbID, sourceID string) error {
	f.deleteCalled, f.lastDelKb, f.lastDelSrc = true, kbID, sourceID
	return nil
}

// fakeIngestCog 只实现 IngestDocument 相关行为（其余 panic 即测试即失败）。
type fakeIngestCog struct {
	cognition.Client
	lastOwner string
	lastKey   string
}

func (f *fakeIngestCog) IngestDocument(_ context.Context, ownerID string, att cognition.Attachment) (bool, string, string, error) {
	f.lastOwner, f.lastKey = ownerID, att.ResourceKey
	return true, "owner:" + ownerID, "", nil
}

func TestKbDocsEndpoints(t *testing.T) {
	fk := &fakeKB{docs: []kb.DocInfo{
		{SourceID: "uploads/u1/s/aa-a.txt", FileName: "a.txt", Chunks: 3, CreatedAt: 100},
		{SourceID: "corpus-x", FileName: "x.md", Chunks: 1, CreatedAt: 50},
	}}
	fc := &fakeIngestCog{}
	router := api.NewRouter(nil, nil, nil, nil, nil, nil, fk, fc, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	// GET：kb 归属由 X-User-Id 推导；uploads 来源带 downloadUrl，脚本灌库来源不带。
	req := httptest.NewRequest(http.MethodGet, "/kb/docs", nil)
	req.Header.Set("X-User-Id", "u1")
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || fk.lastListKb != "owner:u1" {
		t.Fatalf("list: %d kb=%s", rec.Code, fk.lastListKb)
	}
	body := rec.Body.String()
	if !strings.Contains(body, `"downloadUrl":"/artifacts/uploads/u1/s/aa-a.txt"`) {
		t.Fatalf("uploads 来源应带 downloadUrl: %s", body)
	}
	if strings.Contains(body, `"downloadUrl":"/artifacts/corpus-x"`) {
		t.Fatalf("非 uploads 来源不应带 downloadUrl: %s", body)
	}

	// DELETE：kb 恒为本人；缺 source 400。
	req2 := httptest.NewRequest(http.MethodDelete, "/kb/docs?source=uploads/u1/s/aa-a.txt", nil)
	req2.Header.Set("X-User-Id", "u1")
	rec2 := httptest.NewRecorder()
	router.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusOK || fk.lastDelKb != "owner:u1" || fk.lastDelSrc != "uploads/u1/s/aa-a.txt" {
		t.Fatalf("delete: %d %s %s", rec2.Code, fk.lastDelKb, fk.lastDelSrc)
	}
	req3 := httptest.NewRequest(http.MethodDelete, "/kb/docs", nil)
	req3.Header.Set("X-User-Id", "u1")
	rec3 := httptest.NewRecorder()
	router.ServeHTTP(rec3, req3)
	if rec3.Code != http.StatusBadRequest {
		t.Fatalf("missing source: %d", rec3.Code)
	}

	// POST：伪造他人 key 403；本人 key 透传认知面。
	req4 := httptest.NewRequest(http.MethodPost, "/kb/docs",
		strings.NewReader(`{"resourceKey":"uploads/u2/s/zz-b.txt","fileName":"b.txt"}`))
	req4.Header.Set("X-User-Id", "u1")
	rec4 := httptest.NewRecorder()
	router.ServeHTTP(rec4, req4)
	if rec4.Code != http.StatusForbidden {
		t.Fatalf("forged key: %d", rec4.Code)
	}
	req5 := httptest.NewRequest(http.MethodPost, "/kb/docs",
		strings.NewReader(`{"resourceKey":"uploads/u1/s/aa-a.txt","fileName":"a.txt","mimeType":"text/plain","size":6}`))
	req5.Header.Set("X-User-Id", "u1")
	rec5 := httptest.NewRecorder()
	router.ServeHTTP(rec5, req5)
	if rec5.Code != http.StatusOK || fc.lastOwner != "u1" || fc.lastKey != "uploads/u1/s/aa-a.txt" {
		t.Fatalf("ingest: %d owner=%s key=%s body=%s", rec5.Code, fc.lastOwner, fc.lastKey, rec5.Body.String())
	}
	if !strings.Contains(rec5.Body.String(), `"ok":true`) {
		t.Fatalf("ingest body: %s", rec5.Body.String())
	}

	// nil 依赖降级 503。
	bare := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	for _, m := range []string{http.MethodGet, http.MethodDelete} {
		r := httptest.NewRequest(m, "/kb/docs?source=x", nil)
		w := httptest.NewRecorder()
		bare.ServeHTTP(w, r)
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("%s nil kb: %d", m, w.Code)
		}
	}
	r6 := httptest.NewRequest(http.MethodPost, "/kb/docs", strings.NewReader(`{"resourceKey":"uploads/u1/s/k"}`))
	w6 := httptest.NewRecorder()
	bare.ServeHTTP(w6, r6)
	if w6.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil cog: %d", w6.Code)
	}
}
