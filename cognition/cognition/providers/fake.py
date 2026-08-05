"""确定性脚本化模型，用于无需真实 LLM key 的端到端验证。

由 COGNITION_FAKE_MODEL=1 在服务启动时启用：
- ReAct（agent_type=react）：默认先调用一次 calculator(2*(3+4))，再给最终答案；
  生图模式（消息里出现 IMAGE_GEN_INSTRUCTION 的 leading SystemMessage）下第一步改调
  image_generate（prompt/mask/source_images 从最后一条 human 文本解析），再收尾出结论——
  否则 FAKE 全家桶 E2E 永远无人下单出图（COGNITION_IMAGE_GEN_PROVIDER=fake 只保证
  provider 离线可用）。
- Plan-Execute（agent_type=plan_solve）：planner 产出 2 步计划（步骤1 含两个 <sep> 子任务、
  步骤2 单任务），executor 每个子任务做一次 calculator 调用，最后 finish→summary。

切换到真实模型只需去掉该开关。

并行安全：服务长驻、多 run 并发复用同一个 fake 模型实例（Plan-Execute 的 executor 还会
并行分支复用），因此所有 fake **必须无状态**（按传入 messages 决策，而非可变 idx 计数），
否则并行会相互踩踏（idx 串号）、且跨 run 泄漏状态。`ScriptedChatModel`（idx 版）仅留给
单测按脚本喂消息的场景。
"""

from __future__ import annotations

import re
from typing import Any, Callable, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class ScriptedChatModel(BaseChatModel):
    """按预设脚本逐轮返回消息的 fake 模型（支持 bind_tools 与流式）。"""

    responses: List[AIMessage] = []
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":  # noqa: ARG002
        return self

    def _next(self) -> AIMessage:
        msg = self.responses[self.idx % len(self.responses)]
        self.idx += 1
        return msg

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        msg = self._next()
        if msg.content:
            yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content))
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", tool_calls=list(msg.tool_calls or []))
        )


# 生图模式标记：metadata.image_gen 置位时 think 节点会临时前置一条含
# IMAGE_GEN_INSTRUCTION（graphs/nodes.py）的 leading SystemMessage。fake 取其稳定
# 子串做无状态分支判定；与源常量的同步由 test_fake_image_gen 钉住。
IMAGE_GEN_MARKER = "【生图模式已开启】"

# 生图工作区 query 模板的确切措辞（web/src/components/GenerateWorkspace.tsx）：
# - 底图："。以我上传的图片为底图进行修改（图生图）。"
# - 蒙版："使用蒙版文件 <name> 对底图做局部重绘（inpaint），蒙版透明区域=需要重绘的区域。"
_SOURCE_PHRASE = "以我上传的图片为底图"
_MASK_RE = re.compile(r"使用蒙版文件\s*(\S+?)\s*对底图")
# 附件清单注记（attachments.attachment_note）："〔用户上传附件：name（mime, size）、…〕"
_ATT_NOTE_RE = re.compile(r"〔用户上传附件：(.+?)〕")
_ATT_ITEM_RE = re.compile(r"([^、]+?)（([^,）]*),\s*[^）]*）")


def _text_of(message: BaseMessage) -> str:
    """取消息纯文本（str 或 content blocks 的 text 块拼接）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _last_human_text(messages: List[BaseMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return _text_of(m)
    return ""


def _image_gen_requested(messages: List[BaseMessage]) -> bool:
    return any(isinstance(m, SystemMessage) and IMAGE_GEN_MARKER in _text_of(m) for m in messages)


def _image_gen_args(text: str) -> dict:
    """从最后一条 human 文本解析 image_generate 参数（prompt / mask / source_images）。"""
    args: dict[str, Any] = {"prompt": text}
    mask: Optional[str] = None
    m = _MASK_RE.search(text)
    if m:
        mask = m.group(1)
        args["mask"] = mask
    if _SOURCE_PHRASE in text or mask:
        # 底图文件名不在 query 里，而在附件清单注记里：取图片附件名（剔除蒙版自身）。
        note = _ATT_NOTE_RE.search(text)
        names = [
            item.group(1).strip()
            for item in _ATT_ITEM_RE.finditer(note.group(1))
            if item.group(2).strip().startswith("image/") and item.group(1).strip() != mask
        ] if note else []
        if names:
            args["source_images"] = names
    return args


def _fake_react_decide(messages: List[BaseMessage]) -> AIMessage:
    """fake ReAct（无状态）：本轮工具结果已回来→收尾；生图模式→调 image_generate；否则 calculator。

    以「最后一条消息是否 ToolMessage」判轮内阶段（think 只会在 human 输入后或工具结果后
    被调用），多轮续聊时新 human 消息垫底 → 每轮都重新走一次工具调用，与旧 idx 循环脚本
    的按轮行为一致，且并发 run 复用同一实例不串号。
    """
    if messages and isinstance(messages[-1], ToolMessage):
        if _image_gen_requested(messages):
            return AIMessage(content="图片已生成完成，请在产物区查看。")
        return AIMessage(content="答案是 14。")
    if _image_gen_requested(messages):
        return AIMessage(
            content="我来生成图片。",
            tool_calls=[
                {
                    "name": "image_generate",
                    "args": _image_gen_args(_last_human_text(messages)),
                    "id": "call_img_1",
                }
            ],
        )
    return AIMessage(
        content="我先用计算器算一下。",
        tool_calls=[{"name": "calculator", "args": {"expression": "2*(3+4)"}, "id": "call_1"}],
    )


def build_fake_model() -> "MessageDrivenChatModel":
    """ReAct fake：默认 calculator(2*(3+4))→答案；生图模式改调 image_generate→收尾。

    无状态 message-driven（服务长驻、并发 run 共享实例，禁 idx 计数）。
    """
    return MessageDrivenChatModel(decide=_fake_react_decide)


class MessageDrivenChatModel(BaseChatModel):
    """无状态 fake 模型：按传入 messages 决定返回，可安全并行复用。

    `decide(messages) -> AIMessage` 不得依赖任何可变实例状态，从而对并行分支与跨 run 复用安全。
    """

    decide: Callable[[List[BaseMessage]], AIMessage]

    @property
    def _llm_type(self) -> str:
        return "message-driven-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "MessageDrivenChatModel":  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        return ChatResult(generations=[ChatGeneration(message=self.decide(list(messages)))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        msg = self.decide(list(messages))
        if msg.content:
            yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content))
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", tool_calls=list(msg.tool_calls or []))
        )


def _has_prior_plan(messages: List[BaseMessage]) -> bool:
    """历史中是否已有 planning 工具调用（用于区分 create / replan，无状态）。"""
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                if (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) == "planning":
                    return True
    return False


def _fake_planner_decide(messages: List[BaseMessage]) -> AIMessage:
    """fake planner：首轮产出 2 步计划（步骤1 两个 <sep> 子任务、步骤2 单任务）；其后只思考、不改步骤。

    计划推进由确定性 plan-lifecycle 完成（mark_step_completed 自动推进/收尾），因此 replan 轮
    不必再产出步骤——只给一段思考文本即可（plan_thought）。
    """
    if not _has_prior_plan(messages):
        return AIMessage(
            content="我来把任务拆成可并行的步骤。",
            tool_calls=[
                {
                    "name": "planning",
                    "args": {
                        "command": "create",
                        "title": "计算并汇总",
                        "steps": ["计算 2+3<sep>计算 4*5", "计算 10-1"],
                    },
                    "id": "plan_create",
                }
            ],
        )
    return AIMessage(content="上一轮子任务已完成，继续推进计划。")


def _fake_executor_decide(messages: List[BaseMessage]) -> AIMessage:
    """fake executor（无状态）：未执行过工具→调一次 calculator；已有工具结果→给最终答复。"""
    has_tool_result = any(isinstance(m, ToolMessage) for m in messages)
    if has_tool_result:
        return AIMessage(content="子任务完成。")
    return AIMessage(
        content="我用计算器算一下。",
        tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "call_1"}],
    )


def _fake_rag_decide(messages: List[BaseMessage]) -> AIMessage:
    """fake RAG 模型：按提示词关键字分派 route/expand/reflect/generate 响应（无状态）。"""
    text = "".join(str(getattr(m, "content", "")) for m in messages)
    if "只回答 YES 或 NO" in text:  # route
        simple = any(g in text for g in ("你好", "谢谢", "hello", "hi"))
        return AIMessage(content="NO" if simple else "YES")
    if "拆解成" in text:  # expand
        return AIMessage(content="子问题一\n子问题二")
    if "是否足够" in text:  # reflect：一轮即判足够
        return AIMessage(content='{"is_answer": true, "rewrite_query": ""}')
    if "直接" in text:  # direct（simple 路径）
        return AIMessage(content="这是直接回答。")
    return AIMessage(content="根据检索到的证据作答〔1〕。")  # generate


def build_fake_rag_model() -> MessageDrivenChatModel:
    """RAG 子图的 fake 模型（无状态、按提示词关键字分派）。"""
    return MessageDrivenChatModel(decide=_fake_rag_decide)


def build_fake_plan_model() -> MessageDrivenChatModel:
    """plan_solve 的 fake planner（无状态、可安全并行/跨 run 复用）。"""
    return MessageDrivenChatModel(decide=_fake_planner_decide)


def build_fake_executor_model() -> MessageDrivenChatModel:
    """plan_solve 的 fake executor（无状态、可安全并行复用）。"""
    return MessageDrivenChatModel(decide=_fake_executor_decide)
