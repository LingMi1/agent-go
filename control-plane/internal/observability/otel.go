// Package observability 装配 OTel 分布式追踪导出（config-gated；默认关时全 no-op）。
//
// 设计见 docs/18：只做 trace（metrics 由 internal/metrics 的 Prometheus 覆盖，见 §4.2）。
// 未启用（cfg.OTelEnabled==false）时不建 provider、不设全局——OTel 默认返回 no-op
// TracerProvider，埋点方的 tracer.Start 返回 no-op span，零导出、零性能开销、零行为变化。
// 启用时建 OTLP/gRPC exporter（直连 Tempo，见 §3.2）+ TracerProvider + W3C 传播器
// （traceparent 跨 gRPC 传到 Python 认知面，见 §3.1）。
package observability

import (
	"context"
	"fmt"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"my-agent/control-plane/internal/config"
)

// SetupTracing 按配置装配全局 TracerProvider 与 W3C 传播器。
//
// 返回的 shutdown 恒非 nil 且幂等，main 用 defer 调用即可（未启用时是空函数）。
// 未启用：直接返回 no-op shutdown，不触碰任何 otel 全局状态。
// 启用：建 otlptracegrpc exporter（指向 cfg.OTelEndpoint）+ 批处理 TracerProvider
// （resource.service.name=cfg.OTelServiceName），SetTracerProvider + SetTextMapPropagator
// （TraceContext + Baggage，即 W3C traceparent）。
func SetupTracing(ctx context.Context, cfg config.Config) (shutdown func(), err error) {
	noop := func() {}
	if !cfg.OTelEnabled {
		return noop, nil
	}

	// OTEL_EXPORTER_OTLP_ENDPOINT 惯例可带 scheme（http://tempo:4317）；otlptracegrpc
	// 的 WithEndpoint 只吃 host:port，故带 scheme 时走 WithEndpointURL。内网明文用 insecure。
	opts := []otlptracegrpc.Option{otlptracegrpc.WithInsecure()}
	if strings.Contains(cfg.OTelEndpoint, "://") {
		opts = []otlptracegrpc.Option{otlptracegrpc.WithEndpointURL(cfg.OTelEndpoint)}
	} else {
		opts = append(opts, otlptracegrpc.WithEndpoint(cfg.OTelEndpoint))
	}
	exp, err := otlptracegrpc.New(ctx, opts...)
	if err != nil {
		return noop, fmt.Errorf("observability: otlp trace exporter: %w", err)
	}

	// 用 NewSchemaless + 裸 service.name 属性，避开对具体 semconv 版本路径的编译期耦合。
	res := resource.NewSchemaless(attribute.String("service.name", cfg.OTelServiceName))
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))

	return func() {
		// 停机在脱离请求 ctx 的独立超时里 flush 未导出的 span，避免优雅停机被卡死。
		shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = tp.Shutdown(shutCtx)
	}, nil
}
