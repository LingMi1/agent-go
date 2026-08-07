// Package middleware 提供 HTTP 中间件：固定窗口限流（Redis 计数器）。
package middleware

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strconv"
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

		window := time.Now().UTC().Unix() / 60
		key := rateLimitKey(r, window)
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
			if err := rl.rdb.Expire(ctx, key, time.Minute).Err(); err != nil && rl.log != nil {
				rl.log.Warn("rate limit expire set failed", "err", err, "key", key)
			}
		}
		if count > int64(limit) {
			setRateLimitHeaders(w, limit, 0, window)
			w.Header().Set("Retry-After", "60")
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"code":"rate_limited","message":"请求过于频繁，请稍后重试"}`))
			return
		}
		setRateLimitHeaders(w, limit, limit-int(count), window)
		next.ServeHTTP(w, r)
	})
}

// rateLimitKey 构造 Redis key：ratelimit:<ip>:<utc_minute>。
// 优先读 X-Forwarded-For（代理/负载均衡）；无则用 RemoteAddr。
func rateLimitKey(r *http.Request, window int64) string {
	ip := clientIP(r)
	// window 是当前 UTC 分钟序号（固定窗口边界），由调用方传入以避免计时偏差。
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

// setRateLimitHeaders 写入标准的限流信息头（RFC 草案）。
func setRateLimitHeaders(w http.ResponseWriter, limit, remaining int, window int64) {
	w.Header().Set("X-RateLimit-Limit", strconv.Itoa(limit))
	w.Header().Set("X-RateLimit-Remaining", strconv.Itoa(remaining))
	// 窗口重置时间 = 下一个分钟边界（Unix 秒）
	w.Header().Set("X-RateLimit-Reset", strconv.FormatInt((window+1)*60, 10))
}

// TryAcquireIdempotencyKey 幂等去重：SETNX key → val with TTL。返回 true 表示首次获取。
// 用于 POST /runs 的 Idempotency-Key header 去重，防止网络重试产生重复 run。
func (rl *RateLimiter) TryAcquireIdempotencyKey(ctx context.Context, key, val string, ttl time.Duration) (bool, error) {
	if rl == nil || rl.rdb == nil {
		return true, nil // no Redis → 不做去重（fail-open）
	}
	return rl.rdb.SetNX(ctx, "idempotent:"+key, val, ttl).Result()
}
