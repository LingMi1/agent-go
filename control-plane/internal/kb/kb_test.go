package kb

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAggregateDocs(t *testing.T) {
	pts := []scrollPoint{
		{Payload: map[string]any{"source_id": "uploads/u/s/aa-a.txt", "file_name": "a.txt", "created": float64(100)}},
		{Payload: map[string]any{"source_id": "uploads/u/s/aa-a.txt", "file_name": "a.txt", "created": float64(105)}},
		{Payload: map[string]any{"source_id": "uploads/u/s/bb-b.csv", "file_name": "b.csv", "created": float64(200)}},
		{Payload: map[string]any{}}, // 无 source_id 的脏点跳过
	}
	docs := AggregateDocs(pts)
	if len(docs) != 2 {
		t.Fatalf("expected 2 docs, got %+v", docs)
	}
	// 新到旧排序：b.csv(200) 在前；a.txt 聚合 2 chunk 且 created 取最早。
	if docs[0].FileName != "b.csv" || docs[1].FileName != "a.txt" {
		t.Fatalf("order wrong: %+v", docs)
	}
	if docs[1].Chunks != 2 || docs[1].CreatedAt != 100 {
		t.Fatalf("aggregate wrong: %+v", docs[1])
	}
}

// 假 Qdrant：scroll 两页 + delete 记录过滤条件。
func TestClientListAndDelete(t *testing.T) {
	var deleteBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/collections/docs/points/scroll":
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["offset"] == nil { // 首页 → 带 next_page_offset
				_ = json.NewEncoder(w).Encode(map[string]any{"result": map[string]any{
					"points": []map[string]any{
						{"payload": map[string]any{"source_id": "s1", "file_name": "f1.txt", "created": 10}},
					},
					"next_page_offset": "cursor-2",
				}})
			} else {
				_ = json.NewEncoder(w).Encode(map[string]any{"result": map[string]any{
					"points": []map[string]any{
						{"payload": map[string]any{"source_id": "s1", "file_name": "f1.txt", "created": 11}},
						{"payload": map[string]any{"source_id": "s2", "file_name": "f2.md", "created": 20}},
					},
					"next_page_offset": nil,
				}})
			}
		case "/collections/docs/points/delete":
			_ = json.NewDecoder(r.Body).Decode(&deleteBody)
			_ = json.NewEncoder(w).Encode(map[string]any{"result": true})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	c := NewClient(srv.URL, "docs")
	docs, err := c.ListDocs(context.Background(), "owner:u1")
	if err != nil {
		t.Fatalf("ListDocs: %v", err)
	}
	if len(docs) != 2 || docs[0].SourceID != "s2" || docs[1].Chunks != 2 {
		t.Fatalf("scroll 分页聚合错误: %+v", docs)
	}

	if err := c.DeleteDoc(context.Background(), "owner:u1", "s1"); err != nil {
		t.Fatalf("DeleteDoc: %v", err)
	}
	// 删除过滤必须同时含 kb_id 与 source_id 两个条件（少一个=删多）。
	buf, _ := json.Marshal(deleteBody)
	for _, want := range []string{`"kb_id"`, `"owner:u1"`, `"source_id"`, `"s1"`} {
		if !json.Valid(buf) || !contains(string(buf), want) {
			t.Fatalf("delete filter 缺少 %s: %s", want, buf)
		}
	}
}

func TestNewClientNilOnEmpty(t *testing.T) {
	if NewClient("", "docs") != nil || NewClient("http://x", "") != nil {
		t.Fatal("空配置应返回 nil（路由降级 503）")
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (s == sub || len(s) > 0 && (stringIndex(s, sub) >= 0))
}

func stringIndex(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func TestFreshDeploy404ReturnsEmpty(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r) // 集合未建：Qdrant 对 scroll/delete 返回 404
	}))
	defer srv.Close()
	c := NewClient(srv.URL, "docs")
	// 首次部署列表：空而非报错。
	docs, err := c.ListDocs(context.Background(), "owner:u1")
	if err != nil || len(docs) != 0 {
		t.Fatalf("fresh deploy list should be empty, got %v err=%v", docs, err)
	}
	// 删除亦无操作不报错。
	if err := c.DeleteDoc(context.Background(), "owner:u1", "s1"); err != nil {
		t.Fatalf("delete on missing collection should no-op, got %v", err)
	}
}
