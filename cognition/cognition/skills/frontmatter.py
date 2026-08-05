"""SKILL.md 解析（纯逻辑）：拆 frontmatter/正文 + YAML 解析 + 必填校验。

SKILL.md 遵循 Claude Agent Skills 约定：
```
---
name: chart-visualization
description: 把结构化数据渲染成图表
---
（渐进式披露正文……）
```
name/description 缺失即 raise（保证注册表缓存确定性）；多余 frontmatter 字段（如 dependency）保留在 front_matter。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cognition.skills import SkillLoadError

# 起始必须是 --- 分隔的 frontmatter；DOTALL 让 . 匹配换行。
_FM_RE = re.compile(r"^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)$", re.DOTALL)


@dataclass(frozen=True)
class SkillDefinition:
    """一个已加载的 skill（不可变）。"""

    name: str
    description: str
    content: str                      # frontmatter 之后的正文（L2）
    base_path: Path                   # skill 目录（sandbox base + scripts/references 根）
    front_matter: dict[str, Any] = field(default_factory=dict)


def parse_skill_md(text: str, base_path: str | Path) -> SkillDefinition:
    """解析 SKILL.md 文本 → SkillDefinition。缺分隔符/缺 name/缺 description 均 raise。"""
    m = _FM_RE.match(text or "")
    if not m:
        raise SkillLoadError("SKILL.md 缺少 frontmatter（--- 分隔）")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"SKILL.md frontmatter YAML 解析失败: {exc}") from exc
    if not isinstance(fm, dict):
        raise SkillLoadError("SKILL.md frontmatter 必须是键值对")

    name = fm.get("name")
    description = fm.get("description")
    if not name:
        raise SkillLoadError("SKILL.md 缺少必填字段 name")
    if not description:
        raise SkillLoadError("SKILL.md 缺少必填字段 description")

    return SkillDefinition(
        name=str(name).strip(),
        description=str(description).strip(),
        content=m.group(2).strip(),
        base_path=Path(base_path),
        front_matter=fm,
    )
