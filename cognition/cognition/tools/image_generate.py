"""image_generate 本地工具（M9）：文生图 → 多张 PNG 产物。

provider 经装配期注入（fake/ark/wanx 可切换）；图片字节的 MinIO 上传走
asyncio.to_thread（阻塞 I/O 不占 grpc.aio 事件循环——图片是 MB 级，不同于
knowledge_search 的小 md 内联上传）。产物 ArtifactRef 列表复用 Go /artifacts 代理。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolCallId, StructuredTool

from cognition.config import Settings
from cognition.providers.image.base import ImageGenProvider
from cognition.skills.runner.request import resolve_input_files
from cognition.skills.tools import _attachments_from_config
from cognition.tools.report import _maybe_upload, _run_id_from_config

_MAX_N = 4
_MAX_SOURCE_IMAGES = 4


def image_size_of(data: bytes) -> Optional[tuple[int, int]]:
    """取图片 (宽, 高)（纯函数，可测）；解码失败返回 None（调用方跳过尺寸校验）。"""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001 — 源图不可解码时不做尺寸对齐
        return None


def normalize_mask(data: bytes, target_size: Optional[tuple[int, int]]) -> tuple[Optional[bytes], str]:
    """蒙版 Pillow 轻校验（纯函数，可测）：修得动的修，修不动的说人话。

    OpenAI mask 契约：PNG 且必须带 alpha 通道，alpha=0 透明区=要重绘的区域，
    尺寸须与第一张源图逐像素一致。规则：
    - 解码失败 → (None, 错误文本)，调用方作为工具文本让模型转告用户；
    - 无任何透明像素（alpha 全 255）→ (None, 错误文本)：蒙版语义归零（"整幅保留、
      不重绘任何区域"），送 provider 只会白付一次调用还假报局部重绘成功——修不动，
      说人话（评审#8）。无 alpha 来源（JPEG/RGB PNG）补 alpha 后恒全不透明，自然
      落入此检测；LA/P+tRNS 等转 RGBA 保留透明信息，确有透明区则正常放行；
    - 已是 RGBA PNG 且尺寸匹配 → 原字节直通（不重编码）；
    - 非 PNG / 非 RGBA 但确有透明像素 → 转 RGBA 重编码；
    - 尺寸与 target_size 不一致 → NEAREST 重采样（保蒙版边界硬度），并附注明文本。
    返回 (规范化后的 PNG 字节, 附注文本——空串表示无需说明)。
    """
    import io

    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001 — 坏字节：修不动，说人话
        return None, "蒙版图片无法解码（需要 PNG 等常见图片格式），请重新导出蒙版后再试。"
    size_ok = target_size is None or img.size == tuple(target_size)
    passthrough = img.format == "PNG" and img.mode == "RGBA" and size_ok
    if img.mode != "RGBA":
        img = img.convert("RGBA")  # LA/P+tRNS 的透明信息经 convert 保留；无 alpha 来源补出全不透明
    if img.getextrema()[3][0] == 255:  # alpha 通道最小值 255 = 无任何透明（重绘）像素
        return None, (
            "蒙版没有透明（重绘）区域——请在画布上涂抹要重绘的部分"
            "（导出的蒙版需以 alpha=0 的透明像素标记重绘区）。"
        )
    if passthrough:
        return data, ""
    note = ""
    if not size_ok:
        img = img.resize(tuple(target_size), Image.NEAREST)  # type: ignore[arg-type]
        note = f"蒙版尺寸与底图不一致，已重采样到 {target_size[0]}x{target_size[1]}。"
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), note


def exif_upright(data: bytes) -> bytes:
    """把带 EXIF Orientation 的图片物理转正并重编码为 PNG（纯函数，可测）。

    评审#9：手机拍摄的 JPEG 常带 Orientation 标签——浏览器按"显示方向"渲染底图并
    以该坐标系导出蒙版，而 Pillow 的 im.size 与送 provider 的原始像素是未旋转的：
    两个坐标系相差 90°/180°，尺寸兜底重采样只会把蒙版拉伸到错误区域（180° 时尺寸
    校验直接通过、蒙版静默颠倒）。物理转正（像素落地、方向标签随重编码移除）后
    两端天然同帧。无方向标签 → 原字节原样返回（不重编码）；任何失败降级原字节。
    """
    import io

    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.getexif().get(0x0112, 1) in (None, 1):
                return data  # 无旋转语义：零变化
            upright = ImageOps.exif_transpose(im)
            buf = io.BytesIO()
            upright.save(buf, format="PNG")  # PNG 重编码：无损且不携带 EXIF 方向
            return buf.getvalue()
    except Exception:  # noqa: BLE001 — 解析失败保持原字节（后续尺寸校验/重采样兜底仍在）
        return data


def build_image_generate_tool(provider: ImageGenProvider, settings: Settings) -> BaseTool:
    """构造 image_generate 工具（闭包持有 provider/settings）。"""

    def _load_sources(source_images, config) -> tuple[list[bytes], Optional[str]]:
        """按文件名从本 run 附件白名单解析源图并下载（图生图）；返回 (bytes 列表, 错误文本)。

        白名单机制同 script_runner 的 input_files：LLM 只能引用本轮消息注记里列出的
        文件名，key 已过 Go 归属闸；未知名回工具文本让模型自纠，绝不触达任意对象。
        """
        all_names = list(source_images or [])
        names = all_names[:_MAX_SOURCE_IMAGES]
        dropped = all_names[_MAX_SOURCE_IMAGES:]  # 超限截断需回信给模型（否则静默丢图）
        warn = (
            f"源图超过 {_MAX_SOURCE_IMAGES} 张，仅取前 {_MAX_SOURCE_IMAGES} 张，忽略：{', '.join(dropped)}。"
            if dropped else ""
        )
        if not names:
            return [], None, ""
        resolved, problems = resolve_input_files(names, _attachments_from_config(config))
        if problems:
            return [], "；".join(problems), ""
        from cognition.attachments import MinioDownloader

        downloader = MinioDownloader(settings)
        out: list[bytes] = []
        for resource_key, _dest in resolved:
            out.append(downloader(resource_key))
        return out, None, warn

    def _load_mask(mask_name: str, first_source: bytes, config) -> tuple[Optional[bytes], Optional[str], str]:
        """解析并下载蒙版（同 source_images 的白名单机制）+ Pillow 轻校验。

        返回 (规范化 PNG bytes, 错误文本, 附注文本)。错误（未知名/坏字节）作为工具
        文本返回让模型自纠/转告；可修复偏差（无 alpha/尺寸不一致）就地修并附注明。
        """
        resolved, problems = resolve_input_files([mask_name], _attachments_from_config(config))
        if problems:
            return None, "；".join(problems), ""
        from cognition.attachments import MinioDownloader

        raw = MinioDownloader(settings)(resolved[0][0])
        normalized, note = normalize_mask(raw, image_size_of(first_source))
        if normalized is None:
            return None, note, ""  # note 此时为错误文本
        return normalized, None, note

    async def image_generate(
        prompt: str,
        n: int = 1,
        size: Optional[str] = None,
        source_images: Optional[list[str]] = None,
        mask: Optional[str] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
        config: RunnableConfig = None,  # type: ignore[assignment]
    ) -> tuple[str, Optional[list]]:
        """根据文字描述生成图片（1-4 张），产出可下载/预览的 PNG 产物。

        prompt 写清主体/环境/风格/构图/光影（可先看 image-style-library 技能的风格模板）。
        source_images 填**用户上传附件的图片文件名**——本轮或**本会话此前任意轮次**上传的
        都可直接引用（无需让用户重传）；传了就做图生图/编辑（把上传图当底图按 prompt 修改），
        不传就纯文生图。本会话此前已生成过的图也无需重复生成（产物区仍在，网页内联可直接
        引用 generated/ 下的历史文件名）。
        mask 填**蒙版文件名**（本轮或本会话历史附件）做局部重绘（inpaint）：蒙版为 RGBA PNG，
        **alpha=0 的透明区域=要重绘的区域**，只作用于第一张源图；必须配合 source_images
        使用。注意 gpt-image 系对 mask 为提示词引导式而非像素级硬约束。
        """
        count = max(1, min(int(n), _MAX_N))
        # 源图下载是阻塞 I/O → to_thread（事件循环红线）。
        try:
            sources, src_err, src_warn = await asyncio.to_thread(_load_sources, source_images, config)
        except Exception as exc:  # noqa: BLE001 — 下载失败降级为错误文本
            return (f"源图读取失败: {exc}", None)
        if src_err:
            return (src_err, None)
        # 蒙版：能力门控 → 引导 → 白名单解析+下载+轻校验（同样是阻塞 I/O → to_thread）。
        mask_bytes: Optional[bytes] = None
        mask_note = ""
        if mask:
            if not getattr(provider, "supports_inpaint", True):
                # 诚实声明（镜像 supports_image_to_image 的措辞先例）：不谎报、不静默。
                mask_note = "注：当前生图后端不支持局部重绘（inpaint），已忽略蒙版。"
            elif not sources:
                return (
                    "蒙版（mask）需要配合底图使用：请把底图文件名填入 source_images，"
                    "再把蒙版文件名填入 mask（蒙版透明区域=要重绘的区域）。",
                    None,
                )
            else:
                try:
                    # 评审#9：蒙版在浏览器"显示方向"坐标系绘制——先把首张源图（蒙版只
                    # 作用于第一张）按 EXIF 物理转正，尺寸比较与送 provider 都用转正后
                    # 字节。仅蒙版路径生效：无蒙版的图生图行为零变化（刻意范围控制）。
                    sources[0] = await asyncio.to_thread(exif_upright, sources[0])
                    mask_bytes, mask_err, mask_note = await asyncio.to_thread(
                        _load_mask, mask, sources[0], config
                    )
                except Exception as exc:  # noqa: BLE001 — 下载失败降级为错误文本
                    return (f"蒙版读取失败: {exc}", None)
                if mask_err:
                    return (mask_err, None)
        try:
            # mask 仅在真有蒙版时以关键字传入：加性扩展，不支持 mask 形参的旧式
            # provider（含测试替身）在纯文生图/图生图路径上零感知。
            extra = {"mask": mask_bytes} if mask_bytes is not None else {}
            images = await provider.generate(
                prompt, images=sources or None, size=size or settings.image_gen_size, n=count, **extra
            )
        except Exception as exc:  # noqa: BLE001 — provider 失败降级为错误文本，模型可复述
            return (f"图像生成失败: {exc}", None)
        if not images:
            return ("图像生成失败: provider 未返回图片", None)

        run_id = _run_id_from_config(config)
        tcid = tool_call_id or "tc"
        artifacts: list[dict] = []
        # 暂存按**会话**作用域（续轮改需求可复用此前生成图，无需重新生图）；
        # 编号跨轮续接（next_image_index），否则第二轮从 image-1 重编会覆盖第一轮暂存图。
        from cognition.skills.runner.scratch import next_image_index, stash_generated
        from cognition.skills.tools import _session_key_from_config

        session_key = _session_key_from_config(config)
        offset = await asyncio.to_thread(next_image_index, session_key)
        for i, data in enumerate(images):
            file_name = f"image-{offset + i}.png"
            resource_key = f"{run_id}/{tcid}/{file_name}"
            # 上传阻塞 I/O → to_thread（事件循环红线）；失败不阻断（_maybe_upload 内降级）。
            await asyncio.to_thread(_maybe_upload, settings, resource_key, data, "image/png")
            # B.1：同时把副本落进会话生成图暂存区，供 frontend-design 内联进网页。
            await asyncio.to_thread(stash_generated, session_key, file_name, data)
            artifacts.append(
                {
                    "resource_key": resource_key,
                    "name": file_name,
                    "file_name": file_name,
                    "mime_type": "image/png",
                    "size": len(data),
                    "download_url": f"/artifacts/{resource_key}",
                    "preview_url": f"/artifacts/{resource_key}",
                    "missing": False,
                }
            )
        # 措辞按是否传入源图 + provider 是否真吃源图：非 openai/ark（如 wanx 文生图）会
        # 忽略源图，此时不谎称"图生图"。provider 经 supports_image_to_image 声明能力；
        # 蒙版同理经 supports_inpaint（mask_bytes 非 None 即真送 provider）。
        used_source = bool(sources) and getattr(provider, "supports_image_to_image", True)
        mode = "图生图" if used_source else ("文生图（注：当前生图后端不支持图生图，已忽略上传图）" if sources else "文生图")
        if used_source and mask_bytes is not None:
            mode = "图生图·局部重绘（inpaint，蒙版为引导非像素级保证）"
        note = "".join(f" {t}" for t in (src_warn, mask_note) if t)
        return (f"已生成 {len(artifacts)} 张图片（{mode}，prompt: {prompt[:80]}）。{note}", artifacts)

    tool = StructuredTool.from_function(
        coroutine=image_generate,
        name="image_generate",
        description=(
            "文生图/图生图/局部重绘：根据文字描述生成图片；若用户上传了图片，把其文件名填入 "
            "source_images 即做图生图/编辑；若用户还附了蒙版图，把蒙版文件名填入 mask 做"
            "局部重绘（inpaint，蒙版透明区=要重绘的区域，须配合 source_images）。"
            "返回可下载/预览的 PNG 产物；先用 image-style-library 技能确定风格模板效果更好。"
        ),
        response_format="content_and_artifact",
    )
    tool.metadata = {"provider": "local"}
    return tool
