"""SkillRegistry（编排）：扫描目录 → 加载 SKILL.md → 重名 raise → 不可变缓存 / refresh。

约定目录结构：`<skills_dir>/<skill-name>/SKILL.md`（+ 可选 references/ scripts/）。
也支持 `<skills_dir>/SKILL.md`（单技能目录）。重名（frontmatter.name 冲突）直接 raise，
保证缓存确定性。base_paths 供 sandbox 使用。
"""

from __future__ import annotations

from pathlib import Path

from cognition.skills import SkillLoadError
from cognition.skills.frontmatter import SkillDefinition, parse_skill_md

_SCRIPTS_SUBDIR = "scripts"


class SkillRegistry:
    """已加载 skill 的进程级注册表（装配期 refresh 一次）。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}

    def refresh(self, dirs: list[str | Path]) -> None:
        """重新扫描全部目录，重建不可变缓存。重名即 raise。"""
        skills: dict[str, SkillDefinition] = {}
        for d in dirs:
            root = Path(d)
            if not root.exists():
                continue
            for skill_md in self._iter_skill_files(root):
                sk = parse_skill_md(skill_md.read_text(encoding="utf-8"), skill_md.parent)
                if sk.name in skills:
                    raise SkillLoadError(
                        f"skill 重名: {sk.name}（{skill_md} 与 {skills[sk.name].base_path}）"
                    )
                skills[sk.name] = sk
        self._skills = skills

    @staticmethod
    def _iter_skill_files(root: Path):
        if (root / "SKILL.md").is_file():
            yield root / "SKILL.md"
        for child in sorted(root.glob("*/SKILL.md")):
            yield child

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def list(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    @property
    def base_paths(self) -> list[Path]:
        return [s.base_path for s in self._skills.values()]

    def scripts_of(self, skill: SkillDefinition) -> list[str]:
        """列出 skill scripts/ 下的脚本文件名（供 L2 摘要与执行校验）。"""
        scripts_dir = skill.base_path / _SCRIPTS_SUBDIR
        if not scripts_dir.is_dir():
            return []
        return sorted(p.name for p in scripts_dir.iterdir() if p.is_file())
