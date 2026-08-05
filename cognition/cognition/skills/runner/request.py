"""脚本运行请求构造与产物扫描映射（纯逻辑，不触碰文件系统/容器）。

从这里剥离出所有可确定性测试的部分：
- `build_request`：校验脚本名（禁 `..`/绝对路径/未知解释器），算超时（`max(requested+30, 60)`），
  把 args 序列化成单个 JSON 形参，拼出容器内命令行。
- `scan_artifacts`：把 (file_name, size) 列表映射成 ArtifactRef dict（与 report.py 同形状，复用 Go /artifacts 代理）。
真正的 FS 遍历/容器执行放在 docker.py / local.py。
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
from dataclasses import dataclass
from pathlib import Path

from cognition.skills import SkillLoadError, SkillSandboxError
from cognition.skills.frontmatter import SkillDefinition

# 解释器按扩展名派生（容器内可执行）。
_INTERPRETERS = {".js": "node", ".mjs": "node", ".py": "python3", ".sh": "bash"}

_SCRIPTS_SUBDIR = "scripts"
_MIN_TIMEOUT_S = 60.0
_TIMEOUT_GRACE_S = 30.0


@dataclass(frozen=True)
class ScriptRunRequest:
    """一次脚本执行的全部确定性参数。"""

    skill: str
    script: str            # scripts/ 下的相对脚本名
    args: dict
    timeout_s: float
    workdir: str           # 宿主上 skill 目录（挂载源）
    cmd: tuple[str, ...]    # 容器内命令行：interpreter scripts/<script> <json-args>
    # M9：脚本输入文件 (resource_key, dest_name)——runner exec 前从 MinIO 预下载到
    # $SKILL_INPUT_DIR（docker: -v in:/in:ro）。加性默认空，既有调用零影响。
    input_files: tuple[tuple[str, str], ...] = ()


def _validate_script_name(script: str) -> str:
    s = (script or "").strip()
    if not s:
        raise SkillLoadError("script 不能为空")
    # 禁绝对路径与穿越；只允许 scripts/ 下的相对名。
    norm = posixpath.normpath(s.replace("\\", "/"))
    if norm.startswith("/") or norm.startswith("..") or "/../" in norm:
        raise SkillSandboxError(f"script 名越界: {script}")
    if Path(norm).suffix not in _INTERPRETERS:
        raise SkillLoadError(f"不支持的脚本类型: {script}（支持 {sorted(_INTERPRETERS)}）")
    return norm


def build_request(
    skill: SkillDefinition,
    script: str,
    args: dict | None,
    *,
    default_timeout: float,
    requested_timeout: float | None = None,
    input_files: list[tuple[str, str]] | None = None,
) -> ScriptRunRequest:
    """构造脚本运行请求。超时统一 `max(requested+grace, 下限)`；args 作单 JSON 形参。"""
    norm = _validate_script_name(script)
    interpreter = _INTERPRETERS[Path(norm).suffix]
    requested = float(requested_timeout if requested_timeout is not None else default_timeout)
    timeout_s = max(requested + _TIMEOUT_GRACE_S, _MIN_TIMEOUT_S)
    payload = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    rel = posixpath.join(_SCRIPTS_SUBDIR, norm)
    return ScriptRunRequest(
        skill=skill.name,
        script=norm,
        args=dict(args or {}),
        timeout_s=timeout_s,
        workdir=str(skill.base_path),
        cmd=(interpreter, rel, payload),
        input_files=tuple(input_files or ()),
    )


def _sanitize_dest_name(name: str) -> str:
    """输入文件的落地名：只取 basename（上传名可能带路径字符），空则回退 input。"""
    base = posixpath.basename((name or "").replace("\\", "/")).strip()
    return base or "input"


def resolve_input_files(
    requested_names: list[str], attachments: list[dict]
) -> tuple[list[tuple[str, str]], list[str]]:
    """按文件名从**本 run 附件白名单**解析 (resource_key, dest_name)（纯函数）。

    安全设计：LLM 只能引用文件名，映射表来自本 run 的 attachments（其 key 已过 Go
    归属闸）——模型无法让 runner 去拉任意对象。返回 (resolved, problems)：
    problems 为人类可读错误行（不存在/重名歧义），调用方作为工具文本返回让模型自纠。
    """
    by_name: dict[str, list[str]] = {}
    for a in attachments or []:
        fn = str(a.get("file_name", "") or "")
        rk = str(a.get("resource_key", "") or "")
        if fn and rk:
            by_name.setdefault(fn, []).append(rk)

    resolved: list[tuple[str, str]] = []
    problems: list[str] = []
    seen_dest: set[str] = set()
    for name in requested_names or []:
        keys = by_name.get(name)
        if not keys:
            problems.append(f"附件「{name}」不存在。本次可用附件: {sorted(by_name) or '（无）'}")
            continue
        if len(keys) > 1:
            problems.append(f"附件名「{name}」有 {len(keys)} 个同名文件，无法唯一定位，请让用户重传改名。")
            continue
        dest = _sanitize_dest_name(name)
        if dest in seen_dest:
            problems.append(f"输入文件落地名冲突: {dest}")
            continue
        seen_dest.add(dest)
        resolved.append((keys[0], dest))
    return resolved, problems


def artifact_ref(*, file_name: str, size: int, run_id: str, tool_call_id: str) -> dict:
    """构造单个 ArtifactRef dict（与 report.build_report_artifact 同形状）。"""
    resource_key = f"{run_id}/{tool_call_id}/{file_name}"
    mime, _ = mimetypes.guess_type(file_name)
    return {
        "resource_key": resource_key,
        "name": file_name,
        "file_name": file_name,
        "mime_type": mime or "application/octet-stream",
        "size": int(size),
        "download_url": f"/artifacts/{resource_key}",
        "preview_url": f"/artifacts/{resource_key}",
        "missing": False,
    }


def scan_artifacts(files: list[tuple[str, int]], *, run_id: str, tool_call_id: str) -> list[dict]:
    """把 workdir 里产出的 (file_name, size) 列表映射成 ArtifactRef dict 列表。"""
    return [
        artifact_ref(file_name=name, size=size, run_id=run_id, tool_call_id=tool_call_id)
        for name, size in files
    ]
