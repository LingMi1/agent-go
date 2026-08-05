"""Provider 无关的增量内容抽取助手。

不同 provider 的流式 chunk 形状不一：Anthropic 的 content 可能是字符串或内容块列表
（含 text / thinking 块），DeepSeek 把推理放在 additional_kwargs["reasoning_content"]。
这里把"可见文本增量"与"推理增量"的抽取统一成两个纯函数，供 EventMapper 复用。
"""

from __future__ import annotations

from typing import Any

_TEXT_BLOCK_TYPES = {None, "text", "output_text"}
_REASONING_BLOCK_TYPES = {"thinking", "reasoning", "reasoning_content", "redacted_thinking"}


def _content_of(chunk: Any) -> Any:
    """取出 chunk 的 content（chunk 可能是消息对象，也可能直接是 content）。"""
    return getattr(chunk, "content", chunk)


def extract_text_delta(chunk: Any) -> str:
    """抽取一次流式增量里的可见文本。"""
    content = _content_of(chunk)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in _TEXT_BLOCK_TYPES:
                    parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def extract_reasoning_delta(chunk: Any) -> str:
    """抽取一次流式增量里的推理/思考文本（provider 无关，M1 暂未进事件流，留 seam）。"""
    content = _content_of(chunk)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in _REASONING_BLOCK_TYPES:
                parts.append(
                    str(block.get("thinking") or block.get("reasoning") or block.get("text") or "")
                )
        if parts:
            return "".join(parts)
    # DeepSeek 风格：推理放在 additional_kwargs.reasoning_content。
    extra = getattr(chunk, "additional_kwargs", None) or {}
    if isinstance(extra, dict):
        return str(extra.get("reasoning_content", "") or "")
    return ""
