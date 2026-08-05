"""route_after_planner / merge_sub_results / reduce_substate / 信号量限宽。"""

import asyncio

import pytest
from langgraph.types import Send

from cognition.graphs.plan_execute import (
    STATE_ERROR,
    STATE_FINISHED,
    STATE_IDLE,
    merge_sub_results,
    reduce_substate,
    route_after_planner,
    run_branch_guarded,
)
from cognition.graphs.plan_lifecycle import create, mark_step_completed


def _state(plan, round=0, **kw):
    base = {
        "plan": plan,
        "round": round,
        "query": "q",
        "sop": "",
        "request_id": "r1",
        "session_id": "s1",
    }
    base.update(kw)
    return base


# ——————————————————————————— route_after_planner ———————————————————————————
def test_route_splits_current_step_into_sends():
    plan = create("p", ["A<sep>B", "C"])  # current = step1，含两个 <sep> 子任务
    out = route_after_planner(_state(plan, round=0), max_steps=5)
    assert isinstance(out, list) and len(out) == 2
    assert all(isinstance(s, Send) and s.node == "executor" for s in out)
    tasks = [s.arg["task"] for s in out]
    assert tasks == ["A", "B"]
    branches = [s.arg["branch_id"] for s in out]
    assert branches == ["b0", "b1"]
    assert all(s.arg["round"] == 0 for s in out)


def test_route_single_subtask_one_send():
    plan = create("p", ["only"])
    out = route_after_planner(_state(plan, round=0), max_steps=5)
    assert isinstance(out, list) and len(out) == 1
    assert out[0].arg["task"] == "only"


def test_route_all_completed_goes_to_summary():
    plan = create("p", ["a"])
    plan = mark_step_completed(plan, 0)  # all completed
    assert route_after_planner(_state(plan), max_steps=5) == "summary"


def test_route_replan_advances_to_next_step():
    plan = create("p", ["s1", "s2"])
    plan = mark_step_completed(plan, 0)  # advance → current = s2
    out = route_after_planner(_state(plan, round=1), max_steps=5)
    assert isinstance(out, list) and len(out) == 1
    assert out[0].arg["task"] == "s2"
    assert out[0].arg["round"] == 1


def test_route_over_max_steps_goes_to_summary():
    plan = create("p", ["a", "b"])  # not completed
    assert route_after_planner(_state(plan, round=6), max_steps=5) == "summary"


def test_route_error_reduction_goes_to_summary():
    plan = create("p", ["a", "b"])
    assert route_after_planner(_state(plan, round=1, reduced_state=STATE_ERROR), max_steps=5) == "summary"


# ——————————————————————————— merge_sub_results ———————————————————————————
def _r(round, branch, result="x", status=STATE_FINISHED):
    return {"round": round, "branch_id": branch, "task": "t", "result": result, "status": status}


def test_merge_dedups_by_round_branch():
    a = [_r(0, "b0"), _r(0, "b1")]
    b = [_r(0, "b1"), _r(1, "b0")]  # (0,b1) 重复
    merged = merge_sub_results(a, b)
    keys = [(r["round"], r["branch_id"]) for r in merged]
    assert keys == [(0, "b0"), (0, "b1"), (1, "b0")]


def test_merge_commutative_and_associative():
    a = [_r(0, "b0")]
    b = [_r(0, "b1")]
    c = [_r(1, "b0")]
    ab = merge_sub_results(a, b)
    ba = merge_sub_results(b, a)
    assert ab == ba  # 可交换
    left = merge_sub_results(merge_sub_results(a, b), c)
    right = merge_sub_results(a, merge_sub_results(b, c))
    assert left == right  # 可结合


def test_merge_idempotent():
    a = [_r(0, "b0"), _r(0, "b1")]
    assert merge_sub_results(a, a) == merge_sub_results(a, [])


def test_merge_stable_canonical_order():
    # 乱序输入 → 规范排序输出（按 round, branch_id）。
    merged = merge_sub_results([_r(1, "b1"), _r(0, "b1")], [_r(0, "b0")])
    keys = [(r["round"], r["branch_id"]) for r in merged]
    assert keys == [(0, "b0"), (0, "b1"), (1, "b1")]


def test_merge_handles_none():
    assert merge_sub_results(None, None) == []
    assert merge_sub_results([_r(0, "b0")], None) == [_r(0, "b0")]


# ——————————————————————————— reduce_substate ———————————————————————————
def test_reduce_error_dominates():
    res = [_r(0, "b0", status=STATE_FINISHED), _r(0, "b1", status=STATE_ERROR), _r(0, "b2", status=STATE_IDLE)]
    assert reduce_substate(res) == STATE_ERROR


def test_reduce_idle_over_finished():
    res = [_r(0, "b0", status=STATE_FINISHED), _r(0, "b1", status=STATE_IDLE)]
    assert reduce_substate(res) == STATE_IDLE


def test_reduce_all_finished():
    res = [_r(0, "b0", status=STATE_FINISHED), _r(0, "b1", status=STATE_FINISHED)]
    assert reduce_substate(res) == STATE_FINISHED


def test_reduce_empty_is_finished():
    assert reduce_substate([]) == STATE_FINISHED


def test_reduce_unknown_status_is_idle():
    # 非 finished/idle/error 的状态 → 既非全 finished，归 IDLE（镜像 reduceParentState 兜底）。
    assert reduce_substate([_r(0, "b0", status="weird")]) == STATE_IDLE


# ——————————————————————————— 信号量限宽 ———————————————————————————
async def test_semaphore_caps_concurrency():
    sem = asyncio.Semaphore(2)
    state = {"cur": 0, "max": 0}

    async def work():
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.02)
        state["cur"] -= 1
        return 1

    results = await asyncio.gather(*[run_branch_guarded(sem, 1.0, work) for _ in range(6)])
    assert results == [1] * 6
    assert state["max"] <= 2  # 并发宽度被信号量限制在 2


async def test_branch_timeout_raises():
    sem = asyncio.Semaphore(2)

    async def slow():
        await asyncio.sleep(0.5)

    with pytest.raises(asyncio.TimeoutError):
        await run_branch_guarded(sem, 0.01, slow)


async def test_branch_guarded_lifecycle_hooks_wrap_actual_slot():
    sem = asyncio.Semaphore(1)
    events = []

    async def work():
        events.append("work")
        return 7

    async def started():
        events.append("start")

    async def finished(error):
        events.append(("finish", error))

    result = await run_branch_guarded(
        sem, 1.0, work, on_start=started, on_finish=finished
    )
    assert result == 7
    assert events == ["start", "work", ("finish", None)]


async def test_branch_guarded_finish_hook_observes_timeout():
    seen = []

    async def slow():
        await asyncio.sleep(0.1)

    async def finished(error):
        seen.append(error)

    with pytest.raises(asyncio.TimeoutError):
        await run_branch_guarded(
            asyncio.Semaphore(1), 0.001, slow, on_finish=finished
        )
    assert len(seen) == 1 and isinstance(seen[0], asyncio.TimeoutError)


def test_route_summary_when_global_branch_budget_exhausted():
    """全局分支预算：累计派发分支 + 本轮待派发 > 上限 → 直接 summary 收口。
    背景：分支级 40 次工具预算挡不住 8 步计划×多子任务×replan 的乘积
    （实测 30+ 分支 1400+ 次调用仍拖满 RUN_TIMEOUT）。"""
    from cognition.graphs.plan_lifecycle import create

    plan = create("t", ["a<sep>b<sep>c", "后续"])
    state = {"plan": plan, "round": 1, "branches_used": 6}
    assert route_after_planner(state, max_steps=5, max_total_branches=8) == "summary"  # 6+3>8
    sends = route_after_planner(state, max_steps=5, max_total_branches=9)  # 6+3<=9 → 正常扇出
    assert isinstance(sends, list) and len(sends) == 3
    assert route_after_planner({**state, "branches_used": 0}, max_steps=5, max_total_branches=0) != "summary"  # 0=不限
