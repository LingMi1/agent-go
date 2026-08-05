"""后续能力批次：B.4 plan生图 / B.2 图片OCR入库 / B.1 生成图内联网页。"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile

from cognition.attachments import build_ingestor, extract_text, is_image
from cognition.config import Settings
from cognition.graphs.nodes import IMAGE_GEN_INSTRUCTION
from cognition.graphs.plan_execute import PLANNER_SYSTEM, compose_planner_system
from cognition.skills.runner.scratch import has_generated, run_generated_dir, stash_generated

_FMT = {"ppt": "PPT格式提示"}


# —— B.4 plan-mode 生图 ——

def test_compose_planner_system_image_gen():
    on = compose_planner_system(PLANNER_SYSTEM, "SOP", "ppt", _FMT, image_gen=True)
    assert "SOP" in on and IMAGE_GEN_INSTRUCTION in on and _FMT["ppt"] in on
    off = compose_planner_system(PLANNER_SYSTEM, "SOP", "ppt", _FMT, image_gen=False)
    assert IMAGE_GEN_INSTRUCTION not in off and _FMT["ppt"] in off


# —— B.2 图片 OCR 入库 ——

def test_extract_text_image_branch():
    assert is_image("image/png")
    assert extract_text(b"x", "image/png", "a.png") is None  # 无转写器→跳过
    assert extract_text(b"x", "image/png", "a.png", transcribe=lambda d, m: "图中文字") == "图中文字"
    assert extract_text(b"x", "image/png", "a.png", transcribe=lambda d, m: None) is None


def test_ingestor_admits_image_with_transcriber(monkeypatch):
    captured = {}
    import cognition.rag.ingest as ing

    monkeypatch.setattr(ing, "ingest", lambda docs, kb_id, **kw: captured.update(docs=docs))
    ingestor = build_ingestor(
        Settings(rag_enabled=False), downloader=lambda k: b"imgbytes",
        store=object(), embedder=object(), sparse=object(),
        transcribe=lambda d, m: "OCR:销售额100万",
    )
    names = ingestor([{"mime_type": "image/png", "file_name": "chart.png", "resource_key": "r/tc/chart.png"}], "owner:u")
    assert names == ["chart.png"]
    assert "销售额100万" in captured["docs"][0]["text"]


def test_ingestor_skips_image_without_transcriber(monkeypatch):
    import cognition.rag.ingest as ing

    monkeypatch.setattr(ing, "ingest", lambda *a, **k: None)
    # transcribe=None + 无 anthropic key → build_image_transcriber 返回 None → 图片跳过。
    ingestor = build_ingestor(
        Settings(rag_enabled=False, anthropic_api_key=None), downloader=lambda k: b"x",
        store=object(), embedder=object(), sparse=object(), transcribe=None,
    )
    assert ingestor([{"mime_type": "image/png", "file_name": "a.png", "resource_key": "r/tc/a.png"}], "owner:u") == []


# —— B.1 生成图内联网页 ——

def test_scratch_stash_and_has():
    rid = "run-scratch-test"
    stash_generated(rid, "image-1.png", b"\x89PNG\r\n\x1a\nX")
    assert has_generated(rid)
    assert os.path.isfile(os.path.join(run_generated_dir(rid), "image-1.png"))


def test_render_page_inlines_generated_image():
    gen = tempfile.mkdtemp()
    out = tempfile.mkdtemp()
    with open(os.path.join(gen, "image-1.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\nFAKE")
    env = {**os.environ, "SKILL_GENERATED_DIR": gen, "SKILL_OUTPUT_DIR": out}
    html = '<html><body><img src="generated/image-1.png"><img src="generated/nope.png"></body></html>'
    script = os.path.join(os.path.dirname(__file__), "..", "runtime", "skills", "frontend-design", "scripts", "render_page.py")
    r = subprocess.run(["python3", script, json.dumps({"title": "T", "html": html})], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "内联生成图 1 张" in r.stdout
    site = open(os.path.join(out, "site.html")).read()
    assert "data:image/png;base64," in site  # 存在的 generated 图内联
    b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKE").decode()
    assert b64 in site
    assert "generated/nope.png" in site  # 不存在的引用原样保留


def test_image_ocr_ingest_idempotent_across_text_drift():
    """评审#8：同图两次入库、OCR 文本漂移，dedup_seed 相同 → 点数不翻倍（内容寻址幂等）。"""
    from cognition.rag.factory import build_embedder, build_sparse, build_store
    from cognition.rag.ingest import ingest

    s = Settings(rag_enabled=True, qdrant_url=":memory:", embedding_provider="fake", sparse_provider="fake")
    store, emb, sp = build_store(s), build_embedder(s), build_sparse(s)
    seed = "IMGHASH-abc"
    d1 = {"text": "销售额100万 图A", "file_name": "c.png", "source_id": "r/tc/c.png", "dedup_seed": seed}
    d2 = {"text": "销售额 100 万元 图A（措辞略变）", "file_name": "c.png", "source_id": "r/tc/c.png", "dedup_seed": seed}
    ingest([d1], "owner:u", store=store, embedder=emb, sparse=sp, stable_ids=True)
    c1 = store._c.count(store._col).count
    ingest([d2], "owner:u", store=store, embedder=emb, sparse=sp, stable_ids=True)
    c2 = store._c.count(store._col).count
    assert c1 == c2, f"图片 OCR 漂移导致重复入库：{c1}->{c2}"


def test_render_page_inlines_uploaded_input_image_and_warns_missing():
    """缺陷回归：上传原图 <img src="1.jpeg"> 相对引用未内联 → 沙箱预览空白。
    修复后：SKILL_INPUT_DIR（input_files 暂存目录）里的裸文件名引用也内联；
    找不到的引用保留原样并打印警告引导模型补 input_files。"""
    gen = tempfile.mkdtemp()
    inp = tempfile.mkdtemp()
    out = tempfile.mkdtemp()
    with open(os.path.join(inp, "1.jpeg"), "wb") as f:
        f.write(b"\xff\xd8\xffJPEGFAKE")
    env = {**os.environ, "SKILL_GENERATED_DIR": gen, "SKILL_INPUT_DIR": inp, "SKILL_OUTPUT_DIR": out}
    html = (
        '<html><body><img class="a" src="1.jpeg"><img src="lost.png">'
        '<script src="app.js"></script><img src="https://x.cn/a.png"></body></html>'
    )
    script = os.path.join(os.path.dirname(__file__), "..", "runtime", "skills", "frontend-design", "scripts", "render_page.py")
    r = subprocess.run(["python3", script, json.dumps({"title": "T", "html": html})], env=env, capture_output=True, text=True)
    assert r.returncode == 0
    assert "内联上传图 1 张" in r.stdout
    assert "警告: 1 个本地图片引用未找到" in r.stdout and "lost.png" in r.stdout
    site = open(os.path.join(out, "site.html")).read()
    assert "data:image/jpeg;base64," in site  # 上传图按文件名内联
    assert base64.b64encode(b"\xff\xd8\xffJPEGFAKE").decode() in site
    assert 'src="lost.png"' in site  # 未命中原样保留
    assert 'src="app.js"' in site and "https://x.cn/a.png" in site  # 非图片/远程引用不动
