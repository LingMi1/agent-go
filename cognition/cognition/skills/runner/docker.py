"""Docker 容器脚本运行器（生产默认隔离）。

`docker run --rm --network none -v {skill}:/skill:ro -v {out}:/out:rw` + 内存/CPU/pids 限制
+ 超时杀容器；产物从 /out 扫描后经 report._maybe_upload 上传 MinIO。
不改调用方即可换 gVisor/Firecracker（见 base.ScriptRunner）。CI 默认用 Local 运行器覆盖逻辑，
本类的实机用例以 `@pytest.mark.docker` 标注、默认跳过。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import tempfile
from pathlib import Path
from typing import Optional

from cognition.config import Settings
from cognition.skills.runner.base import ScriptResult
from cognition.skills.runner.request import ScriptRunRequest, scan_artifacts
from cognition.skills.runner.staging import stage_inputs

logger = logging.getLogger(__name__)


class DockerScriptRunner:
    """一次性容器执行脚本，强隔离 + 资源限制。"""

    def __init__(
        self,
        image: str = "my-agent/skill-executor:latest",
        *,
        settings: Optional[Settings] = None,
        memory: str = "512m",
        cpus: str = "1",
        pids_limit: int = 128,
    ) -> None:
        self._image = image
        self._settings = settings
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit

    def _get_downloader(self):
        if getattr(self, "_downloader", None) is None:
            from cognition.attachments import MinioDownloader

            self._downloader = MinioDownloader(self._settings)
        return self._downloader

    def _docker_argv(self, req: ScriptRunRequest, out_dir: str, in_dir: str | None = None, gen_dir: str | None = None) -> list[str]:
        # 容器内命令：把 req.cmd 的相对脚本路径挂到 /skill 下执行；产物写 /out。
        # matplotlib 等需要可写缓存目录 → --tmpfs /tmp + MPLCONFIGDIR（--read-only 下唯一可写处）。
        interpreter, rel_script, json_args = req.cmd
        argv = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", self._memory,
            "--cpus", self._cpus,
            "--pids-limit", str(self._pids_limit),
            "--read-only",
            "--tmpfs", "/tmp",
            "-e", "MPLCONFIGDIR=/tmp",
            "-v", f"{req.workdir}:/skill:ro",
            "-v", f"{out_dir}:/out:rw",
            "-e", "SKILL_OUTPUT_DIR=/out",
        ]
        if in_dir is not None:
            argv += ["-v", f"{in_dir}:/in:ro", "-e", "SKILL_INPUT_DIR=/in"]
        if gen_dir is not None:  # B.1：生成图暂存只读挂载（不改 --network none/--read-only）
            argv += ["-v", f"{gen_dir}:/generated:ro", "-e", "SKILL_GENERATED_DIR=/generated"]
        argv += [
            "-w", "/skill",
            self._image,
            interpreter, f"/skill/{rel_script}", json_args,
        ]
        return argv

    async def run(
        self, req: ScriptRunRequest, *, run_id: str, tool_call_id: str, generated_key: str | None = None
    ) -> ScriptResult:
        out_dir = tempfile.mkdtemp(prefix="skill-out-")
        in_dir: str | None = None
        if req.input_files:
            in_dir = tempfile.mkdtemp(prefix="skill-in-")
            try:
                await asyncio.to_thread(stage_inputs, req.input_files, self._get_downloader(), in_dir)
            except Exception as exc:  # noqa: BLE001 — 输入落地失败=确定性前置失败
                return ScriptResult(exit_code=126, stdout="", stderr=f"输入文件下载失败: {exc}")
        from cognition.skills.runner.scratch import has_generated, run_generated_dir

        gk = generated_key or run_id  # 会话作用域键（续轮复用生成图），缺省回落 run_id
        gen_dir = run_generated_dir(gk) if has_generated(gk) else None
        argv = self._docker_argv(req, out_dir, in_dir, gen_dir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError as exc:
            return ScriptResult(exit_code=127, stdout="", stderr=f"docker 不可用: {exc}")

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
        from cognition.tools.report import _maybe_upload

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
