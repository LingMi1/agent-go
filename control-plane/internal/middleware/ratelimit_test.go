package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestClientIP_XForwardedFor(t *testing.T) {
	tests := []struct {
		name     string
		header   string
		expected string
	}{
		{"single ip", "192.168.1.1", "192.168.1.1"},
		{"with comma and second ip", "10.0.0.1, 10.0.0.2", "10.0.0.1"},
		{"with comma no space", "10.0.0.1,10.0.0.2", "10.0.0.1"},
		{"ipv6", "::1", "::1"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/", nil)
			r.Header.Set("X-Forwarded-For", tt.header)
			got := clientIP(r, true)
			if got != tt.expected {
				t.Errorf("clientIP() = %q, want %q", got, tt.expected)
			}
		})
	}
}

func TestClientIP_RemoteAddr(t *testing.T) {
	tests := []struct {
		name       string
		remoteAddr string
		expected   string
	}{
		{"host and port", "192.168.1.1:54321", "192.168.1.1"},
		{"ipv6 with port", "[::1]:54321", "::1"},
		{"no port", "192.168.1.1", "192.168.1.1"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/", nil)
			r.RemoteAddr = tt.remoteAddr
			got := clientIP(r, false)
			if got != tt.expected {
				t.Errorf("clientIP() = %q, want %q", got, tt.expected)
			}
		})
	}
}

func TestClientIP_TrustProxyDisabled(t *testing.T) {
	// 默认不信任 XFF：即使客户端伪造了 X-Forwarded-For，也必须取 RemoteAddr，
	// 否则攻击者可伪造 IP 无限刷新限流桶绕过全局限流。
	r := httptest.NewRequest("GET", "/", nil)
	r.RemoteAddr = "203.0.113.7:4321"
	r.Header.Set("X-Forwarded-For", "198.51.100.9")
	if got := clientIP(r, false); got != "203.0.113.7" {
		t.Errorf("clientIP(trustProxy=false) = %q, want RemoteAddr %q", got, "203.0.113.7")
	}
}

func TestRateLimitKey(t *testing.T) {
	r := httptest.NewRequest("GET", "/", nil)
	r.Header.Set("X-Forwarded-For", "10.0.0.1")
	key := rateLimitKey(r, 123456, true)
	expected := "ratelimit:10.0.0.1:123456"
	if key != expected {
		t.Errorf("rateLimitKey() = %q, want %q", key, expected)
	}
}

func TestSetRateLimitHeaders(t *testing.T) {
	w := httptest.NewRecorder()
	setRateLimitHeaders(w, 60, 58, 100)
	if w.Header().Get("X-RateLimit-Limit") != "60" {
		t.Errorf("X-RateLimit-Limit = %q, want 60", w.Header().Get("X-RateLimit-Limit"))
	}
	if w.Header().Get("X-RateLimit-Remaining") != "58" {
		t.Errorf("X-RateLimit-Remaining = %q, want 58", w.Header().Get("X-RateLimit-Remaining"))
	}
	// Reset = (window+1)*60 = (100+1)*60 = 6060
	if w.Header().Get("X-RateLimit-Reset") != "6060" {
		t.Errorf("X-RateLimit-Reset = %q, want 6060", w.Header().Get("X-RateLimit-Reset"))
	}
}

func TestSetRateLimitHeaders_ZeroRemaining(t *testing.T) {
	w := httptest.NewRecorder()
	setRateLimitHeaders(w, 10, 0, 0)
	if w.Header().Get("X-RateLimit-Remaining") != "0" {
		t.Errorf("X-RateLimit-Remaining = %q, want 0", w.Header().Get("X-RateLimit-Remaining"))
	}
	if w.Header().Get("X-RateLimit-Reset") != "60" {
		t.Errorf("X-RateLimit-Reset = %q, want 60", w.Header().Get("X-RateLimit-Reset"))
	}
}

func TestNewRateLimiterDefaults(t *testing.T) {
	// Zero values → fallback to defaults.
	rl := NewRateLimiter(nil, 0, 0, nil)
	if rl.globalRPM != 60 {
		t.Errorf("globalRPM = %d, want 60", rl.globalRPM)
	}
	if rl.runRPM != 10 {
		t.Errorf("runRPM = %d, want 10", rl.runRPM)
	}
}

func TestNewRateLimiter_CustomValues(t *testing.T) {
	rl := NewRateLimiter(nil, 100, 20, nil)
	if rl.globalRPM != 100 {
		t.Errorf("globalRPM = %d, want 100", rl.globalRPM)
	}
	if rl.runRPM != 20 {
		t.Errorf("runRPM = %d, want 20", rl.runRPM)
	}
}

func TestMiddleware_NilReceiver(t *testing.T) {
	// nil *RateLimiter → passes through.
	var rl *RateLimiter
	h := rl.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	w := httptest.NewRecorder()
	r := httptest.NewRequest("GET", "/", nil)
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", w.Code)
	}
}

func TestMiddleware_NilRDB(t *testing.T) {
	// RateLimiter with nil rdb → passes through (fail-open).
	rl := NewRateLimiter(nil, 60, 10, nil)
	h := rl.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	w := httptest.NewRecorder()
	r := httptest.NewRequest("GET", "/", nil)
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", w.Code)
	}
}

func TestTryAcquireIdempotencyKey_NilReceiver(t *testing.T) {
	var rl *RateLimiter
	ok, err := rl.TryAcquireIdempotencyKey(t.Context(), "key1", "val1", time.Minute)
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}
	if !ok {
		t.Error("expected true (fail-open) when RateLimiter is nil")
	}
}

func TestTryAcquireIdempotencyKey_NilRDB(t *testing.T) {
	// RateLimiter with nil rdb → fail-open, returns true.
	rl := NewRateLimiter(nil, 60, 10, nil)
	ok, err := rl.TryAcquireIdempotencyKey(t.Context(), "key1", "val1", time.Minute)
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}
	if !ok {
		t.Error("expected true (fail-open) when rdb is nil")
	}
}
