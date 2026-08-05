package api_test

import (
	"bytes"
	"encoding/json"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/store"
)

// —— 纯函数族（表驱动）——

func TestAllowedUpload(t *testing.T) {
	cases := []struct {
		mime, name string
		want       bool
	}{
		{"image/png", "a.png", true},
		{"image/jpeg", "a.jpg", true},
		{"text/plain", "a.txt", true},
		{"text/markdown; charset=utf-8", "a.md", true}, // 带参数的 MIME
		{"application/pdf", "a.pdf", true},
		{"application/json", "a.json", true},
		{"", "notes.md", true},                         // 空 MIME 扩展名兜底
		{"application/octet-stream", "data.csv", true}, // 浏览器常见误报
		{"application/octet-stream", "app.exe", false}, // 可执行拒绝
		{"application/zip", "a.zip", false},
		{"video/mp4", "a.mp4", false},
		// office 文档自 M12 起放行（认知面支持 docx/xlsx 文本提取入库）。
		{"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx", true},
		{"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "a.xlsx", true},
		{"application/octet-stream", "sheet.xlsx", true}, // 扩展名兜底
	}
	for _, c := range cases {
		if got := api.AllowedUpload(c.mime, c.name); got != c.want {
			t.Errorf("AllowedUpload(%q,%q)=%v want %v", c.mime, c.name, got, c.want)
		}
	}
}

func TestSanitizeUploadFileName(t *testing.T) {
	cases := map[string]string{
		"报告 v2.md":         "报告-v2.md",
		"../../etc/passwd": "passwd",
		"a\\b\\c.txt":      "c.txt",
		"weird*chars?.png": "weird-chars-.png", // 危险字符逐个转 -（保留扩展名即可）
		"":                 "file",
		"...":              "file",
	}
	for in, want := range cases {
		if got := api.SanitizeUploadFileName(in); got != want {
			t.Errorf("Sanitize(%q)=%q want %q", in, got, want)
		}
	}
}

func TestUploadKeyOwnershipFuncs(t *testing.T) {
	key := api.BuildUploadKey("u1", "s1", "ab12cd34", "a.txt")
	if key != "uploads/u1/s1/ab12cd34-a.txt" {
		t.Fatalf("key=%q", key)
	}
	if api.OwnerOfUploadKey(key) != "u1" {
		t.Fatalf("owner=%q", api.OwnerOfUploadKey(key))
	}
	if api.OwnerOfUploadKey("run1/tc1/a.md") != "" || api.OwnerOfUploadKey("uploads/u1") != "" {
		t.Fatalf("non-upload/malformed key 应返回空 owner")
	}
	// 防伪造闸门：他人 key / 运行产物 key / 空 owner 一律拒。
	if !api.ValidateAttachmentKey("u1", key) {
		t.Fatal("own key should pass")
	}
	if api.ValidateAttachmentKey("u2", key) || api.ValidateAttachmentKey("u1", "run1/tc1/a.md") ||
		api.ValidateAttachmentKey("", "uploads//s/x-a.txt") {
		t.Fatal("forged keys must be rejected")
	}
	// 无 session 归档到 misc。
	if k := api.BuildUploadKey("u1", "", "x", "f.txt"); k != "uploads/u1/misc/x-f.txt" {
		t.Fatalf("default session: %q", k)
	}
}

// —— POST /uploads handler（multipart 全路径）——

func _multipart(t *testing.T, field, name, mimeType string, data []byte) (*bytes.Buffer, string) {
	t.Helper()
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	h := make(map[string][]string)
	h["Content-Disposition"] = []string{`form-data; name="` + field + `"; filename="` + name + `"`}
	if mimeType != "" {
		h["Content-Type"] = []string{mimeType}
	}
	part, err := w.CreatePart(h)
	if err != nil {
		t.Fatalf("create part: %v", err)
	}
	_, _ = part.Write(data)
	_ = w.Close()
	return &buf, w.FormDataContentType()
}

func TestUploadHandler(t *testing.T) {
	arts := &fakeArtifacts{objs: map[string][]byte{}}
	runs := &fakeRuns{runs: map[string]store.Run{}}
	router := api.NewRouter(nil, runs, nil, nil, arts, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	do := func(body *bytes.Buffer, ct, user string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/uploads?sessionId=s1", body)
		req.Header.Set("Content-Type", ct)
		req.Header.Set("X-User-Id", user)
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		return rec
	}

	// 1) 正常上传：200，key 形状 uploads/{owner}/{session}/{uuid8}-{name}，对象落存储。
	body, ct := _multipart(t, "file", "笔记.md", "text/markdown", []byte("# hi"))
	rec := do(body, ct, "u1")
	if rec.Code != http.StatusOK {
		t.Fatalf("upload: %d %s", rec.Code, rec.Body.String())
	}
	var resp map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	key, _ := resp["resourceKey"].(string)
	if !strings.HasPrefix(key, "uploads/u1/s1/") || !strings.HasSuffix(key, "-笔记.md") {
		t.Fatalf("key shape: %q", key)
	}
	if len(strings.Split(key, "/")) != 4 {
		t.Fatalf("key segments: %q", key)
	}
	if string(arts.objs[key]) != "# hi" {
		t.Fatalf("object not stored")
	}
	if resp["mimeType"] != "text/markdown" || resp["size"] != float64(4) {
		t.Fatalf("meta wrong: %v", resp)
	}

	// 2) 白名单外类型 → 415。
	body2, ct2 := _multipart(t, "file", "v.mp4", "video/mp4", []byte("xx"))
	if rec := do(body2, ct2, "u1"); rec.Code != http.StatusUnsupportedMediaType {
		t.Fatalf("mp4: expected 415, got %d", rec.Code)
	}

	// 3) 缺 file 字段 → 400。
	body3, ct3 := _multipart(t, "not_file", "a.txt", "text/plain", []byte("x"))
	if rec := do(body3, ct3, "u1"); rec.Code != http.StatusBadRequest {
		t.Fatalf("missing field: expected 400, got %d", rec.Code)
	}

	// 4) 超大小上限 → 413（MAX_UPLOAD_BYTES 环境变量可配）。
	t.Setenv("MAX_UPLOAD_BYTES", "10")
	smallRouter := api.NewRouter(nil, runs, nil, nil, arts, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	body4, ct4 := _multipart(t, "file", "big.txt", "text/plain", bytes.Repeat([]byte("a"), 1024))
	req := httptest.NewRequest(http.MethodPost, "/uploads", body4)
	req.Header.Set("Content-Type", ct4)
	req.Header.Set("X-User-Id", "u1")
	rec4 := httptest.NewRecorder()
	smallRouter.ServeHTTP(rec4, req)
	if rec4.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversize: expected 413, got %d %s", rec4.Code, rec4.Body.String())
	}

	// 5) 存储未配置 → 503。
	nilRouter := api.NewRouter(nil, runs, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	body5, ct5 := _multipart(t, "file", "a.txt", "text/plain", []byte("x"))
	req5 := httptest.NewRequest(http.MethodPost, "/uploads", body5)
	req5.Header.Set("Content-Type", ct5)
	req5.Header.Set("X-User-Id", "u1")
	rec5 := httptest.NewRecorder()
	nilRouter.ServeHTTP(rec5, req5)
	if rec5.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil store: expected 503, got %d", rec5.Code)
	}
}
