package api

import (
	"errors"
	"mime"
	"net/http"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/google/uuid"
)

// —— M8 上传：校验/命名纯函数族（TDD）+ POST /uploads handler ——
//
// key 方案 uploads/{owner}/{session}/{uuid8}-{fileName}：owner 前置使读取鉴权
// 免查库（/artifacts 的 uploads/ 分支只比对第二段），uuid8 防同名覆盖。

// 默认单文件上限（可经 MAX_UPLOAD_BYTES 覆盖）。注意这是"上传"上限；
// 图片进模型另有 ~5MB 单图上限，在认知面展开期把关。
const DefaultMaxUploadBytes = 20 << 20

// 允许的精确 MIME（text/* 前缀之外的白名单）。
var allowedExactMIME = map[string]bool{
	"image/png": true, "image/jpeg": true, "image/webp": true, "image/gif": true,
	"application/pdf": true, "application/json": true, "application/x-ndjson": true,
	"application/xml": true,
	// M12：office 文档（认知面已支持 docx/xlsx 文本提取入库）。
	"application/vnd.openxmlformats-officedocument.wordprocessingml.document": true,
	"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":       true,
}

// 扩展名兜底（浏览器对 md/csv 等的 MIME 报告不稳定，常给 application/octet-stream 或空）。
var allowedExt = map[string]bool{
	".txt": true, ".md": true, ".markdown": true, ".csv": true, ".json": true,
	".log": true, ".xml": true, ".yaml": true, ".yml": true, ".pdf": true,
	".png": true, ".jpg": true, ".jpeg": true, ".webp": true, ".gif": true,
	".docx": true, ".xlsx": true,
}

// AllowedUpload 判定 MIME/扩展名是否在白名单（可执行等一律拒；docx/xlsx 自 M12 起放行）。
func AllowedUpload(mimeType, fileName string) bool {
	mt := strings.ToLower(strings.TrimSpace(strings.SplitN(mimeType, ";", 2)[0]))
	if strings.HasPrefix(mt, "text/") || allowedExactMIME[mt] {
		return true
	}
	return allowedExt[strings.ToLower(filepath.Ext(fileName))]
}

var unsafeFileChars = regexp.MustCompile(`[^\w.\x{4e00}-\x{9fff}-]+`)

// SanitizeUploadFileName 把文件名压成 key 安全形态：去路径分量、危险字符转 -、限长。
func SanitizeUploadFileName(name string) string {
	base := filepath.Base(strings.ReplaceAll(name, "\\", "/"))
	base = unsafeFileChars.ReplaceAllString(base, "-")
	base = strings.Trim(base, "-.")
	if base == "" {
		base = "file"
	}
	if len(base) > 128 {
		ext := filepath.Ext(base)
		base = base[:128-len(ext)] + ext
	}
	return base
}

// BuildUploadKey 组装上传对象 key（owner 前置=读取鉴权免查库）。
func BuildUploadKey(owner, session, uuid8, fileName string) string {
	if session == "" {
		session = "misc"
	}
	return "uploads/" + owner + "/" + session + "/" + uuid8 + "-" + fileName
}

// OwnerOfUploadKey 从 uploads key 提取 owner 段；非 uploads/ 或缺段返回 ""。
func OwnerOfUploadKey(key string) string {
	parts := strings.SplitN(key, "/", 4)
	if len(parts) < 4 || parts[0] != "uploads" || parts[1] == "" {
		return ""
	}
	return parts[1]
}

// ValidateAttachmentKey 校验 run 请求携带的附件 key 归属：必须是当前用户自己的
// uploads 对象。这是防伪造闸门——否则任意用户可把他人 upload key（乃至任意
// artifacts key）塞进 attachments，让认知面替他读取内容/写进知识库。
func ValidateAttachmentKey(owner, key string) bool {
	return OwnerOfUploadKey(key) == owner && owner != ""
}

// upload 处理 POST /uploads（multipart，字段名 file；可选 ?sessionId= 归档用）。
func (h *handlers) upload(w http.ResponseWriter, r *http.Request) {
	if h.artifacts == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_artifact_store", "产物存储未配置")
		return
	}
	owner := ownerOf(r)
	r.Body = http.MaxBytesReader(w, r.Body, h.maxUploadBytes)
	file, hdr, err := r.FormFile("file")
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			writeProblem(w, http.StatusRequestEntityTooLarge, "too_large", "文件超过上传上限")
			return
		}
		writeProblem(w, http.StatusBadRequest, "bad_request", "multipart 缺少 file 字段")
		return
	}
	defer file.Close()

	name := SanitizeUploadFileName(hdr.Filename)
	mimeType := strings.TrimSpace(hdr.Header.Get("Content-Type"))
	if mimeType == "" || mimeType == "application/octet-stream" {
		if byExt := mime.TypeByExtension(filepath.Ext(name)); byExt != "" {
			mimeType = byExt
		}
	}
	if !AllowedUpload(mimeType, name) {
		writeProblem(w, http.StatusUnsupportedMediaType, "unsupported_type",
			"仅支持图片(png/jpeg/webp/gif)、文本类(txt/md/csv/json/xml/yaml)与 PDF")
		return
	}

	key := BuildUploadKey(owner, r.URL.Query().Get("sessionId"), uuid.NewString()[:8], name)
	if err := h.artifacts.Put(r.Context(), key, file, hdr.Size, mimeType); err != nil {
		h.log.Error("upload put", "key", key, "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "存储上传对象失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"resourceKey": key,
		"fileName":    name,
		"mimeType":    mimeType,
		"size":        hdr.Size,
		"previewUrl":  "/artifacts/" + key,
		"downloadUrl": "/artifacts/" + key,
	})
}
