"""图像生成（M9 B7）：provider 抽象 + image_generate 工具 + 门控注册。"""

from __future__ import annotations

import asyncio

from cognition.config import Settings
from cognition.providers.image import build_image_provider
from cognition.providers.image.ark import build_ark_payload, parse_ark_response
from cognition.providers.image.fake import FakeImageProvider
from cognition.providers.image.wanx import build_wanx_payload, parse_wanx_task_status
from cognition.tools.image_generate import build_image_generate_tool

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_fake_provider_deterministic_and_distinct():
    p = FakeImageProvider()
    a1 = asyncio.run(p.generate("一只橘猫"))
    a2 = asyncio.run(p.generate("一只橘猫"))
    b = asyncio.run(p.generate("赛博朋克城市", n=2))
    assert a1 == a2  # 同 prompt 确定性
    assert a1[0].startswith(PNG_MAGIC)
    assert len(b) == 2 and b[0] != b[1]  # n 张互不相同
    assert a1[0] != b[0]  # 不同 prompt 可区分


def test_provider_factory_selection():
    assert isinstance(build_image_provider(Settings(image_gen_provider="fake")), FakeImageProvider)
    ark = build_image_provider(Settings(image_gen_provider="ark", image_gen_api_key="k"))
    wanx = build_image_provider(Settings(image_gen_provider="wanx", image_gen_api_key="k"))
    assert type(ark).__name__ == "ArkImageProvider"
    assert type(wanx).__name__ == "WanxImageProvider"


def test_payload_builders_and_parsers():
    p = build_ark_payload("m1", "猫", "1024*1024")
    assert p["size"] == "1024x1024" and p["response_format"] == "b64_json" and "image" not in p
    p2 = build_ark_payload("m1", "猫", "1024x1024", image_b64="QUJD")
    assert p2["image"].startswith("data:image/png;base64,")
    import base64

    assert parse_ark_response({"data": [{"b64_json": base64.b64encode(b"IMG").decode()}]}) == b"IMG"

    w = build_wanx_payload("m2", "狗", "1024x1024", 2)
    assert w["parameters"]["size"] == "1024*1024" and w["parameters"]["n"] == 2
    st, urls = parse_wanx_task_status(
        {"output": {"task_status": "SUCCEEDED", "results": [{"url": "http://x/1.png"}]}}
    )
    assert st == "SUCCEEDED" and urls == ["http://x/1.png"]


def test_tool_multi_image_artifacts(monkeypatch, tmp_path):
    # 暂存基目录隔离：编号现按会话作用域跨轮续接（next_image_index 扫描既有文件），
    # 共享磁盘残留会让 image-N 起始编号漂移。
    from cognition.skills.runner import scratch

    monkeypatch.setattr(scratch, "_BASE", str(tmp_path))
    tool = build_image_generate_tool(FakeImageProvider(), Settings())

    async def run():
        return await tool.ainvoke(
            {"args": {"prompt": "水墨山水", "n": 2}, "id": "img1",
             "name": "image_generate", "type": "tool_call"},
            config={"metadata": {"request_id": "run9"}},
        )

    msg = asyncio.run(run())
    assert "已生成 2 张图片" in msg.content
    assert isinstance(msg.artifact, list) and len(msg.artifact) == 2
    a = msg.artifact[0]
    assert a["mime_type"] == "image/png" and a["download_url"] == "/artifacts/run9/img1/image-1.png"
    assert {x["file_name"] for x in msg.artifact} == {"image-1.png", "image-2.png"}


def test_tool_provider_failure_degrades_to_text():
    class Boom:
        async def generate(self, prompt, *, images=None, size="1024x1024", n=1):
            raise RuntimeError("quota exceeded")

    tool = build_image_generate_tool(Boom(), Settings())

    async def run():
        return await tool.ainvoke(
            {"args": {"prompt": "x"}, "id": "img2", "name": "image_generate", "type": "tool_call"},
            config={"metadata": {"request_id": "r"}},
        )

    msg = asyncio.run(run())
    assert "图像生成失败" in msg.content and msg.artifact is None


def test_registry_gating():
    from cognition.tools.registry import build_tool_suite

    async def names(settings):
        tools, provider_map, closers = await build_tool_suite(settings)
        for c in closers:
            await c()
        return {t.name for t in tools}, provider_map

    off, _ = asyncio.run(names(Settings(image_gen_provider="", mcp_enabled=False, skills_enabled=False)))
    on, pm = asyncio.run(names(Settings(image_gen_provider="fake", mcp_enabled=False, skills_enabled=False)))
    assert "image_generate" not in off
    assert "image_generate" in on and pm["image_generate"] == "local"


def test_openai_payload_and_parse():
    """M12：OpenAI gpt-image provider——quality 默认 low、尺寸回落 auto、b64 解析。"""
    from cognition.providers.image.openai import build_openai_image_payload, parse_openai_images

    p = build_openai_image_payload("gpt-image-2", "猫", "1024x1024", "low", 2)
    assert p["quality"] == "low" and p["n"] == 2 and p["size"] == "1024x1024"
    assert build_openai_image_payload("m", "x", "512x512", "", 9)["size"] == "auto"  # 非法尺寸回落
    assert build_openai_image_payload("m", "x", "512x512", "", 9)["n"] == 4  # n 夹取
    import base64 as b64

    assert parse_openai_images({"data": [{"b64_json": b64.b64encode(b"IMG").decode()}]}) == [b"IMG"]


def test_openai_factory_selection():
    p = build_image_provider(Settings(image_gen_provider="openai", image_gen_api_key="k"))
    assert type(p).__name__ == "OpenAIImageProvider"


def test_openai_edit_form_no_forbidden_fields():
    """Y2：/images/edits 表单——不含 response_format/input_fidelity（gpt-image-2 会 400）；
    尺寸回落 auto；n 夹取。图片走 multipart files 不在此。"""
    from cognition.providers.image.openai import build_openai_edit_form

    f = build_openai_edit_form("gpt-image-2", "把猫变成橘色", "1024x1024", "low", 9)
    assert f["prompt"] == "把猫变成橘色" and f["quality"] == "low"
    assert f["n"] == "4" and f["size"] == "1024x1024"
    assert "response_format" not in f and "input_fidelity" not in f
    assert build_openai_edit_form("m", "x", "512x512", "", 1)["size"] == "auto"


def test_image_generate_source_images_whitelist_and_mode():
    """Y2：source_images 按本 run 附件白名单解析；未知名回工具文本自纠；传了走图生图。"""
    import asyncio as _a

    class SpyProvider:
        def __init__(self):
            self.got_images = None

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1):
            self.got_images = images
            return [b"\x89PNG\r\n\x1a\n"]  # 单张假 PNG

    prov = SpyProvider()
    tool = build_image_generate_tool(prov, Settings())
    meta = {"request_id": "run-e",
            "attachments": '[{"resource_key":"uploads/u/s/aa-cat.png","file_name":"cat.png"}]'}

    # 未知文件名 → 工具文本自纠，不调 provider。
    msg_bad = _a.run(tool.ainvoke(
        {"args": {"prompt": "改色", "source_images": ["ghost.png"]}, "id": "i1",
         "name": "image_generate", "type": "tool_call"},
        config={"metadata": meta}))
    assert "不存在" in msg_bad.content and prov.got_images is None

    # 白名单命中 → 走图生图（provider 收到 bytes；下载器需 mock）。
    from cognition import attachments as _att

    orig = _att.MinioDownloader
    _att.MinioDownloader = lambda settings: (lambda key: b"SRCBYTES")  # noqa: E731
    try:
        msg_ok = _a.run(tool.ainvoke(
            {"args": {"prompt": "把猫变橘色", "source_images": ["cat.png"]}, "id": "i2",
             "name": "image_generate", "type": "tool_call"},
            config={"metadata": meta}))
    finally:
        _att.MinioDownloader = orig
    assert "图生图" in msg_ok.content
    assert prov.got_images == [b"SRCBYTES"]


def test_image_generate_text_to_image_no_sources():
    """无 source_images → 文生图（provider images=None）。"""
    import asyncio as _a

    class Spy:
        def __init__(self):
            self.images = "unset"

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1):
            self.images = images
            return [b"\x89PNG\r\n\x1a\n"]

    s = Spy()
    tool = build_image_generate_tool(s, Settings())
    msg = _a.run(tool.ainvoke(
        {"args": {"prompt": "水墨山水"}, "id": "i3", "name": "image_generate", "type": "tool_call"},
        config={"metadata": {"request_id": "r"}}))
    assert "文生图" in msg.content and s.images is None


def test_sniff_image_type_matrix():
    """评审#1/#5：按魔数嗅探真实类型（jpg/webp/gif 照片不标成 png）。"""
    from cognition.providers.image.openai import sniff_image_type

    assert sniff_image_type(b"\x89PNG\r\n\x1a\nxxxx") == ("png", "image/png")
    assert sniff_image_type(b"\xff\xd8\xff\xe0abc") == ("jpg", "image/jpeg")
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPzz") == ("webp", "image/webp")
    assert sniff_image_type(b"GIF89a....") == ("gif", "image/gif")
    assert sniff_image_type(b"garbage") == ("png", "image/png")  # 回落


def test_image_generate_wanx_no_false_i2i_claim():
    """评审#2：忽略源图的 provider（supports_image_to_image=False）不谎称图生图。"""
    import asyncio as _a

    class IgnoreProvider:
        supports_image_to_image = False

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1):
            return [b"\x89PNG\r\n\x1a\n"]  # 无视 images

    tool = build_image_generate_tool(IgnoreProvider(), Settings())
    meta = {"request_id": "r", "attachments": '[{"resource_key":"uploads/u/s/a-cat.png","file_name":"cat.png"}]'}
    from cognition import attachments as _att

    orig = _att.MinioDownloader
    _att.MinioDownloader = lambda s: (lambda k: b"SRC")  # noqa: E731
    try:
        msg = _a.run(tool.ainvoke(
            {"args": {"prompt": "改色", "source_images": ["cat.png"]}, "id": "i",
             "name": "image_generate", "type": "tool_call"},
            config={"metadata": meta}))
    finally:
        _att.MinioDownloader = orig
    assert "不支持图生图" in msg.content  # 不谎称图生图，明示已忽略上传图


# —— inpaint（docs/12）：画布蒙版 → /images/edits mask —— #


def _pil_png(mode="RGBA", size=(8, 8), color=None) -> bytes:
    """用 Pillow 造测试图（RGBA/RGB/任意尺寸）。"""
    import io

    from PIL import Image

    img = Image.new(mode, size, color or ((255, 0, 0, 255) if mode == "RGBA" else (255, 0, 0)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mask_png(size=(8, 8)) -> bytes:
    """带透明重绘区的合规蒙版（RGBA PNG：整幅不透明 + 左上角 alpha=0 的洞）。

    D1（评审#8）起蒙版必须含透明像素才有效——全不透明=无重绘区域会被拒收。
    """
    import io

    from PIL import Image

    img = Image.new("RGBA", size, (0, 0, 0, 255))
    img.putpixel((0, 0), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _exif_jpeg(size=(8, 6), orientation=6) -> bytes:
    """带 EXIF Orientation 的 JPEG（模拟手机竖拍：原始像素横放、靠方向标签旋转显示）。"""
    import io

    from PIL import Image

    img = Image.new("RGB", size, (200, 50, 50))
    exif = img.getexif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


_MASK_META = {
    "request_id": "run-m",
    "attachments": (
        '[{"resource_key":"uploads/u/s/aa-cat.png","file_name":"cat.png"},'
        '{"resource_key":"uploads/u/s/bb-mask-1.png","file_name":"mask-1.png"}]'
    ),
}


def _patched_downloader(files: dict):
    """MinioDownloader 猴子补丁上下文（按 resource_key 分发字节，既有手法）。"""
    import contextlib

    from cognition import attachments as _att

    @contextlib.contextmanager
    def ctx():
        orig = _att.MinioDownloader
        _att.MinioDownloader = lambda s: (lambda k: files[k])  # noqa: E731
        try:
            yield
        finally:
            _att.MinioDownloader = orig

    return ctx()


def test_image_generate_mask_passed_to_provider():
    """§6①：Spy provider 收到 mask bytes（合规 RGBA PNG 直通，不重编码）。"""
    import asyncio as _a

    class SpyProvider:
        supports_image_to_image = True
        supports_inpaint = True

        def __init__(self):
            self.got_images = None
            self.got_mask = "unset"

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1, mask=None):
            self.got_images = images
            self.got_mask = mask
            return [b"\x89PNG\r\n\x1a\n"]

    src = _pil_png("RGB", (8, 8))
    mask = _mask_png((8, 8))
    prov = SpyProvider()
    tool = build_image_generate_tool(prov, Settings())
    with _patched_downloader({"uploads/u/s/aa-cat.png": src, "uploads/u/s/bb-mask-1.png": mask}):
        msg = _a.run(tool.ainvoke(
            {"args": {"prompt": "把猫改成橘色", "source_images": ["cat.png"], "mask": "mask-1.png"},
             "id": "m1", "name": "image_generate", "type": "tool_call"},
            config={"metadata": _MASK_META}))
    assert prov.got_images == [src]
    assert prov.got_mask == mask  # 合规蒙版原字节直通
    assert "局部重绘" in msg.content and "inpaint" in msg.content


def test_openai_edits_multipart_shape_with_mask():
    """§6②：/images/edits multipart——image[] 可重复 + 单 mask 字段走 files；
    表单无 response_format/input_fidelity 且不含 mask；无蒙版时 files 无 mask 字段。"""
    import asyncio as _a
    import base64 as b64

    from cognition.providers.image import openai as openai_mod

    calls: list[dict] = []

    class _FakeResp:
        status_code = 200  # provider 现按 status_code 判错（错误体透传修复）

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"b64_json": b64.b64encode(b"IMG").decode()}]}

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *, headers=None, data=None, files=None, json=None):
            calls.append({"url": url, "data": data, "files": files, "json": json})
            return _FakeResp()

    prov = openai_mod.OpenAIImageProvider(api_key="k", model="gpt-image-2", base_url="https://x/v1")
    orig = openai_mod.httpx.AsyncClient
    openai_mod.httpx.AsyncClient = _FakeClient
    try:
        out = _a.run(prov.generate("改", images=[b"\x89PNG\r\n\x1a\nA", b"\xff\xd8\xffB"], mask=b"MASKBYTES"))
        _a.run(prov.generate("改", images=[b"\x89PNG\r\n\x1a\nA"]))  # 无蒙版对照
    finally:
        openai_mod.httpx.AsyncClient = orig
    assert out == [b"IMG"]
    with_mask, without_mask = calls
    assert with_mask["url"].endswith("/images/edits")
    names = [f[0] for f in with_mask["files"]]
    assert names.count("image[]") == 2 and names.count("mask") == 1  # 单字段名 mask（非 mask[]）
    mask_field = [f for f in with_mask["files"] if f[0] == "mask"][0]
    assert mask_field[1] == ("mask.png", b"MASKBYTES", "image/png")
    for form in (with_mask["data"], without_mask["data"]):  # 表单红线：mask 不进表单、禁字段恒无
        assert "mask" not in form and "response_format" not in form and "input_fidelity" not in form
    assert all(f[0] != "mask" for f in without_mask["files"])


def test_image_generate_mask_whitelist_unknown_name():
    """§6③：未知蒙版文件名 → 白名单 problems 文本自纠，不调 provider。"""
    import asyncio as _a

    class Spy:
        supports_inpaint = True

        def __init__(self):
            self.called = False

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1, mask=None):
            self.called = True
            return [b"\x89PNG\r\n\x1a\n"]

    s = Spy()
    tool = build_image_generate_tool(s, Settings())
    src = _pil_png("RGB", (8, 8))
    with _patched_downloader({"uploads/u/s/aa-cat.png": src}):
        msg = _a.run(tool.ainvoke(
            {"args": {"prompt": "改", "source_images": ["cat.png"], "mask": "ghost-mask.png"},
             "id": "m2", "name": "image_generate", "type": "tool_call"},
            config={"metadata": _MASK_META}))
    assert "不存在" in msg.content and s.called is False


def test_image_generate_mask_without_sources_guides():
    """§6④：mask 有而 source_images 空 → 引导文本（提示补 source_images），不调 provider。"""
    import asyncio as _a

    class Spy:
        supports_inpaint = True

        def __init__(self):
            self.called = False

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1, mask=None):
            self.called = True
            return [b"\x89PNG\r\n\x1a\n"]

    s = Spy()
    tool = build_image_generate_tool(s, Settings())
    msg = _a.run(tool.ainvoke(
        {"args": {"prompt": "改", "mask": "mask-1.png"}, "id": "m3",
         "name": "image_generate", "type": "tool_call"},
        config={"metadata": _MASK_META}))
    assert "source_images" in msg.content and "底图" in msg.content and s.called is False


def test_image_generate_unsupported_provider_honest_about_mask():
    """§6⑤：supports_inpaint=False 的 provider 收到 mask → 如实措辞"已忽略蒙版"，
    不谎称局部重绘；provider 不收 mask 参数（旧签名零感知）。"""
    import asyncio as _a

    class NoInpaint:  # 形如 ark：吃源图、无 mask 概念（签名无 mask，证明工具层未传）
        supports_image_to_image = True
        supports_inpaint = False

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1):
            return [b"\x89PNG\r\n\x1a\n"]

    tool = build_image_generate_tool(NoInpaint(), Settings())
    src = _pil_png("RGB", (8, 8))
    with _patched_downloader({"uploads/u/s/aa-cat.png": src}):
        msg = _a.run(tool.ainvoke(
            {"args": {"prompt": "改", "source_images": ["cat.png"], "mask": "mask-1.png"},
             "id": "m4", "name": "image_generate", "type": "tool_call"},
            config={"metadata": _MASK_META}))
    assert "不支持局部重绘" in msg.content and "已忽略蒙版" in msg.content
    assert "图生图" in msg.content and "图生图·局部重绘" not in msg.content  # mode 不谎称 inpaint


def test_normalize_mask_three_states():
    """§6⑥：Pillow 轻校验——无透明区拒收（D1，评审#8）、尺寸偏差 NEAREST 重采样、坏字节报错。"""
    import io

    from PIL import Image

    from cognition.tools.image_generate import image_size_of, normalize_mask

    # ① 无 alpha（RGB PNG）→ 补出的 alpha 全 255 = 无重绘区域：拒收 + 说人话
    #（旧语义"补 alpha 直通"会静默吞掉蒙版意图、白付一次 API 调用还假报局部重绘成功）。
    out, note = normalize_mask(_pil_png("RGB", (8, 8)), (8, 8))
    assert out is None and "没有透明" in note and "涂抹" in note
    # ①b 有 alpha 但全不透明（RGBA 全 255）→ 同样拒收（含直通形状也不放行）。
    out1b, note1b = normalize_mask(_pil_png("RGBA", (8, 8)), (8, 8))
    assert out1b is None and "没有透明" in note1b
    # ② 尺寸偏差（含透明洞）→ NEAREST 重采样到目标尺寸 + 附注明文本（透明洞保留）。
    out2, note2 = normalize_mask(_mask_png((4, 4)), (8, 8))
    img2 = Image.open(io.BytesIO(out2))
    assert img2.size == (8, 8) and "重采样" in note2
    assert img2.getextrema()[3][0] == 0  # NEAREST 保住 alpha=0 重绘区
    # ③ 坏字节 → (None, 人话错误)。
    out3, err = normalize_mask(b"garbage-not-an-image", (8, 8))
    assert out3 is None and "无法解码" in err
    # 合规 RGBA PNG（含透明区）且尺寸匹配 → 原字节直通（不重编码）。
    good = _mask_png((8, 8))
    assert normalize_mask(good, (8, 8)) == (good, "")
    # image_size_of：可解码取尺寸，不可解码回 None（跳过尺寸对齐）。
    assert image_size_of(good) == (8, 8) and image_size_of(b"junk") is None


def test_image_generate_mask_repair_paths_via_tool():
    """§6⑥（工具层）：尺寸偏差 → 重采样附注进返回文本；坏字节 → problems 文本不调 provider。"""
    import asyncio as _a
    import io

    from PIL import Image

    class Spy:
        supports_image_to_image = True
        supports_inpaint = True

        def __init__(self):
            self.got_mask = None

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1, mask=None):
            self.got_mask = mask
            return [b"\x89PNG\r\n\x1a\n"]

    src = _pil_png("RGB", (16, 16))
    small_mask = _mask_png((8, 8))
    s = Spy()
    tool = build_image_generate_tool(s, Settings())
    with _patched_downloader({"uploads/u/s/aa-cat.png": src, "uploads/u/s/bb-mask-1.png": small_mask}):
        msg = _a.run(tool.ainvoke(
            {"args": {"prompt": "改", "source_images": ["cat.png"], "mask": "mask-1.png"},
             "id": "m5", "name": "image_generate", "type": "tool_call"},
            config={"metadata": _MASK_META}))
    assert "重采样" in msg.content and "16x16" in msg.content
    assert Image.open(io.BytesIO(s.got_mask)).size == (16, 16)  # provider 收到的是修复后的蒙版

    s2 = Spy()
    tool2 = build_image_generate_tool(s2, Settings())
    with _patched_downloader({"uploads/u/s/aa-cat.png": src, "uploads/u/s/bb-mask-1.png": b"bad-bytes"}):
        msg2 = _a.run(tool2.ainvoke(
            {"args": {"prompt": "改", "source_images": ["cat.png"], "mask": "mask-1.png"},
             "id": "m6", "name": "image_generate", "type": "tool_call"},
            config={"metadata": _MASK_META}))
    assert "无法解码" in msg2.content and s2.got_mask is None


def test_inpaint_capability_declarations_and_fake_trace():
    """能力声明矩阵（openai/fake=True，ark/wanx=False）+ fake 收 mask 留可断言痕迹。"""
    import asyncio as _a

    from cognition.providers.image.ark import ArkImageProvider
    from cognition.providers.image.openai import OpenAIImageProvider
    from cognition.providers.image.wanx import WanxImageProvider

    assert OpenAIImageProvider.supports_inpaint is True
    assert FakeImageProvider.supports_inpaint is True
    assert ArkImageProvider.supports_inpaint is False
    assert WanxImageProvider.supports_inpaint is False

    p = FakeImageProvider()
    plain = _a.run(p.generate("猫"))
    masked = _a.run(p.generate("猫", mask=b"M"))
    assert plain != masked and masked == _a.run(p.generate("猫", mask=b"M"))  # 有痕迹且确定性


# —— D1（评审#8）：全不透明蒙版拒收，不假报局部重绘 —— #


def test_image_generate_opaque_mask_rejected_no_provider_call():
    """无任何透明像素的蒙版（无 alpha 补 alpha 的形态 / RGBA 全 255）→ 不送 provider
    （省一次 API 调用），problems 文本引导用户涂抹重绘区，绝不宣称"局部重绘"成功。"""
    import asyncio as _a

    class Spy:
        supports_image_to_image = True
        supports_inpaint = True

        def __init__(self):
            self.called = False

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1, mask=None):
            self.called = True
            return [b"\x89PNG\r\n\x1a\n"]

    src = _pil_png("RGB", (8, 8))
    for opaque_mask in (_pil_png("RGB", (8, 8)), _pil_png("RGBA", (8, 8))):
        s = Spy()
        tool = build_image_generate_tool(s, Settings())
        with _patched_downloader({"uploads/u/s/aa-cat.png": src, "uploads/u/s/bb-mask-1.png": opaque_mask}):
            msg = _a.run(tool.ainvoke(
                {"args": {"prompt": "改", "source_images": ["cat.png"], "mask": "mask-1.png"},
                 "id": "m7", "name": "image_generate", "type": "tool_call"},
                config={"metadata": _MASK_META}))
        assert "没有透明" in msg.content and "涂抹" in msg.content
        assert s.called is False and "局部重绘" not in msg.content and msg.artifact is None


# —— D2（评审#9）：EXIF 旋转底图的蒙版坐标系对齐 —— #


def test_exif_upright_pure():
    """exif_upright：Orientation=6 物理转正（宽高互换、方向标签随重编码移除）；
    无方向语义/坏字节原样返回（零变化）。"""
    import io

    from PIL import Image

    from cognition.tools.image_generate import exif_upright, image_size_of

    up = exif_upright(_exif_jpeg((8, 6), orientation=6))
    assert image_size_of(up) == (6, 8)  # 转正后 = 浏览器显示方向
    with Image.open(io.BytesIO(up)) as im:
        assert im.getexif().get(0x0112, 1) in (None, 1)  # 方向标签已移除，不会被二次旋转
    plain = _pil_png("RGB", (8, 6))
    assert exif_upright(plain) == plain  # 无 EXIF 方向 → 原字节直通（不重编码）
    assert exif_upright(b"garbage") == b"garbage"  # 坏字节降级原样返回


def test_image_generate_exif_rotated_source_mask_aligned():
    """Orientation=6 JPEG 底图 + 显示方向蒙版 → provider 收到物理转正后的底图
    （尺寸=显示方向），蒙版尺寸匹配免重采样（无附注）——两端坐标系同帧。"""
    import asyncio as _a

    from cognition.tools.image_generate import image_size_of

    class Spy:
        supports_image_to_image = True
        supports_inpaint = True

        def __init__(self):
            self.got_images = None
            self.got_mask = None

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1, mask=None):
            self.got_images = images
            self.got_mask = mask
            return [b"\x89PNG\r\n\x1a\n"]

    rotated_src = _exif_jpeg((8, 6), orientation=6)  # 原始像素 8x6，显示方向 6x8
    display_mask = _mask_png((6, 8))  # 浏览器按显示方向导出的蒙版
    s = Spy()
    tool = build_image_generate_tool(s, Settings())
    with _patched_downloader({"uploads/u/s/aa-cat.png": rotated_src,
                              "uploads/u/s/bb-mask-1.png": display_mask}):
        msg = _a.run(tool.ainvoke(
            {"args": {"prompt": "改", "source_images": ["cat.png"], "mask": "mask-1.png"},
             "id": "m8", "name": "image_generate", "type": "tool_call"},
            config={"metadata": _MASK_META}))
    assert image_size_of(s.got_images[0]) == (6, 8)  # 底图已转正，与蒙版同坐标系
    assert s.got_mask == display_mask  # 尺寸匹配 → 蒙版原字节直通，免重采样
    assert "重采样" not in msg.content and "局部重绘" in msg.content


def test_image_generate_no_mask_exif_source_untouched():
    """范围控制（刻意登记）：无蒙版的图生图不做 EXIF 转正——原字节原样送 provider，
    既有行为零变化。"""
    import asyncio as _a

    class Spy:
        supports_image_to_image = True

        def __init__(self):
            self.got_images = None

        async def generate(self, prompt, *, images=None, size="1024x1024", n=1):
            self.got_images = images
            return [b"\x89PNG\r\n\x1a\n"]

    rotated_src = _exif_jpeg((8, 6), orientation=6)
    s = Spy()
    tool = build_image_generate_tool(s, Settings())
    with _patched_downloader({"uploads/u/s/aa-cat.png": rotated_src}):
        _a.run(tool.ainvoke(
            {"args": {"prompt": "改", "source_images": ["cat.png"]}, "id": "m9",
             "name": "image_generate", "type": "tool_call"},
            config={"metadata": _MASK_META}))
    assert s.got_images == [rotated_src]  # EXIF 原样保留：无蒙版路径零感知


def test_format_openai_image_error_surfaces_moderation_body():
    """缺陷回归：/images/edits 400 只抛状态行、吞响应体 → 模型盲猜格式/尺寸浪费重试。
    修复后 error.code/message（如安全审核拦截）必须进错误串。"""
    from cognition.providers.image.openai import format_openai_image_error

    body = (
        '{"error": {"message": "Your request was rejected by the safety system. '
        'safety_violations=[sexual].", "type": "image_generation_user_error", '
        '"code": "moderation_blocked"}}'
    )
    s = format_openai_image_error(400, body)
    assert "moderation_blocked" in s and "safety system" in s and "HTTP 400" in s

    assert format_openai_image_error(502, "<html>bad gateway</html>").startswith("HTTP 502: <html>")
    assert "HTTP 400" in format_openai_image_error(400, '{"error": {}}')  # 空 error 回落原文


def test_attachments_whitelist_merges_session_history():
    """缺陷回归：续轮改需求（如「改成滑块对比」）时，白名单只含本轮附件——历史上传
    引用不了，模型只能让用户重传+重新生图。修复：本轮 ∪ 会话历史（resource_key 去重）。"""
    from cognition.skills.tools import _attachments_from_config

    # 历史名单是 Go 原样透传的 runs.attachments —— 真实形状是 camelCase
    # （上传接口响应），解析层必须归一成 snake_case，否则 resolve_input_files
    # 取不出名字（实测缺陷：可用附件显示（无））。
    cfg = {"metadata": {
        "attachments": '[{"resource_key":"uploads/u/s/aa-new.png","file_name":"new.png"}]',
        "session_attachments": '[{"resourceKey":"uploads/u/s/bb-old.jpeg","fileName":"picture.jpeg"},'
                               '{"resourceKey":"uploads/u/s/aa-new.png","fileName":"new.png"}]',
    }}
    merged = _attachments_from_config(cfg)
    assert [a["file_name"] for a in merged] == ["new.png", "picture.jpeg"]  # 本轮优先 + 去重

    only_hist = _attachments_from_config({"metadata": {
        "session_attachments": '[{"resourceKey":"uploads/u/s/bb-old.jpeg","fileName":"picture.jpeg"}]'}})
    assert [a["file_name"] for a in only_hist] == ["picture.jpeg"]  # 本轮无附件也能引用历史

    assert _attachments_from_config({"metadata": {"session_attachments": "{broken"}}) == []


def test_next_image_index_continues_numbering(monkeypatch, tmp_path):
    """会话作用域暂存的编号续接：第二轮生图从 image-(N+1) 起，不覆盖第一轮暂存图。"""
    from cognition.skills.runner import scratch

    monkeypatch.setattr(scratch, "_BASE", str(tmp_path))
    assert scratch.next_image_index("sess-1") == 1
    scratch.stash_generated("sess-1", "image-1.png", b"A")
    scratch.stash_generated("sess-1", "image-2.png", b"B")
    assert scratch.next_image_index("sess-1") == 3
    assert scratch.next_image_index("sess-other") == 1  # 会话间隔离
