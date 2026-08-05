"""本地子进程运行器（dev/CI 用，无隔离）。

行为与 Docker 运行器一致（同样的产物约定），但直接在宿主子进程里跑——用于单测覆盖
"请求构造→执行→产物扫描→（可选）上传"的整条逻辑，不依赖 docker daemon。
脚本约定：把产物写入环境变量 `SKILL_OUTPUT_DIR` 指向的目录；args 作为 argv[1]（JSON）。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Optional

from cognition.config import Settings
from cognition.skills.runner.base import ScriptResult
from cognition.skills.runner.request import ScriptRunRequest, scan_artifacts
from cognition.skills.runner.staging import stage_inputs

logger = logging.getLogger(__name__)


class LocalSubprocessScriptRunner:
    """在宿主子进程执行脚本（无隔离）。生产用 DockerScriptRunner。"""

    def __init__(self, settings: Optional[Settings] = None, *, downloader=None) -> None:
        self._settings = settings
        self._downloader = downloader  # 测试注入；缺省惰性建 MinioDownloader

    def _get_downloader(self):
        if self._downloader is None:
            from cognition.attachments import MinioDownloader

            self._downloader = MinioDownloader(self._settings)
        return self._downloader

    async def run(
        self, req: ScriptRunRequest, *, run_id: str, tool_call_id: str, generated_key: str | None = None
    ) -> ScriptResult:
        out_dir = tempfile.mkdtemp(prefix="skill-out-")
        env = {**os.environ, "SKILL_OUTPUT_DIR": out_dir, "SKILL_ARGS": req.cmd[-1]}
        if req.input_files:
            in_dir = tempfile.mkdtemp(prefix="skill-in-")
            try:
                # to_thread：MinIO 下载是阻塞 I/O，不得占 grpc.aio 事件循环。
                await asyncio.to_thread(stage_inputs, req.input_files, self._get_downloader(), in_dir)
            except Exception as exc:  # noqa: BLE001 — 输入落地失败=确定性前置失败
                return ScriptResult(exit_code=126, stdout="", stderr=f"输入文件下载失败: {exc}")
            env["SKILL_INPUT_DIR"] = in_dir
        # B.1：生成图暂存挂载（generated_key=会话作用域键，续轮可复用此前轮次的生成图；
        # 缺省回落 run_id 保持旧行为）。
        from cognition.skills.runner.scratch import has_generated, run_generated_dir

        gk = generated_key or run_id
        if has_generated(gk):
            env["SKILL_GENERATED_DIR"] = run_generated_dir(gk)
        cmd = list(req.cmd)
        # local 专属：.py 用当前 venv 解释器（裸 python3 取自 PATH，技能依赖组装在 venv 里
        # 会失效）。req.cmd 保持 python3 不动——docker 路径按容器内解释器执行。
        if cmd[0] == "python3":
            import sys

            cmd[0] = sys.executable
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=req.workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return ScriptResult(exit_code=127, stdout="", stderr=f"运行器无法启动: {exc}")

        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=req.timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            out, err = await proc.communicate()

        files = [
            (p.name, p.stat().st_size) for p in sorted(Path(out_dir).glob("*")) if p.is_file()
        ]
        artifacts = scan_artifacts(files, run_id=run_id, tool_call_id=tool_call_id)
        self._maybe_upload(out_dir, run_id, tool_call_id)

        return ScriptResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
            artifacts=artifacts,
            timed_out=timed_out,
        )

    def _maybe_upload(self, out_dir: str, run_id: str, tool_call_id: str) -> None:
        if self._settings is None or not self._settings.minio_upload_enabled:
            return
        from cognition.tools.report import _maybe_upload  # 复用惰性可降级上传

        for p in sorted(Path(out_dir).glob("*")):
            if not p.is_file():
                continue
            mime, _ = mimetypes.guess_type(p.name)
            _maybe_upload(
                self._settings,
                f"{run_id}/{tool_call_id}/{p.name}",
                p.read_bytes(),
                mime or "application/octet-stream",
            )
