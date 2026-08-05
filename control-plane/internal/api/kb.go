package api

// 用户知识库管理端点（UX-1 Files 面板）。
//
// 归属模型：kb_id 恒由请求者身份推导（owner:{X-User-Id}），客户端不可指定——
// 与认知面 knowledge_search 的 config 优先、/runs 的附件 key 校验同一条纪律：
// 权限边界画在客户端/模型拿不到笔的地方。
//
// 删除语义：只删向量点（此后检索不到），不删 MinIO 对象——历史会话的附件预览、
// pro_attachment 引用块续聊展开仍依赖原对象；事件账本/已生成回答不受影响。

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"my-agent/control-plane/internal/cognition"
)

// 入库 RPC 上限：cognition 宕机时 WaitForReady(true) 会永久阻塞（http.Server 无写超时），
// 用它把请求钉在有界时间内失败。入库含下载+分块+embedding，给足 90s。
const kbIngestTimeout = 90 * time.Second

func kbIDOf(r *http.Request) string { return "owner:" + ownerOf(r) }

type kbDocJSON struct {
	SourceID  string `json:"sourceId"`
	FileName  string `json:"fileName"`
	Chunks    int    `json:"chunks"`
	CreatedAt int64  `json:"createdAt"`
	// 上传对象附带下载链接（/artifacts 代理已做 owner 段鉴权）；脚本灌库等
	// 非 uploads 来源无下载链接。
	DownloadURL string `json:"downloadUrl,omitempty"`
}

// GET /kb/docs：列当前用户知识库的文档（chunk 点聚合）。
func (h *handlers) listKbDocs(w http.ResponseWriter, r *http.Request) {
	if h.kb == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_kb_store", "知识库管理未启用")
		return
	}
	docs, err := h.kb.ListDocs(r.Context(), kbIDOf(r))
	if err != nil {
		h.log.Error("kb list failed", "err", err)
		writeProblem(w, http.StatusBadGateway, "kb_unavailable", "知识库暂不可用")
		return
	}
	owner := ownerOf(r)
	out := make([]kbDocJSON, 0, len(docs))
	for _, d := range docs {
		j := kbDocJSON{SourceID: d.SourceID, FileName: d.FileName, Chunks: d.Chunks, CreatedAt: d.CreatedAt}
		if OwnerOfUploadKey(d.SourceID) == owner {
			j.DownloadURL = "/artifacts/" + d.SourceID
		}
		out = append(out, j)
	}
	writeJSON(w, http.StatusOK, map[string]any{"docs": out})
}

// DELETE /kb/docs?source={sourceID}：从知识库移除一份文档（全部 chunk）。
func (h *handlers) deleteKbDoc(w http.ResponseWriter, r *http.Request) {
	if h.kb == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_kb_store", "知识库管理未启用")
		return
	}
	source := strings.TrimSpace(r.URL.Query().Get("source"))
	if source == "" {
		writeProblem(w, http.StatusBadRequest, "bad_request", "缺少 source 参数")
		return
	}
	// kb_id 由身份推导 → 过滤条件天然限定在本人知识库内，不校验 source 归属也删不到他人文档。
	if err := h.kb.DeleteDoc(r.Context(), kbIDOf(r), source); err != nil {
		h.log.Error("kb delete failed", "err", err)
		writeProblem(w, http.StatusBadGateway, "kb_unavailable", "删除失败，请稍后重试")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

type ingestKbDocRequest struct {
	ResourceKey string `json:"resourceKey"`
	FileName    string `json:"fileName"`
	MimeType    string `json:"mimeType"`
	Size        int64  `json:"size"`
}

// POST /kb/docs：把已上传对象直接入库（"上传即入库"，不经对话轮）。
// 入库走认知面 gRPC（embedding/分块是认知域能力）；key 归属闸与 /runs 附件同款。
func (h *handlers) ingestKbDoc(w http.ResponseWriter, r *http.Request) {
	if h.cog == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_cognition", "认知服务未接入")
		return
	}
	var body ingestKbDocRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.ResourceKey == "" {
		writeProblem(w, http.StatusBadRequest, "bad_request", "缺少 resourceKey")
		return
	}
	owner := ownerOf(r)
	if !ValidateAttachmentKey(owner, body.ResourceKey) {
		writeProblem(w, http.StatusForbidden, "forbidden", "文件不属于当前用户："+body.ResourceKey)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), kbIngestTimeout)
	defer cancel()
	ok, kbID, msg, err := h.cog.IngestDocument(ctx, owner, cognition.Attachment{
		ResourceKey: body.ResourceKey, FileName: body.FileName, MimeType: body.MimeType, Size: body.Size,
	})
	if err != nil {
		h.log.Error("kb ingest rpc failed", "err", err)
		writeProblem(w, http.StatusBadGateway, "cognition_unavailable", "入库服务暂不可用")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": ok, "kbId": kbID, "message": msg})
}
