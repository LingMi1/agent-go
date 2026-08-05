"""火山方舟（豆包 Seedream/Seededit）图像生成：同步 images API，b64 响应。

API 形状（OpenAI images 风格）：POST {base}/images/generations
  {model, prompt, size, response_format: "b64_json", [image]}
逐张请求（部分模型不支持 n>1），失败即抛给工具层降级为错误文本。
"""

from __future__ import annotations

import base64
from typing import Any, Optional

import httpx


def build_ark_payload(
    model: str, prompt: str, size: str, image_b64: Optional[str] = None
) -> dict[str, Any]:
    """请求体构造（纯函数，可测）。image_b64 供图生图/风格化（seededit 系）。"""
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size.replace("*", "x"),
        "response_format": "b64_json",
    }
    if image_b64:
        payload["image"] = f"data:image/png;base64,{image_b64}"
    return payload


def parse_ark_response(data: dict[str, Any]) -> bytes:
    """取第一张 b64 图（纯函数，可测）；形状异常抛 ValueError。"""
    items = data.get("data") or []
    if not items or "b64_json" not in items[0]:
        raise ValueError(f"ark 响应缺少图像数据: {list(data)}")
    return base64.b64decode(items[0]["b64_json"])


class ArkImageProvider:
    # 是否真正把源图送去生成（供 image_generate 措辞判断，不谎称图生图）。
    supports_image_to_image = True
    # seededit 无 mask 概念：不支持局部重绘，收到 mask 忽略（工具层如实措辞）。
    supports_inpaint = False
    def __init__(self, *, api_key: str, model: str, base_url: str, timeout: float = 120.0) -> None:
        self._api_key = api_key
        self._model = model
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    async def generate(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,
        size: str = "1024x1024",
        n: int = 1,
        mask: Optional[bytes] = None,  # 不支持 inpaint，忽略（supports_inpaint=False）
    ) -> list[bytes]:
        image_b64 = base64.b64encode(images[0]).decode() if images else None
        out: list[bytes] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for _ in range(max(1, int(n))):
                resp = await client.post(
                    f"{self._base}/images/generations",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=build_ark_payload(self._model, prompt, size, image_b64),
                )
                resp.raise_for_status()
                out.append(parse_ark_response(resp.json()))
        return out
