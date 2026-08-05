"""反思解析与停止判据（纯逻辑）。

reflect 节点让 LLM 产出 `{"is_answer": bool, "rewrite_query": str}` 判断"证据是否已足够作答"；
解析容错（缺字段/非法 JSON/夹杂文本都要稳），停止判据 = 达轮数上限 或 判定已可答。
"""

from __future__ import annotations

import json
import re

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_TRUE_HINT = re.compile(r"(is_answer|足够|已可|可以回答|sufficient)\W{0,4}(true|是|1)", re.IGNORECASE)


def parse_reflection(raw: str) -> tuple[bool, str]:
    """解析反思输出 → (is_answer, rewrite_query)。任何异常都回退到 (False, "")。"""
    text = raw or ""
    m = _JSON_OBJ.search(text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                is_answer = bool(obj.get("is_answer", obj.get("isAnswer", False)))
                rewrite = str(obj.get("rewrite_query", obj.get("rewrite", "")) or "").strip()
                return is_answer, rewrite
        except (ValueError, TypeError):
            pass
    # JSON 解析失败：启发式兜底。
    return bool(_TRUE_HINT.search(text)), ""


def should_stop(loop: int, limit: int, is_answer: bool) -> bool:
    """达轮数上限或已可作答即停。loop 为已完成的反思轮数。"""
    return is_answer or loop >= limit
