"""eval_metrics 纯逻辑单测（TDD 约定）。运行：cognition/.venv/bin/python -m pytest eval/rag/scripts/test_eval_metrics.py"""

import math

from eval_metrics import (
    all_recall_at_k,
    any_recall_at_k,
    compute_run_metrics,
    first_hit_rank,
    hit_at_k,
    percentile,
    reciprocal_rank_at_k,
)


def test_first_hit_and_hit_at_k():
    ranked = ["a", "b", "c", "d"]
    assert first_hit_rank(ranked, {"c"}) == 3
    assert first_hit_rank(ranked, {"z"}) is None
    assert hit_at_k(ranked, {"c"}, 3) is True
    assert hit_at_k(ranked, {"c"}, 2) is False
    assert hit_at_k([], {"c"}, 5) is False


def test_group_recall_any_all():
    ranked = ["x", "g1a", "y", "g2a"]
    groups = [["g1a", "g1b"], ["g2a"]]
    assert any_recall_at_k(ranked, groups, 2) is True      # 组1命中
    assert all_recall_at_k(ranked, groups, 2) is False     # 组2未进 top2
    assert all_recall_at_k(ranked, groups, 4) is True
    assert all_recall_at_k(ranked, [], 4) is False         # 空组不可判定


def test_reciprocal_rank():
    assert reciprocal_rank_at_k(["a", "g"], {"g"}, 10) == 0.5
    assert reciprocal_rank_at_k(["a", "b"], {"g"}, 10) == 0.0
    assert reciprocal_rank_at_k(["x"] * 10 + ["g"], {"g"}, 10) == 0.0  # 超出 k 计 0


def test_percentile_nearest_rank():
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([1, 2, 3, 4], 95) == 4
    assert percentile([7], 95) == 7
    assert math.isnan(percentile([], 50))


def _q(qid, qtype, gold, groups):
    return {"id": qid, "question_type": qtype, "gold_chunk_ids": gold, "gold_groups": groups}


def test_compute_run_metrics_end_to_end():
    questions = {
        "q1": _q("q1", "single_hop", ["c1"], [["c1"]]),
        "q2": _q("q2", "multi_hop", ["c2", "c3", "c4"], [["c2", "c3"], ["c4"]]),
        "q3": _q("q3", "unanswerable", [], []),
    }
    records = [
        {"qid": "q1", "ranked": ["c1", "x"], "latency_s": 0.1, "evidence_nonempty": True},
        # 多跳：组1 经 c3 命中、组2 c4 在第 6 位 → any@5 是、all@5 否、all@10 是
        {"qid": "q2", "ranked": ["x1", "c3", "x2", "x3", "x4", "c4"], "latency_s": 0.3,
         "evidence_nonempty": True},
        {"qid": "q3", "ranked": ["x", "y"], "latency_s": 0.2, "evidence_nonempty": True},
    ]
    m = compute_run_metrics(records, questions)
    assert m["n_answerable"] == 2 and m["n_unanswerable"] == 1
    assert m["recall_at_1"] == 0.5          # q1 命中 rank1，q2 首命中 rank2
    assert m["recall_at_5"] == 1.0
    assert m["mrr_at_10"] == (1.0 + 0.5) / 2
    assert m["multi_hop_any_recall_at_5"] == 1.0
    assert m["multi_hop_all_recall_at_5"] == 0.0
    assert m["multi_hop_all_recall_at_10"] == 1.0
    assert m["unanswerable_false_retrieval_rate"] == 1.0
    assert m["per_type_recall_at_5"] == {"multi_hop": 1.0, "single_hop": 1.0}
    assert m["latency_p50_s"] == 0.2
