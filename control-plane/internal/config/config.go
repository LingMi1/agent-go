// Package config 从环境变量加载控制面配置（带合理默认值）。
package config

import (
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	HTTPAddr          string        // 对外 HTTP/SSE 监听地址
	CognitionAddr     string        // Python 认知面 gRPC 地址
	PGDSN             string        // PostgreSQL DSN
	MaxConcurrentRuns int64         // 并发 run 上限（信号量），满则“繁忙”
	HeartbeatInterval time.Duration // SSE 心跳间隔
	RunTimeout        time.Duration // 单次 run 超时
	MaxSteps          int32         // ReAct 循环上限
	WebDir            string        // 前端静态资源目录（web/dist）；空 = 不托管（dev 走 Vite）

	// MinIO（产物对象存储）
	MinioEndpoint  string
	MinioAccessKey string
	MinioSecretKey string
	MinioBucket    string
	MinioUseSSL    bool
	// UX-1 知识库管理（Files 面板）：Go 直连 Qdrant REST 做纯管理读/删。
	QdrantURL        string
	QdrantCollection string

	// OTel 分布式追踪（docs/18，config-gated，默认关；关时零行为变化/零开销）。
	OTelEnabled     bool   // OTEL_ENABLED 为真（1/true/yes/on，大小写不敏感）才建 provider + 挂 gRPC stats handler
	OTelEndpoint    string // OTLP/gRPC 导出端点（直连 Tempo；可带 scheme）
	OTelServiceName string // resource service.name（trace 里的服务标识）

	// D3 多用户 + RBAC（docs/17，默认关；关时 X-User-Id 老路径原样工作，既有测试零回归）。
	AuthRequired           bool   // AUTH_REQUIRED 为真（1/true/yes/on，大小写不敏感）：受保护 API 无有效 token → 401
	BootstrapAdminUser     string // BOOTSTRAP_ADMIN_USER：启动时若不存在则建 role=admin
	BootstrapAdminPassword string // BOOTSTRAP_ADMIN_PASSWORD：引导管理员的初始密码

	// D2 Proactive 连接器（docs/16，默认关；关时零行为变化）。
	SecretMasterKey string // SECRET_MASTER_KEY：AES-GCM 主密钥（32 字节 base64）。空 → 连接器端点 503、poller 不起
	PollerEnabled   bool   // POLLER_ENABLED 为真（1/true/yes/on，大小写不敏感）：起独立轮询 goroutine（且须 SecretMasterKey 非空）
}

func Load() Config {
	return Config{
		HTTPAddr:          env("HTTP_ADDR", ":8080"),
		CognitionAddr:     env("COGNITION_ADDR", "localhost:50051"),
		PGDSN:             env("PG_DSN", "postgres://agent:agent_pwd@localhost:55432/my_agent"),
		MaxConcurrentRuns: int64(envInt("MAX_CONCURRENT_RUNS", 16)),
		HeartbeatInterval: time.Duration(envInt("HEARTBEAT_MS", 10000)) * time.Millisecond,
		RunTimeout:        time.Duration(envInt("RUN_TIMEOUT_S", 600)) * time.Second,
		MaxSteps:          int32(envInt("MAX_STEPS", 40)),
		WebDir:            env("WEB_DIR", ""),
		MinioEndpoint:     env("MINIO_ENDPOINT", "localhost:9000"),
		MinioAccessKey:    env("MINIO_ACCESS_KEY", "minioadmin"),
		MinioSecretKey:    env("MINIO_SECRET_KEY", "minioadmin"),
		MinioBucket:       env("MINIO_BUCKET", "artifacts"), // 须与认知面 COGNITION_MINIO_BUCKET 一致
		MinioUseSSL:       env("MINIO_USE_SSL", "false") == "true",
		// 与认知面共用一套 Qdrant：默认接受 COGNITION_QDRANT_URL（deploy/.env 单一事实源），
		// 也可用 QDRANT_URL 单独覆盖。
		QdrantURL:        env("QDRANT_URL", env("COGNITION_QDRANT_URL", "http://localhost:6333")),
		QdrantCollection: env("QDRANT_COLLECTION", env("COGNITION_QDRANT_COLLECTION", "cognition_docs")),
		// OTel：默认关。端点默认 localhost:4317（compose 内经 OTEL_EXPORTER_OTLP_ENDPOINT
		// 指向 tempo:4317）；service.name 用于在 Tempo 里区分控制面与认知面两条 span。
		OTelEnabled:     EnvBool("OTEL_ENABLED"),
		OTelEndpoint:    env("OTEL_EXPORTER_OTLP_ENDPOINT", "localhost:4317"),
		OTelServiceName: env("OTEL_SERVICE_NAME", "my-agent-control-plane"),
		// D3：默认关。引导管理员用于首次拉起 admin 账号（BOOTSTRAP_ADMIN_USER 空则不引导）。
		AuthRequired:           EnvBool("AUTH_REQUIRED"),
		BootstrapAdminUser:     env("BOOTSTRAP_ADMIN_USER", ""),
		BootstrapAdminPassword: env("BOOTSTRAP_ADMIN_PASSWORD", ""),
		// D2 连接器：默认关。SECRET_MASTER_KEY 空则连接器功能整体降级（端点 503、poller 不起）。
		SecretMasterKey: env("SECRET_MASTER_KEY", ""),
		PollerEnabled:   EnvBool("POLLER_ENABLED"),
	}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// EnvBool 解析布尔型开关环境变量：1/true/yes/on（大小写不敏感、去首尾空白）为真，
// 其余一律为假（含未设置/空/0/false/no/off）。
//
// 为什么不只认精确 "true"：项目所有对外文档与 compose/.env 一律用 `X=1` 作启用值
// （AUTH_REQUIRED=1、OTEL_ENABLED=1、POLLER_ENABLED=1），而认知面 pydantic 对同名 env
// 把 "1"/"yes"/"on" 均解析为 True。若 Go 侧只比精确 "true"，运维按文档设 =1 会静默不生效，
// 形成"照文档配置却红线失效"的部署陷阱（AUTH_REQUIRED 尤其危险：受保护 API 悄悄退回
// 可被 X-User-Id 冒充的老语义）。此处放宽到与 pydantic 一致的真值集，Go/Python 取值统一。
func EnvBool(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}
