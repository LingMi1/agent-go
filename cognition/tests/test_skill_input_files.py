"""脚本输入文件管道（M9 B2）：文件名白名单解析 + staging + runner/工具接线。

安全不变量：LLM 只能用**文件名**引用本 run 附件（白名单来自 config.metadata，key 已过
Go 归属闸）——名字不在白名单/歧义返回工具文本自纠，绝不能让模型指定任意对象 key。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from cognition.skills.registry import SkillRegistry
from cognition.skills.runner.local import LocalSubprocessScriptRunner
from cognition.skills.runner.request import resolve_input_files
from cognition.skills.runner.staging import stage_inputs
from cognition.skills.tools import build_skill_tools

ATTS = [
    {"resource_key": "uploads/u1/s1/aa-data.csv", "file_name": "data.csv", "mime_type": "text/csv", "size": 10},
    {"resource_key": "uploads/u1/s1/bb-dup.txt", "file_name": "dup.txt", "mime_type": "text/plain", "size": 1},
    {"resource_key": "uploads/u1/s2/cc-dup.txt", "file_name": "dup.txt", "mime_type": "text/plain", "size": 1},
]


def test_resolve_input_files_matrix():
    ok, problems = resolve_input_files(["data.csv"], ATTS)
    assert ok == [("uploads/u1/s1/aa-data.csv", "data.csv")] and not problems
    # 不存在 → 报错行含可用清单。
    _, p2 = resolve_input_files(["ghost.csv"], ATTS)
    assert p2 and "ghost.csv" in p2[0] and "data.csv" in p2[0]
    # 同名歧义 → 确定性报错。
    _, p3 = resolve_input_files(["dup.txt"], ATTS)
    assert p3 and "同名" in p3[0]
    # 带路径的请求名清洗为 basename 后匹配失败（白名单键是纯文件名）。
    _, p4 = resolve_input_files(["../etc/data.csv"], ATTS)
    assert p4  # "../etc/data.csv" 不在白名单
    assert resolve_input_files([], ATTS) == ([], [])


def test_stage_inputs_writes_and_raises(tmp_path):
    objs = {"uploads/u1/s1/aa-data.csv": b"a,b\n1,2\n"}
    staged = stage_inputs([("uploads/u1/s1/aa-data.csv", "data.csv")], objs.__getitem__, str(tmp_path))
    assert staged == ["data.csv"]
    assert (tmp_path / "data.csv").read_bytes() == b"a,b\n1,2\n"
    try:
        stage_inputs([("nope", "x")], objs.__getitem__, str(tmp_path))
        raise AssertionError("should raise")
    except KeyError:
        pass


_SKILL_MD = """---
name: reader
description: 读输入文件测试
---
读取 $SKILL_INPUT_DIR 下的文件。
"""

_READ_PY = """
import json, os, sys
in_dir = os.environ.get("SKILL_INPUT_DIR", "")
args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
name = args.get("file", "data.csv")
with open(os.path.join(in_dir, name), encoding="utf-8") as f:
    content = f.read()
out = os.environ.get("SKILL_OUTPUT_DIR", ".")
with open(os.path.join(out, "echo.txt"), "w", encoding="utf-8") as f:
    f.write(content.upper())
print(f"read {name}: {len(content)} chars")
"""


def _skill_dir(tmp_path) -> Path:
    d = tmp_path / "reader"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (d / "scripts" / "read.py").write_text(_READ_PY, encoding="utf-8")
    return d


def _tools(tmp_path, downloader):
    reg = SkillRegistry()
    reg.refresh([tmp_path])
    tools = build_skill_tools(reg, LocalSubprocessScriptRunner(downloader=downloader))
    return {t.name: t for t in tools}


def test_local_runner_end_to_end_reads_staged_input(tmp_path):
    objs = {"uploads/u1/s1/aa-data.csv": "名字,分数\n甲,90\n".encode()}
    _skill_dir(tmp_path)
    tools = _tools(tmp_path, objs.__getitem__)

    async def run():
        return await tools["script_runner"].ainvoke(
            {"args": {"skill": "reader", "script": "read.py",
                      "script_args": {"file": "data.csv"}, "input_files": ["data.csv"]},
             "id": "c1", "name": "script_runner", "type": "tool_call"},
            config={"metadata": {"request_id": "r1",
                                 "attachments": '[{"resource_key":"uploads/u1/s1/aa-data.csv","file_name":"data.csv"}]'}},
        )

    msg = asyncio.run(run())
    assert "read data.csv" in msg.content
    assert msg.artifact and msg.artifact[0]["file_name"] == "echo.txt"


def test_tool_rejects_non_whitelisted_name(tmp_path):
    _skill_dir(tmp_path)
    tools = _tools(tmp_path, lambda k: b"")

    async def run():
        return await tools["script_runner"].ainvoke(
            {"args": {"skill": "reader", "script": "read.py", "input_files": ["秘密.txt"]},
             "id": "c2", "name": "script_runner", "type": "tool_call"},
            config={"metadata": {"request_id": "r1", "attachments": "[]"}},
        )

    msg = asyncio.run(run())
    assert "不存在" in msg.content and msg.artifact is None


def test_download_failure_is_deterministic_precondition_failure(tmp_path):
    def boom(key: str) -> bytes:
        raise RuntimeError("minio down")

    _skill_dir(tmp_path)
    tools = _tools(tmp_path, boom)

    async def run():
        return await tools["script_runner"].ainvoke(
            {"args": {"skill": "reader", "script": "read.py", "input_files": ["data.csv"]},
             "id": "c3", "name": "script_runner", "type": "tool_call"},
            config={"metadata": {"request_id": "r1",
                                 "attachments": '[{"resource_key":"uploads/u1/s1/aa-data.csv","file_name":"data.csv"}]'}},
        )

    msg = asyncio.run(run())
    assert "输入文件下载失败" in msg.content  # exit=126 stderr 进摘要，脚本未运行


def test_data_analysis_skill_real_duckdb(tmp_path):
    """B3：data-analysis 技能经 LocalRunner 真实执行（duckdb 视图/summary/query）。"""
    import pytest

    pytest.importorskip("duckdb")
    from pathlib import Path as _P

    skill_dir = _P(__file__).resolve().parents[1] / "runtime" / "skills"
    assert (skill_dir / "data-analysis" / "SKILL.md").exists()

    objs = {"uploads/u/s/aa-sales.csv": "类别,金额\n水果,10\n水果,20\n蔬菜,5\n".encode()}
    reg = SkillRegistry()
    reg.refresh([str(skill_dir)])
    tools = {t.name: t for t in build_skill_tools(reg, LocalSubprocessScriptRunner(downloader=objs.__getitem__))}

    async def run(mode_args):
        return await tools["script_runner"].ainvoke(
            {"args": {"skill": "data-analysis", "script": "analyze.py",
                      "input_files": ["sales.csv"], "script_args": mode_args},
             "id": "da1", "name": "script_runner", "type": "tool_call"},
            config={"metadata": {"request_id": "r1",
                                 "attachments": '[{"resource_key":"uploads/u/s/aa-sales.csv","file_name":"sales.csv"}]'}},
        )

    # summary 模式
    msg = asyncio.run(run({"files": ["sales.csv"], "mode": "summary"}))
    assert "已分析 1 个文件" in msg.content
    names = {a["file_name"] for a in (msg.artifact or [])}
    assert "analysis.md" in names
    # query 模式：聚合正确
    msg2 = asyncio.run(run({"files": ["sales.csv"], "mode": "query",
                            "sql": 'SELECT 类别, SUM(金额) AS s FROM sales GROUP BY 类别 ORDER BY s DESC'}))
    assert "SQL 返回 2 行" in msg2.content
    names2 = {a["file_name"] for a in (msg2.artifact or [])}
    assert {"analysis.md", "result.csv"} <= names2
    # 非 SELECT 拒绝
    msg3 = asyncio.run(run({"files": ["sales.csv"], "mode": "query", "sql": "DROP TABLE sales"}))
    assert "失败" in msg3.content


def test_chart_and_ppt_skills_produce_artifacts():
    """B4：chart 双产物 + pptx 非空 + md→html（经 LocalRunner 真实执行）。"""
    import pytest

    pytest.importorskip("matplotlib")
    pytest.importorskip("pptx")
    from pathlib import Path as _P

    skill_dir = _P(__file__).resolve().parents[1] / "runtime" / "skills"
    reg = SkillRegistry()
    reg.refresh([str(skill_dir)])
    tools = {t.name: t for t in build_skill_tools(reg, LocalSubprocessScriptRunner())}

    async def run(skill, script, args):
        return await tools["script_runner"].ainvoke(
            {"args": {"skill": skill, "script": script, "script_args": args},
             "id": f"{skill}-1", "name": "script_runner", "type": "tool_call"},
            config={"metadata": {"request_id": "r1"}},
        )

    # chart：双产物（吃 B1 多产物修复）。
    msg = asyncio.run(run("chart-visualization", "render.py",
                          {"type": "bar", "title": "销售", "labels": ["水果", "蔬菜"],
                           "series": [{"name": "金额", "data": [30, 5]}]}))
    names = {a["file_name"] for a in (msg.artifact or [])}
    assert names == {"chart.png", "echarts-option.json"}, msg.content

    # pptx：文件非空。
    msg2 = asyncio.run(run("ppt-generation", "build_pptx.py",
                           {"title": "汇报", "slides": [{"title": "P1", "bullets": ["a", "b"]}]}))
    arts2 = {a["file_name"]: a for a in (msg2.artifact or [])}
    assert "presentation.pptx" in arts2 and arts2["presentation.pptx"]["size"] > 1000

    # md→html。
    msg3 = asyncio.run(run("ppt-generation", "md_to_html.py",
                           {"title": "报告", "markdown": "# 一\n\n- 点1\n- 点2\n\n**加粗**"}))
    assert any(a["file_name"] == "document.html" for a in (msg3.artifact or []))
