#!/usr/bin/env python3
"""多子任务编排评测指标：纯逻辑、无 I/O，供 runner、报告与单测复用。"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Iterable


def mean(values: Iterable[float]) -> float:
    xs = list(values)
    return sum(xs) / len(xs) if xs else math.nan


def percentile(values: Iterable[float], p: float) -> float:
    """最近秩百分位；空输入返回 NaN。"""
    xs = sorted(values)
    if not xs:
        return math.nan
    rank = max(1, math.ceil((p / 100.0) * len(xs)))
    return xs[min(rank, len(xs)) - 1]


def estimate_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int | None,
    prices: dict[str, float],
) -> tuple[float, str]:
    """按每百万 token 单价估算成本；无 cache 明细时按全部 miss 给保守上界。"""
    if cache_read_tokens is None:
        hit = 0
        method = "all_input_cache_miss_upper_bound"
    else:
        hit = min(max(int(cache_read_tokens), 0), max(int(input_tokens), 0))
        method = "provider_cache_detail"
    miss = max(int(input_tokens) - hit, 0)
    cost = (
        hit * prices["input_cache_hit_per_million"]
        + miss * prices["input_cache_miss_per_million"]
        + max(int(output_tokens), 0) * prices["output_per_million"]
    ) / 1_000_000
    return cost, method


def max_concurrency(intervals: list[dict]) -> int:
    """从半开区间 [start_ns,end_ns) 扫描实际峰值；同刻先处理结束再处理开始。"""
    points: list[tuple[int, int]] = []
    for item in intervals:
        start, end = int(item["start_ns"]), int(item["end_ns"])
        if end < start:
            continue
        points.append((start, 1))
        points.append((end, -1))
    cur = peak = 0
    for _, delta in sorted(points, key=lambda x: (x[0], x[1])):
        cur += delta
        peak = max(peak, cur)
    return peak


def clustered_bootstrap_reduction_ci(
    paired: list[dict], *, seed: int = 20260711, samples: int = 2000
) -> list[float]:
    """按 task 聚类 bootstrap 平均 latency reduction 的 95% CI。"""
    by_task: dict[str, list[float]] = defaultdict(list)
    for p in paired:
        serial = float(p["serial_latency_s"])
        parallel = float(p["parallel_latency_s"])
        if serial > 0:
            by_task[str(p["task_id"])].append((serial - parallel) / serial)
    task_values = [mean(v) for _, v in sorted(by_task.items()) if v]
    if not task_values:
        return [math.nan, math.nan]
    rng = random.Random(seed)
    draws = [mean(rng.choice(task_values) for _ in task_values) for _ in range(samples)]
    return [percentile(draws, 2.5), percentile(draws, 97.5)]


def _summary(records: list[dict]) -> dict:
    lat = [float(r["latency_s"]) for r in records]
    successful = [r for r in records if r.get("success")]
    valid = [r for r in records if r.get("success") and r.get("completeness_pass")]
    n = len(records)
    return {
        "formal_runs": n,
        "successful_runs": len(successful),
        "mean_latency_s": mean(lat),
        "p50_latency_s": percentile(lat, 50),
        "p95_latency_s": percentile(lat, 95),
        "successful_mean_latency_s": mean(float(r["latency_s"]) for r in successful),
        "success_rate": mean(1.0 if r.get("success") else 0.0 for r in records),
        "timeout_rate": mean(1.0 if r.get("timed_out") else 0.0 for r in records),
        "rate_limit_run_rate": mean(1.0 if int(r.get("rate_limit_count", 0)) else 0.0 for r in records),
        "completeness_pass_rate": mean(1.0 if r.get("completeness_pass") else 0.0 for r in records),
        "dependency_order_pass_rate": mean(
            1.0 if r.get("dependency_order_pass", True) else 0.0 for r in records
        ),
        "quality_valid_runs": len(valid),
        "retry_count": sum(int(r.get("retry_count", 0)) for r in records),
        "tool_error_count": sum(int(r.get("tool_error_count", 0)) for r in records),
        "rate_limit_count": sum(int(r.get("rate_limit_count", 0)) for r in records),
        "llm_calls": sum(int(r.get("llm_calls", 0)) for r in records),
        "input_tokens": sum(int(r.get("input_tokens", 0)) for r in records),
        "output_tokens": sum(int(r.get("output_tokens", 0)) for r in records),
        "estimated_cost_usd": sum(float(r.get("estimated_cost_usd", 0.0)) for r in records),
        "mean_cost_usd": mean(float(r.get("estimated_cost_usd", 0.0)) for r in records),
        "actual_max_concurrency_mean": mean(int(r.get("actual_max_concurrency", 0)) for r in records),
        "actual_max_concurrency_max": max((int(r.get("actual_max_concurrency", 0)) for r in records), default=0),
        "expected_subtasks": sum(int(r.get("expected_subtask_count", 0)) for r in records),
        "observed_subtasks": sum(int(r.get("observed_subtask_count", 0)) for r in records),
    }


def compute_metrics(records: list[dict], *, bootstrap_seed: int = 20260711) -> dict:
    """汇总全部正式记录，并按 concurrency=1 做配对 speedup。"""
    formal = [r for r in records if not r.get("is_warmup")]
    groups: dict[int, list[dict]] = defaultdict(list)
    for record in formal:
        groups[int(record["concurrency"])].append(record)
    if 1 not in groups:
        raise ValueError("serial baseline concurrency=1 is required")

    baseline_by_key = {
        (r["task_id"], int(r["repetition"])): r for r in groups[1]
    }
    out: dict = {"configs": {}, "bootstrap_seed": bootstrap_seed}
    baseline_mean = _summary(groups[1])["mean_latency_s"]
    for concurrency in sorted(groups):
        recs = groups[concurrency]
        summary = _summary(recs)
        paired: list[dict] = []
        for r in recs:
            base = baseline_by_key.get((r["task_id"], int(r["repetition"])))
            if base is None:
                continue
            serial, parallel = float(base["latency_s"]), float(r["latency_s"])
            paired.append(
                {
                    "task_id": r["task_id"],
                    "repetition": int(r["repetition"]),
                    "serial_latency_s": serial,
                    "parallel_latency_s": parallel,
                    "speedup": serial / parallel if parallel > 0 else math.nan,
                    "valid_pair": bool(
                        base.get("success")
                        and base.get("completeness_pass")
                        and r.get("success")
                        and r.get("completeness_pass")
                    ),
                }
            )
        valid_pairs = [p for p in paired if p["valid_pair"]]
        summary.update(
            speedup_ratio_of_means=(baseline_mean / summary["mean_latency_s"])
            if summary["mean_latency_s"] > 0
            else math.nan,
            latency_reduction_ratio=(baseline_mean - summary["mean_latency_s"]) / baseline_mean,
            paired_speedup_mean=mean(float(p["speedup"]) for p in paired),
            paired_valid_speedup_mean=mean(float(p["speedup"]) for p in valid_pairs),
            paired_reduction_ci95=clustered_bootstrap_reduction_ci(
                paired, seed=bootstrap_seed
            ),
            paired_runs=len(paired),
        )
        out["configs"][str(concurrency)] = summary

    out["best_concurrency"] = choose_best_concurrency(out["configs"])
    return out


def choose_best_concurrency(configs: dict[str, dict]) -> dict:
    """按报告预先锁定的安全门选最佳并发度；无合格并行项则返回 1。"""
    serial = configs["1"]
    tolerance = 1.0 / max(int(serial["formal_runs"]), 1)
    eligible: list[tuple[int, dict]] = []
    reasons: dict[str, list[str]] = {}
    for key, item in sorted(configs.items(), key=lambda kv: int(kv[0])):
        c = int(key)
        if c == 1:
            continue
        bad: list[str] = []
        if item["success_rate"] < serial["success_rate"] - tolerance:
            bad.append("success_rate")
        if item["timeout_rate"] > serial["timeout_rate"] + tolerance:
            bad.append("timeout_rate")
        if item["rate_limit_run_rate"] > serial["rate_limit_run_rate"] + tolerance:
            bad.append("rate_limit_rate")
        if item["completeness_pass_rate"] < 1.0:
            bad.append("completeness")
        if item["dependency_order_pass_rate"] < 1.0:
            bad.append("dependency_order")
        if item["mean_cost_usd"] > serial["mean_cost_usd"] * 1.05 + 1e-12:
            bad.append("cost")
        ci = item.get("paired_reduction_ci95", [math.nan, math.nan])
        if not ci or math.isnan(ci[0]) or ci[0] <= 0:
            bad.append("latency_ci")
        reasons[key] = bad
        if not bad:
            eligible.append((c, item))
    if not eligible:
        return {"concurrency": 1, "safe_parallel_gain": False, "rejections": reasons}
    eligible.sort(key=lambda x: (x[1]["mean_latency_s"], x[0]))
    best_c, best = eligible[0]
    # 平均延迟差距不足 2% 时选更低并发，避免为微小收益放大压力。
    near = [
        (c, item)
        for c, item in eligible
        if item["mean_latency_s"] <= best["mean_latency_s"] * 1.02
    ]
    best_c = min(c for c, _ in near)
    return {"concurrency": best_c, "safe_parallel_gain": True, "rejections": reasons}
