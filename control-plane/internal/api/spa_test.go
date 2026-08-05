package api_test

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
)

// SPA 静态托管的四个行为锁：真实文件直出 / 未知 GET 回退 index.html /
// 非 GET 未匹配保 JSON 404 / 已注册 API 路由不被遮蔽。
func TestSPAHandler(t *testing.T) {
	webDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(webDir, "index.html"), []byte(`<div id="root"></div>`), 0o644); err != nil {
		t.Fatalf("write index: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(webDir, "assets"), 0o755); err != nil {
		t.Fatalf("mkdir assets: %v", err)
	}
	if err := os.WriteFile(filepath.Join(webDir, "assets", "app.js"), []byte(`console.log(1)`), 0o644); err != nil {
		t.Fatalf("write asset: %v", err)
	}

	router := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, webDir, discardLogger())
	do := func(method, path string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, httptest.NewRequest(method, path, nil))
		return rec
	}

	// 1) 根路径与未知 SPA 路径 → index.html（no-cache）。
	for _, p := range []string{"/", "/some/spa/route"} {
		rec := do(http.MethodGet, p)
		if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `id="root"`) {
			t.Fatalf("GET %s: expected index.html, got %d %q", p, rec.Code, rec.Body.String())
		}
		if rec.Header().Get("Cache-Control") != "no-cache" {
			t.Fatalf("GET %s: index 应带 no-cache", p)
		}
	}

	// 2) 真实静态文件直出。
	if rec := do(http.MethodGet, "/assets/app.js"); rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "console.log") {
		t.Fatalf("asset serve failed: %d %q", rec.Code, rec.Body.String())
	}

	// 3) 非 GET 的未匹配路径 → JSON 404（不吐 HTML）。
	rec := do(http.MethodPost, "/nope")
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), `"not_found"`) {
		t.Fatalf("POST /nope: expected JSON 404, got %d %q", rec.Code, rec.Body.String())
	}

	// 4) 已注册 API 路由不被遮蔽（/healthz 仍是 JSON）。
	if rec := do(http.MethodGet, "/healthz"); rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"healthy"`) {
		t.Fatalf("/healthz shadowed: %d %q", rec.Code, rec.Body.String())
	}

	// 5) 路径穿越拒绝：自身前缀校验 + http.ServeFile 内建 dotdot 防护（含 .. 的请求
	// 直接 400），双层防护下绝不泄漏 webDir 外文件。
	if rec := do(http.MethodGet, "/../etc/passwd"); rec.Code != http.StatusBadRequest && rec.Code != http.StatusNotFound {
		t.Fatalf("traversal: expected 400/404, got %d %q", rec.Code, rec.Body.String())
	}

	// 6) webDir 为空 → 不注册回退，根路径 404（dev 模式行为不变）。
	bare := api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	rec2 := httptest.NewRecorder()
	bare.ServeHTTP(rec2, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec2.Code != http.StatusNotFound {
		t.Fatalf("empty webDir: expected 404 on /, got %d", rec2.Code)
	}
}
