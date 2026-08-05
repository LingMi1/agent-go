"""子问题扩展解析（纯逻辑）：把 LLM 输出解析成 ≤limit 个干净子问题。

容错：按行分割、去空、去前导编号/符号（`1. `、`- `、`• `）、去重（保序）、截断到 limit。
"""

from __future__ import annotations

import re

_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)、]|[（(]\d+[)）])\s*")


def parse_subquestions(llm_text: str, *, limit: int) -> list[str]:
    """解析子问题列表。limit<=0 视为不限；空输入返回 []。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (llm_text or "").splitlines():
        line = _PREFIX.sub("", raw).strip()
        if not line:
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
        if limit and len(out) >= limit:
            break
    return out
