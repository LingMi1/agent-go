"""子问题扩展解析（纯逻辑）。"""

from __future__ import annotations

from cognition.rag.expand import parse_subquestions


def test_basic_lines():
    out = parse_subquestions("什么是RAG?\n混合检索怎么做?\n", limit=5)
    assert out == ["什么是RAG?", "混合检索怎么做?"]


def test_strips_numbering_and_bullets():
    raw = "1. 第一问\n2) 第二问\n- 第三问\n• 第四问"
    assert parse_subquestions(raw, limit=10) == ["第一问", "第二问", "第三问", "第四问"]


def test_dedup_preserve_order():
    assert parse_subquestions("a\nb\na\nc", limit=10) == ["a", "b", "c"]


def test_truncate_to_limit():
    assert parse_subquestions("a\nb\nc\nd", limit=2) == ["a", "b"]


def test_empty_and_blank():
    assert parse_subquestions("", limit=3) == []
    assert parse_subquestions("\n  \n\t\n", limit=3) == []
