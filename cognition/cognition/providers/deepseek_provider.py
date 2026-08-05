"""DeepSeek provider 工厂。

用官方 langchain-deepseek 的 ChatDeepSeek（继承 ChatOpenAI）而非裸 ChatOpenAI：
后者在流式时**丢弃 reasoning_content**（实测 0 个推理 chunk），导致思考模型
（deepseek-reasoner / deepseek-v4-pro）的思考链到不了事件流；ChatDeepSeek 会把
推理增量放进 additional_kwargs["reasoning_content"]，由 mapper 的 _thought_delta
（extract_reasoning_delta seam）接入 thought 事件。延迟 import。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

from cognition.config import Settings, get_settings


def build_deepseek_chat(settings: Settings | None = None, **kwargs: Any) -> "BaseChatModel":
    """构造指向 DeepSeek 的 ChatDeepSeek（透传思考链）。"""
    from langchain_deepseek import ChatDeepSeek

    settings = settings or get_settings()
    params: dict[str, Any] = {
        "model": settings.deepseek_model,
        "api_base": settings.deepseek_base_url,
    }
    if settings.deepseek_api_key:
        params["api_key"] = settings.deepseek_api_key
    params.update(kwargs)
    return ChatDeepSeek(**params)
