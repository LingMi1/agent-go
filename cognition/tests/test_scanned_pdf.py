"""C 批（docs/13）：扫描版 PDF 逐页 OCR——离线测试（设计文档 §5 测试清单逐条落）。

全程不触网：扫描 PDF 用 Pillow `img.save(format="PDF")` 造（图片型 PDF 的最真实制法），
文本页用 pypdf generic 对象手工写内容流，混合文档用 PdfWriter 拼接；OCR 转写一律注入
fake 闭包，栅格化按需 monkeypatch（真依赖冒烟单列一条锁 pypdfium2 wheel 可用性）。
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

import cognition.attachments as att_mod
from cognition.attachments import build_ingestor, extract_text, extract_text_ex
from cognition.config import Settings

# 超过 SCAN_TEXT_THRESHOLD(30) 的纯 ASCII 文本（PDF 字符串字面量，避免括号/反斜杠转义）。
LONG_TEXT = "This is a plain text page with enough characters to exceed the scan threshold."
SHORT_TEXT = "stub header"  # 低于阈值：模拟扫描件被 pypdf 提出的页眉/页码碎渣
# fake 转写结果：必须 >= MIN_OCR_TEXT_CHARS(10)，否则会被采用闸判为碎渣不采用（评审#6）。
OCR_TEXT = "OCR转写文本内容足够长"


def _scan_pdf(pages: int = 1) -> bytes:
    """Pillow 生成的图片型 PDF：每页只有位图、无文本层（extract_text 提出空串）。"""
    imgs = [Image.new("RGB", (200, 300), "white") for _ in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def _text_pdf(text: str) -> bytes:
    """pypdf 手工造单页文本 PDF（Helvetica 单行内容流，extract_text 可还原 text）。"""
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})}
    )
    stream = StreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _concat_pdf(*parts: bytes) -> bytes:
    """PdfWriter 拼接多份 PDF → 混合文档（文本页 + 扫描页）。"""
    w = PdfWriter()
    for part in parts:
        for p in PdfReader(io.BytesIO(part)).pages:
            w.add_page(p)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _fake_raster(monkeypatch, payload: bytes = b"\x89PNG-fake") -> None:
    monkeypatch.setattr(att_mod, "_rasterize_pdf_page", lambda d, i, **kw: payload)


# —— ① 页标记 / mime 传递 ——

def test_scanned_pdf_pages_get_ocr_markers(monkeypatch):
    """整份扫描 PDF：每个扫描页替换为〔第N页·OCR〕标记 + 转写文本，页序保持。"""
    _fake_raster(monkeypatch)
    mimes: list[str] = []

    def transcribe(data: bytes, mime: str) -> str:
        mimes.append(mime)
        return OCR_TEXT

    text, ocr_used = extract_text_ex(_scan_pdf(pages=2), "application/pdf", "scan.pdf", transcribe=transcribe)
    assert text == f"〔第1页·OCR〕\n{OCR_TEXT}\n〔第2页·OCR〕\n{OCR_TEXT}"
    assert ocr_used is True
    assert mimes == ["image/png", "image/png"]  # 假 PNG 魔数 → mime 按魔数嗅探


def test_jpeg_fallback_bytes_pass_jpeg_mime(monkeypatch):
    """栅格化降级为 JPEG（\\xff\\xd8 魔数）时 transcribe 收到 image/jpeg。"""
    _fake_raster(monkeypatch, payload=b"\xff\xd8\xff\xe0-fake-jpeg")
    mimes: list[str] = []
    text, _ = extract_text_ex(
        _scan_pdf(), "application/pdf", "scan.pdf",
        transcribe=lambda d, m: mimes.append(m) or OCR_TEXT,
    )
    assert text == f"〔第1页·OCR〕\n{OCR_TEXT}"
    assert mimes == ["image/jpeg"]


# —— ② 混合文档：页序 + 只为扫描页付 OCR 成本 ——

def test_mixed_pdf_ocr_only_scan_pages_in_order(monkeypatch):
    """文本页原样保留（无标记），扫描页 OCR 且标记页号正确（逐页判定而非整份判定）。"""
    _fake_raster(monkeypatch)
    calls: list[int] = []

    def transcribe(data: bytes, mime: str) -> str:
        calls.append(1)
        return OCR_TEXT

    mixed = _concat_pdf(_text_pdf(LONG_TEXT), _scan_pdf())
    text, ocr_used = extract_text_ex(mixed, "application/pdf", "mixed.pdf", transcribe=transcribe)
    assert ocr_used is True and len(calls) == 1  # 只有扫描页付 vision 成本
    assert text == f"{LONG_TEXT}\n〔第2页·OCR〕\n{OCR_TEXT}"
    assert "〔第1页" not in text  # 文本页绝不打标记


# —— ③ 阈值行为 ——

def test_threshold_short_text_page_is_ocrd(monkeypatch):
    """strip 后 < SCAN_TEXT_THRESHOLD 的碎渣页判为扫描页去 OCR；>= 阈值的页不动。"""
    assert len(SHORT_TEXT) < att_mod.SCAN_TEXT_THRESHOLD <= len(LONG_TEXT)
    _fake_raster(monkeypatch)
    mixed = _concat_pdf(_text_pdf(SHORT_TEXT), _text_pdf(LONG_TEXT))
    text, ocr_used = extract_text_ex(mixed, "application/pdf", "m.pdf", transcribe=lambda d, m: OCR_TEXT)
    assert ocr_used is True
    assert text == f"〔第1页·OCR〕\n{OCR_TEXT}\n{LONG_TEXT}"
    assert SHORT_TEXT not in text  # 碎渣被 OCR 结果替换


# —— ③b OCR 采用闸（MIN_OCR_TEXT_CHARS，评审#6）——

def test_short_ocr_result_not_adopted_keeps_content_addressing(monkeypatch):
    """OCR 出碎渣（strip 后 < MIN_OCR_TEXT_CHARS）视为未采用：保留原页文本、不置
    ocr_used——含短页（扉页/章节隔页）的文本 PDF 输出与 legacy 公式逐字节一致，
    内容寻址幂等不被切到 dedup_seed（docs/13 §3.3 红线）。"""
    _fake_raster(monkeypatch)
    short_page = _text_pdf("Chapter 2")  # 9 字符 < SCAN_TEXT_THRESHOLD → 被判为扫描页送 OCR
    pdf = _concat_pdf(_text_pdf(LONG_TEXT), short_page, _text_pdf(LONG_TEXT))
    legacy = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages).strip()
    calls: list[int] = []

    def scrap(data: bytes, mime: str) -> str:
        calls.append(1)
        return "Chapter 2"  # vision 对短文本页几乎必然回显碎渣（9 字符 < MIN_OCR_TEXT_CHARS）

    text, ocr_used = extract_text_ex(pdf, "application/pdf", "book.pdf", transcribe=scrap)
    assert len(calls) == 1  # 尝试过（配额已扣）……
    assert ocr_used is False  # ……但未采用：ocr_used 不翻转 → dedup_seed 不触发
    assert text == legacy and "〔" not in text  # 原页文本原样保留，逐字节一致


def test_short_ocr_results_consume_attempt_quota(monkeypatch):
    """碎渣结果虽不采用，但已发起 vision 调用 → 照样消耗尝试配额（硬上限语义）。"""
    monkeypatch.setattr(att_mod, "MAX_OCR_PAGES", 2)
    _fake_raster(monkeypatch)
    calls: list[int] = []
    text, ocr_used = extract_text_ex(
        _scan_pdf(pages=4), "application/pdf", "s.pdf",
        transcribe=lambda d, m: calls.append(1) or "渣",  # 1 字符 < MIN_OCR_TEXT_CHARS
    )
    assert len(calls) == 2 and ocr_used is False


def test_min_ocr_text_boundary_adopted(monkeypatch):
    """恰好 MIN_OCR_TEXT_CHARS 字符的转写被正常采用（边界含等号）。"""
    _fake_raster(monkeypatch)
    exact = "恰好十个字符的转写文"
    assert len(exact) == att_mod.MIN_OCR_TEXT_CHARS
    text, ocr_used = extract_text_ex(_scan_pdf(), "application/pdf", "s.pdf", transcribe=lambda d, m: exact)
    assert ocr_used is True and text == f"〔第1页·OCR〕\n{exact}"


# —— ④ 纯文本 PDF 逐字节一致（回归红线） ——

def test_pure_text_pdf_output_identical_with_transcriber(monkeypatch):
    """有转写器在场时，纯文本 PDF 输出必须与旧实现逐字节一致：不打标记、不调转写。"""
    _fake_raster(monkeypatch)
    pdf = _concat_pdf(_text_pdf(LONG_TEXT), _text_pdf(LONG_TEXT + " Page two marker line here."))
    # 旧实现的输出公式（pypdf 逐页 join + strip），直接在测试里按公式钉死。
    legacy = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages).strip()

    def transcribe(data: bytes, mime: str) -> str:
        raise AssertionError("纯文本 PDF 不得触发 OCR")

    text, ocr_used = extract_text_ex(pdf, "application/pdf", "pure.pdf", transcribe=transcribe)
    assert ocr_used is False
    assert text == legacy
    assert "OCR" not in text and "〔" not in text
    # 公共 extract_text 同样一致（内部走 _ex 丢弃 bool）。
    assert extract_text(pdf, "application/pdf", "pure.pdf", transcribe=transcribe) == legacy


# —— ⑤ MAX_OCR_PAGES 上限（按"尝试"计数，评审#5/#18）+ 如实注记 ——

def test_max_ocr_pages_cap_and_note(monkeypatch):
    """OCR 尝试页达上限后停止 OCR，文末追加超限注记（显式登记而非静默截断）。"""
    monkeypatch.setattr(att_mod, "MAX_OCR_PAGES", 2)
    _fake_raster(monkeypatch)
    calls: list[int] = []

    def transcribe(data: bytes, mime: str) -> str:
        calls.append(1)
        return f"第{len(calls)}次转写：{OCR_TEXT}"

    text, ocr_used = extract_text_ex(_scan_pdf(pages=3), "application/pdf", "big.pdf", transcribe=transcribe)
    assert ocr_used is True and len(calls) == 2  # 第 3 页不再调 transcribe
    assert "〔第1页·OCR〕" in text and "〔第2页·OCR〕" in text and "〔第3页·OCR〕" not in text
    assert text.endswith("〔注：扫描页共3页，仅前2页已尝试OCR〕")


def test_no_note_when_under_cap(monkeypatch):
    """未触及上限不加注记。"""
    _fake_raster(monkeypatch)
    text, _ = extract_text_ex(_scan_pdf(pages=2), "application/pdf", "s.pdf", transcribe=lambda d, m: OCR_TEXT)
    assert "〔注：" not in text


def test_ocr_cap_counts_attempts_including_failures(monkeypatch):
    """评审#5/#18：护栏按"尝试"计数——连续失败页（转写返回空）同样消耗配额，
    vision 调用次数严格 ≤ MAX_OCR_PAGES（key 限流/空白图册不再无界烧调用）。"""
    monkeypatch.setattr(att_mod, "MAX_OCR_PAGES", 2)
    _fake_raster(monkeypatch)
    calls: list[int] = []

    def failing(data: bytes, mime: str):
        calls.append(1)
        return None  # 恒失败：旧实现（按成功计数）会对 5 页全部调用

    text, ocr_used = extract_text_ex(_scan_pdf(pages=5), "application/pdf", "f.pdf", transcribe=failing)
    assert len(calls) == 2  # 尝试即计数：第 3 页起不再调 transcribe
    assert ocr_used is False
    assert text == "〔注：扫描页共5页，仅前2页已尝试OCR〕"  # 全页无文本 → 只剩注记


def test_ocr_cap_counts_raising_attempts(monkeypatch):
    """转写抛异常的页同样消耗配额（异常前已实际付出一次 vision 往返）。"""
    monkeypatch.setattr(att_mod, "MAX_OCR_PAGES", 2)
    _fake_raster(monkeypatch)
    calls: list[int] = []

    def boom(data: bytes, mime: str) -> str:
        calls.append(1)
        raise RuntimeError("vision down")

    text, ocr_used = extract_text_ex(_scan_pdf(pages=4), "application/pdf", "b.pdf", transcribe=boom)
    assert len(calls) == 2 and ocr_used is False


# —— ⑥ extract_text / extract_text_ex 信号一致性 ——

def test_extract_text_ex_signal_and_parity(monkeypatch):
    _fake_raster(monkeypatch)
    scan = _scan_pdf()
    fake = lambda d, m: OCR_TEXT  # noqa: E731
    assert extract_text_ex(scan, "application/pdf", "s.pdf", transcribe=fake) == (f"〔第1页·OCR〕\n{OCR_TEXT}", True)
    # 公共 extract_text 输出与 _ex 的 text 完全一致（仅丢弃 bool）。
    assert extract_text(scan, "application/pdf", "s.pdf", transcribe=fake) == f"〔第1页·OCR〕\n{OCR_TEXT}"
    # 非 PDF 路径 ocr_used 恒 False（图片分支幂等种子由 build_ingestor 的既有 ocr_image 门负责）。
    assert extract_text_ex("你好".encode(), "text/plain") == ("你好", False)
    assert extract_text_ex(b"x", "image/png", "a.png", transcribe=fake) == (OCR_TEXT, False)


# —— ⑦ build_ingestor：仅 ocr_used 时设 dedup_seed（联动） ——

def test_ingestor_dedup_seed_only_when_ocr_used(monkeypatch):
    """扫描 PDF（发生 OCR）→ dedup_seed=md5(文件字节)；纯文本 PDF 绝不带 seed（内容寻址不变）。"""
    captured: dict = {}
    import cognition.rag.ingest as ing

    monkeypatch.setattr(ing, "ingest", lambda docs, kb_id, **kw: captured.update(docs=docs))
    _fake_raster(monkeypatch)
    scan, plain = _scan_pdf(), _text_pdf(LONG_TEXT)
    objects = {"r/tc/scan.pdf": scan, "r/tc/plain.pdf": plain}
    ingestor = build_ingestor(
        Settings(rag_enabled=False), downloader=lambda k: objects[k],
        store=object(), embedder=object(), sparse=object(),
        transcribe=lambda d, m: OCR_TEXT,
    )
    names = ingestor(
        [
            {"mime_type": "application/pdf", "file_name": "scan.pdf", "resource_key": "r/tc/scan.pdf"},
            {"mime_type": "application/pdf", "file_name": "plain.pdf", "resource_key": "r/tc/plain.pdf"},
        ],
        "owner:u",
    )
    assert names == ["scan.pdf", "plain.pdf"]
    docs = {d["file_name"]: d for d in captured["docs"]}
    assert docs["scan.pdf"]["dedup_seed"] == hashlib.md5(scan).hexdigest()
    assert "dedup_seed" not in docs["plain.pdf"]


# —— ⑧ 降级矩阵：任何一步失败不炸、行为等于现状 ——

def test_degrade_no_transcriber():
    """无转写器：扫描 PDF 提不出文本 → None（与现状一致）；混合文档只剩文本页。"""
    assert extract_text(_scan_pdf(), "application/pdf", "s.pdf") is None
    assert extract_text_ex(_scan_pdf(), "application/pdf", "s.pdf") == (None, False)
    mixed = _concat_pdf(_text_pdf(LONG_TEXT), _scan_pdf())
    text, ocr_used = extract_text_ex(mixed, "application/pdf", "m.pdf")
    assert ocr_used is False and LONG_TEXT in text and "〔" not in text


def test_degrade_rasterize_none(monkeypatch):
    """栅格化失败（None）：保留原文本，不打标记，ocr_used=False。"""
    monkeypatch.setattr(att_mod, "_rasterize_pdf_page", lambda d, i, **kw: None)
    mixed = _concat_pdf(_text_pdf(LONG_TEXT), _scan_pdf())
    text, ocr_used = extract_text_ex(mixed, "application/pdf", "m.pdf", transcribe=lambda d, m: "不应出现")
    assert ocr_used is False and text == LONG_TEXT
    assert extract_text_ex(_scan_pdf(), "application/pdf", "s.pdf", transcribe=lambda d, m: "x") == (None, False)


def test_degrade_transcribe_none(monkeypatch):
    """转写失败（None）：同上降级。"""
    _fake_raster(monkeypatch)
    mixed = _concat_pdf(_text_pdf(LONG_TEXT), _scan_pdf())
    text, ocr_used = extract_text_ex(mixed, "application/pdf", "m.pdf", transcribe=lambda d, m: None)
    assert ocr_used is False and text == LONG_TEXT


def test_degrade_transcribe_raises(monkeypatch):
    """转写器抛异常：单页降级保留原文本，整体不炸（入库 best-effort）。"""
    _fake_raster(monkeypatch)

    def boom(data: bytes, mime: str) -> str:
        raise RuntimeError("vision down")

    mixed = _concat_pdf(_text_pdf(LONG_TEXT), _scan_pdf())
    text, ocr_used = extract_text_ex(mixed, "application/pdf", "m.pdf", transcribe=boom)
    assert ocr_used is False and text == LONG_TEXT


def test_degrade_corrupt_pdf():
    """坏 PDF 必须仍返回 None（回归红线），带转写器也一样。"""
    assert extract_text(b"not a real pdf", "application/pdf", "x.pdf") is None
    assert extract_text_ex(b"not a real pdf", "application/pdf", "x.pdf", transcribe=lambda d, m: "x") == (None, False)


# —— ⑨ 真依赖离线冒烟：pypdfium2 wheel 可用性 ——

def test_real_pypdfium2_rasterize_smoke():
    """真 pypdfium2 栅格化 Pillow 图片 PDF → PNG bytes 可被 PIL 打开（锁 wheel 可用）。"""
    png = att_mod._rasterize_pdf_page(_scan_pdf(), 0)
    assert png is not None and png.startswith(b"\x89PNG")
    img = Image.open(io.BytesIO(png))
    assert img.size[0] > 0 and img.size[1] > 0
    # 越界页号 / 坏字节：降级 None 不炸。
    assert att_mod._rasterize_pdf_page(_scan_pdf(), 5) is None
    assert att_mod._rasterize_pdf_page(b"not a real pdf", 0) is None


def test_real_rasterize_gives_up_when_over_limit(monkeypatch):
    """极小尺寸闸下（降 scale/转 JPEG 仍超）返回 None——转写器本会拒转，提前放弃。"""
    monkeypatch.setattr(att_mod, "MAX_IMAGE_BYTES", 50)
    assert att_mod._rasterize_pdf_page(_scan_pdf(), 0) is None


# —— ⑨b 渲染前像素预算（MAX_RENDER_PIXELS，评审#7/#17）——

def _blank_pdf_page(width: float, height: float) -> bytes:
    """pypdf 造指定 MediaBox 的空白页 PDF（无文本层 → 会被判为扫描页）。"""
    w = PdfWriter()
    w.add_blank_page(width=width, height=height)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_huge_mediabox_page_skipped_before_render():
    """巨幅 MediaBox（14400pt 见方，scale=2 全渲 ≈ 2.5GB 位图）在 render **前**被预算闸
    拒掉：不分配缓冲（不 OOM）、返回 None 优雅降级；链路上 transcribe 不被调用。"""
    giant = _blank_pdf_page(14400, 14400)
    assert att_mod._rasterize_pdf_page(giant, 0) is None
    calls: list[int] = []
    text, ocr_used = extract_text_ex(
        giant, "application/pdf", "giant.pdf",
        transcribe=lambda d, m: calls.append(1) or OCR_TEXT,
    )
    assert calls == [] and ocr_used is False and text is None  # 跳过 OCR，降级保留（空）原文本


def test_render_scale_clamped_by_pixel_budget(monkeypatch):
    """预算不足以 scale=2 全渲时按 sqrt(MAX_RENDER_PIXELS/(w*h)) 反解缩渲（≥0.5 仍可用）。"""
    monkeypatch.setattr(att_mod, "MAX_RENDER_PIXELS", 40_000)
    png = att_mod._rasterize_pdf_page(_scan_pdf(), 0)  # 页 200x300pt：scale=2 应为 400x600=24 万 px
    assert png is not None
    img = Image.open(io.BytesIO(png))
    assert img.size[0] < 400 and img.size[1] < 600  # 已被缩渲而非全分辨率
    assert img.size[0] * img.size[1] <= 40_000 * 1.05  # 像素总量落在预算内（留取整余量）


# —— ⑨c pdfium 互斥锁（_PDFIUM_LOCK，评审#4）——

def test_rasterize_concurrent_threads_smoke():
    """并发入库线程同时栅格化：模块级 _PDFIUM_LOCK 串行化全部 pdfium 调用
    （PDFium 官方声明非线程安全，并发 C 调用是未定义行为）——多线程冒烟不崩、全部成功。"""
    import concurrent.futures
    import threading

    assert isinstance(att_mod._PDFIUM_LOCK, type(threading.Lock()))
    pdf = _scan_pdf()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: att_mod._rasterize_pdf_page(pdf, 0), range(16)))
    assert all(r is not None and r.startswith(b"\x89PNG") for r in results)


# —— ⑩ 幂等：同扫描 PDF + 转写漂移两次入库 → point 数不增 ——

def test_scanned_pdf_ingest_idempotent_across_ocr_drift(monkeypatch):
    """评审#8 同款红线：OCR 文本天然漂移，dedup_seed=md5(文件字节) 保证原地 upsert。"""
    from cognition.rag.factory import build_embedder, build_sparse, build_store

    s = Settings(rag_enabled=True, qdrant_url=":memory:", embedding_provider="fake", sparse_provider="fake")
    store, emb, sp = build_store(s), build_embedder(s), build_sparse(s)
    _fake_raster(monkeypatch)
    pdf = _scan_pdf()
    calls: list[int] = []

    def drifting(data: bytes, mime: str) -> str:
        calls.append(1)
        return f"发票号 INV-2026-001 金额100万（第{len(calls)}次转写，措辞漂移）"

    ingestor = build_ingestor(s, downloader=lambda k: pdf, store=store, embedder=emb, sparse=sp,
                              transcribe=drifting)
    att = [{"mime_type": "application/pdf", "file_name": "scan.pdf", "resource_key": "r/u/scan.pdf"}]
    assert ingestor(att, "owner:u") == ["scan.pdf"]
    c1 = store._c.count(store._col).count  # noqa: SLF001
    assert c1 > 0
    assert ingestor(att, "owner:u") == ["scan.pdf"]  # 第二次转写文本已漂移
    c2 = store._c.count(store._col).count  # noqa: SLF001
    assert c1 == c2, f"扫描 PDF OCR 漂移导致重复入库：{c1}->{c2}"
    assert len(calls) == 2


# —— servicer 文案（同步补扫描版 PDF） ——

def test_ingest_document_message_mentions_scanned_pdf():
    import inspect

    from cognition.server import servicer

    src = inspect.getsource(servicer)
    assert "扫描版 PDF（需 vision key）" in src
