package api_test

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/artifact"
	"my-agent/control-plane/internal/store"
)

// 假 RunRepository（仅 GetRun 用于产物 ownership 校验）。
type fakeRuns struct{ runs map[string]store.Run }

func (f *fakeRuns) CreateRun(context.Context, store.CreateRunParams) error { return nil }
func (f *fakeRuns) FinishRun(context.Context, store.FinishRunParams) error { return nil }
func (f *fakeRuns) GetRun(_ context.Context, id string) (store.Run, error) {
	r, ok := f.runs[id]
	if !ok {
		return store.Run{}, store.ErrRunNotFound
	}
	return r, nil
}

type fakeArtifacts struct{ objs map[string][]byte }

func (f *fakeArtifacts) EnsureBucket(context.Context) error { return nil }
func (f *fakeArtifacts) Open(_ context.Context, key string) (*artifact.Object, error) {
	b, ok := f.objs[key]
	if !ok {
		return nil, artifact.ErrNotFound
	}
	return &artifact.Object{Body: io.NopCloser(bytes.NewReader(b)), ContentType: "text/markdown", Size: int64(len(b))}, nil
}
func (f *fakeArtifacts) Put(_ context.Context, key string, body io.Reader, _ int64, _ string) error {
	b, err := io.ReadAll(body)
	if err != nil {
		return err
	}
	f.objs[key] = b
	return nil
}

func TestArtifactProxy_Ownership(t *testing.T) {
	runs := &fakeRuns{runs: map[string]store.Run{"run1": {RunID: "run1", OwnerID: "u1"}}}
	arts := &fakeArtifacts{objs: map[string][]byte{"run1/tc1/report.md": []byte("# 报告\n14")}}
	router := api.NewRouter(nil, runs, nil, nil, arts, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	do := func(path, user string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req.Header.Set("X-User-Id", user)
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		return rec
	}

	if rec := do("/artifacts/run1/tc1/report.md", "u1"); rec.Code != http.StatusOK || rec.Body.String() != "# 报告\n14" {
		t.Fatalf("owner: expected 200+body, got %d %q", rec.Code, rec.Body.String())
	}
	if rec := do("/artifacts/run1/tc1/report.md", "intruder"); rec.Code != http.StatusForbidden {
		t.Fatalf("other owner: expected 403, got %d", rec.Code)
	}
	if rec := do("/artifacts/nope/x/y", "u1"); rec.Code != http.StatusNotFound {
		t.Fatalf("missing run: expected 404, got %d", rec.Code)
	}
	if rec := do("/artifacts/run1/tc1/missing.md", "u1"); rec.Code != http.StatusNotFound {
		t.Fatalf("missing object: expected 404, got %d", rec.Code)
	}
}

// M8：uploads/ 前缀走 owner 段比对（免查库），不进 runID 反查。
func TestArtifactProxy_UploadsOwnership(t *testing.T) {
	runs := &fakeRuns{runs: map[string]store.Run{}} // 无任何 run：uploads 分支不得依赖 GetRun
	arts := &fakeArtifacts{objs: map[string][]byte{"uploads/u1/s1/ab12-a.txt": []byte("内容")}}
	router := api.NewRouter(nil, runs, nil, nil, arts, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	do := func(path, user string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req.Header.Set("X-User-Id", user)
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		return rec
	}

	if rec := do("/artifacts/uploads/u1/s1/ab12-a.txt", "u1"); rec.Code != http.StatusOK || rec.Body.String() != "内容" {
		t.Fatalf("owner upload: expected 200, got %d %q", rec.Code, rec.Body.String())
	}
	if rec := do("/artifacts/uploads/u1/s1/ab12-a.txt", "intruder"); rec.Code != http.StatusForbidden {
		t.Fatalf("other owner upload: expected 403, got %d", rec.Code)
	}
	if rec := do("/artifacts/uploads/u1/s1/nope.txt", "u1"); rec.Code != http.StatusNotFound {
		t.Fatalf("missing upload: expected 404, got %d", rec.Code)
	}
	// 畸形 uploads key（缺段）→ owner 段为空 → 403，不会 panic/放行。
	if rec := do("/artifacts/uploads/u1", "u1"); rec.Code != http.StatusForbidden {
		t.Fatalf("malformed uploads key: expected 403, got %d", rec.Code)
	}
}
