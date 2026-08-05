"""反思解析与停止判据（纯逻辑）。"""

from __future__ import annotations

from cognition.rag.reflect import parse_reflection, should_stop


def test_parse_valid_json():
    assert parse_reflection('{"is_answer": true, "rewrite_query": ""}') == (True, "")
    assert parse_reflection('{"is_answer": false, "rewrite_query": "换个问法"}') == (False, "换个问法")


def test_parse_json_embedded_in_text():
    raw = '好的，我的判断是：{"is_answer": false, "rewrite_query": "细化查询X"} 以上。'
    assert parse_reflection(raw) == (False, "细化查询X")


def test_parse_missing_fields_defaults_false():
    assert parse_reflection("{}") == (False, "")


def test_parse_invalid_json_fallback():
    # 非法 JSON：回退 (False, "")；除非命中启发式
    assert parse_reflection("完全不是 json") == (False, "")


def test_should_stop_truth_table():
    assert should_stop(loop=0, limit=2, is_answer=True) is True    # 已可答
    assert should_stop(loop=2, limit=2, is_answer=False) is True   # 达上限
    assert should_stop(loop=3, limit=2, is_answer=False) is True   # 超上限
    assert should_stop(loop=1, limit=2, is_answer=False) is False  # 继续
