"""ScriptRunner 抽象 + 结果类型。

调用方（skills/tools.py 的 script_runner）只依赖这个抽象；具体隔离由 Docker / Local /
（未来）gVisor 实现。这就是 §00 §4.2"真沙箱留 seam"的落点：换实现不改调用方。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cognition.skills.runner.request import ScriptRunRequest


@dataclass(frozen=True)
class ScriptResult:
    """一次脚本执行的结果 + 产物登记。"""

    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[dict] = field(default_factory=list)
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class ScriptRunner(Protocol):
    async def run(self, req: ScriptRunRequest, *, run_id: str, tool_call_id: str) -> ScriptResult:
        """在隔离环境执行脚本，扫描产物并登记为 ArtifactRef。"""
        ...
