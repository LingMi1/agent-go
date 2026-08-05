"""OpenAI 图像生成（gpt-image 系列）：同步 images API，b64 响应。

对齐原项目取向（DEFAULT_IMAGE_MODEL="gpt-image-2"，OpenAI 兼容端点可换 base_url）。
quality 默认 "low"（成本优先，用户可经 COGNITION_IMAGE_GEN_QUALITY 调 medium/high）；
gpt-image 系列默认返回 b64_json，无需 response_format 参数。
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

import httpx

_VALID_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}


def build_openai_image_payload(
    model: str, prompt: str, size: str, quality: str, n: int
) -> dict[str, Any]:
    """请求体构造（纯函数，可测）。size 不合法回落 auto（gpt-image 不吃任意尺寸）。"""
    sz = size.replace("*", "x")
    if sz not in _VALID_SIZES:
        sz = "auto"
    return {
        "model": model,
        "prompt": prompt,
        "size": sz,
        "quality": quality or "low",
        "n": max(1, min(int(n), 4)),
    }


def build_openai_edit_form(model: str, prompt: str, size: str, quality: str, n: int) -> dict[str, str]:
    """/images/edits 的表单字段（纯函数，可测）。**不含 response_format（gpt-image 默认
    b64_json）、不含 input_fidelity（gpt-image-2 不支持）**；图片走 multipart files 的
    可重复字段 image[]，不在此。size 非法回落 auto。"""
    sz = size.replace("*", "x")
    if sz not in _VALID_SIZES:
        sz = "auto"
    return {
        "model": model,
        "prompt": prompt,
        "size": sz,
        "quality": quality or "low",
        "n": str(max(1, min(int(n), 4))),
    }


def sniff_image_type(data: bytes) -> tuple[str, str]:
    """按魔数嗅探图片真实类型，返回 (扩展名, content-type)（纯函数，可测）。

    /images/edits 是 multipart：字段的文件名扩展名与 content-type 必须与真实字节匹配，
    否则用户上传的 jpg/webp 照片被标成 png 可能被 OpenAI 拒（400）。默认回落 png。
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    return "png", "image/png"


def format_openai_image_error(status_code: int, body_text: str) -> str:
    """非 2xx 响应 → 模型/用户可读的错误行（纯函数，可测）。

    OpenAI 把拒绝原因放在 body 的 error.message/code（如 moderation_blocked 安全审核
    拦截）——此前只 raise_for_status 抛状态行、吞掉 body，模型看不到真实原因只能盲猜
    格式/尺寸浪费重试（线上缺陷：图生图内容触审 400，被误判为接口坏了）。
    """
    try:
        err = json.loads(body_text).get("error") or {}
        code = str(err.get("code") or "").strip()
        msg = " ".join(str(err.get("message") or "").split())[:280]
        if code or msg:
            return f"HTTP {status_code} {code}: {msg}".strip()
    except Exception:  # noqa: BLE001 — body 非 JSON 时回落原文截断
        pass
    return f"HTTP {status_code}: {body_text[:200]}"


def parse_openai_images(data: dict[str, Any]) -> list[bytes]:
    """响应 → 图片字节列表（纯函数，可测）；形状异常抛 ValueError。"""
    items = data.get("data") or []
    out = [base64.b64decode(it["b64_json"]) for it in items if isinstance(it, dict) and it.get("b64_json")]
    if not out:
        raise ValueError(f"openai 响应缺少图像数据: {list(data)}")
    return out


class OpenAIImageProvider:
    # 是否真正把源图送去生成（供 image_generate 措辞判断，不谎称图生图）。
    supports_image_to_image = True
    # 是否支持 mask 局部重绘（/images/edits 的 mask 字段；gpt-image 系为提示词引导式）。
    supports_inpaint = True
    def __init__(
        self, *, api_key: str, model: str, base_url: str, quality: str = "low", timeout: float = 180.0
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base = base_url.rstrip("/")
        self._quality = quality
        self._timeout = timeout

    async def generate(
        self,
        prompt: str,
        *,
        images: Optional[list[bytes]] = None,  # 传了→图生图走 /images/edits(multipart)
        size: str = "1024x1024",
        n: int = 1,
        mask: Optional[bytes] = None,  # inpaint 蒙版（RGBA PNG，alpha=0=重绘区，仅作用于第一张）
    ) -> list[bytes]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if images:
                # 图生图：multipart，图片用可重复字段名 image[]（OpenAI 契约），其余走表单。
                # 文件名扩展名/content-type 按真实字节嗅探（jpg/webp 照片不能标成 png）。
                files = []
                for i, buf in enumerate(images):
                    ext, ctype = sniff_image_type(buf)
                    files.append(("image[]", (f"src-{i}.{ext}", buf, ctype)))
                if mask is not None:
                    # mask 是**单字段名 mask**（非 mask[]），走 files 不进表单纯字段；
                    # 表单红线不变：绝不加 response_format/input_fidelity（gpt-image-2 400）。
                    files.append(("mask", ("mask.png", mask, "image/png")))
                resp = await client.post(
                    f"{self._base}/images/edits",
                    headers=headers,
                    data=build_openai_edit_form(self._model, prompt, size, self._quality, n),
                    files=files,
                )
            else:
                resp = await client.post(
                    f"{self._base}/images/generations",
                    headers=headers,
                    json=build_openai_image_payload(self._model, prompt, size, self._quality, n),
                )
            if resp.status_code >= 400:
                # 带响应体抛错：审核拦截/参数错误等真实原因必须进工具观察串（模型据此止损，
                # 而不是盲猜格式/尺寸反复重试）。
                raise RuntimeError("openai images " + format_openai_image_error(resp.status_code, resp.text))
            return parse_openai_images(resp.json())
