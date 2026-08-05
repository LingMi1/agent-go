"""文本切分（纯逻辑）：中文标点感知、带重叠、单块硬上限。

策略（对齐 MRAG）：优先在中文/英文句子边界切，累计到 ~size 收一个块，块间保留 overlap 字符，
任何块不超过 hard_max（防超长块打爆 embedding）。
"""

from __future__ import annotations

import re

# 句子边界：中文句末标点 + 英文 .!? + 换行。保留标点在句尾。
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _sentences(text: str) -> list[str]:
    parts = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    return parts or ([text] if text.strip() else [])


def split_text(text: str, *, size: int = 500, overlap: int = 100, hard_max: int = 8000) -> list[str]:
    """把 text 切成若干块。空串→[]。相邻块保留 overlap 字符尾接头。单块 ≤ hard_max。"""
    text = (text or "").strip()
    if not text:
        return []
    if overlap >= size:
        overlap = size // 2

    chunks: list[str] = []
    buf = ""
    for sent in _sentences(text):
        # 单句超 hard_max：硬切。
        while len(sent) > hard_max:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(sent[:hard_max])
            sent = sent[hard_max:]
        if len(buf) + len(sent) <= size:
            buf += sent
        else:
            if buf:
                chunks.append(buf)
            # 用上一块尾部 overlap 字符做接头，减少边界信息丢失。
            tail = chunks[-1][-overlap:] if chunks and overlap else ""
            buf = tail + sent
            while len(buf) > hard_max:
                chunks.append(buf[:hard_max])
                buf = buf[hard_max:]
    if buf.strip():
        chunks.append(buf)
    return chunks
