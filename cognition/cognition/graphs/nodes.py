"""ReAct 图的节点实现。

`make_think_node(model)` 返回 `agent`/think 节点：调用绑定了工具的模型，产出一条
AIMessage（可能含 tool_calls），并把 step +1。模型通过工厂注入，便于测试传入 fake。

act（工具执行）节点直接复用 `langgraph.prebuilt.ToolNode`，不在此重造。
"""

from __future__ import annotations

from typing import Callable, Optional

import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from cognition.graphs.guard import check_input_injection, inject_safety_suffix
from cognition.graphs.history import (
    HistoryPolicy,
    plan_history_reduction,
    repair_dangling_tool_calls,
)
from cognition.graphs.state import AgentState


def format_prompt_from_config(config: Optional[RunnableConfig], prompts: dict[str, str]) -> str:
    """从 config.metadata 取 output_format 并映射为提示词（未知值/缺省→空串）。

    per-run 值走 config 而非 state：绝不把 SystemMessage 写进 checkpoint——
    持久化后不同格式的多轮 run 会累积互相矛盾的指令，且中位 system 消息会被
    langchain-anthropic 直接拒绝（续聊切 Claude 即 400）。
    """
    if not config or not prompts:
        return ""
    fmt = str((config.get("metadata") or {}).get("output_format", "") or "")
    return prompts.get(fmt, "")


# 生图模式指令（Composer 生图开关置位 → metadata.image_gen）：引导模型可靠调用
# image_generate（含图生图 source_images），并在指定输出格式时用对应技能把图嵌入。
IMAGE_GEN_INSTRUCTION = (
    "【生图模式已开启】用户希望本轮生成图片。请务必调用 image_generate 工具产出图片："
    "prompt 写清主体/风格/构图/光影（可先查 image-style-library 技能的风格模板）。"
    "若用户上传了图片附件，把其文件名填入 image_generate 的 source_images 参数做图生图/编辑。"
    "若用户还附了蒙版文件（要求局部重绘/inpaint 时），把蒙版文件名填入 image_generate 的 "
    "mask 参数（蒙版透明区域=要重绘的区域），底图文件名仍填入 source_images。"
    "若同时指定了输出格式（如网页/文档/PPT），先生成图片，再用对应技能"
    "（frontend-design 做网页、ppt-generation 做文档/PPT）把生成的图片与文字一起编排进最终产物。"
)


def leading_prompt_from_config(
    config: Optional[RunnableConfig], format_prompts: Optional[dict[str, str]]
) -> str:
    """把 image_gen 指令与 output_format 提示词拼成**一条** leading system 文本（可空）。

    合并成一条：多条中位/前置 system 消息在续聊切 Claude 时会被 langchain-anthropic 拒；
    单条 leading 是验证过的安全形态。两者正交，各有则用空行分隔。
    """
    parts: list[str] = []
    meta = (config.get("metadata") or {}) if config else {}
    if str(meta.get("image_gen", "") or "").lower() in ("1", "true", "yes"):
        parts.append(IMAGE_GEN_INSTRUCTION)
    fmt = format_prompt_from_config(config, format_prompts or {})
    if fmt:
        parts.append(fmt)
    return "\n\n".join(parts)


def make_think_node(
    model: BaseChatModel,
    *,
    history_policy: Optional[HistoryPolicy] = None,
    expander: Optional[Callable[[list], list]] = None,
    format_prompts: Optional[dict[str, str]] = None,
    max_tool_calls: int = 0,
) -> Callable[[AgentState], dict]:
    """构造 think 节点（闭包注入模型）。

    注意：节点内用 `model.invoke`。在 `graph.astream_events(version="v2")` 上下文中，
    LangGraph 会请求流式，BaseChatModel 据此走 `_stream` 路径，从而产出 token 流。

    入模型前三道只读投影（都不改 state、不写 events），顺序固定：
    1. `repair_dangling_tool_calls`：修复悬空 tool_calls/孤儿 ToolMessage——工具崩溃
       当轮留下的病态 checkpoint 会让此后每轮 provider 400（线程永久污染），修复投影
       让已污染的旧会话自动痊愈；
    2. 若给定 history_policy，做「token 预算·近期优先」裁剪（附件引用块按固定估价，
       见 history.IMAGE_CHAR_COST）；
    3. 若给定 expander，把 pro_attachment 引用块展开为真实内容（base64 图片/占位文本）
       ——放最后：裁剪按占位估价，展开后的大 base64 只活在本次模型调用里。

    max_tool_calls：每次图执行（=每个研究/执行分支）的**工具调用硬预算**（0=不限）。
    动机（实测）：DeepSeek 每轮并行发大量工具调用×研究分支数，曾单 run 961 次 web_fetch
    把整轮拖到 RUN_TIMEOUT_S、烧穿 Tavily 免费额度与模型余额——提示词软约束收敛有限，
    必须在节点层硬兜底。实现：已执行 ToolMessage 数达预算 → 前置「预算已尽」指令并以
    `tool_choice="none"` 调用，模型只能基于已有信息收口（超发的并行调用最多溢出一轮）。
    """

    def think(state: AgentState, config: RunnableConfig = None) -> dict:  # type: ignore[assignment]
        step = int(state.get("step", 0))
        logger = logging.getLogger(__name__)

        # —— Prompt Injection guard: layer 2 — prompt isolation (safety suffix appended to SystemMessage) ——
        # On step==0, check the first user input for injection patterns. If detected,
        # return a safe rejection without forwarding to the LLM (defense in depth: never trust user input).
        if step == 0:
            user_msgs = [m for m in state["messages"] if isinstance(m, HumanMessage)]
            if user_msgs:
                user_text = str(user_msgs[-1].content)
                safe, reason = check_input_injection(user_text)
                if not safe:
                    logger.warning("prompt_injection_blocked: reason=%r", reason)
                    return {
                        "messages": [
                            AIMessage(
                                content=(
                                    "抱歉，您的输入触发了安全检测（{}）。"
                                    "请重新描述您的需求，不要包含系统指令。"
                                ).format(reason)
                            )
                        ],
                        "step": step + 1,
                    }

        messages = repair_dangling_tool_calls(state["messages"])
        used_tool_calls = sum(1 for m in messages if isinstance(m, ToolMessage))
        if history_policy is not None:
            messages = plan_history_reduction(messages, history_policy).messages
        if expander is not None:
            messages = expander(messages)
        # 生图指令 + 输出格式：调用期临时前置**单条** leading SystemMessage（只活在本次
        # invoke，不进 checkpoint；plan 的 executor 分支经 metadata spread 免费获得）。
        prefix = leading_prompt_from_config(config, format_prompts)
        if prefix:
            # Append safety instruction to SystemMessage (Prompt Injection guard: prompt isolation layer)
            prefix = inject_safety_suffix(prefix)
            messages = [SystemMessage(content=prefix), *messages]
        if max_tool_calls > 0 and used_tool_calls >= max_tool_calls:
            budget_note = SystemMessage(
                content=(
                    f"工具调用预算已用尽（本任务已执行 {used_tool_calls} 次，上限 {max_tool_calls} 次）。"
                    "禁止再调用任何工具。立即基于已获得的信息给出最终结论；"
                    "信息不足之处如实说明，不要编造。"
                )
            )
            ai_msg = model.invoke([budget_note, *messages], tool_choice="none")
        else:
            ai_msg = model.invoke(messages)
        return {"messages": [ai_msg], "step": step + 1}

    return think
