"""运行级生成图暂存区（B.1：让生成的图能内联进 frontend-design 的网页）。

问题：生图工具把图存进 MinIO 的 run/tc/ 下（run 产物），但技能沙箱 --network none、
输入只从附件白名单落地，够不到 run 产物——所以"生成图 + 做成网页"里网页嵌不进那张图。

方案：一个**按 run_id 键控的本地暂存目录**。image_generate 生图后把副本写进这里；
script_runner 跑技能时若该 run 有暂存图，就把此目录**只读**挂进沙箱（SKILL_GENERATED_DIR），
render_page.py 据此把 src="generated/xxx" 的引用替换成 data-URI 内联。

安全：只读挂载，不改 --network none / --read-only 语义；路径在进程本机 tmp（docker 挂宿主路径，
与 out_dir/in_dir 同机制）。清理：随 OS tmp 回收（单用户 dev 可接受，见 docs/10）。
"""

from __future__ import annotations

import os
import re
import tempfile

_BASE = os.path.join(tempfile.gettempdir(), "agent-go-generated")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(seg: str) -> str:
    out = _SAFE.sub("_", seg or "unknown")[:120]
    # 中和纯点段（. / ..）：否则 run_generated_dir(".") 会回落到 _BASE（读到别 run 的图）。
    return out if out.strip(".") else "unknown"


def run_generated_dir(run_id: str, *, create: bool = False) -> str:
    """返回某 run 的生成图暂存目录路径；create=True 时确保存在。"""
    path = os.path.join(_BASE, _safe(run_id))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


# 暂存目录保留上限（无 run-end 钩子，就地机会性清理防无界增长）。
# 24h：暂存现按**会话**作用域（key=session_id），续轮改需求要能复用此前轮次的生成图
#（1h 曾导致「改个版式就得重新生图」）；MinIO 产物仍是持久层，这里只是内联缓存。
_MAX_AGE_S = 86400


def _sweep_old() -> None:
    """机会性清理：删掉超龄的 run 暂存目录（best-effort，任何失败都吞掉）。"""
    try:
        import time

        now = time.time()
        for name in os.listdir(_BASE):
            p = os.path.join(_BASE, name)
            try:
                if os.path.isdir(p) and now - os.path.getmtime(p) > _MAX_AGE_S:
                    import shutil

                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def stash_generated(run_id: str, file_name: str, data: bytes) -> None:
    """把一张生成图写进该 run 的暂存区（best-effort，失败不抛——不阻断生图主流程）。"""
    try:
        d = run_generated_dir(run_id, create=True)
        with open(os.path.join(d, _safe(file_name)), "wb") as f:
            f.write(data)
        _sweep_old()  # 顺手清超龄目录（成本极低，只在生图后触发）
    except OSError:
        pass


def next_image_index(key: str) -> int:
    """该暂存区下一个可用的 image-N.png 编号（纯扫描；空目录→1）。

    会话作用域后编号必须跨轮续接：第二轮再生图若仍从 image-1 起会覆盖第一轮的暂存图，
    frontend-design 引用 generated/image-1.png 就指错图。
    """
    import re

    d = run_generated_dir(key)
    mx = 0
    try:
        for entry in os.scandir(d):
            m = re.fullmatch(r"image-(\d+)\.png", entry.name)
            if m:
                mx = max(mx, int(m.group(1)))
    except OSError:
        pass
    return mx + 1


def has_generated(run_id: str) -> bool:
    """该 run 是否有暂存的生成图（决定要不要给沙箱挂载 SKILL_GENERATED_DIR）。"""
    d = run_generated_dir(run_id)
    try:
        return os.path.isdir(d) and any(os.scandir(d))
    except OSError:
        return False
