"""OTel seam：默认关 no-op、非硬依赖；启用且可用则注入（对齐 test_langfuse_seam 先例）。"""

from __future__ import annotations

from cognition.config import Settings
from cognition.observability import otel_seam
from cognition.observability.otel_seam import (
    build_server_interceptors,
    current_trace_id,
    setup_tracing,
)


# —— setup_tracing：默认关 / 启用但未装 / 启用且可用 ——


def test_setup_tracing_disabled_returns_none_no_import():
    assert setup_tracing(Settings(otel_enabled=False)) is None


def test_setup_tracing_enabled_but_unavailable_degrades_to_none(monkeypatch):
    # 模拟未安装/构造失败：_setup_provider 抛错 → 降级为 None（不阻断 serve）。
    def boom(_settings):
        raise ImportError("opentelemetry 未安装")

    monkeypatch.setattr(otel_seam, "_setup_provider", boom)
    assert setup_tracing(Settings(otel_enabled=True)) is None


def test_setup_tracing_enabled_and_available_injects(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(otel_seam, "_setup_provider", lambda _s: sentinel)
    assert setup_tracing(Settings(otel_enabled=True)) is sentinel


# —— build_server_interceptors：默认关 / 启用但未装 / 启用且可用 ——


def test_interceptors_disabled_returns_empty_no_import():
    assert build_server_interceptors(Settings(otel_enabled=False)) == []


def test_interceptors_enabled_but_unavailable_degrades_to_empty(monkeypatch):
    def boom(_settings):
        raise ImportError("opentelemetry-instrumentation-grpc 未安装")

    monkeypatch.setattr(otel_seam, "_make_interceptor", boom)
    assert build_server_interceptors(Settings(otel_enabled=True)) == []


def test_interceptors_enabled_and_available_injects(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(otel_seam, "_make_interceptor", lambda _s: sentinel)
    assert build_server_interceptors(Settings(otel_enabled=True)) == [sentinel]


# —— current_trace_id：无 span/未装 SDK 返回 None（no-op 关联） ——


def test_current_trace_id_no_active_span_returns_none():
    # 无 opentelemetry 或无活跃 span 时必须返回 None（日志不加 trace_id）。
    assert current_trace_id() is None
