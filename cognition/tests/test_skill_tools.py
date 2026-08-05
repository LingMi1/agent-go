"""Skill 工具契约：L2 展开 / L3 沙箱读 / script_runner（本地运行器，真实子进程）。"""

from __future__ import annotations

import textwrap

from cognition.skills.registry import SkillRegistry
from cognition.skills.runner.local import LocalSubprocessScriptRunner
from cognition.skills.tools import build_skill_tools

_GEN_PY = textwrap.dedent(
    """
    import os, sys, json
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    out = os.environ["SKILL_OUTPUT_DIR"]
    with open(os.path.join(out, "result.txt"), "w") as f:
        f.write("hello " + str(args.get("who", "world")))
    print("generated result.txt for", args.get("who", "world"))
    """
).strip()


def _build_skill(tmp_path):
    d = tmp_path / "chart"
    (d / "scripts").mkdir(parents=True)
    (d / "references").mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: chart\ndescription: 画图技能\n---\n用 scripts/gen.py 生成结果。", encoding="utf-8"
    )
    (d / "scripts" / "gen.py").write_text(_GEN_PY, encoding="utf-8")
    (d / "references" / "guide.md").write_text("# 参考\n详细说明", encoding="utf-8")
    return d


def _tools(tmp_path):
    reg = SkillRegistry()
    reg.refresh([tmp_path])
    tools = build_skill_tools(reg, LocalSubprocessScriptRunner())
    return {t.name: t for t in tools}, reg


def test_skill_l2_body_and_provider(tmp_path):
    _build_skill(tmp_path)
    tools, _ = _tools(tmp_path)
    assert all(t.metadata.get("provider") == "skill" for t in tools.values())
    out = tools["skill"].invoke({"name": "chart"})
    assert "生成结果" in out
    assert "gen.py" in out  # 脚本摘要


def test_skill_read_sandbox(tmp_path):
    d = _build_skill(tmp_path)
    tools, _ = _tools(tmp_path)
    ok = tools["skill_read"].invoke({"path": str(d / "references" / "guide.md")})
    assert "详细说明" in ok
    denied = tools["skill_read"].invoke({"path": "/etc/passwd"})
    assert "拒绝" in denied  # 沙箱外被拒


async def test_script_runner_executes_and_registers_artifact(tmp_path):
    _build_skill(tmp_path)
    tools, _ = _tools(tmp_path)
    msg = await tools["script_runner"].ainvoke(
        {
            "args": {"skill": "chart", "script": "gen.py", "script_args": {"who": "阿里"}},
            "id": "call-1",
            "name": "script_runner",
            "type": "tool_call",
        }
    )
    # ToolNode 风格：ainvoke(ToolCall) 返回 ToolMessage（content + artifact）。
    # artifact 为列表（多产物技能不丢件；mapper._coerce_artifacts 原生接受列表）。
    assert "generated result.txt" in msg.content
    assert isinstance(msg.artifact, list) and len(msg.artifact) == 1
    assert msg.artifact[0]["file_name"] == "result.txt"
    assert msg.artifact[0]["download_url"] == "/artifacts/run/call-1/result.txt"


_GEN2_PY = '''
import json, os, sys
out = os.environ.get("SKILL_OUTPUT_DIR", ".")
for name in ("a.png", "b.json"):
    with open(os.path.join(out, name), "w") as f:
        f.write("x")
print("two files")
'''


async def test_script_runner_returns_all_artifacts(tmp_path):
    """多产物技能（如 chart 的 PNG+JSON）必须全部回传，不只 artifacts[0]。"""
    d = _build_skill(tmp_path)
    (d / "scripts" / "gen2.py").write_text(_GEN2_PY, encoding="utf-8")
    tools, _ = _tools(tmp_path)
    msg = await tools["script_runner"].ainvoke(
        {"args": {"skill": "chart", "script": "gen2.py"},
         "id": "call-2", "name": "script_runner", "type": "tool_call"}
    )
    assert isinstance(msg.artifact, list) and len(msg.artifact) == 2
    names = {a["file_name"] for a in msg.artifact}
    assert names == {"a.png", "b.json"}
