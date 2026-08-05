"""SOP（标准作业流程）召回。

最小 in-repo 语料 + 朴素关键词匹配，作为 M4 接 Qdrant 检索的 seam。
"""

from __future__ import annotations

from cognition.sop.store import SopEntry, SopStore, default_sop_store

__all__ = ["SopEntry", "SopStore", "default_sop_store"]
