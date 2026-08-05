"""HITL 人工审批门（M11）。

受保护工具（settings.approval_tools）在**执行前**经 LangGraph `interrupt()` 挂起：
- 挂起态持久在 PG checkpoint（thread=session）——跨 SSE 断连/服务重启/隔夜审批都成立；
- 决议以 **resume 字符串**回注（"approved[:备注]" / "rejected[:备注]"）——绝不用 dict，
  dict 会被 langgraph 解释为 interrupt-id 映射；
- **节点重放语义**（实验证实）：interrupt 之前的代码在 resume 时重跑——所以本模块把
  一切副作用放在 interrupt **之后**；同批次其它工具也会重执行（固有语义，登记为限制，
  approval_tools 默认空列表）。

仅包装 react 主图的工具列表：plan 家族的 executor 分支以 `except Exception` 吞掉
GraphInterrupt（分支失败语义），且分支子图无 checkpointer——受保护工具在 plan 模式
不生效（docs/08 §6 登记限制）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from typing import Any, Optional, Sequence

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

DEFAULT_REASON = "该操作被列为高危，需人工确认后执行"

_PREVIEW_MAX_CHARS = 500
_INJECTED_KEYS = {"config", "run_manager", "callbacks"}


def make_decision(approved: bool, comment: str = "") -> str:
    """决议 → resume 字符串（纯函数）。"""
    head = "approved" if approved else "rejected"
    return f"{head}:{comment}" if comment else head


def parse_decision(resume: Any) -> tuple[bool, str]:
    """resume 值 → (approved, comment)（纯函数，容错：未知值按拒绝处理）。"""
    s = str(resume or "")
    head, _, comment = s.partition(":")
    return head.strip() == "approved", comment.strip()


def preview_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    """工具入参 → 审批卡预览（纯函数）：剔除注入参数、截断长值、保证可 JSON 化。"""
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        if k in _INJECTED_KEYS or k.startswith("_"):
            continue
        try:
            json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            v = str(v)
        if isinstance(v, str) and len(v) > _PREVIEW_MAX_CHARS:
            v = v[:_PREVIEW_MAX_CHARS] + "…"
        out[k] = v
    return out


def _accepted_kwargs(fn: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """按原函数签名过滤 kwargs（原函数不收 config/run_manager 时不得强塞）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in sig.parameters}


def wrap_with_approval(
    tools: Sequence[BaseTool],
    approval_tools: Sequence[str],
    *,
    reason: str = DEFAULT_REASON,
) -> list[BaseTool]:
    """把列表中名字命中 approval_tools 的工具替换为审批门包装副本（其余原样）。

    用 model_copy 生成副本——不改共享单例（calculator 等模块级工具被两张图引用）。
    """
    protected = set(approval_tools or [])
    out: list[BaseTool] = []
    for t in tools:
        out.append(_wrap_one(t, reason) if t.name in protected else t)
    return out


def _wrap_one(tool: BaseTool, reason: str) -> BaseTool:
    orig_coro = getattr(tool, "coroutine", None)
    orig_func = getattr(tool, "func", None)
    content_and_artifact = getattr(tool, "response_format", "") == "content_and_artifact"
    name = tool.name

    def _reject_value(comment: str) -> Any:
        text = f"已被人工拒绝{('：' + comment) if comment else ''}，未执行 {name}。"
        return (text, None) if content_and_artifact else text

    # config 必须显式声明（带 RunnableConfig 注解）——否则 StructuredTool._arun 不会注入
    # 它，原工具拿到 config=None：script_runner 读不到附件白名单、knowledge_search 丢
    # kb 安全保证、write_report 的 run_id 错乱致产物 404。注入的 config 需原样转发。
    async def gate(config: RunnableConfig = None, **kwargs: Any) -> Any:  # type: ignore[assignment]
        # interrupt 之前禁止任何副作用（重放语义：本段代码 resume 时重跑）。
        decision = interrupt(
            {
                "approval_id": uuid.uuid4().hex[:12],
                "tool": name,
                "input": preview_args(kwargs),
                "reason": reason,
            }
        )
        approved, comment = parse_decision(decision)
        if not approved:
            return _reject_value(comment)
        forwarded = {**kwargs, "config": config}  # 按原函数签名过滤（不收 config 者自动剔除）
        if orig_coro is not None:
            return await orig_coro(**_accepted_kwargs(orig_coro, forwarded))
        if orig_func is not None:
            return await asyncio.to_thread(orig_func, **_accepted_kwargs(orig_func, forwarded))
        raise RuntimeError(f"tool {name} has no callable")  # 结构性错误，不该发生

    return tool.model_copy(update={"coroutine": gate, "func": None})


def first_interrupt_payload(state: Any) -> Optional[dict[str, Any]]:
    """从 graph.aget_state() 快照提取首个 pending interrupt 的审批载荷（纯函数）。

    v1 约束：每超步单审批（多并发 interrupt 不支持——resume 字符串只喂首个）。
    """
    for task in getattr(state, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            value = getattr(intr, "value", None)
            if isinstance(value, dict) and value.get("approval_id"):
                return value
    return None
