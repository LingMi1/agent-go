"""渐进式披露的文本构造（纯逻辑）。

- L1 `catalog`：装配期一次，作为 `skill` 工具的 description，让 LLM 看到"有哪些 skill"。
- L2 `body`：LLM 调用 `skill(name=...)` 时返回 SKILL.md 正文 + 脚本摘要（按预算裁剪）。
- L3 由 skill_read/glob/grep 文件工具按需读 references（不在此文件）。
控制塞进上下文的体量（prompt 预算）是这里的核心职责。
"""

from __future__ import annotations

from typing import Iterable

from cognition.skills.frontmatter import SkillDefinition

_EMPTY_CATALOG = "（当前无可用 skill）"


def catalog(skills: Iterable[SkillDefinition]) -> str:
    """L1 目录：每个 skill 一行 `- name: description`。"""
    items = list(skills)
    if not items:
        return _EMPTY_CATALOG
    lines = ["可用 skills（用 skill(name=...) 展开正文）："]
    lines += [f"- {s.name}: {s.description}" for s in items]
    return "\n".join(lines)


def body(skill: SkillDefinition, scripts: Iterable[str], max_chars: int) -> str:
    """L2 正文 + 脚本摘要；超过 max_chars 截断并标注原长度。"""
    parts = [skill.content]
    script_list = list(scripts)
    if script_list:
        parts.append("\n可用脚本（用 script_runner 执行）: " + ", ".join(script_list))
    text = "\n".join(p for p in parts if p)
    if max_chars and len(text) > max_chars:
        original = len(text)
        text = text[:max_chars] + f"\n…（已截断，原文共 {original} 字符）"
    return text
