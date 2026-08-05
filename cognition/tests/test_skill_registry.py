"""SkillRegistry 扫描/重名/不可变缓存（tmp_path，无模型/网络）。"""

from __future__ import annotations

import pytest

from cognition.skills import SkillLoadError
from cognition.skills.registry import SkillRegistry


def _write_skill(root, dirname, name, desc="d", scripts=None):
    d = root / dirname
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n正文", encoding="utf-8")
    if scripts:
        sd = d / "scripts"
        sd.mkdir()
        for s in scripts:
            (sd / s).write_text("# script", encoding="utf-8")
    return d


def test_scan_loads_multiple(tmp_path):
    _write_skill(tmp_path, "a", "chart", scripts=["gen.js", "render.py"])
    _write_skill(tmp_path, "b", "ppt")
    reg = SkillRegistry()
    reg.refresh([tmp_path])
    names = {s.name for s in reg.list()}
    assert names == {"chart", "ppt"}
    assert reg.get("chart") is not None
    assert reg.scripts_of(reg.get("chart")) == ["gen.js", "render.py"]  # 排序稳定


def test_duplicate_name_raises(tmp_path):
    _write_skill(tmp_path, "a", "dup")
    _write_skill(tmp_path, "b", "dup")
    reg = SkillRegistry()
    with pytest.raises(SkillLoadError):
        reg.refresh([tmp_path])


def test_missing_dir_ignored(tmp_path):
    reg = SkillRegistry()
    reg.refresh([tmp_path / "nope"])  # 不存在的目录被忽略
    assert reg.list() == []


def test_base_paths_for_sandbox(tmp_path):
    d = _write_skill(tmp_path, "a", "chart")
    reg = SkillRegistry()
    reg.refresh([tmp_path])
    assert d in reg.base_paths
