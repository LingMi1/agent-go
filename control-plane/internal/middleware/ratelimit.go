// Package middleware 提供 HTTP 中间件：固定窗口限流（Redis 计数器）。
package middleware

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// RateLimiter 对 HTTP 请求执行固定窗口（每分钟）速率限制。
// Redis 不可用时 fail-open：请求允许通过并记录告警。
type RateLimiter struct {
	rdb       *redis.Client
	globalRPM int // 全部 API 的默认每分钟请求上限
	runRPM    int // POST /runs 的每分钟请求上限（比全局更严）
	log       *slog.Logger
}

// NewRateLimiter 创建限流器。globalRPM/runRPM 为 0 时使用默认值 60/10。
func NewRateLimiter(rdb *redis.Client, globalRPM, runRPM int, log *slog.Logger) *RateLimiter {
	if globalRPM <= 0 {
		globalRPM = 60
	}
	if runRPM <= 0 {
		runRPM = 10
	}
	return &RateLimiter{rdb: rdb, globalRPM: globalRPM, runRPM: runRPM, log: log}
}

// Middleware 返回 chi 兼容的限流中间件。固定窗口 = 当前 UTC 分钟。
// 每个 IP 每分钟最多 globalRPM 次请求；POST /runs 额外限制 runRPM。
// Redis 不可用时 fail-open：请求放行且记录 warn。
func (rl *RateLimiter) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if rl == nil || rl.rdb == nil {
			next.ServeHTTP(w, r)
			return
		}

		limit := rl.globalRPM
		if r.Method == http.MethodPost && strings.TrimRight(r.URL.Path, "/") == "/runs" {
			limit = rl.runRPM
		}

		key := rateLimitKey(r)
		ctx, cancel := context.WithTimeout(r.Context(), 200*time.Millisecond)
		defer cancel()

		count, err := rl.rdb.Incr(ctx, key).Result()
		if err != nil {
			// Redis 不可用 → fail-open：避免基础设施故障阻断业务。
			rl.log.Warn("rate limit redis incr failed, allowing request", "err", err, "key", key)
			next.ServeHTTP(w, r)
			return
		}
		// 首个请求设置 1 分钟过期（固定窗口自动清理）。
		if count == 1 {
			_ = rl.rdb.Expire(ctx, key, time.Minute)
		}
		if count > int64(limit) {
			w.Header().Set("Retry-After", "60")
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"code":"rate_limited","message":"请求过于频繁，请稍后重试"}`))
			return
		}
		next.ServeHTTP(w, r)
	})
}

// rateLimitKey 构造 Redis key：ratelimit:<ip>:<utc_minute>。
// 优先读 X-Forwarded-For（代理/负载均衡）；无则用 RemoteAddr。
func rateLimitKey(r *http.Request) string {
	ip := clientIP(r)
	// Unix timestamp 除以 60 得到当前 UTC 分钟序号（固定窗口边界）。
	window := time.Now().UTC().Unix() / 60
	return fmt.Sprintf("ratelimit:%s:%d", ip, window)
}

// clientIP 提取请求的客户端 IP。
func clientIP(r *http.Request) string {
	if fwd := r.Header.Get("X-Forwarded-For"); fwd != "" {
		if i := strings.IndexByte(fwd, ','); i > 0 {
			fwd = strings.TrimSpace(fwd[:i])
		}
		return fwd
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
