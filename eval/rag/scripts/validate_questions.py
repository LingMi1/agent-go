#!/usr/bin/env python3
"""校验 RAG 检索评测集 questions.jsonl（仅标准库，无第三方依赖）。

硬性检查（任一失败即退出码 1）：
 1. 记录 schema 完整、无多余字段，ID 唯一；
 2. question_type/difficulty 为合法枚举，各题型数量与设计分布一致；
 3. gold_document_ids 均存在于 corpus_manifest.json，gold_chunk_ids 均存在于 chunks.jsonl，
    且 gold chunk 的所属文档集合 == gold_document_ids；
 4. evidence quote 必须逐字存在于其标注 chunk 的文本中，evidence chunk 必须列入 gold；
 5. gold_chunk_ids 必须恰好等于「全库中包含任一 evidence quote 的 chunk」集合
    （防 overlap 接头或跨文档撞句导致漏标/多标）；
 6. required_facts 均存在于 manifest，且其所属文档在 gold_document_ids 内；
    可回答题至少 1 条 fact；
 7. multi_hop 至少 2 个 gold chunk 且至少 2 篇 gold 文档；
 8. unanswerable 的 gold/evidence/required_facts 全为空；
 9. 可回答题必须有非空 answer 和至少 1 条 evidence；
10. chunks.jsonl 与当前语料重建结果逐字节一致（分块可复现，gold 未过期）。

软性警告（不影响退出码）：
 - 题面与 gold chunk 存在过长的逐字重合（疑似复制原文表述）；
 - 含 LangGraph/RAG/RRF/Qdrant 等显眼关键词的题面占比超过 20%。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
RAG_ROOT = SCRIPT_PATH.parents[1]
QUESTIONS_PATH = RAG_ROOT / "questions.jsonl"
CHUNKS_PATH = RAG_ROOT / "chunks.jsonl"
MANIFEST_PATH = RAG_ROOT / "corpus_manifest.json"

REQUIRED_KEYS = {
    "id", "question", "answer", "gold_document_ids", "gold_chunk_ids",
    "evidence_spans", "question_type", "difficulty", "required_facts", "notes",
}
QUESTION_TYPES = {
    "single_hop", "paraphrase", "multi_hop",
    "disambiguation", "troubleshooting", "unanswerable",
}
DIFFICULTIES = {"easy", "medium", "hard"}
EXPECTED_TYPE_COUNTS = {
    "single_hop": 18,
    "paraphrase": 12,
    "multi_hop": 10,
    "disambiguation": 8,
    "troubleshooting": 6,
    "unanswerable": 6,
}
MIN_QUOTE_CHARS = 8
OBVIOUS_KEYWORDS = ("LangGraph", "RAG", "RRF", "Qdrant", "ReAct", "Agentic")
QUESTION_COPY_NGRAM = 12  # 题面与原文连续重合超过该长度视为疑似照抄


def load_jsonl(path: Path) -> list[dict]:
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"ERROR: {path.name}:{line_no} JSON 非法: {exc}")
    return records


def check_chunks_reproducible(errors: list[str]) -> None:
    spec = importlib.util.spec_from_file_location(
        "_build_chunks", SCRIPT_PATH.parent / "build_chunks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rebuilt = module.build_all_chunks()
    on_disk = load_jsonl(CHUNKS_PATH)
    if rebuilt != on_disk:
        errors.append(
            "chunks.jsonl 与当前语料重建结果不一致：语料已变更或文件被手改，请重跑 build_chunks.py"
        )


def main() -> None:
    errors: list[str] = []
    warnings: list[str] = []

    chunks = {c["chunk_id"]: c for c in load_jsonl(CHUNKS_PATH)}
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_docs = {d["document_id"] for d in manifest["documents"]}
    fact_to_doc = {
        f["fact_id"]: d["document_id"]
        for d in manifest["documents"]
        for f in d.get("facts", [])
    }
    questions = load_jsonl(QUESTIONS_PATH)

    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    type_counts: Counter = Counter()
    keyword_hits = 0

    for q in questions:
        qid = q.get("id", "<no-id>")
        keys = set(q)
        if keys != REQUIRED_KEYS:
            errors.append(f"{qid}: 字段缺失或多余: -{sorted(REQUIRED_KEYS - keys)} +{sorted(keys - REQUIRED_KEYS)}")
            continue
        if qid in seen_ids:
            errors.append(f"{qid}: ID 重复")
        seen_ids.add(qid)
        if q["question"] in seen_questions:
            errors.append(f"{qid}: 题面重复")
        seen_questions.add(q["question"])

        if q["question_type"] not in QUESTION_TYPES:
            errors.append(f"{qid}: question_type 非法: {q['question_type']!r}")
            continue
        type_counts[q["question_type"]] += 1
        if q["difficulty"] not in DIFFICULTIES:
            errors.append(f"{qid}: difficulty 非法: {q['difficulty']!r}")
        if not str(q["question"]).strip() or not str(q["answer"]).strip():
            errors.append(f"{qid}: question/answer 为空")

        gold_docs = q["gold_document_ids"]
        gold_chunks = q["gold_chunk_ids"]
        spans = q["evidence_spans"]
        facts = q["required_facts"]

        for doc_id in gold_docs:
            if doc_id not in manifest_docs:
                errors.append(f"{qid}: gold 文档不存在于 manifest: {doc_id}")
        for chunk_id in gold_chunks:
            if chunk_id not in chunks:
                errors.append(f"{qid}: gold chunk 不存在: {chunk_id}")

        if q["question_type"] == "unanswerable":
            if gold_docs or gold_chunks or spans or facts:
                errors.append(f"{qid}: unanswerable 的 gold/evidence/required_facts 必须全为空")
            continue

        # ---- 以下均为可回答题 ----
        if not gold_docs or not gold_chunks:
            errors.append(f"{qid}: 可回答题必须有 gold 文档与 gold chunk")
        if not spans:
            errors.append(f"{qid}: 可回答题必须有 evidence")
        if not facts:
            errors.append(f"{qid}: 可回答题必须至少引用 1 条 manifest fact")

        docs_of_gold_chunks = {
            chunks[cid]["document_id"] for cid in gold_chunks if cid in chunks
        }
        if docs_of_gold_chunks != set(gold_docs):
            errors.append(
                f"{qid}: gold_document_ids 与 gold chunk 所属文档不一致: "
                f"{sorted(set(gold_docs))} != {sorted(docs_of_gold_chunks)}"
            )

        quotes: list[str] = []
        for span in spans:
            chunk_id, quote = span.get("chunk_id"), span.get("quote", "")
            if len(quote) < MIN_QUOTE_CHARS:
                errors.append(f"{qid}: quote 过短（<{MIN_QUOTE_CHARS} 字符）: {quote!r}")
            if chunk_id not in chunks:
                errors.append(f"{qid}: evidence chunk 不存在: {chunk_id}")
                continue
            if chunk_id not in gold_chunks:
                errors.append(f"{qid}: evidence chunk 未列入 gold: {chunk_id}")
            if quote not in chunks[chunk_id]["text"]:
                errors.append(f"{qid}: quote 不是 chunk {chunk_id} 的逐字子串: {quote[:40]}…")
            quotes.append(quote)

        containing = {
            cid for cid, chunk in chunks.items()
            if any(quote and quote in chunk["text"] for quote in quotes)
        }
        if containing != set(gold_chunks):
            errors.append(
                f"{qid}: gold_chunk_ids 与实际包含 quote 的 chunk 集合不一致: "
                f"gold={sorted(gold_chunks)} 实际={sorted(containing)}"
            )

        covered = {
            cid for cid in gold_chunks
            if cid in chunks and any(quote in chunks[cid]["text"] for quote in quotes)
        }
        if set(gold_chunks) - covered:
            errors.append(f"{qid}: gold chunk 无任何 quote 覆盖: {sorted(set(gold_chunks) - covered)}")

        for fact_id in facts:
            if fact_id not in fact_to_doc:
                errors.append(f"{qid}: required_fact 不存在于 manifest: {fact_id}")
            elif fact_to_doc[fact_id] not in gold_docs:
                errors.append(f"{qid}: required_fact 所属文档不在 gold 内: {fact_id}")

        if q["question_type"] == "multi_hop":
            if len(gold_chunks) < 2:
                errors.append(f"{qid}: multi_hop 至少需要 2 个 gold chunk")
            if len(set(gold_docs)) < 2:
                errors.append(f"{qid}: multi_hop 至少需要 2 篇 gold 文档")

        # 软性：题面照抄原文检测（连续 n-gram 命中 gold chunk）
        question_text = q["question"]
        for cid in gold_chunks:
            if cid not in chunks:
                continue
            text = chunks[cid]["text"]
            if any(
                question_text[i : i + QUESTION_COPY_NGRAM] in text
                for i in range(0, max(1, len(question_text) - QUESTION_COPY_NGRAM))
            ):
                warnings.append(f"{qid}: 题面与 {cid} 存在 ≥{QUESTION_COPY_NGRAM} 字连续重合，疑似复用原文表述")
                break

        if any(k.lower() in question_text.lower() for k in OBVIOUS_KEYWORDS):
            keyword_hits += 1

    if len(questions) != sum(EXPECTED_TYPE_COUNTS.values()):
        errors.append(f"总题数 {len(questions)} != {sum(EXPECTED_TYPE_COUNTS.values())}")
    for qtype, expected in EXPECTED_TYPE_COUNTS.items():
        if type_counts.get(qtype, 0) != expected:
            errors.append(f"题型 {qtype} 数量 {type_counts.get(qtype, 0)} != {expected}")

    if keyword_hits > len(questions) * 0.2:
        warnings.append(f"{keyword_hits} 条题面含显眼关键词（>20%），削弱检索评测区分度")

    check_chunks_reproducible(errors)

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)

    difficulty_counts = Counter(q["difficulty"] for q in questions)
    print("Questions validation passed")
    print(f"questions: {len(questions)}")
    print("by type: " + ", ".join(f"{t}={type_counts[t]}" for t in EXPECTED_TYPE_COUNTS))
    print("by difficulty: " + ", ".join(f"{d}={difficulty_counts[d]}" for d in ("easy", "medium", "hard")))
    print(f"gold chunks referenced: {len({cid for q in questions for cid in q['gold_chunk_ids']})}/{len(chunks)}")
    print(f"gold documents referenced: {len({d for q in questions for d in q['gold_document_ids']})}/{len(manifest_docs)}")


if __name__ == "__main__":
    main()
