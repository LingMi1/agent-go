"""Langfuse seam：默认关 no-op、非硬依赖；启用且可用则注入。"""

from __future__ import annotations

from cognition.config import Settings
from cognition.observability import langfuse_seam
from cognition.observability.langfuse_seam import build_langfuse_callbacks


def test_disabled_returns_empty_no_import():
    assert build_langfuse_callbacks(Settings(langfuse_enabled=False)) == []


def test_enabled_but_unavailable_degrades_to_noop(monkeypatch):
    # 模拟未安装/构造失败：_make_handler 抛错 → 降级为 []
    def boom(_settings):
        raise ImportError("langfuse 未安装")

    monkeypatch.setattr(langfuse_seam, "_make_handler", boom)
    assert build_langfuse_callbacks(Settings(langfuse_enabled=True)) == []


def test_enabled_and_available_injects(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(langfuse_seam, "_make_handler", lambda _s: sentinel)
    cbs = build_langfuse_callbacks(Settings(langfuse_enabled=True))
    assert cbs == [sentinel]
