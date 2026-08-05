// 控制面入口：装配存储/认知客户端/泵/调度/HTTP，并优雅启停。
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"golang.org/x/crypto/bcrypt"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/artifact"
	"my-agent/control-plane/internal/cognition"
	"my-agent/control-plane/internal/config"
	"my-agent/control-plane/internal/connector"
	"my-agent/control-plane/internal/dispatch"
	"my-agent/control-plane/internal/health"
	"my-agent/control-plane/internal/kb"
	"my-agent/control-plane/internal/metrics"
	"my-agent/control-plane/internal/observability"
	"my-agent/control-plane/internal/poller"
	"my-agent/control-plane/internal/scheduler"
	"my-agent/control-plane/internal/secret"
	"my-agent/control-plane/internal/store"
	"my-agent/control-plane/internal/stream"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	cfg := config.Load()

	ctx := context.Background()

	// OTel 追踪（docs/18，默认关：SetupTracing 直接返回 no-op shutdown，不建 provider）。
	// 启用时建 OTLP/gRPC exporter + 全局 TracerProvider + W3C 传播器；defer flush。
	shutdownTracing, err := observability.SetupTracing(ctx, cfg)
	if err != nil {
		log.Warn("otel tracing setup failed; continuing without traces", "err", err)
	}
	defer shutdownTracing()

	pool, err := store.NewPool(ctx, cfg.PGDSN)
	if err != nil {
		log.Error("connect postgres", "err", err)
		os.Exit(1)
	}
	defer pool.Close()
	metrics.RegisterPgxPool(pool) // 连接池水位 gauge（docs/11 §3.2 pgpool 集）
	if err := store.Migrate(ctx, pool); err != nil {
		log.Error("migrate", "err", err)
		os.Exit(1)
	}

	client, err := cognition.Dial(cfg.CognitionAddr)
	if err != nil {
		log.Error("dial cognition", "err", err)
		os.Exit(1)
	}
	defer client.Close()

	runs := store.NewRunRepository(pool)
	sessions := store.NewSessionRepository(pool)
	events := store.NewEventRepository(pool)

	artStore, err := artifact.NewMinioStore(cfg.MinioEndpoint, cfg.MinioAccessKey, cfg.MinioSecretKey, cfg.MinioBucket, cfg.MinioUseSSL)
	if err != nil {
		log.Error("minio client", "err", err)
		os.Exit(1)
	}
	// 桶不可用不致命：仅 /artifacts 受影响，ReAct/Plan-Execute 主链路照常。
	if err := artStore.EnsureBucket(ctx); err != nil {
		log.Warn("minio bucket ensure failed; artifacts unavailable", "err", err)
	}

	hub := stream.NewHub(events, cfg.HeartbeatInterval, log)
	dispatcher := dispatch.New(cfg.MaxConcurrentRuns, runs, client, hub, cfg.MaxSteps, log)
	// 健康检查：PG ping + 认知面 grpc.health.v1（业务就绪）。
	healthChecks := map[string]health.Check{
		"postgres":  func(ctx context.Context) error { return pool.Ping(ctx) },
		"cognition": client.HealthCheck,
	}
	kbStore := kb.NewClient(cfg.QdrantURL, cfg.QdrantCollection)
	var kbIface kb.Store
	if kbStore != nil { // *Client(nil) 塞进接口会变成非 nil 接口，路由降级判断失效
		kbIface = kbStore
	}
	statsRepo := store.NewStatsRepository(pool)
	artifactListRepo := store.NewArtifactListRepository(pool)
	schedRepo := store.NewSchedulesRepository(pool)
	// D3 多用户 + RBAC（docs/17）：账号/token repo + 启动引导管理员。
	userRepo := store.NewUserRepository(pool)
	tokenRepo := store.NewSessionTokenRepository(pool)
	bootstrapAdmin(ctx, userRepo, cfg, log)
	// D2 Proactive 连接器（docs/16）：连接器/触发规则 repo。
	connectorRepo := store.NewConnectorRepository(pool)
	triggerRepo := store.NewTriggerRepository(pool)
	router := api.NewRouter(dispatcher, runs, sessions, events, artStore, healthChecks, kbIface, client, statsRepo, artifactListRepo, schedRepo, userRepo, tokenRepo, connectorRepo, triggerRepo, cfg.RunTimeout, cfg.WebDir, log)

	// M11 定时触发：调度器 goroutine（优雅停机时 cancel；headless run 自建超时）。
	schedCtx, schedCancel := context.WithCancel(context.Background())
	go scheduler.New(schedRepo, runs, dispatcher, cfg.RunTimeout, 30*time.Second, 2, log).Run(schedCtx)

	// D2 Proactive 连接器：独立 poller goroutine（默认关）。仅当 POLLER_ENABLED 且
	// SECRET_MASTER_KEY 合法（32 字节）时才起——否则零行为变化（既有链路零回归）。
	pollCtx, pollCancel := context.WithCancel(context.Background())
	if cfg.PollerEnabled {
		if key, kerr := secret.DecodeMasterKey(cfg.SecretMasterKey); kerr != nil || len(key) == 0 {
			log.Warn("poller enabled but SECRET_MASTER_KEY missing/invalid; poller not started", "err", kerr)
		} else {
			gh := connector.NewGitHub()
			go poller.New(connectorRepo, triggerRepo, dispatcher, gh, key, cfg.RunTimeout, 30*time.Second, 2, log).Run(pollCtx)
			log.Info("proactive poller started")
		}
	}

	srv := &http.Server{Addr: cfg.HTTPAddr, Handler: router}

	go func() {
		log.Info("control-plane listening", "addr", cfg.HTTPAddr, "cognition", cfg.CognitionAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server", "err", err)
			os.Exit(1)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	schedCancel()
	pollCancel()
	log.Info("shutting down")
	shutCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutCtx)
}

// bootstrapAdmin：BOOTSTRAP_ADMIN_USER 已设且用户不存在 → 建 role=admin（docs/17 §3.3）。
// 幂等：已存在则跳过；失败只告警不致命（平台仍可跑，登录/自助注册不受影响）。
func bootstrapAdmin(ctx context.Context, users store.UserRepository, cfg config.Config, log *slog.Logger) {
	if cfg.BootstrapAdminUser == "" {
		return
	}
	if _, err := users.GetUserByName(ctx, cfg.BootstrapAdminUser); err == nil {
		return // 已存在
	} else if !errors.Is(err, store.ErrUserNotFound) {
		log.Warn("bootstrap admin: lookup failed", "err", err)
		return
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(cfg.BootstrapAdminPassword), bcrypt.DefaultCost)
	if err != nil {
		log.Warn("bootstrap admin: hash failed", "err", err)
		return
	}
	err = users.CreateUser(ctx, store.User{
		UserID:       cfg.BootstrapAdminUser,
		Username:     cfg.BootstrapAdminUser,
		PasswordHash: string(hash),
		Role:         store.RoleAdmin,
	})
	if err != nil {
		log.Warn("bootstrap admin: create failed", "err", err)
		return
	}
	log.Info("bootstrap admin created", "user", cfg.BootstrapAdminUser)
}
