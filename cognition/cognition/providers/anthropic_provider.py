"""Anthropic（Claude）provider 工厂。

薄封装 langchain-anthropic 的 ChatAnthropic：用 tool-calling 的惯用法（bind_tools +
规范的 on_chat_model_* 事件），契合"一行就接"。默认模型见 Settings.anthropic_model
（claude-opus-4-8）。延迟 import，避免纯逻辑测试加载重依赖。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查，运行时不强制依赖
    from langchain_core.language_models.chat_models import BaseChatModel

from cognition.config import Settings, get_settings


def build_anthropic_chat(settings: Settings | None = None, **kwargs: Any) -> "BaseChatModel":
    """构造 ChatAnthropic。支持 .bind_tools()（由调用方在 router 中绑定）。"""
    from langchain_anthropic import ChatAnthropic

    settings = settings or get_settings()
    params: dict[str, Any] = {
        "model": settings.anthropic_model,
        "max_tokens": 4096,
    }
    if settings.anthropic_api_key:
        params["api_key"] = settings.anthropic_api_key
    params.update(kwargs)
    return ChatAnthropic(**params)
