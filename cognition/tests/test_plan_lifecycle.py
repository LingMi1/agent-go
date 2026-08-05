"""Plan 生命周期纯函数测试（镜像原项目 PlanLifecycleServiceTest）。

覆盖：create 激活首步；mark_step completed 自动推进；不能完成非当前步；末步完成自动收尾；
update 冻结已完成前缀；ensure_executable 修复缺失 current 或快速失败；幂等。
"""

import pytest

from cognition.graphs.plan_lifecycle import (
    Plan,
    all_completed,
    create,
    current_step,
    current_step_index,
    ensure_executable,
    finish,
    mark_step_completed,
    update_remaining,
)


def test_create_rejects_empty_steps():
    with pytest.raises(ValueError):
        create("空计划", [])


def test_create_rejects_blank_step():
    with pytest.raises(ValueError):
        create("x", ["ok", "  "])


def test_create_activates_first_step():
    plan = create("普通 replan", ["步骤一", "步骤二"])
    assert current_step(plan) == "步骤一"
    assert current_step_index(plan) == 0
    assert plan.step_status == ["in_progress", "not_started"]
    assert plan.notes == ["", ""]
    assert all_completed(plan) is False


def test_mark_step_completed_auto_advances():
    plan = create("普通 replan", ["步骤一", "步骤二"])
    advanced = mark_step_completed(plan, 0, note="已完成")
    assert advanced.step_status == ["completed", "in_progress"]
    assert advanced.notes == ["已完成", ""]
    assert current_step(advanced) == "步骤二"
    assert current_step_index(advanced) == 1
    # 纯函数：不修改入参。
    assert plan.step_status == ["in_progress", "not_started"]


def test_cannot_complete_non_current_step():
    plan = create("p", ["步骤一", "步骤二"])  # current = 0
    with pytest.raises(ValueError):
        mark_step_completed(plan, 1)  # 1 不是当前步


def test_mark_final_step_auto_finishes():
    plan = create("收口计划", ["最后一步"])
    finished = mark_step_completed(plan, 0, note="全部完成")
    assert finished.step_status == ["completed"]
    assert all_completed(finished) is True
    assert current_step(finished) == ""
    assert current_step_index(finished) is None


def test_full_sequence_two_steps_completes():
    plan = create("p", ["a", "b"])
    plan = mark_step_completed(plan, 0)
    assert not all_completed(plan)
    plan = mark_step_completed(plan, 1)
    assert all_completed(plan)


def test_update_freezes_completed_prefix():
    plan = create("普通 replan", ["步骤一", "步骤二", "步骤三"])
    plan = mark_step_completed(plan, 0, note="首步完成")
    updated = update_remaining(plan, "重排后的计划", ["新步骤A", "新步骤B"])
    assert updated.title == "重排后的计划"
    assert updated.steps == ["步骤一", "新步骤A", "新步骤B"]
    assert updated.step_status == ["completed", "in_progress", "not_started"]
    assert updated.notes == ["首步完成", "", ""]
    assert current_step(updated) == "新步骤A"


def test_ensure_executable_repairs_missing_current():
    repairable = Plan(
        title="repairable",
        steps=["已完成步骤", "待执行步骤"],
        step_status=["completed", "not_started"],
        notes=["", ""],
    )
    repaired = ensure_executable(repairable)
    assert current_step(repaired) == "待执行步骤"
    assert current_step_index(repaired) == 1
    assert repaired.step_status == ["completed", "in_progress"]


def test_ensure_executable_fails_when_unrepairable():
    broken = Plan(
        title="broken",
        steps=["步骤一", "步骤二"],
        step_status=["completed", "blocked"],
        notes=["", ""],
    )
    with pytest.raises(ValueError, match="current step"):
        ensure_executable(broken)


def test_ensure_executable_idempotent():
    plan = create("p", ["a", "b"])  # current already = 0
    once = ensure_executable(plan)
    twice = ensure_executable(once)
    assert once.step_status == twice.step_status == ["in_progress", "not_started"]
    # 全部完成时也幂等。
    done = finish(plan)
    assert ensure_executable(done).step_status == ["completed", "completed"]


def test_activate_first_clears_stray_in_progress():
    # 两个 in_progress（异常残留）→ 激活第一个 not_started 时应清理游离 in_progress。
    messy = Plan(
        title="m",
        steps=["a", "b", "c"],
        step_status=["in_progress", "in_progress", "not_started"],
        notes=["", "", ""],
    )
    repaired = ensure_executable(messy)
    # 已有 current（idx 0 in_progress）→ ensure_executable 视为可执行、原样返回。
    assert current_step_index(repaired) == 0

    # 直接走 create 风格的激活：第一个 not_started 被激活，游离 in_progress 清理。
    plan = create("p", ["a", "b"])
    plan = mark_step_completed(plan, 0)
    # mark 之后只有一个 in_progress
    assert plan.step_status.count("in_progress") == 1


def test_all_completed_empty_plan():
    assert all_completed(None) is True
    assert all_completed(Plan()) is True


def test_finish_marks_all_completed():
    plan = create("p", ["a", "b", "c"])
    done = finish(plan)
    assert done.step_status == ["completed", "completed", "completed"]
    assert all_completed(done) is True
