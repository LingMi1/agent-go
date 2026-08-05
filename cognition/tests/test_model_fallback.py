"""模型分层两层交叉兜底（纯逻辑，注入假 build_fn，不触真实 provider/网络）。"""

from __future__ import annotations

import pytest

from cognition.providers.router import resolve_with_fallback


def test_primary_success():
    assert resolve_with_fallback("deepseek", "anthropic", lambda p: f"model:{p}") == "model:deepseek"


def test_primary_fails_falls_back_to_secondary():
    def build(p):
        if p == "deepseek":
            raise RuntimeError("no key")
        return f"model:{p}"

    assert resolve_with_fallback("deepseek", "anthropic", build) == "model:anthropic"


def test_both_fail_raises_runtime_error():
    def build(p):
        raise RuntimeError(f"down:{p}")

    with pytest.raises(RuntimeError) as ei:
        resolve_with_fallback("deepseek", "anthropic", build)
    assert "deepseek" in str(ei.value) and "anthropic" in str(ei.value)


def test_no_secondary_reraises():
    def build(p):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        resolve_with_fallback("deepseek", "deepseek", build)  # secondary==primary → 不重试
