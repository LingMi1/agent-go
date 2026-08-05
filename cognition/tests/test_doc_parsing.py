"""D1 批（docs/15）：更强文档解析——表格 / 多栏 / 公式还原（离线测试，设计 §6 逐条落）。

全程不触网：文本页用 pypdf generic 手写内容流造（含精确词定位与数学符号），pdfplumber
结构化层按需 monkeypatch fake（表格用固定二维数组）或用真依赖（双栏裁列 / 表格冒烟），
公式兜底的 vision 转写注入 fake 闭包、栅格化按需 monkeypatch。

覆盖设计 §6 六条：① 表格 markdown+页标记；② 双栏裁列还原阅读顺序；③ 纯 prose 单栏页
逐字节红线；④ dedup_seed 结构化重排幂等；⑤ 真 pdfplumber 表格冒烟（锁 wheel）；
⑥ 降级矩阵（未装 / 坏页 → 回退 plain）。外加公式启发式 + vision 兜底与红线加固。
"""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

import cognition.attachments as att
from cognition.attachments import extract_text, extract_text_ex
from cognition.config import Settings

# 超过 SCAN_TEXT_THRESHOLD(30) 的纯 ASCII 文本 → 判为"文本页"、走结构化提取层。
LONG_TEXT = "This is a plain text page with enough characters to exceed the scan threshold."


# —— PDF 造具（pypdf 手写内容流；字体挂 WinAnsiEncoding 以便回读 ± × ÷ 等数学符号）——

def _new_page_writer() -> tuple[PdfWriter, object]:
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})}
    )
    return w, page


def _content_pdf(raw: bytes) -> bytes:
    """单页 PDF，内容流为 raw（latin-1 字节）。"""
    w, page = _new_page_writer()
    stream = StreamObject()
    stream.set_data(raw)
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _text_pdf(text: str) -> bytes:
    """单行文本页（Helvetica 12pt @72,720）。"""
    return _content_pdf(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))


def _op(x: float, y: float, s: str) -> str:
    return f"BT /F1 11 Tf {x} {y} Td ({s}) Tj ET"


def _two_col_pdf() -> bytes:
    """交错内容流的两栏对半页：左栏 x=72、右栏 x=400，内容流按行交错写（左1右1左2右2…）
    → pypdf plain 读序交错错乱；裁左右列各自 extract_text 后先左后右可还原正确阅读序。"""
    ops = [
        _op(72, 700, "Alpha Bravo"), _op(400, 700, "Uniform Victor"),
        _op(72, 680, "Charlie Delta"), _op(400, 680, "Whiskey Xray"),
        _op(72, 660, "Echo Foxtrot"), _op(400, 660, "Yankee Zulu"),
        _op(72, 640, "Golf Hotel"), _op(400, 640, "India Juliet"),
    ]
    return _content_pdf("\n".join(ops).encode("latin-1"))


def _aligned_text_columns_pdf() -> bytes:
    """对齐文本列的 3 列 4 行内容（x=72/250/430），**无任何绘制线**。pdfplumber text 策略
    会把它偶合切成伪表——评审 #9 去 text 策略后，无线即不判表，正确保 plain（红线）。"""
    rows = [("Model", "Price", "Stock"), ("A100", "1000", "yes"),
            ("H100", "2000", "no"), ("B200", "3000", "yes")]
    ops = []
    for i, (a, b, c) in enumerate(rows):
        y = 700 - i * 20
        ops += [_op(72, y, a), _op(250, y, b), _op(430, y, c)]
    return _content_pdf("\n".join(ops).encode("latin-1"))


def _aligned_prose_pdf() -> bytes:
    """多行左对齐普通 prose（每行几个词、行间 y 对齐、词落在 3 个对齐 x 锚点 72/250/430、
    无任何绘制线）——评审 #9 复现场景：pdfplumber text 策略靠词 x 对齐会把它误判为 3 列伪表。
    修复前（text 策略兜底）→ transformed=True + 尾部塞乱切 markdown 表；修复后（仅 lines
    策略、无绘制线）→ 无表、逐字节保 plain（docs/13 §3.3 + docs/15 §3.2 红线）。"""
    rows = [
        ("Lorem ipsum", "dolor consectetur", "adipiscing elit"),
        ("sed eiusmod", "tempor incididunt", "labore magna"),
        ("aliqua enim", "minim veniam", "nostrud ullamco"),
        ("laboris nisi", "aliquip commodo", "consequat duis"),
        ("aute irure", "reprehenderit voluptate", "velit esse"),
        ("cillum dolore", "fugiat nulla", "pariatur excepteur"),
    ]
    ops = []
    for i, (a, b, c) in enumerate(rows):
        y = 700 - i * 20
        ops += [_op(72, y, a), _op(250, y, b), _op(430, y, c)]
    return _content_pdf("\n".join(ops).encode("latin-1"))


def _ruled_table_pdf() -> bytes:
    """带**真实绘制表格线**的 3 列 4 行表：内容流先画网格线（m/l/S）再逐格写文字。
    pdfplumber 默认 lines 策略据线检出——评审 #9 去 text 策略后，唯有绘制线的真表被抽取。"""
    xs = [72, 240, 400, 540]        # 4 条竖线 → 3 列
    ys = [720, 700, 680, 660, 640]  # 5 条横线 → 4 行
    parts = ["1 w 0 0 0 RG"]        # 线宽 1 + 黑色描边
    for y in ys:                    # 横线
        parts.append(f"{xs[0]} {y} m {xs[-1]} {y} l S")
    for x in xs:                    # 竖线
        parts.append(f"{x} {ys[-1]} m {x} {ys[0]} l S")
    rows = [("Model", "Price", "Stock"), ("A100", "1000", "yes"),
            ("H100", "2000", "no"), ("B200", "3000", "yes")]
    for i, cells in enumerate(rows):
        y = ys[i] - 14              # 单元格内基线
        for j, val in enumerate(cells):
            parts.append(_op(xs[j] + 4, y, val))
    return _content_pdf("\n".join(parts).encode("latin-1"))


def _formula_pdf() -> bytes:
    """单行公式页：>= FORMULA_MIN_SYMBOLS(8) 个数学符号（± × ÷ 反复），命中公式启发式。"""
    body = "E = mc formula " + ("± × ÷ " * 6)
    return _content_pdf(f"BT /F1 12 Tf 72 720 Td ({body}) Tj ET".encode("latin-1"))


def _plain(pdf: bytes) -> str:
    """旧实现输出公式（pypdf 逐页 join + strip），红线对照基准。"""
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages).strip()


# —— fake pdfplumber（隔离结构层，不触真依赖）——

class _FakeCrop:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePage:
    def __init__(self, *, tables=None, text_tables=None, words=None, width=612.0,
                 height=792.0, left_text="", right_text="", raise_on=None) -> None:
        self._tables = tables if tables is not None else []
        self._text_tables = text_tables  # 传 text 策略 settings 时返回它（模拟 lines 空→text 兜底）
        self._words = words if words is not None else []
        self.width = width
        self.height = height
        self._left_text = left_text
        self._right_text = right_text
        self._raise_on = raise_on or set()

    def extract_tables(self, settings=None):
        if "extract_tables" in self._raise_on:
            raise RuntimeError("boom tables")
        if settings is not None and self._text_tables is not None:
            return self._text_tables
        return self._tables

    def extract_words(self):
        if "extract_words" in self._raise_on:
            raise RuntimeError("boom words")
        return self._words

    def crop(self, bbox):
        x0, _, x1, _ = bbox
        return _FakeCrop(self._left_text if x0 == 0 else self._right_text)


class _FakePlumber:
    def __init__(self, pages) -> None:
        self.pages = pages
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_plumber(monkeypatch, *pages: _FakePage) -> _FakePlumber:
    fake = _FakePlumber(list(pages))
    monkeypatch.setattr(att, "_open_plumber", lambda data: fake)
    return fake


# —— ① 表格 → markdown + 〔第N页·表格〕标记（monkeypatch fake extract_tables）——

def test_table_page_markdown_and_marker(monkeypatch):
    """文本页检出表格：plain 正文 + 各表 markdown（页标记同 OCR 款），transformed=True，句柄释放。"""
    fixed = [[["型号", "价格"], ["A100", "8万"], ["H100", "25万"]]]
    fake = _patch_plumber(monkeypatch, _FakePage(tables=fixed, words=[]))
    text, changed = extract_text_ex(_text_pdf(LONG_TEXT), "application/pdf", "t.pdf")
    expected_md = "| 型号 | 价格 |\n| --- | --- |\n| A100 | 8万 |\n| H100 | 25万 |"
    assert text == f"{LONG_TEXT}\n〔第1页·表格〕\n{expected_md}"
    assert changed is True
    assert fake.closed is True  # finally 释放 pdfplumber 句柄


def test_multiple_tables_each_get_marker(monkeypatch):
    """一页多表：每表各带 〔第N页·表格〕 标记、按序拼接。"""
    fixed = [[["a", "b"], ["1", "2"]], [["c", "d"], ["3", "4"]]]
    _patch_plumber(monkeypatch, _FakePage(tables=fixed, words=[]))
    text, _ = extract_text_ex(_text_pdf(LONG_TEXT), "application/pdf", "t.pdf")
    assert text.count("〔第1页·表格〕") == 2
    assert "| a | b |" in text and "| c | d |" in text


def test_table_no_text_strategy_fallback_keeps_plain(monkeypatch):
    """评审 #9 红线：**去掉 text 策略兜底**——lines 策略无表（无绘制线）时，即便 text 策略
    本会切出伪表，也绝不兜底：保 plain 逐字节原样、transformed=False。fake 的
    extract_tables(settings) 会返回 text_tables，修复后不再带 settings 调它，故 text_tables 不生效。"""
    _patch_plumber(monkeypatch, _FakePage(tables=[], text_tables=[[["x", "y"], ["p", "q"]]], words=[]))
    text, changed = extract_text_ex(_text_pdf(LONG_TEXT), "application/pdf", "t.pdf")
    assert text == LONG_TEXT and changed is False
    assert "〔" not in text and "| x | y |" not in text


def test_degenerate_table_ignored_keeps_plain(monkeypatch):
    """退化表（单行/单列）被 _table_to_markdown 过滤 → 保 plain 原样、transformed=False。"""
    _patch_plumber(monkeypatch, _FakePage(tables=[[["only one row"]]], words=[]))
    text, changed = extract_text_ex(_text_pdf(LONG_TEXT), "application/pdf", "t.pdf")
    assert text == LONG_TEXT and changed is False and "〔" not in text


def test_table_cell_pipe_and_newline_escaped(monkeypatch):
    """单元格内竖线/换行做转义，不破坏 markdown 表结构。"""
    _patch_plumber(monkeypatch, _FakePage(tables=[[["a|b", "c\nd"], ["e", "f"]]], words=[]))
    text, _ = extract_text_ex(_text_pdf(LONG_TEXT), "application/pdf", "t.pdf")
    assert "| a\\|b | c d |" in text


# —— ② 双栏裁列还原阅读顺序（真 pdfplumber，隔离表格分支）——

def test_two_column_reorder(monkeypatch):
    """两栏对半页：裁左右列先左后右重排——左栏末元素排在右栏首元素之前，且不同于 plain 交错序。"""
    monkeypatch.setattr(att, "_extract_tables_md", lambda pg: None)  # 隔离：只验裁列逻辑
    pdf = _two_col_pdf()
    text, changed = extract_text_ex(pdf, "application/pdf", "c.pdf")
    assert changed is True
    assert text.index("Golf Hotel") < text.index("Uniform Victor")  # 先左后右
    assert "Alpha Bravo" in text and "India Juliet" in text
    assert text != _plain(pdf)  # 确有重排（plain 是左右交错序）


def test_two_column_gutter_branch_fires_end_to_end():
    """评审 #9 附带收益：去掉 text 策略兜底后，真两栏 prose 不再被误当表抓走，端到端（不
    monkeypatch 抽表）自然落到 gutter 裁列分支——抽表返回 None → 先左后右重排、无表格标记。
    这正是 docs/15 §7 登记的『双栏页原被 text 策略当表抓走』踩坑的正解。"""
    pdf = _two_col_pdf()
    plumber = att._open_plumber(pdf)
    try:
        assert att._extract_tables_md(plumber.pages[0]) is None  # 无绘制线 → 不判表
    finally:
        plumber.close()
    text, changed = extract_text_ex(pdf, "application/pdf", "c.pdf")
    assert changed is True
    assert "〔" not in text  # 走裁列分支、非表格分支（无 〔第N页·表格〕 标记）
    assert text.index("Golf Hotel") < text.index("Uniform Victor")  # 先左后右
    assert text != _plain(pdf)


def test_two_column_fake_crop_order(monkeypatch):
    """fake plumber 精确验证裁列拼接顺序：先左后右、各去首尾空白。"""
    words = [{"x0": 60, "x1": 120}] * 6 + [{"x0": 400, "x1": 460}] * 6  # 左右各 6 词、gutter 空
    _patch_plumber(monkeypatch, _FakePage(words=words, left_text="LEFT-A\nLEFT-B",
                                          right_text="RIGHT-A\nRIGHT-B"))
    text, changed = extract_text_ex(_text_pdf(LONG_TEXT), "application/pdf", "c.pdf")
    assert changed is True and text == "LEFT-A\nLEFT-B\nRIGHT-A\nRIGHT-B"


def test_gutter_occupied_not_two_column(monkeypatch):
    """有词横占页中部 gutter 中带（如贯穿全宽单行）→ 判否两栏 → 保 plain（红线加固）。"""
    words = [{"x0": 60, "x1": 400}] * 12  # 每词横跨中线 → gutter 被占
    _patch_plumber(monkeypatch, _FakePage(words=words, left_text="L", right_text="R"))
    text, changed = extract_text_ex(_text_pdf(LONG_TEXT), "application/pdf", "c.pdf")
    assert text == LONG_TEXT and changed is False


def test_single_side_not_two_column(monkeypatch):
    """词全在一侧（单栏左对齐）→ 另一侧太空 → 判否两栏 → 保 plain。"""
    words = [{"x0": 60, "x1": 120}] * 12  # 全在左半、右半空
    _patch_plumber(monkeypatch, _FakePage(words=words, left_text="L", right_text=""))
    text, changed = extract_text_ex(_text_pdf(LONG_TEXT), "application/pdf", "c.pdf")
    assert text == LONG_TEXT and changed is False


# —— ③ 纯 prose 单栏页逐字节红线（真 pdfplumber）——

def test_pure_prose_page_byte_identical():
    """真 pdfplumber：单行 prose 无表格无栏无公式 → plain 逐字节原样、不打标记、transformed=False，
    不触发 transcribe（红线：文本漂移会让既有已入库文档漂出新 point，docs/15 §3.2）。"""
    pdf = _text_pdf(LONG_TEXT)

    def no_transcribe(data, mime):
        raise AssertionError("纯 prose 页不得触发 vision")

    text, changed = extract_text_ex(pdf, "application/pdf", "p.pdf", transcribe=no_transcribe)
    assert text == _plain(pdf) and changed is False
    assert "〔" not in text and "OCR" not in text
    # 公共 extract_text 同样一致（内部走 _ex 丢弃 bool）。
    assert extract_text(pdf, "application/pdf", "p.pdf", transcribe=no_transcribe) == _plain(pdf)


def test_multiline_aligned_prose_not_misdetected_as_table():
    """评审 #9 核心红线复现（真 pdfplumber）：多行左对齐 prose（词 x 对齐会让 text 策略偶合出
    竖边）无绘制线 → 仅 lines 策略即无表 → _extract_tables_md 返回 None、逐字节 == plain、
    transformed=False、不触发 vision。**修复前**（text 策略兜底）此断言失败：尾部被追加乱切
    markdown 表、transformed=True、既有已入库文档漂出新 point（docs/13 §3.3 + docs/15 §3.2）。"""
    pdf = _aligned_prose_pdf()
    plumber = att._open_plumber(pdf)
    assert plumber is not None  # 锁真依赖在场，否则本用例无意义
    try:
        # 该页 lines 策略无绘制线 → 抽表必返回 None（不再用 text 策略把 prose 切成伪表）。
        assert att._extract_tables_md(plumber.pages[0]) is None
    finally:
        plumber.close()

    def no_transcribe(data, mime):
        raise AssertionError("多行 prose 页不得触发 vision")

    text, changed = extract_text_ex(pdf, "application/pdf", "prose.pdf", transcribe=no_transcribe)
    assert changed is False
    assert text == _plain(pdf)  # 逐字节红线
    assert "〔" not in text and "|" not in text  # 无表格标记、无 markdown 竖线


def test_aligned_text_columns_without_lines_stay_plain():
    """无绘制线的对齐文本列（形似表但无线）：text 策略本会切成 3 列表，去 text 策略后不判表 →
    保 plain 逐字节、transformed=False（红线：无线不可区分 aligned-table 与 prose，一律不重排）。"""
    pdf = _aligned_text_columns_pdf()
    text, changed = extract_text_ex(pdf, "application/pdf", "cols.pdf")
    assert changed is False
    assert text == _plain(pdf)
    assert "〔" not in text and "|" not in text


# —— ④ dedup_seed：结构化重排（含抽取漂移）两次入库 → point 数不增 ——

def test_structural_rearrange_ingest_idempotent(monkeypatch):
    """表格页 transformed=True → build_ingestor 设 dedup_seed=md5(文件字节)；即使抽表内容跨次
    漂移，同文件重传原地 upsert、point 数不增（docs/15 §3.2 幂等红线）。"""
    from cognition.attachments import build_ingestor
    from cognition.rag.factory import build_embedder, build_sparse, build_store

    s = Settings(rag_enabled=True, qdrant_url=":memory:", embedding_provider="fake", sparse_provider="fake")
    store, emb, sp = build_store(s), build_embedder(s), build_sparse(s)
    pdf = _text_pdf(LONG_TEXT)
    calls: list[int] = []

    def drifting_open(data):
        calls.append(1)
        # 抽表内容跨次漂移（模拟 pdfplumber 抽取非完全确定）——dedup_seed 须抵消漂移。
        return _FakePlumber([_FakePage(tables=[[["型号", "价格"], ["A100", f"{len(calls)}万"]]], words=[])])

    monkeypatch.setattr(att, "_open_plumber", drifting_open)
    ingestor = build_ingestor(s, downloader=lambda k: pdf, store=store, embedder=emb, sparse=sp,
                              transcribe=lambda d, m: None)
    item = [{"mime_type": "application/pdf", "file_name": "t.pdf", "resource_key": "r/u/t.pdf"}]
    assert ingestor(item, "owner:u") == ["t.pdf"]
    c1 = store._c.count(store._col).count  # noqa: SLF001
    assert c1 > 0
    assert ingestor(item, "owner:u") == ["t.pdf"]  # 第二次抽表内容已漂移
    c2 = store._c.count(store._col).count  # noqa: SLF001
    assert c1 == c2, f"结构化重排漂移导致重复入库：{c1}->{c2}"
    assert len(calls) == 2


def test_ingestor_sets_dedup_seed_on_structural_transform(monkeypatch):
    """联动：结构化重排页设 dedup_seed=md5(data)；纯 prose PDF 绝不带 seed（内容寻址不变）。"""
    import hashlib

    captured: dict = {}
    import cognition.rag.ingest as ing
    monkeypatch.setattr(ing, "ingest", lambda docs, kb_id, **kw: captured.update(docs=docs))
    from cognition.attachments import build_ingestor

    table_pdf, plain_pdf = _text_pdf(LONG_TEXT), _text_pdf(LONG_TEXT + " Second distinct doc.")
    objects = {"r/tc/tbl.pdf": table_pdf, "r/tc/plain.pdf": plain_pdf}

    def selective_open(data):
        # 仅对 table_pdf 返回带表的 fake；plain_pdf 无表（保持 plain）。
        if data == table_pdf:
            return _FakePlumber([_FakePage(tables=[[["a", "b"], ["1", "2"]]], words=[])])
        return _FakePlumber([_FakePage(tables=[], words=[])])

    monkeypatch.setattr(att, "_open_plumber", selective_open)
    ingestor = build_ingestor(Settings(rag_enabled=False), downloader=lambda k: objects[k],
                              store=object(), embedder=object(), sparse=object(),
                              transcribe=lambda d, m: None)
    ingestor(
        [{"mime_type": "application/pdf", "file_name": "tbl.pdf", "resource_key": "r/tc/tbl.pdf"},
         {"mime_type": "application/pdf", "file_name": "plain.pdf", "resource_key": "r/tc/plain.pdf"}],
        "owner:u",
    )
    docs = {d["file_name"]: d for d in captured["docs"]}
    assert docs["tbl.pdf"]["dedup_seed"] == hashlib.md5(table_pdf).hexdigest()
    assert "dedup_seed" not in docs["plain.pdf"]


# —— ⑤ 真 pdfplumber 表格冒烟（锁 wheel 可用性）——

def test_real_pdfplumber_table_smoke():
    """真 pdfplumber 抽**带绘制线**的手造表 → markdown 表格 + 页标记，行内关联（型号↔价格）
    可检索。评审 #9 去 text 策略后，真表靠 lines 策略据绘制线仍被抽取（红线不误伤真表）。"""
    pdf = _ruled_table_pdf()
    assert att._open_plumber(pdf) is not None  # 锁 pdfplumber wheel 可用
    text, changed = extract_text_ex(pdf, "application/pdf", "smoke.pdf")
    assert changed is True
    assert "〔第1页·表格〕" in text
    assert "| Model | Price | Stock |" in text
    assert "A100" in text and "1000" in text  # 某行的型号与价格同块出现


# —— ⑥ 公式 → 整页 vision 兜底 ——

def test_formula_heuristic_unit():
    """数学符号密度启发式：命中公式串、放过中文正文与 ASCII prose（无误触发）。"""
    assert att._looks_formula_heavy("∑∫∂∇ ≈ ≠ ≤ ≥ α β γ")
    assert not att._looks_formula_heavy("这是一段中文正文没有任何数学符号存在于其中的普通文字段落")
    assert not att._looks_formula_heavy(LONG_TEXT)


def test_formula_page_triggers_vision_fallback(monkeypatch):
    """公式页（数学符号密集）无表无栏 → 整页 vision 兜底，替换为 〔第N页·OCR〕、transformed=True。"""
    monkeypatch.setattr(att, "_rasterize_pdf_page", lambda d, i, **k: b"\x89PNG-fake")
    calls: list[str] = []

    def transcribe(data, mime):
        calls.append(mime)
        return "转写还原的公式内容 LaTeX 版足够长"

    text, changed = extract_text_ex(_formula_pdf(), "application/pdf", "f.pdf", transcribe=transcribe)
    assert changed is True and calls == ["image/png"]
    assert text.startswith("〔第1页·OCR〕")


def test_formula_no_transcriber_keeps_plain():
    """公式页但无转写器 → 不兜底、保 plain、transformed=False（降级不炸）。"""
    text, changed = extract_text_ex(_formula_pdf(), "application/pdf", "f.pdf")
    assert changed is False and "〔" not in text and text  # plain 保留


def test_formula_shares_ocr_page_cap(monkeypatch):
    """公式兜底与扫描 OCR 共享 MAX_OCR_PAGES 硬上限：上限=0 时不发起 vision 调用。"""
    monkeypatch.setattr(att, "MAX_OCR_PAGES", 0)
    monkeypatch.setattr(att, "_rasterize_pdf_page", lambda d, i, **k: b"\x89PNG-fake")
    calls: list[int] = []
    text, changed = extract_text_ex(_formula_pdf(), "application/pdf", "f.pdf",
                                    transcribe=lambda d, m: calls.append(1) or "x")
    assert calls == [] and changed is False and "〔" not in text


# —— ⑦ 降级矩阵：pdfplumber 未装 / 坏页 / 坏字节 → 回退 plain 不炸 ——

def test_degrade_plumber_unavailable(monkeypatch):
    """pdfplumber 未装（_open_plumber → None）：文本页回退 plain 逐字节、transformed=False。"""
    monkeypatch.setattr(att, "_open_plumber", lambda data: None)
    pdf = _text_pdf(LONG_TEXT)
    text, changed = extract_text_ex(pdf, "application/pdf", "p.pdf")
    assert text == _plain(pdf) and changed is False


def test_degrade_plumber_page_raises(monkeypatch):
    """pdfplumber 抽表/取词抛异常：逐步 except 降级回退 plain、transformed=False。"""
    _patch_plumber(monkeypatch, _FakePage(raise_on={"extract_tables", "extract_words"}))
    pdf = _text_pdf(LONG_TEXT)
    text, changed = extract_text_ex(pdf, "application/pdf", "p.pdf")
    assert text == _plain(pdf) and changed is False


def test_open_plumber_bad_bytes_returns_none():
    """真 _open_plumber 遇坏字节 → None（惰性打开异常降级），链路回退 plain 不炸。"""
    assert att._open_plumber(b"not a real pdf") is None


def test_plumber_page_out_of_range_returns_none(monkeypatch):
    """pdfplumber 页数与 pypdf 不一致（越界）→ _plumber_page 返回 None，保 plain。"""
    _patch_plumber(monkeypatch)  # 空 pages → idx 0 越界
    pdf = _text_pdf(LONG_TEXT)
    text, changed = extract_text_ex(pdf, "application/pdf", "p.pdf")
    assert text == _plain(pdf) and changed is False


def test_degrade_corrupt_pdf_still_none():
    """坏 PDF 仍返回 None（回归红线），结构层不改变该保证。"""
    assert extract_text_ex(b"not a real pdf", "application/pdf", "x.pdf") == (None, False)
