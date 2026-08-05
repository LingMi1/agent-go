"""会话历史的「token 预算·近期优先」只读投影（纯逻辑，无 I/O、无 LLM）。

把 checkpoint 里累积的 messages 裁剪成有界视图再喂模型（不改已落库事实、不写 events）：
- 恒保留 system 锚点 + 首条 Human（会话意图）；
- 从最新往旧保留最近窗口，直至 max_messages / max_chars 预算用尽；
- 被挤出的较旧轮次折叠成 1 条确定性摘要 SystemMessage（默认拼接，LLM 压缩走 summarize_fn seam）；
- **绝不拆散 tool_use↔tool_result 配对**（AIMessage(tool_calls) 与其 ToolMessage 作为原子组同去同留），
  否则 Anthropic 会因 orphan tool_use/tool_result 报 400——这是本模块最需被测试钉死的不变量。

「源无关」：本函数只认 messages 序列，不关心来自 checkpoint 还是（未来）events 账本重建，故两种记忆
方案都能原样复用（见 docs/06 §3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

SUMMARY_PREFIX = "〔前情摘要〕"

# 合成应答文案：让模型明确知道该工具调用没有产生结果（而非凭空猜测）。
DANGLING_TOOL_NOTE = "（该工具调用执行被中断，未产生结果）"


def _expected_tool_call_ids(m: AIMessage) -> list[str]:
    """出站请求会携带的全部 tool_call id：tool_calls ∪ invalid_tool_calls。

    关键陷阱：JSON 参数损坏的调用落在 `invalid_tool_calls`（`.tool_calls` 为空），
    但 langchain-openai 序列化出站 assistant 消息时**两者都会算进 tool_calls**——
    只按 `.tool_calls` 判断会漏掉 invalid 项，缺应答 → provider 400（实测 DeepSeek
    深研长跑中 planning 参数被截断即触发，线程从此每轮 400）。
    """
    ids: list[str] = []
    for tc in [*(getattr(m, "tool_calls", None) or []), *(getattr(m, "invalid_tool_calls", None) or [])]:
        tcid = str((tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")) or "")
        if tcid:
            ids.append(tcid)
    return ids


def repair_dangling_tool_calls(messages: list[AnyMessage]) -> list[AnyMessage]:
    """把消息序列修复成 provider 合法形态（只读投影，不回写 state/checkpoint）。

    三类病态都会让 DeepSeek/OpenAI/Anthropic 直接 400，且一旦进入 checkpoint 线程，
    该会话每一轮都失败（永久污染）：
    - **悬空 tool_calls**：AIMessage 的调用缺少紧随的 ToolMessage 应答——工具执行
      崩溃当轮 think 已提交、tools 未提交时产生 → 紧随其组补一条合成 error ToolMessage；
    - **invalid_tool_calls**（JSON 损坏）：同样会被序列化进出站 tool_calls，一并补应答；
      无 id 的 invalid 项无法应答 → 从投影副本中剥除（连同 additional_kwargs 里的原始
      tool_calls，防序列化兜底路径把它捞回来）；
    - **孤儿 ToolMessage**：无前置 tool_call 应答对象 → 丢弃。

    健康序列原样返回（不复制、不改动），因此可无条件挂在每次入模型前。
    """
    out: list[AnyMessage] = []
    changed = False
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        if isinstance(m, AIMessage) and (
            getattr(m, "tool_calls", None) or getattr(m, "invalid_tool_calls", None)
        ):
            head: AIMessage = m
            has_dangling_invalid = any(
                not str((tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")) or "")
                for tc in getattr(m, "invalid_tool_calls", None) or []
            )
            if has_dangling_invalid:
                # 无 id 的损坏调用没法配 ToolMessage：投影副本里剥掉（含 additional_kwargs
                # 原始 tool_calls，避免序列化兜底路径复原它）。
                kwargs = {k: v for k, v in (m.additional_kwargs or {}).items() if k != "tool_calls"}
                head = m.model_copy(update={"invalid_tool_calls": [], "additional_kwargs": kwargs})
                changed = True
            group: list[AnyMessage] = [head]
            answered: set[str] = set()
            j = i + 1
            while j < n and isinstance(messages[j], ToolMessage):
                group.append(messages[j])
                answered.add(str(getattr(messages[j], "tool_call_id", "") or ""))
                j += 1
            for tcid in _expected_tool_call_ids(head):
                if tcid not in answered:
                    group.append(
                        ToolMessage(content=DANGLING_TOOL_NOTE, tool_call_id=tcid, status="error")
                    )
                    changed = True
            out.extend(group)
            i = j
        elif isinstance(m, ToolMessage):
            changed = True  # 孤儿：丢弃
            i += 1
        else:
            out.append(m)
            i += 1
    return out if changed else messages


@dataclass(frozen=True)
class HistoryPolicy:
    """记忆投影预算。max_chars 用字符近似 token（对齐原项目 TokenCounter 的字符估算）。"""

    max_messages: int = 40
    max_chars: int = 24000


@dataclass
class HistoryReduction:
    messages: list[AnyMessage]
    summarized: bool = False
    dropped_count: int = 0
    dropped_groups: int = field(default=0)


# 一张图按固定字符数估价（对齐 HistoryPolicy「max_chars 用字符近似 token」的约定，
# ~1600 字符 ≈ Anthropic 常规图片 token 量级）。绝不能用 len(base64)：单图 base64
# 可达数十万字符，会瞬间打爆预算、错误触发裁剪，还会经摘要把 base64 灌回模型。
IMAGE_CHAR_COST = 1600

# 视为"图片类"的内容块 type（含 M8 的 pro_attachment 引用块——展开前也按图估价）。
_IMAGE_BLOCK_TYPES = {"image", "image_url", "pro_attachment"}


def _text(msg: AnyMessage) -> str:
    """消息的可见文本（多模态块列表：text 块拼接 + 图片块记 [image] 占位）。

    摘要（_summarize_default 的 gist）与展示都走这里——base64 永不出现在返回值中。
    """
    c = getattr(msg, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: list[str] = []
        for b in c:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    parts.append(str(b.get("text", "")))
                elif t in _IMAGE_BLOCK_TYPES:
                    parts.append("[image]")
                # 其他块（tool_use 等）不计入可见文本
        return "".join(parts)
    return str(c)


def _char_cost(msg: AnyMessage) -> int:
    """预算用的字符成本：文本按长度，图片块按 IMAGE_CHAR_COST 固定估价。"""
    c = getattr(msg, "content", "")
    if isinstance(c, list):
        cost = 0
        for b in c:
            if isinstance(b, str):
                cost += len(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    cost += len(str(b.get("text", "")))
                elif t in _IMAGE_BLOCK_TYPES:
                    cost += IMAGE_CHAR_COST
        return cost
    return len(_text(msg))


def _total_chars(messages: list[AnyMessage]) -> int:
    return sum(_char_cost(m) for m in messages)


def _group_body(body: list[AnyMessage]) -> list[list[AnyMessage]]:
    """把 body 分成原子组：AIMessage(tool_calls) + 其后紧邻的 ToolMessage 归为一组。"""
    groups: list[list[AnyMessage]] = []
    i = 0
    n = len(body)
    while i < n:
        m = body[i]
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            grp = [m]
            j = i + 1
            while j < n and isinstance(body[j], ToolMessage):
                grp.append(body[j])
                j += 1
            groups.append(grp)
            i = j
        else:
            groups.append([m])
            i += 1
    return groups


def _summarize_default(dropped: list[list[AnyMessage]]) -> str:
    """确定性折叠：每个被挤出的组取一行要点。"""
    lines = [SUMMARY_PREFIX + "（较早对话已折叠，仅保留要点）"]
    for grp in dropped:
        head = grp[0]
        role = type(head).__name__.replace("Message", "").lower() or "msg"
        gist = _text(head).strip().replace("\n", " ")
        if not gist and isinstance(head, AIMessage) and head.tool_calls:
            gist = "调用工具 " + ", ".join(str(tc.get("name", "")) for tc in head.tool_calls)
        if len(gist) > 120:
            gist = gist[:120] + "…"
        lines.append(f"- [{role}] {gist}")
    return "\n".join(lines)


def plan_history_reduction(
    messages: list[AnyMessage],
    policy: HistoryPolicy,
    *,
    summarize_fn: Optional[Callable[[list[list[AnyMessage]]], str]] = None,
) -> HistoryReduction:
    """把 messages 投影成不超预算的有界视图。未超预算则原样返回。"""
    messages = list(messages)
    if len(messages) <= policy.max_messages and _total_chars(messages) <= policy.max_chars:
        return HistoryReduction(messages=messages, summarized=False)

    # 1) 锚点：连续前导 system + 首条 Human。
    anchor_end = 0
    while anchor_end < len(messages) and isinstance(messages[anchor_end], SystemMessage):
        anchor_end += 1
    if anchor_end < len(messages) and isinstance(messages[anchor_end], HumanMessage):
        anchor_end += 1
    anchors = messages[:anchor_end]
    body = messages[anchor_end:]

    # 2) body 分原子组（保护 tool 配对）。
    groups = _group_body(body)

    # 3) 从尾部保留最近组，预算 = 总预算扣除锚点与 1 条摘要占位。
    budget_msgs = max(policy.max_messages - len(anchors) - 1, 1)
    budget_chars = max(policy.max_chars - _total_chars(anchors), 1)
    kept: list[list[AnyMessage]] = []
    used_msgs = 0
    used_chars = 0
    for grp in reversed(groups):
        g_msgs = len(grp)
        g_chars = _total_chars(grp)
        if kept and (used_msgs + g_msgs > budget_msgs or used_chars + g_chars > budget_chars):
            break
        kept.insert(0, grp)  # 维持原序
        used_msgs += g_msgs
        used_chars += g_chars
    kept_count = len(kept)
    dropped_groups = groups[: len(groups) - kept_count]

    recent = [m for grp in kept for m in grp]
    if not dropped_groups:
        # 全部近期组都在预算内（说明是锚点/单组过大），无可折叠，原样返回。
        return HistoryReduction(messages=anchors + recent, summarized=False)

    summary_text = (summarize_fn or _summarize_default)(dropped_groups)
    summary_msg = SystemMessage(content=summary_text)
    dropped_msg_count = sum(len(g) for g in dropped_groups)
    return HistoryReduction(
        messages=anchors + [summary_msg] + recent,
        summarized=True,
        dropped_count=dropped_msg_count,
        dropped_groups=len(dropped_groups),
    )
