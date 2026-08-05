#!/usr/bin/env python3
"""检索评测指标（纯逻辑，无 I/O，可单测）。

Recall 定义（与 REPORT.md 一致）：
- 单跳/同义改写/消歧/故障排查题：top-k 是否包含任一 gold chunk（gold 并集命中）；
- 多跳 Any Recall@k：至少一个证据组被命中（组内任一 chunk 出现在 top-k 即该组命中）；
- 多跳 All Recall@k：全部证据组均被命中；
- gold_groups：组间=回答该题的必要事实（AND），组内=同一事实的等价证据 chunk（OR）。
  gold 并集命中 ≡ Any 命中（组并集=gold_chunk_ids）。
- MRR@k：gold 并集首个命中名次的倒数，超过 k 或未命中计 0。
"""

from __future__ import annotations

import math


def first_hit_rank(ranked: list[str], gold: set[str]) -> int | None:
    """gold 并集在 ranked 中的首个命中名次（1-based），未命中返回 None。"""
    for i, cid in enumerate(ranked, start=1):
        if cid in gold:
            return i
    return None


def hit_at_k(ranked: list[str], gold: set[str], k: int) -> bool:
    """top-k 是否包含任一 gold chunk。"""
    return any(cid in gold for cid in ranked[:k])


def group_hit_at_k(ranked: list[str], group: list[str], k: int) -> bool:
    """单个证据组是否被命中（组内任一成员出现在 top-k）。"""
    top = set(ranked[:k])
    return any(cid in top for cid in group)


def any_recall_at_k(ranked: list[str], groups: list[list[str]], k: int) -> bool:
    """多跳 Any：至少一个证据组命中。"""
    return any(group_hit_at_k(ranked, g, k) for g in groups)


def all_recall_at_k(ranked: list[str], groups: list[list[str]], k: int) -> bool:
    """多跳 All：全部证据组命中。groups 为空视为不可判定，返回 False。"""
    return bool(groups) and all(group_hit_at_k(ranked, g, k) for g in groups)


def reciprocal_rank_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    rank = first_hit_rank(ranked[:k], gold)
    return 1.0 / rank if rank else 0.0


def percentile(values: list[float], p: float) -> float:
    """最近秩法百分位（p ∈ [0,100]）；空列表返回 nan。"""
    if not values:
        return math.nan
    xs = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(xs)))
    return xs[min(rank, len(xs)) - 1]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else math.nan


def compute_run_metrics(records: list[dict], questions: dict[str, dict], ks=(1, 3, 5, 10)) -> dict:
    """单个 (config, run) 的全部指标。

    records: [{qid, ranked: [chunk_id...], latency_s, evidence_nonempty, ...}]
    questions: qid -> 题目（含 question_type / gold_chunk_ids / gold_groups）。
    """
    by_type: dict[str, list[tuple[dict, dict]]] = {}
    for r in records:
        q = questions[r["qid"]]
        by_type.setdefault(q["question_type"], []).append((r, q))

    answerable = [(r, q) for r, q in
                  ((r, questions[r["qid"]]) for r in records)
                  if q["question_type"] != "unanswerable"]
    unanswerable = [(r, q) for r, q in
                    ((r, questions[r["qid"]]) for r in records)
                    if q["question_type"] == "unanswerable"]

    m: dict = {"n_questions": len(records), "n_answerable": len(answerable),
               "n_unanswerable": len(unanswerable)}

    for k in ks:
        hits = [hit_at_k(r["ranked"], set(q["gold_chunk_ids"]), k) for r, q in answerable]
        m[f"recall_at_{k}"] = mean([1.0 if h else 0.0 for h in hits])
    m["mrr_at_10"] = mean([reciprocal_rank_at_k(r["ranked"], set(q["gold_chunk_ids"]), 10)
                           for r, q in answerable])

    multi = [(r, q) for r, q in answerable if q["question_type"] == "multi_hop"]
    if multi:
        m["multi_hop_any_recall_at_5"] = mean(
            [1.0 if any_recall_at_k(r["ranked"], q["gold_groups"], 5) else 0.0 for r, q in multi])
        m["multi_hop_all_recall_at_5"] = mean(
            [1.0 if all_recall_at_k(r["ranked"], q["gold_groups"], 5) else 0.0 for r, q in multi])
        m["multi_hop_all_recall_at_10"] = mean(
            [1.0 if all_recall_at_k(r["ranked"], q["gold_groups"], 10) else 0.0 for r, q in multi])

    if unanswerable:
        m["unanswerable_false_retrieval_rate"] = mean(
            [1.0 if r.get("evidence_nonempty") else 0.0 for r, _ in unanswerable])

    m["per_type_recall_at_5"] = {
        t: mean([1.0 if hit_at_k(r["ranked"], set(q["gold_chunk_ids"]), 5) else 0.0
                 for r, q in pairs])
        for t, pairs in sorted(by_type.items()) if t != "unanswerable"
    }

    lats = [r["latency_s"] for r in records]
    m["latency_mean_s"] = mean(lats)
    m["latency_p50_s"] = percentile(lats, 50)
    m["latency_p95_s"] = percentile(lats, 95)
    retr = [r["retrieval_latency_s"] for r in records if "retrieval_latency_s" in r]
    if retr:
        m["retrieval_only_latency_mean_s"] = mean(retr)
        m["retrieval_only_latency_p50_s"] = percentile(retr, 50)
        m["retrieval_only_latency_p95_s"] = percentile(retr, 95)

    for key in ("expand_calls", "reflect_calls", "rewrite_triggered", "route_simple", "llm_retries"):
        vals = [r.get(key) for r in records if r.get(key) is not None]
        if vals:
            m[f"total_{key}"] = int(sum(vals))
    return m
