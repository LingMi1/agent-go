package api

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// spaHandler 托管前端静态资源（web/dist）并做 SPA 回退：
//   - 仅 GET/HEAD 参与回退；其他方法的未匹配路径回 JSON 404（保住 API 错误语义，
//     误拼的 POST 端点不会拿到一坨 HTML）。
//   - 路径清洗并锁死在 webDir 内（拒绝 ..）。
//   - 命中真实文件则直接服务（构建产物 /assets/* 带内容哈希，浏览器缓存天然正确）；
//     未命中回 index.html（Cache-Control: no-cache，新版本立即生效）。
//
// 取舍：未知的 GET API 路径也会拿到 index.html——这是 SPA 单端口托管的常规代价；
// 所有已注册路由（/runs、/sessions、/artifacts/*、/healthz）先于 NotFound 匹配，
// 不受影响（例如 /runs/{id}/events 的 404 仍由其 handler 返回 JSON）。
// /artifacts（API）与 /assets（静态）前缀不同，不冲突。
func spaHandler(webDir string) http.HandlerFunc {
	index := filepath.Join(webDir, "index.html")
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			writeProblem(w, http.StatusNotFound, "not_found", "路径不存在")
			return
		}
		rel := strings.TrimPrefix(filepath.Clean("/"+r.URL.Path), "/")
		path := filepath.Join(webDir, rel)
		// Clean 后仍防御一次：目标必须落在 webDir 内。
		if !strings.HasPrefix(path, filepath.Clean(webDir)+string(os.PathSeparator)) && path != filepath.Clean(webDir) {
			writeProblem(w, http.StatusNotFound, "not_found", "路径不存在")
			return
		}
		if info, err := os.Stat(path); err == nil && !info.IsDir() {
			http.ServeFile(w, r, path)
			return
		}
		w.Header().Set("Cache-Control", "no-cache")
		http.ServeFile(w, r, index)
	}
}
