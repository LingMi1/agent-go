package metrics_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"my-agent/control-plane/internal/metrics"
)

// scrape 打一次 /metrics handler，断言 200 并返回文本 exposition。
func scrape(t *testing.T, h http.Handler) string {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("scrape 期望 200，得到 %d", rec.Code)
	}
	return rec.Body.String()
}

// 自有 Registry 决策的护栏：多次 New 互不冲突（DefaultRegisterer 全局态下重复注册会 panic），
// 各实例的 Handler 均可独立工作。
func TestNew_MultipleInstancesNoConflict(t *testing.T) {
	a, b := metrics.New(), metrics.New()
	for i, m := range []*metrics.Metrics{a, b} {
		if body := scrape(t, m.Handler()); !strings.Contains(body, "myagent_runs_in_flight") {
			t.Fatalf("实例 %d 的 Handler 输出缺少基础 gauge", i)
		}
	}
}

// Handler 输出必须包含 docs/11 §3.2 清单里的全部 family。
// 注意：包级单例跨测试累积，只断言 family 出现，不断言绝对值。
func TestHandler_ExposesAllFamilies(t *testing.T) {
	// Vec 类 instrument 无子序列时不出现在 exposition 里，先各打一笔。
	metrics.RunsRejected().Inc()
	metrics.Runs().WithLabelValues("SUCCESS", "react").Inc()
	metrics.RunDuration().WithLabelValues("react").Observe(1.5)
	metrics.RunTokens().WithLabelValues("input").Add(10)
	metrics.RunTokens().WithLabelValues("output").Add(5)
	metrics.ModelCalls().Add(2)
	metrics.EventsPersisted().Inc()
	metrics.SSEFramesWritten().Inc()
	metrics.PumpErrors().WithLabelValues("SEQ_GAP").Inc()
	metrics.SchedulerFired().Inc()
	metrics.SchedulerSkipped().WithLabelValues("overlap").Inc()

	// HTTP 两族经中间件产生。
	r := chi.NewRouter()
	r.Use(metrics.HTTPMiddleware)
	r.Get("/ping", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/ping", nil))

	body := scrape(t, metrics.Handler())
	for _, fam := range []string{
		"myagent_http_requests_total",
		"myagent_http_request_duration_seconds",
		"myagent_runs_in_flight",
		"myagent_runs_rejected_total",
		"myagent_runs_total",
		"myagent_run_duration_seconds",
		"myagent_run_tokens_total",
		"myagent_model_calls_total",
		"myagent_events_persisted_total",
		"myagent_sse_frames_written_total",
		"myagent_pump_errors_total",
		"myagent_scheduler_fired_total",
		"myagent_scheduler_skipped_total",
	} {
		if !strings.Contains(body, fam) {
			t.Errorf("/metrics 输出缺少 family %q", fam)
		}
	}
	// route/method/status label 形状抽查。
	if !strings.Contains(body, `myagent_http_requests_total{method="GET",route="/ping",status="200"}`) {
		t.Errorf("http_requests_total 缺少期望的 label 组合，body 片段:\n%s", grepLines(body, "myagent_http_requests_total"))
	}
	if !strings.Contains(body, `myagent_http_request_duration_seconds_bucket{method="GET",route="/ping",`) {
		t.Errorf("http_request_duration_seconds 缺少 bucket 序列")
	}
}

// 红线（docs/11 §3.3）：中间件包裹 ResponseWriter 后必须仍满足 http.Flusher，
// 否则 sse.go 的类型断言失败、SSE 全废。
func TestHTTPMiddleware_PreservesFlusher(t *testing.T) {
	var isFlusher bool
	r := chi.NewRouter()
	r.Use(metrics.HTTPMiddleware)
	r.Get("/f", func(w http.ResponseWriter, _ *http.Request) {
		_, isFlusher = w.(http.Flusher)
		w.WriteHeader(http.StatusOK)
	})
	rec := httptest.NewRecorder() // ResponseRecorder 实现 http.Flusher
	r.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/f", nil))
	if !isFlusher {
		t.Fatal("中间件必须透传 http.Flusher（chi NewWrapResponseWriter），否则 SSE 断言失败")
	}
}

// method label 白名单归并：任意自定义 method token（chi 未知 method 走 405 且仍过
// 中间件链）不得各成一条时序——白名单外统一记 "other"，小写规范形并入大写，
// method 维度基数封顶为常数（与 route 的 unmatched 策略对齐）。
func TestHTTPMiddleware_MethodLabelAllowlist(t *testing.T) {
	r := chi.NewRouter()
	r.Use(metrics.HTTPMiddleware)
	r.Get("/m", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })

	const otherSeries = `myagent_http_requests_total{method="other",route="unmatched",status="405"}`
	const lowerSeries = `myagent_http_requests_total{method="GET",route="unmatched",status="405"}`
	before := scrape(t, metrics.Handler())
	otherBefore, lowerBefore := metricValue(before, otherSeries), metricValue(before, lowerSeries)

	// 两个怪 method + 一个小写 method（Go net/http 均接受为合法 token）。
	for _, method := range []string{"EVIL123", "PROPFIND", "get"} {
		rec := httptest.NewRecorder()
		r.ServeHTTP(rec, httptest.NewRequest(method, "/m", nil))
		if rec.Code != http.StatusMethodNotAllowed {
			t.Fatalf("method %q 期望 405，得到 %d", method, rec.Code)
		}
	}

	body := scrape(t, metrics.Handler())
	for _, leaked := range []string{`method="EVIL123"`, `method="PROPFIND"`, `method="get"`} {
		if strings.Contains(body, leaked) {
			t.Fatalf("非白名单 method 泄漏为独立时序 %s:\n%s", leaked, grepLines(body, "myagent_http_requests_total"))
		}
	}
	if got := metricValue(body, otherSeries) - otherBefore; got != 2 {
		t.Fatalf("期望两个怪 method 归并进 other（增量 2），得到 %v:\n%s", got, grepLines(body, "myagent_http_requests_total"))
	}
	if got := metricValue(body, lowerSeries) - lowerBefore; got != 1 {
		t.Fatalf("期望小写 get 归并进 GET（增量 1），得到 %v:\n%s", got, grepLines(body, "myagent_http_requests_total"))
	}
}

// 未匹配路由归并为 route="unmatched"，防任意 URL 打爆 label 基数。
func TestHTTPMiddleware_UnmatchedRoute(t *testing.T) {
	r := chi.NewRouter()
	r.Use(metrics.HTTPMiddleware)
	// chi 无任何路由时不挂载中间件链，注册一条陪跑路由以贴近真实路由器形态。
	r.Get("/exists", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/no-such-route", nil))

	body := scrape(t, metrics.Handler())
	if !strings.Contains(body, `myagent_http_requests_total{method="GET",route="unmatched",status="404"}`) {
		t.Fatalf("未匹配路由应计入 route=unmatched，body 片段:\n%s", grepLines(body, "myagent_http_requests_total"))
	}
}

// pgxpool GaugeFunc 集：注册后出现在输出里；重复注册幂等（不 panic）。
// pgxpool.New 惰性建连，不需要真实 PG 即可 Stat()。
func TestRegisterPgxPool_GaugesAndIdempotent(t *testing.T) {
	pool, err := pgxpool.New(context.Background(), "postgres://u:p@localhost:1/none")
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)
	metrics.RegisterPgxPool(pool)
	metrics.RegisterPgxPool(pool) // 幂等：AlreadyRegistered 静默忽略

	body := scrape(t, metrics.Handler())
	for _, fam := range []string{
		"myagent_pgpool_acquired_conns",
		"myagent_pgpool_idle_conns",
		"myagent_pgpool_total_conns",
		"myagent_pgpool_max_conns",
		"myagent_pgpool_constructing_conns",
	} {
		if !strings.Contains(body, fam) {
			t.Errorf("/metrics 输出缺少连接池 gauge %q", fam)
		}
	}
}

// metricValue 从 exposition 文本里取指定序列（`name{labels}`）的当前值；不存在返回 0。
func metricValue(body, series string) float64 {
	for _, ln := range strings.Split(body, "\n") {
		if rest, ok := strings.CutPrefix(ln, series+" "); ok {
			v, _ := strconv.ParseFloat(strings.TrimSpace(rest), 64)
			return v
		}
	}
	return 0
}

// grepLines 取包含子串的行，测试失败时给出可读上下文。
func grepLines(body, sub string) string {
	var out []string
	for _, ln := range strings.Split(body, "\n") {
		if strings.Contains(ln, sub) {
			out = append(out, ln)
		}
	}
	return strings.Join(out, "\n")
}
