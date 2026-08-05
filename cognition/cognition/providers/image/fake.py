"""确定性假图 provider：同 prompt 恒同图（单测/离线 e2e 用，不触网）。"""

from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Optional


def _png(rgb: tuple[int, int, int], w: int = 8, h: int = 8) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeImageProvider:
    # 是否真正把源图送去生成（供 image_generate 措辞判断，不谎称图生图）。
    supports_image_to_image = True
    # 声明支持 inpaint（供离线 e2e 走通 mask 链路）：收到 mask 时把它混入哈希——
    # 同 prompt 有/无 mask 产出不同图，测试可据此断言 mask 真被消费。
    supports_inpaint = True
    """颜色由 prompt 哈希决定：确定性 + 不同 prompt 可区分。"""

    async def generate(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,
        size: str = "1024x1024",
        n: int = 1,
        mask: Optional[bytes] = None,
    ) -> list[bytes]:
        digest = hashlib.sha256(prompt.encode("utf-8") + (b"+mask" if mask is not None else b"")).digest()
        return [_png((digest[0], digest[1], (digest[2] + i) % 256)) for i in range(max(1, int(n)))]
