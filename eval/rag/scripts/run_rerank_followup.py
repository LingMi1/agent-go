#!/usr/bin/env python3
"""Rerank 跟进实验（基线 results 之上的增量评测，不改生产代码）。

两个配置：
- hybrid_rrf_rerank（实跑）：生产 Retriever 混合检索 top-10 → 生产 ApiReranker
  （SiliconFlow bge-reranker-v2-m3）打分 → 生产 order_by_score 重排。运行 3 次。
- full_agentic_rag_rerank_offline（离线重排）：rerank 在生产子图中是 reflect 循环结束后的
  纯后处理节点（graph.py rerank_node），不影响 route/expand/reflect 行为；因此对基线运行
  已保存的每题累计检索结果（per_question.jsonl 的 ranked 全列表）离线执行同一 rerank
  逻辑，与图内开启 rerank 的输出逐位等价，无需重烧 LLM。基线 3 次运行 → 3 组重排结果。

阈值消融：同一批 API 打分零成本计算 threshold=0.0（deploy/.env 生产值，纯重排）与
threshold=0.3（config.py 注释建议值，过滤低分证据）两档，后者用于观察不可回答题的
拒答（sources 为空 → 生成节点回退直答、不引用）。

用法：
  cognition/.venv/bin/python eval/rag/scripts/run_rerank_followup.py \
      --baseline eval/rag/results/20260711T093300Z [--limit N]
需要环境变量 SILICONFLOW_API_KEY（不落盘、不入 config.json）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RAG_ROOT = SCRIPT_DIR.parent
REPO_ROOT = RAG_ROOT.parents[1]
sys.path.insert(0, str(REPO_ROOT / "cognition"))
sys.path.insert(0, str(SCRIPT_DIR))

import eval_metrics  # noqa: E402
from run_retrieval_eval import (  # noqa: E402 — 复用主 harness 的固定项
    KB_ID, TOP_K, EMBED_MODEL, EMBED_DIM, SPARSE_MODEL, COLLECTION,
    build_settings, ingest_corpus, load_jsonl, sha256_file,
)

from cognition.rag.factory import build_embedder, build_reranker, build_sparse  # noqa: E402
from cognition.rag.rerank import order_by_score  # noqa: E402
from cognition.rag.retriever import Retriever  # noqa: E402
from cognition.rag.store import QdrantStore  # noqa: E402

RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
THRESHOLDS = (0.0, 0.3)  # 0.0=deploy/.env 生产值（纯重排）；0.3=config.py 注释建议值


def scored_rerank(reranker, query: str, chunk_ids: list[str], texts: list[str]):
    """调用生产 ApiReranker 打分（带瞬时错误重试），返回 (scores, latency_s)。"""
    if not chunk_ids:
        return [], 0.0
    t0 = time.perf_counter()
    last = None
    for attempt in range(3):
        try:
            scores = reranker.score(query, texts)
            return scores, time.perf_counter() - t0
        except Exception as exc:  # noqa: BLE001 — API 瞬时错误重试，不丢题
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"rerank 三次失败: {last}") from last


def apply_threshold(chunk_ids, texts, scores, threshold):
    """生产 order_by_score 语义（分降序、>threshold 过滤、截 top-k），返回 chunk_id 列表。"""
    docs = [{"dedup_key": cid, "text": t, "score": 0.0} for cid, t in zip(chunk_ids, texts)]
    ranked = order_by_score(docs, scores, threshold=threshold, top_k=TOP_K)
    return [d["dedup_key"] for d in ranked], [d["score"] for d in ranked]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default=str(RAG_ROOT / "results" / "20260711T093300Z"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    baseline_dir = Path(args.baseline)
    baseline_metrics = json.loads((baseline_dir / "metrics.json").read_text(encoding="utf-8"))
    baseline_cfg = json.loads((baseline_dir / "config.json").read_text(encoding="utf-8"))

    started = datetime.now(timezone.utc)
    out_dir = RAG_ROOT / "results" / (started.strftime("%Y%m%dT%H%M%SZ") + "_rerank")
    out_dir.mkdir(parents=True, exist_ok=True)

    questions = load_jsonl(RAG_ROOT / "questions_verified.jsonl")
    if args.limit:
        questions = questions[:args.limit]
    qmap = {q["id"]: q for q in questions}
    chunks = load_jsonl(RAG_ROOT / "chunks.jsonl")
    text_of = {c["chunk_id"]: c["text"] for c in chunks}

    settings = build_settings()
    # 评测侧覆写：真实 reranker（生产 factory 构建；key 从环境读，不写任何输出文件）
    settings.rerank_provider = "siliconflow"
    assert settings.rerank_api_key, "缺 SILICONFLOW_API_KEY 环境变量"
    reranker = build_reranker(settings)
    embedder = build_embedder(settings)
    sparse = build_sparse(settings)
    store = QdrantStore.from_settings(settings)

    print(f"ingesting {len(chunks)} chunks (确定性重建) ...", flush=True)
    n = ingest_corpus(store, embedder, sparse, chunks)
    assert n == len(chunks)
    retriever = Retriever(store, embedder, sparse, top_k=TOP_K)

    all_records: list[dict] = []

    # ---- 配置 1：hybrid_rrf_rerank（实跑 3 次）----
    for run_idx in range(1, 4):
        print(f"=== hybrid_rrf_rerank run{run_idx} ===", flush=True)
        for q in questions:
            t0 = time.perf_counter()
            docs = retriever.retrieve([q["question"]], kb_id=KB_ID, top_k=TOP_K)
            cids = [d["dedup_key"] for d in docs]
            texts = [text_of[c] for c in cids]
            scores, rlat = scored_rerank(reranker, q["question"], cids, texts)
            total = time.perf_counter() - t0
            rec_base = {"config": "hybrid_rrf_rerank", "run": run_idx, "qid": q["id"],
                        "question_type": q["question_type"], "latency_s": round(total, 4),
                        "rerank_latency_s": round(rlat, 4), "candidates": cids,
                        "rerank_scores": [round(s, 6) for s in scores]}
            for th in THRESHOLDS:
                ranked, rscores = apply_threshold(cids, texts, scores, th)
                rec = dict(rec_base)
                rec["config"] = ("hybrid_rrf_rerank" if th == 0.0
                                 else "hybrid_rrf_rerank_t03")
                rec["ranked"] = ranked
                rec["evidence_nonempty"] = bool(ranked[:5])
                all_records.append(rec)

    # ---- 配置 2：full_agentic_rag_rerank_offline（基线 3 次运行离线重排）----
    base_pq = [json.loads(l) for l in (baseline_dir / "per_question.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    d_recs = [r for r in base_pq if r["config"] == "full_agentic_rag"
              and r["qid"] in qmap]
    print(f"=== full_agentic_rag_rerank_offline: 重排基线 {len(d_recs)} 条记录 ===", flush=True)
    score_cache: dict[tuple, tuple] = {}  # (qid, tuple(cids)) -> (scores, lat)
    for r in d_recs:
        q = qmap[r["qid"]]
        cids = r["ranked"]
        texts = [text_of[c] for c in cids]
        key = (r["qid"], tuple(cids))
        if key not in score_cache:
            score_cache[key] = scored_rerank(reranker, q["question"], cids, texts)
        scores, rlat = score_cache[key]
        for th in THRESHOLDS:
            ranked, _ = apply_threshold(cids, texts, scores, th)
            all_records.append({
                "config": ("full_agentic_rag_rerank_offline" if th == 0.0
                           else "full_agentic_rag_rerank_offline_t03"),
                "run": r["run"], "qid": r["qid"], "question_type": r["question_type"],
                # 估算端到端延迟 = 基线子图延迟 + 本次实测 rerank 调用延迟（报告注明）
                "latency_s": round(r["latency_s"] + rlat, 4),
                "rerank_latency_s": round(rlat, 4),
                "baseline_latency_s": r["latency_s"],
                "ranked": ranked,
                "evidence_nonempty": bool(ranked[:5]),
            })
        if r["run"] == 1:
            print(f"  {r['qid']} reranked ({len(cids)} docs)", flush=True)

    # ---- 指标 ----
    metrics: dict = {}
    configs = ["hybrid_rrf_rerank", "hybrid_rrf_rerank_t03",
               "full_agentic_rag_rerank_offline", "full_agentic_rag_rerank_offline_t03"]
    for config in configs:
        run_ids = sorted({r["run"] for r in all_records if r["config"] == config})
        run_metrics, run_hashes = [], []
        for run_idx in run_ids:
            recs = [r for r in all_records if r["config"] == config and r["run"] == run_idx]
            run_metrics.append(eval_metrics.compute_run_metrics(recs, qmap))
            run_hashes.append(hashlib.sha256(
                json.dumps([r["ranked"] for r in recs]).encode()).hexdigest()[:16])
        numeric_keys = [k for k, v in run_metrics[0].items() if isinstance(v, (int, float))]
        metrics[config] = {
            "runs": run_metrics, "n_runs": len(run_ids),
            "runs_identical": len(set(run_hashes)) == 1,
            "run_result_hashes": run_hashes,
            "mean": {k: eval_metrics.mean([m[k] for m in run_metrics]) for k in numeric_keys},
            "per_type_recall_at_5_mean": {
                t: eval_metrics.mean([m["per_type_recall_at_5"][t] for m in run_metrics])
                for t in run_metrics[0]["per_type_recall_at_5"]},
        }

    with (out_dir / "per_question.jsonl").open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    failures = []
    for config in configs:
        for r in all_records:
            if r["config"] != config or r["run"] != 1:
                continue
            q = qmap[r["qid"]]
            if q["question_type"] != "unanswerable":
                if not eval_metrics.hit_at_k(r["ranked"], set(q["gold_chunk_ids"]), 5):
                    failures.append({"config": config, "qid": r["qid"],
                                     "type": q["question_type"],
                                     "failure": "recall_at_5_miss",
                                     "question": q["question"],
                                     "gold_groups": q["gold_groups"],
                                     "top10": r["ranked"][:10]})
            elif r.get("evidence_nonempty"):
                failures.append({"config": config, "qid": r["qid"], "type": "unanswerable",
                                 "failure": "false_retrieval", "question": q["question"],
                                 "top5": r["ranked"][:5]})
    with (out_dir / "failures.jsonl").open("w", encoding="utf-8") as f:
        for r in failures:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    finished = datetime.now(timezone.utc)
    git_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                                capture_output=True, text=True).stdout.strip()
    config_json = {
        "started_utc": started.isoformat(), "finished_utc": finished.isoformat(),
        "duration_s": round((finished - started).total_seconds(), 1),
        "git_commit": git_commit,
        "baseline_results": baseline_dir.name,
        "baseline_git_commit": baseline_cfg["git_commit"],
        "questions_sha256": sha256_file(RAG_ROOT / "questions_verified.jsonl"),
        "chunks_sha256": sha256_file(RAG_ROOT / "chunks.jsonl"),
        "collection": COLLECTION, "kb_id": KB_ID, "top_k": TOP_K,
        "embedding_model": EMBED_MODEL, "embedding_dimension": EMBED_DIM,
        "sparse_model": SPARSE_MODEL,
        "rerank_model": RERANK_MODEL,
        "rerank_base_url": settings.rerank_base_url,
        "rerank_thresholds": list(THRESHOLDS),
        "notes": [
            "hybrid_rrf_rerank：生产 Retriever top-10 → ApiReranker → order_by_score，实跑 3 次",
            "full_agentic_rag_rerank_offline：对基线已保存的累计检索结果离线执行图内同一 "
            "rerank 后处理（rerank 不影响 reflect 循环，逐位等价）；其延迟=基线子图延迟"
            "+实测 rerank 调用延迟（估算口径）",
            "_t03 后缀为 threshold=0.3 消融（config.py 注释建议值）；主口径 threshold=0.0 "
            "与 deploy/.env 一致",
            "rerank API key 仅从环境变量读取，不写入任何输出文件",
        ],
    }
    (out_dir / "config.json").write_text(
        json.dumps(config_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    render_report(out_dir, config_json, metrics, baseline_metrics, failures, qmap)
    print(f"done → {out_dir}")


def render_report(out_dir, cfg, metrics, base, failures, qmap) -> None:
    """渲染对比报告（数字全部来自 metrics/baseline metrics）。"""
    L: list[str] = []
    w = L.append

    def bm(c, k):
        return base[c]["mean"].get(k, float("nan"))

    def fm(c, k):
        return metrics[c]["mean"].get(k, float("nan"))

    def p(x, d=1):
        return f"{x * 100:.{d}f}%"

    n_ans = fm("hybrid_rrf_rerank", "n_answerable")
    w(f"# Rerank 跟进实验报告（{out_dir.name}）")
    w("")
    w(f"基线：`{cfg['baseline_results']}`（四配置，rerank 关闭=生产默认）。本实验验证开启生产"
      f"已实现的 rerank（`{cfg['rerank_model']}` via SiliconFlow）对检索质量的影响，"
      "**未修改任何生产代码**，仅改配置与评测脚本。指标口径、题目集、语料、embedding 与"
      "基线完全一致。")
    w("")
    w("## 主结果（threshold=0.0，与 deploy/.env 一致：纯重排不过滤）")
    w("")
    rows = [
        ("Recall@1", "recall_at_1", True), ("Recall@3", "recall_at_3", True),
        ("Recall@5", "recall_at_5", True), ("Recall@10", "recall_at_10", True),
        ("MRR@10", "mrr_at_10", False),
        ("多跳 Any Recall@5", "multi_hop_any_recall_at_5", True),
        ("多跳 All Recall@5", "multi_hop_all_recall_at_5", True),
        ("多跳 All Recall@10", "multi_hop_all_recall_at_10", True),
        ("不可回答误检率", "unanswerable_false_retrieval_rate", True),
        ("平均延迟(s)", "latency_mean_s", False), ("P95 延迟(s)", "latency_p95_s", False),
    ]
    w("| 指标 | C 基线 hybrid_rrf | **C+rerank** | D 基线 agentic | **D+rerank(离线)** |")
    w("|---|---|---|---|---|")
    for label, k, is_pct in rows:
        def v(x):
            return p(x) if is_pct else f"{x:.3f}"
        w(f"| {label} | {v(bm('hybrid_rrf', k))} | **{v(fm('hybrid_rrf_rerank', k))}** | "
          f"{v(bm('full_agentic_rag', k))} | **{v(fm('full_agentic_rag_rerank_offline', k))}** |")
    w("")
    for c in ("hybrid_rrf_rerank", "full_agentic_rag_rerank_offline"):
        w(f"- {c}：{metrics[c]['n_runs']} 次运行，结果哈希"
          + ("一致" if metrics[c]["runs_identical"] else "不一致（均值）") + "；")
    w("- D+rerank 延迟为估算口径：基线子图延迟 + 本次实测 rerank 调用延迟"
      f"（rerank 单次均值 "
      f"{statistics.mean([r.get('rerank_latency_s', 0) for m in metrics['full_agentic_rag_rerank_offline']['runs'] for r in []] or [fm('full_agentic_rag_rerank_offline', 'latency_mean_s') - bm('full_agentic_rag', 'latency_mean_s')]):.3f}s）。")
    w("")
    w("**分题型 Recall@5**：")
    w("")
    types = sorted(metrics["hybrid_rrf_rerank"]["per_type_recall_at_5_mean"])
    w("| 题型 | C 基线 | C+rerank | D 基线 | D+rerank |")
    w("|---|---|---|---|---|")
    for t in types:
        w(f"| {t} | {p(base['hybrid_rrf']['per_type_recall_at_5_mean'][t])} | "
          f"{p(metrics['hybrid_rrf_rerank']['per_type_recall_at_5_mean'][t])} | "
          f"{p(base['full_agentic_rag']['per_type_recall_at_5_mean'][t])} | "
          f"{p(metrics['full_agentic_rag_rerank_offline']['per_type_recall_at_5_mean'][t])} |")
    w("")
    w("## threshold=0.3 消融（config.py 注释建议值：过滤低分证据，观察拒答）")
    w("")
    w("| 指标 | C+rerank t0.3 | D+rerank t0.3 |")
    w("|---|---|---|")
    for label, k, is_pct in rows:
        def v(x):
            return p(x) if is_pct else f"{x:.3f}"
        w(f"| {label} | {v(fm('hybrid_rrf_rerank_t03', k))} | "
          f"{v(fm('full_agentic_rag_rerank_offline_t03', k))} |")
    w("")
    fs = {c: [f for f in failures if f["config"] == c and f["failure"] == "recall_at_5_miss"]
          for c in metrics}
    w("## 失败案例（run1，Recall@5 未命中）")
    w("")
    for c in ("hybrid_rrf_rerank", "full_agentic_rag_rerank_offline"):
        w(f"### {c}：未命中 {len(fs[c])} 题")
        w("")
        for f in fs[c][:3]:
            gold_flat = sorted({cid for g in f["gold_groups"] for cid in g})
            w(f"- `{f['qid']}`（{f['type']}）：{f['question']}")
            w(f"  - gold: {', '.join('`' + g + '`' for g in gold_flat[:4])}")
            w(f"  - top-5: {', '.join('`' + t + '`' for t in f['top10'][:5])}")
        w("")

    # 结论（条件生成）
    w("## 结论与可安全写入简历的表述（更新版，替代基线报告 §9 中被禁写的条目）")
    w("")
    concl: list[str] = []
    c5b, c5r = bm("hybrid_rrf", "recall_at_5"), fm("hybrid_rrf_rerank", "recall_at_5")
    d5b, d5r = bm("full_agentic_rag", "recall_at_5"), fm("full_agentic_rag_rerank_offline", "recall_at_5")
    mb, mr = bm("hybrid_rrf", "mrr_at_10"), fm("hybrid_rrf_rerank", "mrr_at_10")
    dmb, dmr = bm("full_agentic_rag", "mrr_at_10"), fm("full_agentic_rag_rerank_offline", "mrr_at_10")
    mh_b = bm("full_agentic_rag", "multi_hop_all_recall_at_5")
    mh_r = fm("full_agentic_rag_rerank_offline", "multi_hop_all_recall_at_5")
    if c5r > c5b + 1e-9:
        concl.append(f"✅ “混合检索 + bge-reranker-v2-m3 精排：Recall@5 从 {p(c5b)} 提升至 "
                     f"{p(c5r)}、MRR@10 从 {mb:.3f} 提升至 {mr:.3f}"
                     f"（自建 {n_ans:.0f} 条可回答中文评测集，3 次运行均值）”")
    elif mr > mb + 0.03:
        concl.append(f"✅ “rerank 未改变 Recall@5（{p(c5r)}），但 MRR@10 从 {mb:.3f} 提升至 "
                     f"{mr:.3f}”——排序质量口径，须如实注明 Recall 持平")
    if d5r > max(d5b, c5b) + 1e-9:
        concl.append(f"✅ “Agentic RAG（子查询扩展 + Reflect 重检索 + 精排）：Recall@5 从单轮"
                     f"混合检索的 {p(c5b)} 提升至 {p(d5r)}（评测集同上；对比未开精排的 "
                     f"agentic {p(d5b)}，精排修复了扩展检索的排序损失）”")
    elif d5r > d5b + 1e-9:
        concl.append(f"✅ “精排将 agentic 检索 Recall@5 从 {p(d5b)} 修复至 {p(d5r)}，"
                     f"但仍{'低于' if d5r < c5b - 1e-9 else '不高于'}单轮混合检索基线 "
                     f"{p(c5b)}”——只能作为失败归因叙述，不能写成端到端提升")
    if mh_r > mh_b + 0.1:
        concl.append(f"✅ “多跳 All Recall@5 从 {p(mh_b)}（未精排 agentic）提升至 {p(mh_r)}”"
                     f"（10 条多跳题×3 次运行；基线单轮混合检索为 "
                     f"{p(bm('hybrid_rrf', 'multi_hop_all_recall_at_5'))}）")
    fr03 = fm("full_agentic_rag_rerank_offline_t03", "unanswerable_false_retrieval_rate")
    if fr03 < 1.0 - 1e-9:
        concl.append(f"✅ “rerank 阈值过滤（0.3）将不可回答问题误检率从 100% 降至 {p(fr03)}”"
                     "（6 条不可回答题，附 threshold 对 Recall 的代价见消融表）")
    if not concl:
        concl.append("❌ rerank 在本评测集上未产生任何可写指标提升——如实记录，不得写入简历")
    for s in concl:
        w(f"- {s}")
    w("")
    w("仍然禁写：任何不带“自建 59 题/58 chunk 评测集、3 次运行”限定的绝对数字；"
      "“大规模/生产流量验证”。基线报告 §8 的数据集局限全部适用。")
    w("")
    (out_dir / "REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
