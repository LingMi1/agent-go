"""图像生成 provider（M9 线 B 生成侧）。

协议：`generate(prompt, images=None, size, n, mask=None) -> list[bytes]`（PNG/JPEG 原始字节；
mask 为 inpaint 蒙版，能力经 supports_inpaint 类属性诚实声明，不支持的实现忽略参数）。
实现：fake（确定性，测试/离线）| openai（gpt-image 系列，quality 默认 low 成本优先）
| ark（火山方舟豆包，同步 b64）| wanx（通义万相，异步任务轮询）。
生图 key 缺省时不注册 image_generate 工具（registry 门控）。
"""

from __future__ import annotations

from typing import Any

from cognition.providers.image.base import ImageGenProvider


def build_image_provider(settings: Any) -> ImageGenProvider:
    """按 settings 构建图像 provider（镜像 rag/factory 的集中选择模式）。"""
    provider = getattr(settings, "image_gen_provider", "") or ""
    if provider == "openai":
        from cognition.providers.image.openai import OpenAIImageProvider

        return OpenAIImageProvider(
            api_key=getattr(settings, "image_gen_api_key", "") or "",
            model=getattr(settings, "image_gen_model", "") or "gpt-image-2",
            base_url=getattr(settings, "image_gen_base_url", "") or "https://api.openai.com/v1",
            quality=getattr(settings, "image_gen_quality", "low") or "low",
        )
    if provider == "ark":
        from cognition.providers.image.ark import ArkImageProvider

        return ArkImageProvider(
            api_key=getattr(settings, "image_gen_api_key", "") or "",
            model=getattr(settings, "image_gen_model", "") or "doubao-seedream-3-0-t2i-250415",
            base_url=getattr(settings, "image_gen_base_url", "") or "https://ark.cn-beijing.volces.com/api/v3",
        )
    if provider == "wanx":
        from cognition.providers.image.wanx import WanxImageProvider

        return WanxImageProvider(
            api_key=getattr(settings, "image_gen_api_key", "") or "",
            model=getattr(settings, "image_gen_model", "") or "wanx2.1-t2i-turbo",
            base_url=getattr(settings, "image_gen_base_url", "") or "https://dashscope.aliyuncs.com",
        )
    from cognition.providers.image.fake import FakeImageProvider

    return FakeImageProvider()
