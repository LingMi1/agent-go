"""共享事件契约 v1 的 pydantic 镜像（与 proto/agent/v1/agent.proto 一致）。

类型：tool_thought | tool_call | tool_result | result
      | plan_thought | plan | task（Plan-Execute 加性扩展，仍 v1，线兼容）。
不变量：finish 仅 RESULT 为 true（整 run 终态，恰好一次）；is_final 是单条消息终态。
`Event.to_proto()` 转换为生成的 proto 消息——在方法内**延迟** import 生成模块，
以便纯逻辑测试无需 genproto。

Plan-Execute 扩展要点（镜像已提交的 Go 契约）：
- plan_thought 复用 `tool_thought`（ThoughtPayload）oneof 槽，由 `Event.type` 区分；
  ThoughtPayload 增加 `planner_round_id`（仅 plan_thought 填写，executor 的 tool_thought 留空）。
- plan → PlanPayload{title, steps, step_status, notes, planner_round_id}；plan 事件 is_final=True。
- task → TaskPayload{text}（无 planner_round_id）；task 事件 is_final=True。
"""

from __future__ import annotations

import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:  # 仅类型提示
    from cognition._genproto import agent_pb2


def now_unix_ms() -> int:
    """当前时间（毫秒）。"""
    return int(time.time() * 1000)


class EventType(str, Enum):
    """事件消息类型。"""

    TOOL_THOUGHT = "tool_thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    # —— Plan-Execute 加性扩展 ——
    PLAN_THOUGHT = "plan_thought"  # 规划器思考（复用 ThoughtPayload + planner_round_id）
    PLAN = "plan"                  # 计划快照（PlanPayload）
    TASK = "task"                  # 单个 <sep> 子任务（TaskPayload）
    # —— M11 HITL 加性扩展 ——
    APPROVAL_REQUEST = "approval_request"  # 人工审批请求（ApprovalPayload；finish 恒 False）


class ToolCallStatus(str, Enum):
    """工具调用生命周期状态。"""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class ArtifactRef(BaseModel):
    """产物引用（沿用原项目 8 字段形状）。"""

    resource_key: str = ""
    name: str = ""
    preview_url: str = ""
    download_url: str = ""
    file_name: str = ""
    mime_type: str = ""
    size: int = 0
    missing: bool = False


class ThoughtPayload(BaseModel):
    """tool_thought / plan_thought 复用的载荷。

    `planner_round_id` 仅 plan_thought 填写（同一 planner round 的 thought/plan 共享），
    executor 的 tool_thought 留空。
    """

    text: str = ""
    planner_round_id: str = ""


class PlanPayload(BaseModel):
    """plan 事件载荷：计划快照（steps/step_status/notes 并行索引）。"""

    title: str = ""
    steps: list[str] = Field(default_factory=list)
    step_status: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    planner_round_id: str = ""


class TaskPayload(BaseModel):
    """task 事件载荷：单个 <sep> 子任务文本（无 planner_round_id）。"""

    text: str = ""


class ToolPayload(BaseModel):
    """tool_call / tool_result 复用的载荷。"""

    tool_call_id: str = ""
    tool_name: str = ""
    tool_provider: str = "local"
    status: Optional[ToolCallStatus] = None  # tool_result 不带 status
    dispatch_index: int = 0
    input: dict[str, Any] = Field(default_factory=dict)
    tool_result: str = ""  # observation 文本（tool_result 用）
    summary: str = ""
    error_msg: str = ""
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)


class ApprovalPayload(BaseModel):
    """approval_request 载荷（M11 HITL）：受保护工具挂起时的审批请求。

    pending_tool_call_ids：run1 已发 RUNNING 的工具卡 id——前端据此把卡翻成
    "待审批"（不发伪造终态帧；validate 只认 running/success/failed）。
    """

    approval_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    pending_tool_call_ids: list[str] = Field(default_factory=list)


class UsageInfo(BaseModel):
    """全 run 累计 token 用量（mapper 单咽喉聚合，随终态 RESULT 附带）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0


class ResultPayload(BaseModel):
    """result 载荷。"""

    text: str = ""
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    usage: Optional[UsageInfo] = None  # M11 加性；None=不带（旧行为）


class Event(BaseModel):
    """流出的领域事件（信封 + oneof 载荷）。"""

    seq: int = Field(ge=1)         # 每 run 单调、无空洞、从 1
    run_id: str
    message_id: str               # 原位更新键；TOOL_CALL 时 == tool_call_id
    type: EventType
    ts_unix_ms: int = Field(default_factory=now_unix_ms)
    is_final: bool = False        # 本条消息终态
    finish: bool = False          # 整个 run 终态；仅 RESULT 为 true
    step: str = ""                # ReAct 步序/节点名（可观测）

    # oneof payload —— 按 type 设置其一
    # 注意：plan_thought 复用 tool_thought 槽（与 proto/Go 契约一致），由 type 区分。
    tool_thought: Optional[ThoughtPayload] = None
    tool_call: Optional[ToolPayload] = None
    tool_result: Optional[ToolPayload] = None
    result: Optional[ResultPayload] = None
    plan: Optional[PlanPayload] = None
    task: Optional[TaskPayload] = None
    approval: Optional[ApprovalPayload] = None

    @model_validator(mode="after")
    def _check_invariants(self) -> "Event":
        # finish 只允许出现在 RESULT 上。
        if self.finish and self.type is not EventType.RESULT:
            raise ValueError("finish=True is only allowed on RESULT events")
        # 对应 type 的载荷必须存在（结构完整性）。
        expected = {
            EventType.TOOL_THOUGHT: self.tool_thought,
            EventType.TOOL_CALL: self.tool_call,
            EventType.TOOL_RESULT: self.tool_result,
            EventType.RESULT: self.result,
            # plan_thought 复用 tool_thought 槽（与 Go 的 oneof 一致）。
            EventType.PLAN_THOUGHT: self.tool_thought,
            EventType.PLAN: self.plan,
            EventType.TASK: self.task,
            EventType.APPROVAL_REQUEST: self.approval,
        }[self.type]
        if expected is None:
            raise ValueError(f"event type {self.type.value} requires its matching payload")
        # 镜像 Go Validate：plan / task / approval_request 为单条终态（is_final=True）。
        if self.type in (EventType.PLAN, EventType.TASK, EventType.APPROVAL_REQUEST) and not self.is_final:
            raise ValueError(f"event type {self.type.value} must have is_final=True")
        return self

    # ——————————————————————————————————————————————————————————————
    # proto 转换（延迟 import 生成模块；纯逻辑测试不会触发）
    # ——————————————————————————————————————————————————————————————
    def to_proto(self) -> "agent_pb2.Event":
        """转换为生成的 proto Event。"""
        from cognition._genproto import agent_pb2 as pb

        type_map = {
            EventType.TOOL_THOUGHT: pb.EVENT_TYPE_TOOL_THOUGHT,
            EventType.TOOL_CALL: pb.EVENT_TYPE_TOOL_CALL,
            EventType.TOOL_RESULT: pb.EVENT_TYPE_TOOL_RESULT,
            EventType.RESULT: pb.EVENT_TYPE_RESULT,
            EventType.PLAN_THOUGHT: pb.EVENT_TYPE_PLAN_THOUGHT,
            EventType.PLAN: pb.EVENT_TYPE_PLAN,
            EventType.TASK: pb.EVENT_TYPE_TASK,
            EventType.APPROVAL_REQUEST: pb.EVENT_TYPE_APPROVAL_REQUEST,
        }

        proto = pb.Event(
            seq=self.seq,
            run_id=self.run_id,
            message_id=self.message_id,
            type=type_map[self.type],
            ts_unix_ms=self.ts_unix_ms,
            is_final=self.is_final,
            finish=self.finish,
            step=self.step,
        )

        if self.type is EventType.TOOL_THOUGHT and self.tool_thought is not None:
            proto.tool_thought.CopyFrom(pb.ThoughtPayload(text=self.tool_thought.text))
        elif self.type is EventType.PLAN_THOUGHT and self.tool_thought is not None:
            # plan_thought 复用 tool_thought oneof 槽，带上 planner_round_id。
            proto.tool_thought.CopyFrom(
                pb.ThoughtPayload(
                    text=self.tool_thought.text,
                    planner_round_id=self.tool_thought.planner_round_id,
                )
            )
        elif self.type is EventType.PLAN and self.plan is not None:
            proto.plan.CopyFrom(
                pb.PlanPayload(
                    title=self.plan.title,
                    steps=list(self.plan.steps),
                    step_status=list(self.plan.step_status),
                    notes=list(self.plan.notes),
                    planner_round_id=self.plan.planner_round_id,
                )
            )
        elif self.type is EventType.TASK and self.task is not None:
            proto.task.CopyFrom(pb.TaskPayload(text=self.task.text))
        elif self.type is EventType.TOOL_CALL and self.tool_call is not None:
            proto.tool_call.CopyFrom(_tool_payload_to_proto(self.tool_call, pb))
        elif self.type is EventType.TOOL_RESULT and self.tool_result is not None:
            proto.tool_result.CopyFrom(_tool_payload_to_proto(self.tool_result, pb))
        elif self.type is EventType.RESULT and self.result is not None:
            rp = pb.ResultPayload(
                text=self.result.text,
                artifact_refs=[_artifact_to_proto(a, pb) for a in self.result.artifact_refs],
            )
            if self.result.usage is not None:
                rp.usage.CopyFrom(
                    pb.UsageInfo(
                        input_tokens=self.result.usage.input_tokens,
                        output_tokens=self.result.usage.output_tokens,
                        model_calls=self.result.usage.model_calls,
                    )
                )
            proto.result.CopyFrom(rp)
        elif self.type is EventType.APPROVAL_REQUEST and self.approval is not None:
            ap = pb.ApprovalPayload(
                approval_id=self.approval.approval_id,
                tool_name=self.approval.tool_name,
                reason=self.approval.reason,
                pending_tool_call_ids=list(self.approval.pending_tool_call_ids),
            )
            if self.approval.input:
                ap.input.update(self.approval.input)
            proto.approval.CopyFrom(ap)
        return proto


def _artifact_to_proto(a: ArtifactRef, pb: Any) -> Any:
    return pb.ArtifactRef(
        resource_key=a.resource_key,
        name=a.name,
        preview_url=a.preview_url,
        download_url=a.download_url,
        file_name=a.file_name,
        mime_type=a.mime_type,
        size=a.size,
        missing=a.missing,
    )


def _tool_payload_to_proto(p: ToolPayload, pb: Any) -> Any:
    status_map = {
        ToolCallStatus.RUNNING: pb.TOOL_CALL_STATUS_RUNNING,
        ToolCallStatus.SUCCESS: pb.TOOL_CALL_STATUS_SUCCESS,
        ToolCallStatus.FAILED: pb.TOOL_CALL_STATUS_FAILED,
    }
    tp = pb.ToolPayload(
        tool_call_id=p.tool_call_id,
        tool_name=p.tool_name,
        tool_provider=p.tool_provider,
        status=status_map.get(p.status, pb.TOOL_CALL_STATUS_UNSPECIFIED),
        dispatch_index=p.dispatch_index,
        tool_result=p.tool_result,
        summary=p.summary,
        error_msg=p.error_msg,
        artifact_refs=[_artifact_to_proto(a, pb) for a in p.artifact_refs],
    )
    # google.protobuf.Struct：用 update 填充解析后的参数对象（非字符串）。
    if p.input:
        tp.input.update(p.input)
    return tp
