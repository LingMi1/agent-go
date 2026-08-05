// Package metrics 是控制面的 Prometheus 指标出口（docs/11 §3）：自有
// prometheus.NewRegistry()（不用全局 DefaultRegisterer——它是进程级可变状态，
// go_collector 噪声不可控、多包测试并行下重复注册直接 panic），实例化为包级单例，
// 以包级函数暴露各 instrument 与 /metrics Handler。
//
// 取舍：否掉「NewRouter 加参数注入」——NewRouter 已是 15 个位置参数、测试 11+ 处
// 调用，为一个横切关注面改一圈调用点不值（对齐 api.go 就地读 MAX_UPLOAD_BYTES 的
// 先例）；埋点方直接 metrics.RunsInFlight().Inc() 式调用。指标全部 myagent_ 前缀，
// 清单严格按 docs/11 §3.2。
package metrics

import (
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Metrics 持有自有 Registry 与全部 instrument。进程内只用包级单例 std；
// New 保持可导出供测试构造隔离实例（自有 Registry 使多次 New 互不冲突）。
type Metrics struct {
	reg *prometheus.Registry

	httpRequests *prometheus.CounterVec
	httpDuration *prometheus.HistogramVec

	runsInFlight prometheus.Gauge
	runsRejected prometheus.Counter
	runs         *prometheus.CounterVec
	runDuration  *prometheus.HistogramVec
	runTokens    *prometheus.CounterVec
	modelCalls   prometheus.Counter

	eventsPersisted  prometheus.Counter
	sseFramesWritten prometheus.Counter
	pumpErrors       *prometheus.CounterVec

	schedulerFired   prometheus.Counter
	schedulerSkipped *prometheus.CounterVec
}

// New 构造一套完整 instrument 并注册进自有 Registry。
func New() *Metrics {
	m := &Metrics{reg: prometheus.NewRegistry()}

	m.httpRequests = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "myagent_http_requests_total",
		Help: "HTTP 请求总数（流量与错误率）。",
	}, []string{"route", "method", "status"})
	m.httpDuration = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "myagent_http_request_duration_seconds",
		Help:    "HTTP 请求时长分布；SSE 长连（/runs）靠 route label 隔离。",
		Buckets: prometheus.DefBuckets, // 5ms…10s 常规档
	}, []string{"route", "method"})

	m.runsInFlight = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "myagent_runs_in_flight",
		Help: "进行中 run 数（并发水位 vs MAX_CONCURRENT_RUNS）。",
	})
	m.runsRejected = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "myagent_runs_rejected_total",
		Help: "准入被拒（429 优雅繁忙）总数。",
	})
	m.runs = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "myagent_runs_total",
		Help: "run 终态计数（SUCCESS/FAILED/STOPPED/TIMEOUT；RUNNING 不计）。",
	}, []string{"status", "agent_type"})
	m.runDuration = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name: "myagent_run_duration_seconds",
		Help: "run 端到端时长（含定时 headless run）。",
		// 100ms…600s 档，对齐 RUN_TIMEOUT 默认 600s。
		Buckets: []float64{0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600},
	}, []string{"agent_type"})
	m.runTokens = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "myagent_run_tokens_total",
		Help: "run 消耗 token 总数（direction=input/output）。",
	}, []string{"direction"})
	m.modelCalls = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "myagent_model_calls_total",
		Help: "模型调用次数总数。",
	})

	m.eventsPersisted = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "myagent_events_persisted_total",
		Help: "事件账本落库总数（先持久化后展示）。",
	})
	m.sseFramesWritten = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "myagent_sse_frames_written_total",
		Help: "真实写给 SSE 客户端的内容帧总数（实时推流与回放；headless 定时 run 无客户端不计；心跳不计）。",
	})
	m.pumpErrors = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "myagent_pump_errors_total",
		Help: "事件泵异常终态计数（code=hub 的 ErrorCode 字符串）。",
	}, []string{"code"})

	m.schedulerFired = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "myagent_scheduler_fired_total",
		Help: "定时任务触发成功总数。",
	})
	m.schedulerSkipped = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "myagent_scheduler_skipped_total",
		Help: "定时任务跳拍计数（reason=overlap/slots/busy/claim）。",
	}, []string{"reason"})

	m.reg.MustRegister(
		m.httpRequests, m.httpDuration,
		m.runsInFlight, m.runsRejected, m.runs, m.runDuration, m.runTokens, m.modelCalls,
		m.eventsPersisted, m.sseFramesWritten, m.pumpErrors,
		m.schedulerFired, m.schedulerSkipped,
	)
	return m
}

// Handler 返回 /metrics 端点 handler（只暴露自有 Registry，无 go_collector 噪声）。
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.reg, promhttp.HandlerOpts{})
}

// httpMethodAllowlist 是 method label 白名单：Go net/http 接受任意合法 token 作为
// method（chi 对未知 method 走 405 且仍经完整中间件链），若原样入 label，无鉴权
// 客户端每换一个 method token 就新增一组时序（histogram 一次 13 条），指标内存
// 持久累积不释放——与 route 的 unmatched 归并同源的基数失控风险，必须封顶。
var httpMethodAllowlist = map[string]bool{
	http.MethodGet: true, http.MethodPost: true, http.MethodPut: true,
	http.MethodDelete: true, http.MethodPatch: true, http.MethodHead: true,
	http.MethodOptions: true,
}

// normalizeMethod 把 method 归一到有界集合：大写比对白名单，命中取大写规范形
// （小写 "get" 与 "GET" 合并同一时序），不在集合内统一记 "other"。
func normalizeMethod(m string) string {
	if u := strings.ToUpper(m); httpMethodAllowlist[u] {
		return u
	}
	return "other"
}

// HTTPMiddleware 记录每个请求的计数与时长。
// 红线（docs/11 §3.3）：必须用 chi 的 NewWrapResponseWriter 包裹——它按底层
// ResponseWriter 的能力透传 http.Flusher，否则 sse.go 的 Flusher 断言失败、SSE 全废。
// route label 在 handler 执行后取 RoutePattern 才完整；未匹配路由（404/SPA 回退）
// 归并为 "unmatched"、method 白名单外归并为 "other"，两个维度都防 label 基数被
// 任意输入打爆。
func (m *Metrics) HTTPMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ww := chimw.NewWrapResponseWriter(w, r.ProtoMajor)
		start := time.Now()
		next.ServeHTTP(ww, r)
		route := "unmatched"
		if rc := chi.RouteContext(r.Context()); rc != nil && rc.RoutePattern() != "" {
			route = rc.RoutePattern()
		}
		status := ww.Status()
		if status == 0 { // handler 未显式写响应：net/http 默认 200
			status = http.StatusOK
		}
		method := normalizeMethod(r.Method)
		m.httpRequests.WithLabelValues(route, method, strconv.Itoa(status)).Inc()
		m.httpDuration.WithLabelValues(route, method).Observe(time.Since(start).Seconds())
	})
}

// RegisterPgxPool 注册 pgxpool 连接池水位 GaugeFunc 集（client_golang 无官方
// pgx collector，自写最小集包 pool.Stat()）。重复注册静默忽略（幂等），main 只调一次。
func (m *Metrics) RegisterPgxPool(pool *pgxpool.Pool) {
	for _, g := range []struct {
		name, help string
		value      func(*pgxpool.Stat) float64
	}{
		{"myagent_pgpool_acquired_conns", "当前被占用的连接数。", func(s *pgxpool.Stat) float64 { return float64(s.AcquiredConns()) }},
		{"myagent_pgpool_idle_conns", "当前空闲连接数。", func(s *pgxpool.Stat) float64 { return float64(s.IdleConns()) }},
		{"myagent_pgpool_total_conns", "池内连接总数。", func(s *pgxpool.Stat) float64 { return float64(s.TotalConns()) }},
		{"myagent_pgpool_max_conns", "池连接数上限。", func(s *pgxpool.Stat) float64 { return float64(s.MaxConns()) }},
		{"myagent_pgpool_constructing_conns", "建立中的连接数。", func(s *pgxpool.Stat) float64 { return float64(s.ConstructingConns()) }},
	} {
		fn := g.value
		gf := prometheus.NewGaugeFunc(prometheus.GaugeOpts{Name: g.name, Help: g.help},
			func() float64 { return fn(pool.Stat()) })
		if err := m.reg.Register(gf); err != nil {
			var are prometheus.AlreadyRegisteredError
			if !errors.As(err, &are) {
				panic(err)
			}
		}
	}
}

// std 是包级单例：包初始化即建全部 instrument，进程内天然幂等、零构造签名变更。
var std = New()

// —— 以下为包级转发：埋点方与路由装配直接调用 ——

// Handler 返回 /metrics 端点 handler。
func Handler() http.Handler { return std.Handler() }

// HTTPMiddleware 是请求计数/时长中间件（挂在 middleware.Recoverer 之前/外层：
// handler panic 时内层 Recoverer 写的 500 经 WrapResponseWriter 一样被计数）。
func HTTPMiddleware(next http.Handler) http.Handler { return std.HTTPMiddleware(next) }

// RegisterPgxPool 注册连接池水位 gauge（main 装配处调用）。
func RegisterPgxPool(pool *pgxpool.Pool) { std.RegisterPgxPool(pool) }

// RunsInFlight：进行中 run 数 gauge（dispatch.Admit 成功 Inc / release Dec）。
func RunsInFlight() prometheus.Gauge { return std.runsInFlight }

// RunsRejected：429 拒绝计数（dispatch.Admit TryAcquire 失败）。
func RunsRejected() prometheus.Counter { return std.runsRejected }

// Runs：run 终态计数（label: status, agent_type）。
func Runs() *prometheus.CounterVec { return std.runs }

// RunDuration：run 端到端时长直方图（label: agent_type）。
func RunDuration() *prometheus.HistogramVec { return std.runDuration }

// RunTokens：token 用量计数（label: direction=input/output）。
func RunTokens() *prometheus.CounterVec { return std.runTokens }

// ModelCalls：模型调用次数计数。
func ModelCalls() prometheus.Counter { return std.modelCalls }

// EventsPersisted：事件落库计数（hub.Pump Append 成功后）。
func EventsPersisted() prometheus.Counter { return std.eventsPersisted }

// SSEFramesWritten：SSE 内容帧写出计数（api sseSink.WriteFrame 成功后——实时与
// 回放共用该 sink；headless 定时 run 的 nullSink 不计）。
func SSEFramesWritten() prometheus.Counter { return std.sseFramesWritten }

// PumpErrors：事件泵异常终态计数（label: code）。
func PumpErrors() *prometheus.CounterVec { return std.pumpErrors }

// SchedulerFired：定时触发成功计数。
func SchedulerFired() prometheus.Counter { return std.schedulerFired }

// SchedulerSkipped：定时跳拍计数（label: reason=overlap/slots/busy/claim）。
func SchedulerSkipped() *prometheus.CounterVec { return std.schedulerSkipped }
