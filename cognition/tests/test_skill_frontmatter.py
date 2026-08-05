"""SKILL.md frontmatter/正文解析与校验（纯逻辑）。"""

from __future__ import annotations

import pytest

from cognition.skills import SkillLoadError
from cognition.skills.frontmatter import parse_skill_md

_OK = """---
name: chart-visualization
description: 把结构化数据渲染成图表
dependency: ["node>=18"]
---
# 图表技能

用 scripts/generate.js 生成图表。
"""


def test_parse_ok():
    sk = parse_skill_md(_OK, "/skills/chart")
    assert sk.name == "chart-visualization"
    assert sk.description == "把结构化数据渲染成图表"
    assert "生成图表" in sk.content
    assert sk.front_matter["dependency"] == ["node>=18"]  # 多余字段保留
    assert str(sk.base_path) == "/skills/chart"


def test_missing_frontmatter_delimiter_raises():
    with pytest.raises(SkillLoadError):
        parse_skill_md("# 没有 frontmatter 的正文", "/skills/x")


def test_missing_name_raises():
    with pytest.raises(SkillLoadError):
        parse_skill_md("---\ndescription: 有描述没名字\n---\n正文", "/skills/x")


def test_missing_description_raises():
    with pytest.raises(SkillLoadError):
        parse_skill_md("---\nname: x\n---\n正文", "/skills/x")
