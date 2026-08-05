#!/usr/bin/env python3
"""构建 RAG 检索评测用 chunks.jsonl（仅标准库，无第三方依赖）。

为什么不直接把 cognition.rag.chunking.split_text 作为主切分：
- 评测集需要稳定可复现的 chunk_id 和 section_path 元数据，split_text 只返回纯文本列表；
- 评测语料是结构化 Markdown（固定 H2 小节），标题优先分块保证块边界与语义边界一致，
  gold 标注不会被句子级切分随机切断；
- 单小节超过打包上限时仍回退复用项目 split_text（同 500/100 的句界+overlap 语义），
  保持与生产入库分块行为一致。

分块规则：
- 先按 H2 标题切成小节（H1 标题行并入首个小节组）；
- 贪心打包连续小节，单块正文 ≤ PACK_MAX 字符；
- 相邻块之间保留前一块尾部 OVERLAP 字符作为接头（与生产 chunker 的 tail-overlap 一致）；
- chunk_id 为 `{document_id}::c{seq:02d}`，遍历顺序、打包过程均确定性，可重复构建比对。

尺寸说明：评测语料单篇正文约 800–1150 字符，均衡打包成两块后单块约 280–660 字符，
低于泛用场景的 500–800 名义区间；这是在"小节语义边界完整"与"块长均衡"之间的取舍，
不足 500 的块均为完整小节组，未做跨文档拼接凑长度。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
RAG_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = SCRIPT_PATH.parents[3]
CORPUS_ROOT = RAG_ROOT / "corpus"
OUTPUT_PATH = RAG_ROOT / "chunks.jsonl"
PROJECT_CHUNKER = REPO_ROOT / "cognition" / "cognition" / "rag" / "chunking.py"

PACK_MAX = 560  # 单块正文（不含 overlap 接头）上限；语料正文约 800–1150 字，560 使两块均衡落在 ~450–660
OVERLAP = 100  # 相邻块接头长度，落在需求的 80–120 区间


def _load_project_split_text():
    """直接按文件加载项目 chunker，避免触发 cognition 包 __init__ 的重依赖。"""
    spec = importlib.util.spec_from_file_location("_project_chunking", PROJECT_CHUNKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.split_text


def parse_markdown(path: Path) -> tuple[dict, str]:
    """解析 front matter 与正文（与 validate_corpus.py 相同的轻量约定）。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"缺少 front matter: {path}")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ValueError(f"front matter 未闭合: {path}")
    header, body = text[4:marker], text[marker + 5 :]
    metadata: dict = {}
    active_list: str | None = None
    for line in header.splitlines():
        if line.startswith("  - "):
            if active_list is None:
                raise ValueError(f"front matter 游离列表项: {path}")
            metadata[active_list].append(line[4:].strip().strip('"'))
            continue
        active_list = None
        key, _, raw_value = line.partition(":")
        key = key.strip()
        if not key:
            raise ValueError(f"front matter 行非法: {path}")
        if raw_value.strip():
            metadata[key] = raw_value.strip().strip('"')
        else:
            metadata[key] = []
            active_list = key
    return metadata, body.strip()


def split_sections(body: str) -> list[tuple[str, str]]:
    """按 H2 标题切小节，返回 (小节名, 含标题行的小节文本)。H1 前导归入 `_intro`。"""
    sections: list[tuple[str, list[str]]] = [("_intro", [])]
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), [line]))
        else:
            sections[-1][1].append(line)
    result = []
    for name, lines in sections:
        text = "\n".join(lines).strip()
        if text:
            result.append((name, text))
    return result


def pack_blocks(blocks: list[tuple[str, str]], split_text) -> list[tuple[list[str], str]]:
    """按目标长度均衡打包小节为块，返回 (小节名列表, 块正文)。

    先按总长决定块数（total/PACK_MAX 向上取整），再以最小偏差贪心切分，
    避免纯贪心产生过小的尾块。超长小节回退项目 split_text。
    """
    expanded: list[tuple[str, str]] = []
    for name, text in blocks:
        if len(text) > PACK_MAX:
            parts = split_text(text, size=PACK_MAX, overlap=OVERLAP)
            expanded.extend((f"{name}[{i}]", part) for i, part in enumerate(parts))
        else:
            expanded.append((name, text))

    total = sum(len(text) for _, text in expanded) + 2 * max(0, len(expanded) - 1)
    n_chunks = max(1, -(-total // PACK_MAX))
    target = total / n_chunks

    packed: list[tuple[list[str], str]] = []
    names: list[str] = []
    texts: list[str] = []
    length = 0
    for name, text in expanded:
        extra = len(text) + (2 if texts else 0)
        overshoot = abs(length + extra - target) >= abs(length - target)
        if texts and len(packed) < n_chunks - 1 and (overshoot or length + extra > PACK_MAX):
            packed.append((names, "\n\n".join(texts)))
            names, texts, length = [], [], 0
            extra = len(text)
        names.append(name)
        texts.append(text)
        length += extra
    if texts:
        packed.append((names, "\n\n".join(texts)))
    return packed


def build_document_chunks(path: Path, split_text) -> list[dict]:
    metadata, body = parse_markdown(path)
    document_id = metadata["document_id"]
    chunks: list[dict] = []
    prev_text = ""
    for seq, (names, block) in enumerate(pack_blocks(split_sections(body), split_text)):
        text = block if seq == 0 else prev_text[-OVERLAP:] + "\n\n" + block
        chunks.append(
            {
                "chunk_id": f"{document_id}::c{seq:02d}",
                "document_id": document_id,
                "title": metadata["title"],
                "module": metadata["module"],
                "status": metadata["status"],
                "seq": seq,
                "section_path": names,
                "source_files": metadata["source_files"],
                "text": text,
                "char_count": len(text),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            }
        )
        prev_text = text
    return chunks


def build_all_chunks() -> list[dict]:
    split_text = _load_project_split_text()
    chunks: list[dict] = []
    for path in sorted(CORPUS_ROOT.rglob("*.md")):
        chunks.extend(build_document_chunks(path, split_text))
    return chunks


def main() -> None:
    chunks = build_all_chunks()
    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    sizes = [chunk["char_count"] for chunk in chunks]
    documents = {chunk["document_id"] for chunk in chunks}
    print(f"documents: {len(documents)}")
    print(f"chunks: {len(chunks)}")
    print(f"chars per chunk: min={min(sizes)} mean={sum(sizes) // len(sizes)} max={max(sizes)}")
    print(f"output: {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
