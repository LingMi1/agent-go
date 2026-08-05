"""脚本运行请求构造与产物扫描映射（纯逻辑，不起容器）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognition.skills import SkillLoadError, SkillSandboxError
from cognition.skills.frontmatter import SkillDefinition
from cognition.skills.runner.request import build_request, scan_artifacts


def _skill():
    return SkillDefinition(name="chart", description="d", content="", base_path=Path("/skills/chart"))


def test_build_request_basic():
    req = build_request(_skill(), "generate.js", {"b": 2, "a": 1}, default_timeout=120)
    assert req.skill == "chart"
    assert req.script == "generate.js"
    assert req.workdir == "/skills/chart"
    # cmd = (interpreter, scripts/<script>, json-args)
    assert req.cmd[0] == "node"
    assert req.cmd[1] == "scripts/generate.js"
    assert json.loads(req.cmd[2]) == {"a": 1, "b": 2}  # 稳定序列化


def test_timeout_is_max_of_grace_and_floor():
    # requested + 30
    assert build_request(_skill(), "s.py", {}, default_timeout=120, requested_timeout=120).timeout_s == 150.0
    # 下限 60
    assert build_request(_skill(), "s.py", {}, default_timeout=1, requested_timeout=1).timeout_s == 60.0


def test_interpreter_by_extension():
    assert build_request(_skill(), "a.py", {}, default_timeout=60).cmd[0] == "python3"
    assert build_request(_skill(), "a.sh", {}, default_timeout=60).cmd[0] == "bash"


def test_unknown_extension_rejected():
    with pytest.raises(SkillLoadError):
        build_request(_skill(), "a.rb", {}, default_timeout=60)


def test_path_traversal_script_rejected():
    with pytest.raises(SkillSandboxError):
        build_request(_skill(), "../../etc/evil.sh", {}, default_timeout=60)
    with pytest.raises(SkillSandboxError):
        build_request(_skill(), "/abs/evil.py", {}, default_timeout=60)


def test_scan_artifacts_shape():
    refs = scan_artifacts([("chart.png", 1024), ("data.json", 20)], run_id="r1", tool_call_id="tc1")
    assert refs[0]["resource_key"] == "r1/tc1/chart.png"
    assert refs[0]["download_url"] == "/artifacts/r1/tc1/chart.png"
    assert refs[0]["mime_type"] == "image/png"
    assert refs[0]["size"] == 1024
    assert refs[0]["missing"] is False
    assert refs[1]["mime_type"] == "application/json"
