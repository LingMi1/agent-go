"""Plan 生命周期纯函数（镜像原项目 PlanLifecycleService + Plan）。

设计取舍（见 docs/03 §6.2）：**确定性 plan-lifecycle helper + LLM 只产出步骤列表**。
计划的状态机（激活首步 / 完成自动推进 / 冻结已完成前缀 / 缺失 current 受控修复）全部是
可单测的纯逻辑，LLM 只负责生成/重排步骤文本。这样可测、可控、并与原项目逐条对齐。

Plan 模型（并行索引）：title, steps[], step_status[], notes[]。
step_status ∈ {not_started, in_progress, completed, blocked}；currentStep = in_progress 那步。

所有变更函数都是**纯函数**：不修改入参，返回新的 Plan（深拷贝后修改），便于在 LangGraph
状态通道里安全地覆盖写入，避免就地变更引发并发/快照问题。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
BLOCKED = "blocked"

_VALID_STATUS = {NOT_STARTED, IN_PROGRESS, COMPLETED, BLOCKED}


class Plan(BaseModel):
    """计划快照（并行索引 steps/step_status/notes）。"""

    title: str = ""
    steps: list[str] = Field(default_factory=list)
    step_status: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def clone(self) -> "Plan":
        """深拷贝，避免就地变更影响调用方持有的引用。"""
        return self.model_copy(deep=True)


# ——————————————————————————————————————————————————————————————
# 查询（只读）
# ——————————————————————————————————————————————————————————————
def current_step_index(plan: Optional[Plan]) -> Optional[int]:
    """当前 in_progress 步骤的索引；无则 None（镜像 Plan.getCurrentStepIndex）。"""
    if plan is None or not plan.steps:
        return None
    n = min(len(plan.steps), len(plan.step_status))
    for i in range(n):
        if plan.step_status[i] == IN_PROGRESS:
            return i
    return None


def current_step(plan: Optional[Plan]) -> str:
    """当前 in_progress 步骤的文本；无则 ""（镜像 Plan.getCurrentStep）。"""
    idx = current_step_index(plan)
    return plan.steps[idx] if (plan is not None and idx is not None) else ""


def all_completed(plan: Optional[Plan]) -> bool:
    """计划是否全部完成（镜像 isAllStepsCompleted）：空/None → True。"""
    if plan is None or not plan.steps:
        return True
    normalized = _normalize(plan)
    return all(s == COMPLETED for s in normalized.step_status)


# ——————————————————————————————————————————————————————————————
# 变更（纯函数：返回新 Plan）
# ——————————————————————————————————————————————————————————————
def create(title: str, steps: list[str]) -> Plan:
    """创建计划并自动激活首个可执行步骤（镜像 create + Plan.create）。"""
    _validate_non_empty_steps(steps)
    plan = Plan(
        title=title or "",
        steps=list(steps),
        step_status=[NOT_STARTED for _ in steps],
        notes=["" for _ in steps],
    )
    return _activate_first_not_started(plan)


def mark_step_completed(plan: Plan, idx: int, note: Optional[str] = None) -> Plan:
    """标记步骤为 completed 并自动推进/自动收尾（镜像 markStep 的 completed 分支）。

    规则：
    - 计划必须存在；先 normalize 修复列表长度。
    - 校验 idx 合法、状态合法。
    - 只允许完成「当前步骤」（currentStepIndex 存在且 != idx → 抛错）。
    - 写入 completed + note，随后：全部完成→自动收尾（不再推进）；否则自动激活下一个 not_started。
    """
    _validate_plan_exists(plan)
    plan = _normalize(plan)
    _validate_step_index(plan, idx)

    cur_idx = current_step_index(plan)
    if cur_idx is not None and cur_idx != idx:
        raise ValueError("only current step can be completed in ordinary replan mode")

    plan.step_status[idx] = COMPLETED
    if note is not None:
        plan.notes[idx] = note

    if all_completed(plan):
        return plan
    return _activate_first_not_started(plan)


def set_note(plan: Plan, idx: int, note: str) -> Plan:
    """写入某步骤的 note（用于把上一轮 sub_results 落到 notes[current]）。"""
    _validate_plan_exists(plan)
    plan = _normalize(plan)
    _validate_step_index(plan, idx)
    plan.notes[idx] = note or ""
    return plan


def update_remaining(plan: Plan, title: Optional[str], remaining_steps: list[str]) -> Plan:
    """冻结已完成前缀，仅替换未完成部分（镜像 update + ensureExecutable）。"""
    _validate_plan_exists(plan)
    plan = _normalize(plan)
    if title:
        plan.title = title
    if remaining_steps is None:
        return ensure_executable(plan)
    _validate_non_empty_steps(remaining_steps)

    prefix = _completed_prefix(plan)
    merged_steps: list[str] = []
    merged_status: list[str] = []
    merged_notes: list[str] = []
    for i in range(prefix):
        merged_steps.append(plan.steps[i])
        merged_status.append(COMPLETED)
        merged_notes.append(plan.notes[i])
    for step in remaining_steps:
        merged_steps.append(step)
        merged_status.append(NOT_STARTED)
        merged_notes.append("")

    plan = Plan(title=plan.title, steps=merged_steps, step_status=merged_status, notes=merged_notes)
    return ensure_executable(plan)


def finish(plan: Optional[Plan]) -> Plan:
    """显式提前结束：把所有步骤标记 completed（镜像 finish）。"""
    if plan is None:
        plan = Plan()
    plan = _normalize(plan)
    for i in range(len(plan.steps)):
        plan.step_status[i] = COMPLETED
    return plan


def ensure_executable(plan: Plan) -> Plan:
    """计划未完成但缺失 current 时受控修复（镜像 ensureExecutable）。

    - 全部完成 → 原样返回（已收尾）。
    - 已有 current → 原样返回（幂等）。
    - 否则激活第一个 not_started；无可激活 → 抛错（无法修复）。
    """
    _validate_plan_exists(plan)
    plan = _normalize(plan)
    if all_completed(plan):
        return plan
    if current_step_index(plan) is not None:
        return plan
    repaired = _activate_first_not_started(plan)
    if current_step_index(repaired) is None:
        raise ValueError("current step is missing and cannot be repaired")
    return repaired


# ——————————————————————————————————————————————————————————————
# 内部工具（镜像私有方法）
# ——————————————————————————————————————————————————————————————
def _activate_first_not_started(plan: Plan) -> Plan:
    """激活第一个 not_started，并清理游离 in_progress（幂等）。"""
    plan = plan.clone()
    next_index = None
    for i, status in enumerate(plan.step_status):
        if status == NOT_STARTED:
            next_index = i
            break
    if next_index is None:
        return plan
    for i, status in enumerate(plan.step_status):
        if status == IN_PROGRESS:
            plan.step_status[i] = NOT_STARTED
    plan.step_status[next_index] = IN_PROGRESS
    return plan


def _normalize(plan: Plan) -> Plan:
    """修正列表长度，避免错位/缺失（镜像 normalizePlan）。返回新 Plan。"""
    steps = list(plan.steps or [])
    status = list(plan.step_status or [])
    notes = list(plan.notes or [])
    while len(status) < len(steps):
        status.append(NOT_STARTED)
    while len(notes) < len(steps):
        notes.append("")
    if len(status) > len(steps):
        status = status[: len(steps)]
    if len(notes) > len(steps):
        notes = notes[: len(steps)]
    return Plan(title=plan.title, steps=steps, step_status=status, notes=notes)


def _completed_prefix(plan: Plan) -> int:
    count = 0
    for status in plan.step_status:
        if status != COMPLETED:
            break
        count += 1
    return count


def _validate_plan_exists(plan: Optional[Plan]) -> None:
    if plan is None:
        raise ValueError("No plan exists. Create a plan first.")


def _validate_non_empty_steps(steps: Optional[list[str]]) -> None:
    if not steps:
        raise ValueError("plan steps cannot be empty")
    for step in steps:
        if step is None or not str(step).strip():
            raise ValueError("plan step cannot be blank")


def _validate_step_index(plan: Plan, idx: Optional[int]) -> None:
    if idx is None:
        raise ValueError("step_index is required for mark_step command")
    if idx < 0 or idx >= len(plan.steps):
        raise ValueError(f"Invalid step index: {idx}")
