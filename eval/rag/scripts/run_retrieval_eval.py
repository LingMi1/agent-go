#!/usr/bin/env python3
"""RAG 检索评测 harness：dense_only / sparse_only / hybrid_rrf / full_agentic_rag 四配置。

复用生产代码（cognition 包）：
- QdrantStore（同名向量 schema、每路 Prefetch 带 kb filter、原生 Fusion.RRF）；
- Retriever（多查询混合检索 + dedup_docs 并集去重）；
- build_embedder / build_sparse（fastembed bge-small-zh-v1.5 + Qdrant/bm25，与 deploy/.env 一致）；
- build_rag_subgraph（route→expand→hybrid_retrieve→reflect→rerank→generate 完整子图 + DeepSeek）。

dense_only / sparse_only 是消融配置：生产只暴露混合查询，这两条用同一 client/collection/filter
各查单路（qdrant query_points 单向量），除融合方式外与生产路径逐参一致。

固定项：chunks.jsonl 预分块语料（不重新分块，dedup_key=chunk_id 使检索结果直接映射 gold）、
embedding 模型与维度、top_k=10、prefetch=20、reflection_limit=2、subquery_max=3、temperature=0、
PYTHONHASHSEED=0。评测语料写入独立 collection，不触碰生产 cognition_docs。

用法：
  cognition/.venv/bin/python eval/rag/scripts/run_retrieval_eval.py \
      --configs dense_only sparse_only hybrid_rrf full_agentic_rag --runs 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RAG_ROOT = SCRIPT_DIR.parent
REPO_ROOT = RAG_ROOT.parents[1]
sys.path.insert(0, str(REPO_ROOT / "cognition"))
sys.path.insert(0, str(SCRIPT_DIR))

from qdrant_client import models  # noqa: E402

from cognition.config import Settings  # noqa: E402
from cognition.rag import prompts, reflect as reflect_mod  # noqa: E402
from cognition.rag.factory import build_embedder, build_sparse  # noqa: E402
from cognition.rag.graph import build_rag_subgraph  # noqa: E402
from cognition.rag.retriever import Retriever  # noqa: E402
from cognition.rag.store import DENSE_VECTOR, SPARSE_VECTOR, QdrantStore  # noqa: E402

import eval_metrics  # noqa: E402

KB_ID = "eval_rag_v1"
COLLECTION = "eval_rag_bench_v1"
TOP_K = 10
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
EMBED_DIM = 512
SPARSE_MODEL = "Qdrant/bm25"
DETERMINISTIC_CONFIGS = {"dense_only", "sparse_only", "hybrid_rrf"}
_STABLE_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # 与生产 ingest 相同命名空间


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def build_settings() -> Settings:
    return Settings(
        embedding_provider="fastembed",
        embedding_model=EMBED_MODEL,
        embedding_dimension=EMBED_DIM,
        sparse_provider="fastembed",
        qdrant_url="http://localhost:6333",
        qdrant_collection=COLLECTION,
        rag_top_k=TOP_K,
        rag_rerank_top_k=TOP_K,
        rag_prefetch_limit=20,
        rag_reflection_limit=2,
        rag_subquery_max=3,
        rerank_enabled=False,
        model_provider="deepseek",
    )


def ingest_corpus(store: QdrantStore, embedder, sparse, chunks: list[dict]) -> int:
    """固定 chunk 入库：不重新分块；payload 与生产 schema 同形，dedup_key=chunk_id。"""
    client = store._c  # noqa: SLF001 — 评测需要重建集合，生产 store 未暴露该操作
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    store.ensure_collection()
    texts = [c["text"] for c in chunks]
    dense_vecs = embedder.embed(texts)
    sparse_vecs = sparse.embed(texts)
    now = int(time.time())
    points = []
    for c, dv, (si, sv) in zip(chunks, dense_vecs, sparse_vecs):
        points.append(models.PointStruct(
            id=str(uuid.uuid5(_STABLE_NS, f"{KB_ID}|{c['chunk_id']}")),
            vector={DENSE_VECTOR: dv,
                    SPARSE_VECTOR: models.SparseVector(indices=si, values=sv)},
            payload={"kb_id": KB_ID, "text": c["text"], "source_id": c["document_id"],
                     "file_name": f"{c['document_id']}.md", "chunk_index": c["seq"],
                     "dedup_key": c["chunk_id"], "chunk_type": "text", "image_url": None,
                     "created": now},
        ))
    store.upsert(points)
    return int(client.count(COLLECTION, exact=True).count)


def kb_filter() -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key="kb_id",
                                                     match=models.MatchValue(value=KB_ID))])


class SingleVectorRetriever:
    """消融配置：单路（dense 或 sparse）查询，复用同一 client/collection/filter。"""

    def __init__(self, client, embedder, sparse, mode: str):
        self._c, self._embedder, self._sparse, self._mode = client, embedder, sparse, mode

    def query(self, question: str, top_k: int) -> list[tuple[str, float]]:
        if self._mode == "dense":
            qv = self._embedder.embed([question])[0]
            using = DENSE_VECTOR
        else:
            si, sv = self._sparse.embed([question])[0]
            qv = models.SparseVector(indices=si, values=sv)
            using = SPARSE_VECTOR
        res = self._c.query_points(COLLECTION, query=qv, using=using, limit=top_k,
                                   query_filter=kb_filter(), with_payload=True)
        return [(str(p.payload["dedup_key"]), float(p.score or 0.0)) for p in res.points]


class InstrumentedModel:
    """包装 DeepSeek：按提示词前缀分类每次 LLM 调用，记录延迟与 Reflect 判定。"""

    _PREFIXES = [
        ("route", prompts.ROUTE_PROMPT.split("{query}")[0][:16]),
        ("expand", prompts.EXPAND_PROMPT.split("{max}")[0][:12]),
        ("reflect", prompts.REFLECT_PROMPT.split("{evidence}")[0][:12]),
        ("answer", prompts.ANSWER_PROMPT.split("{context}")[0][:16]),
        ("direct", prompts.DIRECT_PROMPT.split("{query}")[0][:12]),
    ]

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[dict] = []
        self.retries = 0

    def reset(self):
        self.calls, self.retries = [], 0

    def _classify(self, prompt: str) -> str:
        for kind, prefix in self._PREFIXES:
            if prompt.startswith(prefix):
                return kind
        return "other"

    def invoke(self, prompt):
        kind = self._classify(str(prompt))
        t0 = time.perf_counter()
        last_exc = None
        for attempt in range(3):
            try:
                resp = self._inner.invoke(prompt)
                break
            except Exception as exc:  # noqa: BLE001 — API 瞬时错误重试，不丢题
                last_exc = exc
                self.retries += 1
                time.sleep(2.0 * (attempt + 1))
        else:
            raise RuntimeError(f"LLM 调用三次失败({kind}): {last_exc}") from last_exc
        rec = {"kind": kind, "latency_s": round(time.perf_counter() - t0, 4)}
        if kind == "reflect":
            text = resp.content if hasattr(resp, "content") else str(resp)
            is_answer, rewrite = reflect_mod.parse_reflection(text)
            rec.update(is_answer=is_answer, rewrite=rewrite)
        self.calls.append(rec)
        return resp


def run_config(config: str, questions: list[dict], ctx: dict, run_idx: int) -> list[dict]:
    records = []
    for q in questions:
        qid, question = q["id"], q["question"]
        rec = {"config": config, "run": run_idx, "qid": qid,
               "question_type": q["question_type"]}
        if config in ("dense_only", "sparse_only"):
            r = ctx[config]
            t0 = time.perf_counter()
            ranked_scored = r.query(question, TOP_K)
            rec["latency_s"] = round(time.perf_counter() - t0, 4)
            rec["ranked"] = [c for c, _ in ranked_scored]
            rec["scores"] = [s for _, s in ranked_scored]
            rec["evidence_nonempty"] = bool(ranked_scored[:5])
        elif config == "hybrid_rrf":
            retriever = ctx["retriever"]
            t0 = time.perf_counter()
            docs = retriever.retrieve([question], kb_id=KB_ID, top_k=TOP_K)
            rec["latency_s"] = round(time.perf_counter() - t0, 4)
            rec["ranked"] = [d["dedup_key"] for d in docs]
            rec["scores"] = [d["score"] for d in docs]
            rec["evidence_nonempty"] = bool(docs[:5])
        elif config == "full_agentic_rag":
            graph, im = ctx["graph"], ctx["model"]
            im.reset()
            t0 = time.perf_counter()
            state = graph.invoke({"query": question, "kb_id": KB_ID})
            total = time.perf_counter() - t0
            docs = list(state.get("docs") or [])
            sources = list(state.get("sources") or [])
            gen_time = sum(c["latency_s"] for c in im.calls if c["kind"] in ("answer", "direct"))
            rec.update(
                latency_s=round(total, 4),
                retrieval_latency_s=round(total - gen_time, 4),
                ranked=[d["dedup_key"] for d in docs],
                scores=[d["score"] for d in docs],
                evidence_nonempty=bool(sources),
                route_simple=1 if state.get("is_simple") else 0,
                loop=int(state.get("loop") or 0),
                expand_calls=sum(1 for c in im.calls if c["kind"] == "expand"),
                reflect_calls=sum(1 for c in im.calls if c["kind"] == "reflect"),
                rewrite_triggered=sum(1 for c in im.calls
                                      if c["kind"] == "reflect" and c.get("rewrite")),
                llm_retries=im.retries,
                llm_calls=im.calls,
                subquestions=state.get("subquestions") or [],
                answer_preview=(state.get("answer") or "")[:200],
            )
        records.append(rec)
        if config == "full_agentic_rag":
            print(f"  [{config} run{run_idx}] {qid} done "
                  f"({rec['latency_s']:.1f}s, docs={len(rec['ranked'])})", flush=True)
    return records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", nargs="+", default=["dense_only", "sparse_only",
                                                     "hybrid_rrf", "full_agentic_rag"])
    ap.add_argument("--runs", type=int, default=3, help="非确定性配置的运行次数")
    ap.add_argument("--det-runs", type=int, default=3,
                    help="确定性配置的运行次数（用于验证确定性）")
    ap.add_argument("--out", default=None, help="输出目录（默认 results/<UTC 时间戳>）")
    ap.add_argument("--limit", type=int, default=0, help="仅取前 N 题（调试用）")
    args = ap.parse_args()

    started = datetime.now(timezone.utc)
    out_dir = Path(args.out) if args.out else RAG_ROOT / "results" / started.strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)

    questions = load_jsonl(RAG_ROOT / "questions_verified.jsonl")
    chunks = load_jsonl(RAG_ROOT / "chunks.jsonl")
    if args.limit:
        questions = questions[:args.limit]
    qmap = {q["id"]: q for q in questions}

    settings = build_settings()
    embedder = build_embedder(settings)
    sparse = build_sparse(settings)
    store = QdrantStore.from_settings(settings)
    client = store._c  # noqa: SLF001

    print(f"ingesting {len(chunks)} chunks into {COLLECTION} ...", flush=True)
    n_points = ingest_corpus(store, embedder, sparse, chunks)
    assert n_points == len(chunks), f"入库点数 {n_points} != chunk 数 {len(chunks)}"

    retriever = Retriever(store, embedder, sparse, top_k=TOP_K)
    ctx: dict = {
        "dense_only": SingleVectorRetriever(client, embedder, sparse, "dense"),
        "sparse_only": SingleVectorRetriever(client, embedder, sparse, "sparse"),
        "retriever": retriever,
    }
    if "full_agentic_rag" in args.configs:
        from cognition.providers.deepseek_provider import build_deepseek_chat
        im = InstrumentedModel(build_deepseek_chat(settings, temperature=0))
        ctx["model"] = im
        ctx["graph"] = build_rag_subgraph(settings, model=im, retriever=retriever,
                                          top_k=TOP_K, rerank_top_k=TOP_K)

    git_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                                capture_output=True, text=True).stdout.strip()
    git_dirty = bool(subprocess.run(["git", "status", "--porcelain", "-uno"], cwd=REPO_ROOT,
                                    capture_output=True, text=True).stdout.strip())
    import importlib.metadata as md
    pkg_versions = {p: md.version(p) for p in
                    ("qdrant-client", "fastembed", "langgraph", "langchain-core",
                     "langchain-deepseek")}
    qdrant_server = client.info().version if hasattr(client, "info") else "unknown"

    all_records: list[dict] = []
    metrics: dict = {}
    for config in args.configs:
        n_runs = args.det_runs if config in DETERMINISTIC_CONFIGS else args.runs
        print(f"=== {config}: {n_runs} run(s) × {len(questions)} questions ===", flush=True)
        run_metrics, run_hashes = [], []
        for run_idx in range(1, n_runs + 1):
            recs = run_config(config, questions, ctx, run_idx)
            all_records.extend(recs)
            run_metrics.append(eval_metrics.compute_run_metrics(recs, qmap))
            run_hashes.append(hashlib.sha256(
                json.dumps([r["ranked"] for r in recs]).encode()).hexdigest()[:16])
            print(f"  run{run_idx}: recall@5={run_metrics[-1]['recall_at_5']:.3f} "
                  f"mrr@10={run_metrics[-1]['mrr_at_10']:.3f}", flush=True)
        numeric_keys = [k for k, v in run_metrics[0].items() if isinstance(v, (int, float))]
        metrics[config] = {
            "runs": run_metrics,
            "n_runs": n_runs,
            "deterministic_claim": config in DETERMINISTIC_CONFIGS,
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
    for config in args.configs:
        run1 = [r for r in all_records if r["config"] == config and r["run"] == 1]
        for r in run1:
            q = qmap[r["qid"]]
            if q["question_type"] != "unanswerable":
                if not eval_metrics.hit_at_k(r["ranked"], set(q["gold_chunk_ids"]), 5):
                    failures.append({
                        "config": config, "qid": r["qid"], "type": q["question_type"],
                        "failure": "recall_at_5_miss", "question": q["question"],
                        "gold_groups": q["gold_groups"], "top10": r["ranked"][:10]})
            elif r.get("evidence_nonempty"):
                failures.append({
                    "config": config, "qid": r["qid"], "type": "unanswerable",
                    "failure": "false_retrieval", "question": q["question"],
                    "top5": r["ranked"][:5]})
    with (out_dir / "failures.jsonl").open("w", encoding="utf-8") as f:
        for r in failures:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    finished = datetime.now(timezone.utc)
    config_json = {
        "started_utc": started.isoformat(), "finished_utc": finished.isoformat(),
        "duration_s": round((finished - started).total_seconds(), 1),
        "git_commit": git_commit, "git_dirty_tracked": git_dirty,
        "questions_file": "questions_verified.jsonl",
        "questions_sha256": sha256_file(RAG_ROOT / "questions_verified.jsonl"),
        "chunks_sha256": sha256_file(RAG_ROOT / "chunks.jsonl"),
        "n_questions": len(questions), "n_chunks": len(chunks),
        "collection": COLLECTION, "kb_id": KB_ID,
        "embedding_model": EMBED_MODEL, "embedding_dimension": EMBED_DIM,
        "sparse_model": SPARSE_MODEL,
        "fusion": "Qdrant native FusionQuery(Fusion.RRF), prefetch=20/路",
        "top_k": TOP_K, "rerank_enabled": False,
        "rag_reflection_limit": 2, "rag_subquery_max": 3,
        "llm_model": settings.deepseek_model, "llm_temperature": 0,
        "llm_base_url": settings.deepseek_base_url,
        "runs": {c: metrics[c]["n_runs"] for c in args.configs},
        "python": platform.python_version(),
        "pythonhashseed": os.environ.get("PYTHONHASHSEED", ""),
        "platform": platform.platform(),
        "packages": pkg_versions,
        "qdrant_server_version": qdrant_server,
        "notes": [
            "dense_only/sparse_only 为消融配置：单路 query_points，其余参数与生产一致",
            "full_agentic_rag 的 ranked 取子图累计去重 docs 的生产顺序（首次出现序），"
            "reranked/sources 为进入生成的 top-k",
            "qdrant-client 1.18 对 server 1.12 有版本告警，功能已验证正常",
            "指标全部由 eval_metrics.py 计算（含单测），报告由 render_report.py 从 "
            "metrics.json 渲染",
        ],
    }
    (out_dir / "config.json").write_text(
        json.dumps(config_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done → {out_dir}")


if __name__ == "__main__":
    main()
