"""输入文件落地（local/docker 两 runner 共用，避免下载逻辑双份漂移）。

同步函数：runner 必须经 `asyncio.to_thread` 调用——MinIO 下载是阻塞 I/O，
在 grpc.aio 单事件循环上裸跑会冻结全部并发 run（M8 立下的红线）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable


def stage_inputs(
    input_files: Iterable[tuple[str, str]],
    downloader: Callable[[str], bytes],
    in_dir: str,
) -> list[str]:
    """把 (resource_key, dest_name) 逐个下载写入 in_dir，返回落地文件名列表。

    任一文件失败即抛（调用方转成确定性的脚本前置失败——静默缺文件会让脚本
    产出看似成功的错误结果）。dest_name 已由 resolve_input_files 清洗为 basename。
    """
    staged: list[str] = []
    root = Path(in_dir)
    for resource_key, dest_name in input_files:
        data = downloader(resource_key)
        (root / dest_name).write_bytes(data)
        staged.append(dest_name)
    return staged
