"""可选 OTel trace seam：config-gated + import-guard，无配置/未安装即 no-op。

逐字对齐 `langfuse_seam.py` 的结构与纪律：`opentelemetry-*` 不进核心依赖（放在
pyproject 的 `otel` optional 组），仅当 `settings.otel_enabled` 且能成功 import/构造
SDK 时才生效——任何 import/构造失败都降级为 no-op（`None` / `[]`），绝不阻断 `serve()`。

三个入口（都在启用+可用时才做真事，否则 no-op）：
- `setup_tracing(settings)`：装配全局 TracerProvider + OTLP/gRPC exporter（直连 Tempo）
  + W3C traceparent 传播器。未启用/未装返回 `None`。
- `build_server_interceptors(settings)`：返回 grpc.aio server 拦截器列表——拦截器从
  invocation metadata 提取 `traceparent` 建 server span，覆盖 `servicer.Run` 整个生成器。
  未启用/未装返回 `[]`。
- `current_trace_id()`：当前 span 的 trace_id（32 位 hex，与 Go `TraceID().String()`
  同形），供 `servicer.Run` 关联结构化日志；无 span/未装返回 `None`。

`_setup_provider` / `_make_interceptor` 单独抽出，便于测试 monkeypatch 验证注入路径而
无需真安装 opentelemetry（对齐 `test_langfuse_seam.py` 先例）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _setup_provider(settings: Any):  # pragma: no cover - 真实 provider 构造走人工/联调
    """构造并注册全局 TracerProvider（惰性 import；版本差异在此吸收）。"""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    # 内网明文直连 Tempo（4317）；endpoint 惯例可带 scheme（http://tempo:4317）。
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint, insecure=True
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    # W3C traceparent：与 Go 侧 propagation.TraceContext 对齐，跨面同一条 trace。
    set_global_textmap(TraceContextTextMapPropagator())
    return provider


def setup_tracing(settings: Any):
    """启用且可用则装配并返回 provider，否则返回 None（no-op）。"""
    if not getattr(settings, "otel_enabled", False):
        return None
    try:
        return _setup_provider(settings)
    except Exception as exc:  # noqa: BLE001 — 未装/构造失败一律降级，不阻断 serve
        logger.warning("otel 已启用但不可用，降级为 no-op: %s", exc)
        return None


def _make_interceptor(settings: Any):  # pragma: no cover - 真实拦截器走人工/联调
    """构造 grpc.aio server 拦截器（惰性 import；从 metadata 提取 traceparent 建 server span）。"""
    from opentelemetry.instrumentation.grpc import aio_server_interceptor

    return aio_server_interceptor()


def build_server_interceptors(settings: Any) -> list:
    """启用且可用则返回 [interceptor]，否则 []（no-op，等价于 grpc.aio.server 无拦截器）。"""
    if not getattr(settings, "otel_enabled", False):
        return []
    try:
        return [_make_interceptor(settings)]
    except Exception as exc:  # noqa: BLE001 — 未装/构造失败一律降级，不阻断 serve
        logger.warning("otel server 拦截器不可用，降级为 no-op: %s", exc)
        return []


def current_trace_id() -> str | None:
    """当前 span 的 trace_id（32 位 hex），无 span/未装 SDK 返回 None（no-op）。

    server span 由 aio_server_interceptor 建立并设为当前 context；此处只读，故即便
    未装 opentelemetry（默认离线路径）也安全——import 失败即返回 None，日志不加 trace_id。
    """
    try:
        from opentelemetry import trace
    except Exception:  # noqa: BLE001 — 未装 SDK：无 trace 关联
        return None
    ctx = trace.get_current_span().get_span_context()
    if not ctx or not ctx.trace_id:
        return None
    return format(ctx.trace_id, "032x")
