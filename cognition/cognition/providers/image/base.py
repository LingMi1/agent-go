"""图像生成 provider 协议（结构化 duck-typing，与 rag 的 provider 协议同风格）。"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class ImageGenProvider(Protocol):
    """文生图/图生图/局部重绘统一入口。

    images：可选源图字节（风格化/编辑场景），不支持的实现应忽略而非报错。
    mask：可选蒙版字节（inpaint 局部重绘，RGBA PNG、alpha=0 透明区=要重绘的区域，
    只作用于第一张源图）。不支持的实现同样忽略参数、由工具层按 `supports_inpaint`
    类属性如实措辞（镜像 `supports_image_to_image` 的诚实声明约定），绝不谎报能力。
    返回原始图片字节（PNG/JPEG），由调用方负责落 MinIO 与 ArtifactRef。
    """

    async def generate(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,
        size: str = "1024x1024",
        n: int = 1,
        mask: Optional[bytes] = None,
    ) -> list[bytes]: ...
