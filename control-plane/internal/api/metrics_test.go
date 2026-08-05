package api_test

import (
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
)

// newMetricsRouter：照 health_test.go 模板，nil 注入全部依赖的最小路由器。
func newMetricsRouter() http.Handler {
	return api.NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
}

// scrapeMetrics 打一次 GET /metrics，断言 200 并返回文本 exposition。
func scrapeMetrics(t *testing.T, router http.Handler) string {
	t.Helper()
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /metrics 期望 200，得到 %d", rec.Code)
	}
	return rec.Body.String()
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

// /metrics 与请求指标中间件：打 N 个 /healthz 后计数器增量恰为 N，histogram 出现。
// 注意：metrics 是包级单例，跨测试（本包其余测试也过中间件）累积——断言用增量而非绝对值。
func TestMetrics_RequestCounterAndHistogram(t *testing.T) {
	router := newMetricsRouter()
	const series = `myagent_http_requests_total{method="GET",route="/healthz",status="200"}`
	before := metricValue(scrapeMetrics(t, router), series)

	const n = 3
	for i := 0; i < n; i++ {
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("GET /healthz 期望 200，得到 %d", rec.Code)
		}
	}

	body := scrapeMetrics(t, router)
	if got := metricValue(body, series) - before; got != n {
		t.Fatalf("期望 %s 增量 %d，得到 %v", series, n, got)
	}
	if !strings.Contains(body, `myagent_http_request_duration_seconds_bucket{method="GET",route="/healthz",`) {
		t.Fatal("期望出现 /healthz 的时长 histogram bucket 序列")
	}
	if metricValue(body, `myagent_http_request_duration_seconds_count{method="GET",route="/healthz"}`) <= 0 {
		t.Fatal("期望 /healthz 的 histogram _count > 0")
	}
}

// panic 500 也计入指标：metrics 中间件挂在 Recoverer 外层，Recoverer 写的 500 经
// WrapResponseWriter 正常计数（nil 依赖装配下 /runs/{id}/events 的 GetRun 必 panic）。
// 若中间件顺序回退到 metrics 居内层，panic 会跳过计数代码，本用例即失败。
func TestMetrics_PanicRequestCountsAs500(t *testing.T) {
	router := newMetricsRouter()
	const series = `myagent_http_requests_total{method="GET",route="/runs/{runID}/events",status="500"}`
	before := metricValue(scrapeMetrics(t, router), series)

	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/runs/x/events", nil))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("期望 panic 被 Recoverer 收为 500，得到 %d", rec.Code)
	}

	if got := metricValue(scrapeMetrics(t, router), series) - before; got != 1 {
		t.Fatalf("期望 %s 增量 1（panic 500 不可对指标全盲），得到 %v", series, got)
	}
}

// 未匹配路由（webDir 为空时 404）归并为 route="unmatched"，防 label 基数爆炸。
func TestMetrics_UnmatchedRouteLabel(t *testing.T) {
	router := newMetricsRouter()
	const series = `myagent_http_requests_total{method="GET",route="unmatched",status="404"}`
	before := metricValue(scrapeMetrics(t, router), series)

	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/definitely-not-a-route", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("期望 404，得到 %d", rec.Code)
	}

	if got := metricValue(scrapeMetrics(t, router), series) - before; got != 1 {
		t.Fatalf("期望 unmatched 序列增量 1，得到 %v", got)
	}
}
