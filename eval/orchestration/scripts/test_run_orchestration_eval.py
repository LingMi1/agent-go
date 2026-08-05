from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import run_orchestration_eval as runner
from cognition.graphs.plan_execute import build_plan_execute_graph
from cognition.graphs.react import build_react_graph
from cognition.providers.fake import MessageDrivenChatModel
from cognition.tools.calculator import calculator


def _task(dependency="independent", count=3):
    return {
        "id": "t",
        "prompt": "x",
        "expected_subtask_count": count,
        "dependency_type": dependency,
        "timeout_seconds": 300,
    }


def test_validate_frozen_independent_plan_and_derive_dag():
    valid, errors, branches = runner.validate_frozen_plan(
        _task(), "title", ["[S1] a<sep>[S2] b<sep>[S3] c"]
    )
    assert valid and not errors
    assert [(b["round"], b["branch_id"]) for b in branches] == [
        (0, "b0"), (0, "b1"), (0, "b2")
    ]
    assert all(not b["depends_on"] for b in branches)


def test_validate_frozen_allows_marker_references_in_dependent_round():
    # 依赖型计划第二步必然引用前序标记（"汇总 [S1]～[S3] 结果"）——归属制只认
    # 每分支首个标记，引用不计数；旧的全文唯一计数规则会误杀这类合法计划。
    valid, errors, branches = runner.validate_frozen_plan(
        _task("partially_dependent", 4),
        "title",
        ["[S1] a<sep>[S2] b<sep>[S3] c", "[S4] 汇总 [S1][S2][S3] 的实际结果"],
    )
    assert valid and not errors
    assert branches[-1]["task"].startswith("[S4]")


def test_validate_frozen_rejects_missing_or_duplicate_owned_markers():
    bad, errors, _ = runner.validate_frozen_plan(
        _task(count=3), "title", ["[S1] a<sep>[S1] b<sep>[S3] c"]
    )
    assert not bad and any(e.startswith("owned_markers") for e in errors)
    bad, errors, _ = runner.validate_frozen_plan(
        _task(count=3), "title", ["[S1] a<sep>no marker<sep>[S3] c"]
    )
    assert not bad and "unmarked_branches:1" in errors


def test_run_timeout_scales_with_workload_per_concurrency():
    frozen = {
        "selected_plan": {
            "branches": [
                {"round": 0, "branch_id": f"b{i}", "task": f"[S{i+1}]"} for i in range(6)
            ]
            + [{"round": 1, "branch_id": "b0", "task": "[S7]"}]
        }
    }
    assert runner.run_timeout_seconds(frozen, 1) == 60.0 + 6 * 300.0 + 300.0
    assert runner.run_timeout_seconds(frozen, 4) == 60.0 + 2 * 300.0 + 300.0
    assert runner.run_timeout_seconds(frozen, 8) == 60.0 + 300.0 + 300.0


def test_calc_quality_accepts_self_corrected_tool_error():
    task = _task(count=1)
    task["id"] = "calc_x"
    frozen = {"selected_plan": {"branches": [
        {"round": 0, "branch_id": "b0", "task": "[S1] t"}
    ]}}
    results = [{
        "round": 0, "branch_id": "b0", "task": "[S1] t", "status": "finished",
        "result": "[S1] 计算结果：**2669.324324**（保留 6 位小数）",
        "observations": ["Error: 不支持的运算符 ^", "2669.3243243243243"],
    }]
    checks = runner.check_run(task, frozen, [], results, "[S1] t => 2669.324324 [S1] 计算结果：**2669.324324**（保留 6 位小数）", 1)
    assert checks["quality_pass"]
    assert checks["tool_error_count"] == 1
    # 没有任何成功观测时仍必须判失败。
    results[0]["observations"] = ["Error: bad"]
    checks = runner.check_run(task, frozen, [], results, "x", 1)
    assert not checks["quality_pass"]


def test_validate_frozen_partial_requires_second_serial_round():
    valid, errors, branches = runner.validate_frozen_plan(
        _task("partially_dependent", 4),
        "title",
        ["[S1] a<sep>[S2] b<sep>[S3] c", "[S4] combine"],
    )
    assert valid and not errors
    assert branches[-1]["depends_on"] == ["r0:b0", "r0:b1", "r0:b2"]
    bad, bad_errors, _ = runner.validate_frozen_plan(
        _task("partially_dependent", 4), "title", ["[S1] a<sep>[S2] b<sep>[S3] c<sep>[S4] combine"]
    )
    assert not bad and "partial_rounds:1" in bad_errors


def test_model_stats_counts_explicit_retry_and_usage():
    calls = [
        {"status": "error", "error_class": "rate_limit", "input_tokens": 0,
         "output_tokens": 0, "cache_read_tokens": None},
        {"status": "success", "input_tokens": 1000, "output_tokens": 200,
         "cache_read_tokens": 300},
    ]
    stats = runner.model_stats(calls)
    assert stats["llm_calls"] == 1
    assert stats["retry_count"] == 1 and stats["rate_limit_count"] == 1
    assert stats["input_tokens"] == 1000 and stats["cache_read_tokens"] == 300
    assert stats["estimated_cost_usd"] > 0


def test_instrumented_model_records_success_without_network():
    class Inner:
        def invoke(self, input, config=None, **kwargs):
            return AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            )

    recorder = runner.ModelRecorder()
    recorder.reset("r")
    token = runner._CURRENT_RUN.set("r")
    try:
        result = runner.InstrumentedModel(Inner(), recorder, "test").invoke("x")
    finally:
        runner._CURRENT_RUN.reset(token)
    assert result.content == "ok"
    assert recorder.get("r")[0]["input_tokens"] == 7


def test_usage_reads_openai_cached_token_detail():
    response = SimpleNamespace(
        usage_metadata=None,
        response_metadata={
            "token_usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 8},
            }
        },
    )
    assert runner.usage_of(response) == {
        "input_tokens": 12, "output_tokens": 3, "cache_read_tokens": 8
    }


@pytest.mark.asyncio
async def test_run_one_extracts_production_subresults_and_completeness():
    task = {
        "id": "calc_fake",
        "prompt": "[S1] a; [S2] b; [S3] c",
        "expected_subtask_count": 3,
        "dependency_type": "independent",
        "timeout_seconds": 10,
    }
    selected = {
        "title": "fake",
        "steps": ["[S1] a<sep>[S2] b<sep>[S3] c"],
        "branches": [
            {"round": 0, "branch_id": f"b{i}", "task": f"[S{i + 1}] {x}", "depends_on": []}
            for i, x in enumerate(("a", "b", "c"))
        ],
    }
    frozen = {
        "task_id": task["id"], "valid": True, "selected_plan": selected,
    }
    planner = runner.build_frozen_planner([frozen], {task["id"]: task})

    def executor_decide(messages):
        if messages and isinstance(messages[-1], ToolMessage):
            return AIMessage(content="计算结果为 2")
        return AIMessage(
            content="calculate",
            tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "c1"}],
        )

    executor = build_react_graph(
        MessageDrivenChatModel(decide=executor_decide), [calculator]
    )
    graph = build_plan_execute_graph(planner, executor, [calculator], max_parallel=2)
    record = await runner.run_one(
        task=task, frozen=frozen, graph=graph, concurrency=2, repetition=1,
        is_warmup=False, recorder=runner.ModelRecorder(),
    )
    assert record["observed_subtask_count"] == 3
    assert record["completeness_pass"] and record["quality_pass"]
    assert record["actual_max_concurrency"] <= 2
    assert record["success"]
