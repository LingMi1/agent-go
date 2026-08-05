"""渐进式披露 L1 目录 / L2 正文裁剪（纯逻辑）。"""

from __future__ import annotations

from pathlib import Path

from cognition.skills.disclosure import body, catalog
from cognition.skills.frontmatter import SkillDefinition


def _skill(name, desc, content="正文内容"):
    return SkillDefinition(name=name, description=desc, content=content, base_path=Path("/s"))


def test_catalog_lists_all():
    out = catalog([_skill("a", "描述A"), _skill("b", "描述B")])
    assert "- a: 描述A" in out
    assert "- b: 描述B" in out


def test_catalog_empty():
    assert catalog([]) == "（当前无可用 skill）"


def test_body_includes_scripts_summary():
    out = body(_skill("a", "d", content="怎么用"), ["generate.js", "render.py"], max_chars=0)
    assert "怎么用" in out
    assert "generate.js" in out and "render.py" in out


def test_body_truncates_when_over_budget():
    long = "x" * 500
    out = body(_skill("a", "d", content=long), [], max_chars=100)
    assert "已截断" in out
    assert "500" in out  # 标注原文长度
    assert len(out) < 500
