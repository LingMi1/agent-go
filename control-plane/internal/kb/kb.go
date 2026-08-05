// Package kb：用户知识库的管理读写（Files 面板，UX-1）。
//
// 通道取舍：读/删走 Go 直连 Qdrant REST——纯管理操作不涉及向量化（embedding 是认知域
// 能力），与 Go 直连 MinIO 代理产物同一先例；"上传即入库"仍走认知面 gRPC
// IngestDocument（见 internal/cognition）。这是控制面第一个出站 HTTP 客户端。
//
// 删除语义（对齐业界，docs/08 §6.5.3）：只删向量点，不删 MinIO 对象——历史会话的
// pro_attachment 引用块续聊展开、附件下载都还依赖原对象；删除只影响"此后的检索"。
package kb

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"
)

// ErrCollectionMissing：集合尚未创建（Qdrant 404）。首次入库前 scroll 会命中——
// 属"知识库为空"而非故障，ListDocs 据此返回空列表而非 502。
var ErrCollectionMissing = errors.New("kb: qdrant collection not found")

// DocInfo 是知识库中一份文档的聚合视图（多个 chunk 点聚合为一行）。
type DocInfo struct {
	SourceID  string `json:"sourceId"` // 入库时的 source_id（上传文件=resource_key）
	FileName  string `json:"fileName"`
	Chunks    int    `json:"chunks"`
	CreatedAt int64  `json:"createdAt"` // 最早 chunk 的 created（unix 秒）
}

// Store 是 api 层依赖的最小接口（测试注入 fake）。
type Store interface {
	ListDocs(ctx context.Context, kbID string) ([]DocInfo, error)
	DeleteDoc(ctx context.Context, kbID, sourceID string) error
}

// Client 直连 Qdrant REST。
type Client struct {
	base       string
	collection string
	http       *http.Client
}

// NewClient 构造 Qdrant REST 客户端；url 为空返回 nil（路由降级 503）。
func NewClient(qdrantURL, collection string) *Client {
	u := strings.TrimRight(strings.TrimSpace(qdrantURL), "/")
	if u == "" || collection == "" {
		return nil
	}
	return &Client{base: u, collection: collection, http: &http.Client{Timeout: 15 * time.Second}}
}

// —— 请求体构造与聚合（纯函数，可测）——

func kbFilter(kbID string, sourceID string) map[string]any {
	must := []map[string]any{
		{"key": "kb_id", "match": map[string]any{"value": kbID}},
	}
	if sourceID != "" {
		must = append(must, map[string]any{"key": "source_id", "match": map[string]any{"value": sourceID}})
	}
	return map[string]any{"must": must}
}

// ScrollBody 组装 scroll 请求体（offset 为上一页 next_page_offset，nil=首页）。
func ScrollBody(kbID string, offset any) map[string]any {
	body := map[string]any{
		"filter":       kbFilter(kbID, ""),
		"limit":        256,
		"with_payload": []string{"source_id", "file_name", "created"},
		"with_vector":  false,
	}
	if offset != nil {
		body["offset"] = offset
	}
	return body
}

type scrollPoint struct {
	Payload map[string]any `json:"payload"`
}

// AggregateDocs 把 chunk 级点聚合成文档级列表（按 source_id 聚合，新到旧排序）。
func AggregateDocs(points []scrollPoint) []DocInfo {
	bySource := map[string]*DocInfo{}
	for _, p := range points {
		src, _ := p.Payload["source_id"].(string)
		if src == "" {
			continue
		}
		d, ok := bySource[src]
		if !ok {
			name, _ := p.Payload["file_name"].(string)
			d = &DocInfo{SourceID: src, FileName: name}
			bySource[src] = d
		}
		d.Chunks++
		if created, ok := p.Payload["created"].(float64); ok {
			if d.CreatedAt == 0 || int64(created) < d.CreatedAt {
				d.CreatedAt = int64(created)
			}
		}
	}
	out := make([]DocInfo, 0, len(bySource))
	for _, d := range bySource {
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].CreatedAt != out[j].CreatedAt {
			return out[i].CreatedAt > out[j].CreatedAt
		}
		return out[i].FileName < out[j].FileName
	})
	return out
}

// —— REST 调用 ——

func (c *Client) post(ctx context.Context, path string, body any, out any) error {
	buf, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+path, bytes.NewReader(buf))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return ErrCollectionMissing
	}
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("qdrant %s: HTTP %d", path, resp.StatusCode)
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}

// ListDocs 分页 scroll 指定 kb 的全部点并聚合为文档列表。
func (c *Client) ListDocs(ctx context.Context, kbID string) ([]DocInfo, error) {
	var points []scrollPoint
	var offset any
	for {
		var resp struct {
			Result struct {
				Points         []scrollPoint `json:"points"`
				NextPageOffset any           `json:"next_page_offset"`
			} `json:"result"`
		}
		if err := c.post(ctx, "/collections/"+c.collection+"/points/scroll", ScrollBody(kbID, offset), &resp); err != nil {
			if errors.Is(err, ErrCollectionMissing) {
				return []DocInfo{}, nil // 集合未建=知识库为空（首次部署/未入库过）
			}
			return nil, err
		}
		points = append(points, resp.Result.Points...)
		if resp.Result.NextPageOffset == nil {
			break
		}
		offset = resp.Result.NextPageOffset
	}
	return AggregateDocs(points), nil
}

// DeleteDoc 按 kb_id + source_id 过滤删除该文档的全部 chunk 点。
// kbID 由调用方从请求者身份推导（owner:{user}），天然不可能删到他人文档。
func (c *Client) DeleteDoc(ctx context.Context, kbID, sourceID string) error {
	body := map[string]any{"filter": kbFilter(kbID, sourceID)}
	err := c.post(ctx, "/collections/"+c.collection+"/points/delete?wait=true", body, nil)
	if errors.Is(err, ErrCollectionMissing) {
		return nil // 集合不存在=没什么可删
	}
	return err
}
