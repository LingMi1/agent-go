package observability

import (
	"context"
	"testing"

	"my-agent/control-plane/internal/config"
)

// 默认关（OTelEnabled=false）：SetupTracing 返回非 nil、幂等的 no-op shutdown、无错，
// 且不触碰任何 otel 全局状态。零行为变化红线（docs/18 §6）。
func TestSetupTracing_DisabledReturnsNoop(t *testing.T) {
	shutdown, err := SetupTracing(context.Background(), config.Config{OTelEnabled: false})
	if err != nil {
		t.Fatalf("disabled setup should not error, got: %v", err)
	}
	if shutdown == nil {
		t.Fatalf("shutdown must be non-nil even when disabled")
	}
	// no-op shutdown 幂等可重复调用，不 panic。
	shutdown()
	shutdown()
}

// 关时不设全局 TextMapPropagator（未启用即不注入 W3C 传播器；grpc 侧亦不挂 handler）。
func TestSetupTracing_DisabledDoesNotConfigureExporter(t *testing.T) {
	// 端点故意填非法值：关时根本不构造 exporter，故不应因此报错。
	shutdown, err := SetupTracing(context.Background(), config.Config{
		OTelEnabled:  false,
		OTelEndpoint: "not-a-valid-endpoint:::",
	})
	if err != nil {
		t.Fatalf("disabled setup must skip exporter construction, got err: %v", err)
	}
	shutdown()
}
