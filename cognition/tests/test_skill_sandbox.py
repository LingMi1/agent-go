"""沙箱路径校验防逃逸（安全关键，穷举逃逸用例）。"""

from __future__ import annotations

import os

import pytest

from cognition.skills import SkillSandboxError
from cognition.skills.sandbox import assert_path_allowed


def test_inside_base_allowed(tmp_path):
    base = tmp_path / "skill"
    (base / "references").mkdir(parents=True)
    f = base / "references" / "doc.md"
    f.write_text("x")
    got = assert_path_allowed(f, [base])
    assert got == f.resolve()


def test_base_itself_allowed(tmp_path):
    base = tmp_path / "skill"
    base.mkdir()
    assert assert_path_allowed(base, [base]) == base.resolve()


def test_dotdot_traversal_rejected(tmp_path):
    base = tmp_path / "skill"
    base.mkdir()
    with pytest.raises(SkillSandboxError):
        assert_path_allowed(base / ".." / ".." / "etc" / "passwd", [base])


def test_absolute_outside_rejected(tmp_path):
    base = tmp_path / "skill"
    base.mkdir()
    with pytest.raises(SkillSandboxError):
        assert_path_allowed("/etc/passwd", [base])


def test_symlink_escape_rejected(tmp_path):
    base = tmp_path / "skill"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = base / "link.txt"
    os.symlink(outside, link)  # base 内的软链指向 base 外
    with pytest.raises(SkillSandboxError):
        assert_path_allowed(link, [base])


def test_multiple_bases_any_match(tmp_path):
    b1 = tmp_path / "a"
    b2 = tmp_path / "b"
    b1.mkdir()
    b2.mkdir()
    f = b2 / "x.md"
    f.write_text("y")
    assert assert_path_allowed(f, [b1, b2]) == f.resolve()
