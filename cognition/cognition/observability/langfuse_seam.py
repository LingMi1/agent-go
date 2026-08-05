"""可选 Langfuse trace seam：config-gated + import-guard，无配置/未安装即 no-op。

`langfuse` 不进核心依赖；仅当 `settings.langfuse_enabled` 且能成功构造 handler 时，才把
CallbackHandler 注入 LangGraph 的 `config["callbacks"]`。任何 import/构造失败都降级为 []（不阻断 run）。
`_make_handler` 单独抽出，便于测试 monkeypatch 验证注入路径而无需真安装 langfuse。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _make_handler(settings: Any):  # pragma: no cover - 真实 handler 构造走人工/联调
    """构造 Langfuse CallbackHandler（惰性 import；版本差异在此吸收）。"""
    try:
        from langfuse.langchain import CallbackHandler  # langfuse v3
    except Exception:  # noqa: BLE001
        from langfuse.callback import CallbackHandler  # 兼容旧版
    import os

    # 经环境变量传凭证（langfuse SDK 约定），避免各版本构造签名差异。
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key or "")
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key or "")
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host or "")
    return CallbackHandler()


def build_langfuse_callbacks(settings: Any) -> list:
    """启用且可用则返回 [handler]，否则 []（no-op）。"""
    if not getattr(settings, "langfuse_enabled", False):
        return []
    try:
        return [_make_handler(settings)]
    except Exception as exc:  # noqa: BLE001 — 未装/构造失败一律降级，不阻断
        logger.warning("langfuse 已启用但不可用，降级为 no-op: %s", exc)
        return []
