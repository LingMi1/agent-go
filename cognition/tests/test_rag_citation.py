"""引用上下文与来源产物（纯逻辑）。"""

from __future__ import annotations

from cognition.rag.citation import build_ref_context, sources_to_artifact_md


def _docs():
    return [
        {"file_name": "a.md", "text": "证据一", "score": 0.9},
        {"file_name": "b.md", "text": "证据二", "score": 0.7},
    ]


def test_build_ref_context_numbered_from_one():
    ctx = build_ref_context(_docs())
    assert "〔1〕" in ctx and "〔2〕" in ctx
    assert "证据一" in ctx and "[a.md]" in ctx
    assert "〔3〕" not in ctx


def test_artifact_md_contains_query_answer_sources():
    md = sources_to_artifact_md("什么是X", "X 是一个概念〔1〕。", _docs())
    assert "什么是X" in md
    assert "X 是一个概念" in md
    assert "a.md" in md and "b.md" in md
    assert "〔1〕" in md


def test_artifact_md_no_sources():
    md = sources_to_artifact_md("q", "a", [])
    assert "无命中来源" in md
