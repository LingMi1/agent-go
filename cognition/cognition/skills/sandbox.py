"""沙箱路径校验（纯逻辑，安全关键）。

限制 L3 文件读与脚本 workdir 只能落在已注册的 skill base 目录内，
防 `../` 穿越与 symlink 逃逸（`Path.resolve()` 会展开二者，再做前缀归属判断）。
这是容器隔离之外的纵深防御。
"""

from __future__ import annotations

from pathlib import Path

from cognition.skills import SkillSandboxError


def assert_path_allowed(target: str | Path, base_paths: list[str | Path]) -> Path:
    """校验 target 落在某个 base 之下，返回其真实绝对路径；越界 raise。"""
    resolved = Path(target).resolve()
    for base in base_paths:
        b = Path(base).resolve()
        if resolved == b or b in resolved.parents:
            return resolved
    raise SkillSandboxError(f"路径越出沙箱: {target}")
