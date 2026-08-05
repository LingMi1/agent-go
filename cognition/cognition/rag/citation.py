"""引用上下文与来源产物（纯逻辑）。

- build_ref_context：把证据编号成 `〔1〕...` 供 ANSWER_PROMPT 引用（编号从 1 连续）。
- sources_to_artifact_md：生成 search-results.md（Q/A + 来源清单），作为 ArtifactRef 落对象存储。
"""

from __future__ import annotations

from cognition.rag.types import RetrievedDoc


def build_ref_context(docs: list[RetrievedDoc]) -> str:
    """把 docs 组成 `〔n〕 [file] text` 证据块（编号从 1）。"""
    lines: list[str] = []
    for i, d in enumerate(docs, 1):
        fn = str(d.get("file_name") or d.get("source_id") or "")
        prefix = f"〔{i}〕" + (f"[{fn}] " if fn else "")
        lines.append(prefix + str(d.get("text", "")).strip())
    return "\n".join(lines)


def sources_to_artifact_md(query: str, answer: str, docs: list[RetrievedDoc]) -> str:
    """检索结果产物正文（Markdown）。"""
    lines = ["# 检索结果\n", f"**问题**：{query}\n", f"**回答**：\n\n{answer}\n", "## 来源"]
    if not docs:
        lines.append("（无命中来源）")
    for i, d in enumerate(docs, 1):
        fn = str(d.get("file_name") or d.get("source_id") or "unknown")
        score = float(d.get("score", 0.0))
        snippet = str(d.get("text", "")).strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        lines.append(f"〔{i}〕 `{fn}` (score={score:.4f})\n  {snippet}")
    return "\n".join(lines) + "\n"
