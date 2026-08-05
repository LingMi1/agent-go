"""通义万相（DashScope）图像生成：异步任务 submit + 轮询（有界于超时预算）。

API 形状：POST {base}/api/v1/services/aigc/text2image/image-synthesis
  headers: X-DashScope-Async: enable
  {model, input:{prompt}, parameters:{size:"1024*1024", n}}
→ output.task_id → GET {base}/api/v1/tasks/{id} 至 SUCCEEDED → results[].url 下载。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

_POLL_INTERVAL_S = 2.0


def build_wanx_payload(model: str, prompt: str, size: str, n: int) -> dict[str, Any]:
    """请求体构造（纯函数，可测）。DashScope 的 size 用 * 分隔。"""
    return {
        "model": model,
        "input": {"prompt": prompt},
        "parameters": {"size": size.replace("x", "*"), "n": max(1, int(n))},
    }


def parse_wanx_task_status(data: dict[str, Any]) -> tuple[str, list[str]]:
    """从轮询响应取 (status, image_urls)（纯函数，可测）。"""
    output = data.get("output") or {}
    status = str(output.get("task_status", ""))
    urls = [r["url"] for r in (output.get("results") or []) if isinstance(r, dict) and r.get("url")]
    return status, urls


class WanxImageProvider:
    # 是否真正把源图送去生成（供 image_generate 措辞判断，不谎称图生图）。
    supports_image_to_image = False
    # v1 文生图无 mask 概念：不支持局部重绘，收到 mask 忽略（工具层如实措辞）。
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
        images: Optional[list[bytes]] = None,  # v1 文生图不支持源图，忽略
        size: str = "1024x1024",
        n: int = 1,
        mask: Optional[bytes] = None,  # 不支持 inpaint，忽略（supports_inpaint=False）
    ) -> list[bytes]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._base}/api/v1/services/aigc/text2image/image-synthesis",
                headers={**headers, "X-DashScope-Async": "enable"},
                json=build_wanx_payload(self._model, prompt, size, n),
            )
            resp.raise_for_status()
            task_id = (resp.json().get("output") or {}).get("task_id")
            if not task_id:
                raise ValueError(f"wanx 提交未返回 task_id: {resp.json()}")

            # 有界轮询：总预算 self._timeout，超出即抛（工具层转错误文本）。
            deadline = asyncio.get_event_loop().time() + self._timeout
            while True:
                poll = await client.get(f"{self._base}/api/v1/tasks/{task_id}", headers=headers)
                poll.raise_for_status()
                status, urls = parse_wanx_task_status(poll.json())
                if status == "SUCCEEDED":
                    out = []
                    for u in urls:
                        img = await client.get(u)
                        img.raise_for_status()
                        out.append(img.content)
                    return out
                if status in ("FAILED", "CANCELED"):
                    raise RuntimeError(f"wanx 任务失败: {status}")
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError(f"wanx 任务超时（>{self._timeout}s）")
                await asyncio.sleep(_POLL_INTERVAL_S)
