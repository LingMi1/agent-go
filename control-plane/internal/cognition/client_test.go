package cognition

import (
	"testing"

	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/stats"
)

// #8：OTel 开启时，client stats handler 不得给周期性 gRPC 健康探测建 span
// （否则 /healthz 每 10s 一个孤儿 root span 淹没 Tempo）。healthCheckSpanFilter 是那道过滤器：
// Health/Check → false（不建 span）；业务 RPC → true（照建）。
func TestHealthCheckSpanFilter(t *testing.T) {
	if healthCheckSpanFilter(&stats.RPCTagInfo{FullMethodName: grpc_health_v1.Health_Check_FullMethodName}) {
		t.Fatalf("grpc.health.v1.Health/Check 不应建 span")
	}
	for _, m := range []string{
		"/agent.v1.CognitionService/Run",
		"/agent.v1.CognitionService/IngestDocument",
	} {
		if !healthCheckSpanFilter(&stats.RPCTagInfo{FullMethodName: m}) {
			t.Fatalf("业务 RPC %s 应建 span", m)
		}
	}
}
