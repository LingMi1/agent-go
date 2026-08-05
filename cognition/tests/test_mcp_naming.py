"""MCP 工具名命名空间与去重（纯逻辑）。"""

from __future__ import annotations

from dataclasses import dataclass

from cognition.mcp.naming import dedup, namespaced


@dataclass
class _Tool:
    name: str
    tag: str = ""


def test_namespaced():
    assert namespaced("github", "search") == "mcp__github__search"


def test_cross_server_same_original_name_no_collision():
    a = namespaced("github", "search")
    b = namespaced("gitlab", "search")
    assert a != b


def test_dedup_later_overrides_earlier_stable_order():
    items = [_Tool("x", "1"), _Tool("y", "2"), _Tool("x", "3"), _Tool("z", "4")]
    out = dedup(items)
    assert [t.name for t in out] == ["x", "y", "z"]  # 首次出现定位置
    assert next(t for t in out if t.name == "x").tag == "3"  # 后者覆盖前者的值


def test_dedup_no_duplicates_identity():
    items = [_Tool("a"), _Tool("b")]
    out = dedup(items)
    assert [t.name for t in out] == ["a", "b"]
