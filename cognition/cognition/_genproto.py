"""生成的 protobuf/gRPC stub 的统一加载入口。

背景：`make proto-py` 把 stub 生成到 `cognition/genproto/`（protoc 的输出根），其中
`agent_pb2_grpc.py` 里是 `from agent.v1 import agent_pb2`——这要求 `genproto/` 目录本身
在 sys.path 上（即把 `agent` 当顶层包）。genproto 被 gitignore 且由 make 重新生成，所以
不去改生成文件，而是在这里把该目录加入 sys.path 并复用导出。

**所有代码都经由本模块取 stub**，以保证 `_pb2` 只有一个模块身份——同一个 .proto 在
protobuf 默认 descriptor pool 里重复注册会抛 "duplicate file" 错误。

纯逻辑测试不会 import 本模块（Event.to_proto 内部才延迟 import），因此无需 genproto。
"""

from __future__ import annotations

import sys
from pathlib import Path

_GENPROTO_DIR = Path(__file__).resolve().parent / "genproto"
if _GENPROTO_DIR.is_dir():
    _p = str(_GENPROTO_DIR)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent.v1 import agent_pb2, agent_pb2_grpc  # noqa: E402

__all__ = ["agent_pb2", "agent_pb2_grpc"]
