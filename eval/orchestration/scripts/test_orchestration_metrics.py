from __future__ import annotations

import math

import orchestration_metrics as m


PRICES = {
    "input_cache_hit_per_million": 0.1,
    "input_cache_miss_per_million": 1.0,
    "output_per_million": 2.0,
}


def test_cost_uses_cache_detail_and_upper_bound():
    cost, method = m.estimate_cost_usd(
        input_tokens=1_000_000, output_tokens=500_000, cache_read_tokens=250_000, prices=PRICES
    )
    assert math.isclose(cost, 1.775)
    assert method == "provider_cache_detail"
    upper, method2 = m.estimate_cost_usd(
        input_tokens=1_000_000, output_tokens=500_000, cache_read_tokens=None, prices=PRICES
    )
    assert math.isclose(upper, 2.0)
    assert method2 == "all_input_cache_miss_upper_bound"


def test_max_concurrency_uses_half_open_intervals():
    intervals = [
        {"start_ns": 0, "end_ns": 10},
        {"start_ns": 5, "end_ns": 15},
        {"start_ns": 10, "end_ns": 20},
    ]
    assert m.max_concurrency(intervals) == 2


def _record(task: str, concurrency: int, rep: int, latency: float) -> dict:
    return {
        "task_id": task,
        "concurrency": concurrency,
        "repetition": rep,
        "is_warmup": False,
        "latency_s": latency,
        "success": True,
        "timed_out": False,
        "rate_limit_count": 0,
        "completeness_pass": True,
        "dependency_order_pass": True,
        "retry_count": 0,
        "llm_calls": 3,
        "input_tokens": 100,
        "output_tokens": 20,
        "estimated_cost_usd": 0.001,
        "actual_max_concurrency": concurrency,
        "expected_subtask_count": 4,
        "observed_subtask_count": 4,
    }


def test_compute_metrics_pairs_runs_and_selects_fastest_safe_config():
    records = []
    for task_no in range(1, 21):
        for rep in range(1, 4):
            records.extend(
                [
                    _record(f"t{task_no}", 1, rep, 10.0),
                    _record(f"t{task_no}", 2, rep, 5.0),
                    _record(f"t{task_no}", 4, rep, 3.0),
                    _record(f"t{task_no}", 8, rep, 3.2),
                ]
            )
    metrics = m.compute_metrics(records, bootstrap_seed=1)
    assert metrics["configs"]["2"]["speedup_ratio_of_means"] == 2.0
    assert math.isclose(metrics["configs"]["4"]["latency_reduction_ratio"], 0.7)
    assert metrics["configs"]["4"]["paired_reduction_ci95"][0] > 0
    assert metrics["best_concurrency"]["concurrency"] == 4


def test_best_concurrency_rejects_incomplete_parallel_runs():
    serial = {
        "formal_runs": 60, "success_rate": 1.0, "timeout_rate": 0.0,
        "rate_limit_run_rate": 0.0, "completeness_pass_rate": 1.0,
        "dependency_order_pass_rate": 1.0, "mean_cost_usd": 1.0,
        "mean_latency_s": 10.0, "paired_reduction_ci95": [0.0, 0.0],
    }
    parallel = dict(serial)
    parallel.update(
        mean_latency_s=5.0, completeness_pass_rate=0.98,
        paired_reduction_ci95=[0.4, 0.6],
    )
    out = m.choose_best_concurrency({"1": serial, "2": parallel})
    assert out["concurrency"] == 1
    assert "completeness" in out["rejections"]["2"]
