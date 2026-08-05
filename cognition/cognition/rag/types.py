"""RAG 数据结构（纯逻辑，无 I/O）。"""

from __future__ import annotations

from typing import Optional, TypedDict


class RetrievedDoc(TypedDict, total=False):
    """一条检索到的文档块。score 初始为 RRF 融合分，rerank 后被 cross-encoder 分覆盖。"""

    id: str
    text: str
    score: float
    dedup_key: str          # 跨子问题去重键（正文规范化 hash）
    source_id: str
    file_name: str
    chunk_type: str         # "text" | "ocr" | "caption"（多模态 seam）
    image_url: Optional[str]  # 多模态 seam，本次恒 None


class RagState(TypedDict, total=False):
    """LangGraph RAG 子图状态。"""

    query: str
    kb_id: str
    is_simple: bool
    subquestions: list[str]
    loop: int
    docs: list[RetrievedDoc]
    reranked: list[RetrievedDoc]
    answer: str
    sources: list[RetrievedDoc]
