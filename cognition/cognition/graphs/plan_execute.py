"""Plan-Execute 编排图（plan → executor 子图 → summary，含动态 replan 与可控并行子任务）。

映射原项目 Step2PlanExecuteNode 的外层循环（非字面照搬，落到 LangGraph 拓扑）：

    START → sop_recall → planner ──route_after_planner──▶ Send("executor", …) ×N（并行，信号量限宽）
                            ▲                              └──(计划完成/超步/ERROR)──▶ summary → END
                            └──── executor（复用 M1 ReAct 子图）──(分支 join，sub_results 经 reducer 合并)──┘

确定性 plan-lifecycle 管步骤状态（graphs/plan_lifecycle.py）；LLM 只产出步骤列表/思考。
executor 复用 M1 `build_react_graph`（原 ExecutorAgent 即任务级 ReAct）。

★ 并行控制（框架给 vs 我控制，见 docs/03 §4）★
① 并行宽度：`Send` 无内建上限 → executor 节点内用 `asyncio.Semaphore(max_parallel)` 限流
   （按 running loop 取，跨 loop 安全、不跨 run/测试泄漏）。
② reducer 并发安全：`merge_sub_results` 可交换/可结合、按 (round, branch_id) 去重、规范排序。
③ 每分支预算/超时：`asyncio.wait_for` + try/except → SubResult(status="error")，不抛出（其他分支存活）。
④ tool_call_id 跨分支唯一：executor 把 branch_id 注入子图 config.metadata，mapper 据此给
   tool_call_id 加分支前缀（并行共用 fake 模型也不会撞 id）。
⑤ 状态归约 ERROR>IDLE>FINISHED：planner 消费 sub_results 时用 `reduce_substate` 计算。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Annotated, Any, Awaitable, Callable, Optional, Sequence, TypedDict

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from cognition.graphs.history import repair_dangling_tool_calls
from cognition.graphs.plan_lifecycle import (
    Plan,
    all_completed,
    create,
    current_step,
    current_step_index,
    ensure_executable,
    mark_step_completed,
    set_note,
    update_remaining,
)

logger = logging.getLogger(__name__)

# 子任务字面量分隔符（与原项目一致）。
SEP = "<sep>"

# 自定义事件名（mapper 据此产出 plan / task / result）。
EVENT_PLAN = "plan"
EVENT_TASK = "task"
EVENT_RESULT = "result"
# 评测/trace 可消费的内部事件：在信号量真正获得与释放的位置发出，因此可以区分
# “LangGraph 已调度分支”与“executor 实际占用并发槽”。EventMapper 未映射这两类事件，
# 对现有 gRPC/SSE 契约零影响。
EVENT_BRANCH_START = "orchestration_branch_start"
EVENT_BRANCH_END = "orchestration_branch_end"

# 状态归约取值。
STATE_FINISHED = "finished"
STATE_IDLE = "idle"
STATE_ERROR = "error"

PLANNER_SYSTEM = (
    "你是任务规划器。把用户任务拆解为有序、可执行的步骤，并调用 planning 工具产出 "
    "{title, steps}。同一步骤内若包含可并行的子任务，用字面量 <sep> 分隔。\n"
    "{{sop}}\n"
    "硬性要求：每个步骤必须**自包含**——执行者看不到本对话的完整历史，禁止使用"
    "「上面/之前/该文件」等对话指代；若步骤需要引用对话中的内容，把所需关键要点"
    "直接写进步骤文本。产出必须明确（生成文件的步骤要点名用 write_report 工具）。"
)

def compose_planner_system(
    planner_system: str, sop: str, output_format: str, format_prompts: dict[str, str],
    image_gen: bool = False,
) -> str:
    """拼 planner 系统提示词（纯函数）：{{sop}} 槽 + per-run 输出格式模板（未知值忽略）。"""
    text = planner_system.replace("{{sop}}", sop)
    parts = [text]
    if image_gen:
        # B.4：与 react think 的 IMAGE_GEN_INSTRUCTION 对齐——让 planner 把"生成图片"
        # 显式排进计划步骤（executor 执行时再由自身 think 节点拿到同指令）。
        from cognition.graphs.nodes import IMAGE_GEN_INSTRUCTION

        parts.append(IMAGE_GEN_INSTRUCTION)
    extra = format_prompts.get(output_format or "", "")
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


# 深度研究模式的规划器提示词（M9：agent_type=deep_research 用第二编译图，
# 拓扑与 plan_solve 完全相同，仅提示词与轮次预算不同——"配置化变体而非新图"）。
RESEARCH_PLANNER_SYSTEM = (
    "你是深度研究规划器。面对研究型问题，把调研拆解为有序步骤并调用 planning 工具产出 "
    "{title, steps}：先**广度检索**，再**交叉验证**关键论断（不同角度/来源互证），最后"
    "**汇总**为带〔n〕引用的结论。需要外部网络信息时（若工具可用）：广度检索用 web_search "
    "拿候选来源，关键来源用 web_fetch 深读原文，知识库检索用 knowledge_search。"
    "同一步骤内可并行的检索用字面量 <sep> 分隔。\n"
    "{{sop}}\n"
    "计划固定为 **3~4 步、不得超过 4 步**：① 广度检索步——把调研面用 <sep> 拆成 2~4 个"
    "并行子任务（禁止把整个调研塞进一个子任务）；② 交叉验证/补缺步——针对首轮结果的"
    "缺口与关键论断补充检索；③（可选）单一主题针对性深挖；④ 汇总步——整合全部证据、"
    "点名用 write_report 工具产出最终研究报告文件。执行预算有限：步数超过 4 或反复"
    "replan 会导致任务被强制截断，宁可每步做实。\n"
    "硬性要求：每个步骤必须自包含（执行者看不到对话历史，禁止「上面/之前」等指代，"
    "要查的问题与期望证据写进步骤文本）；**子任务粒度要小而聚焦**——单个子任务只覆盖"
    "1~2 个主题、约 2~3 次搜索能完成（每分支有执行时限，塞太多主题会超时作废）；"
    "结论只能基于检索到的证据并标注〔n〕引用，证据不足必须如实说明而非编造。"
)


# ——————————————————————————————————————————————————————————————
# planning 工具（规划器的结构化输出通道）与解析
# ——————————————————————————————————————————————————————————————
class PlanningArgs(BaseModel):
    """planning 工具入参。"""

    command: str = Field(description="create=新建计划；update=更新剩余步骤")
    title: str = Field(description="计划标题")
    steps: list[str] = Field(
        description="有序步骤列表；同一步骤内可并行的子任务用字面量 <sep> 分隔"
    )


@tool("planning", args_schema=PlanningArgs)
def planning_tool(command: str, title: str, steps: list[str]) -> str:
    """创建或更新任务计划。这是规划器的结构化输出通道：调用即视为计划登记成功。

    注意：本工具只绑定给 planner 模型（grpc_server 装配期），从不进入 executor 工具集；
    planner 节点解析调用参数驱动确定性 plan-lifecycle，并落一条 ack ToolMessage 保证
    历史配对合法（真实 provider 要求 tool_calls 必有应答）。
    """
    return "计划已登记"


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_planning_text(text: str) -> Optional[dict]:
    """从正文提取计划 JSON（模型未走工具调用时的兜底）。

    真实模型（尤其思考模型）即使绑定了工具也可能把计划 JSON 写进正文；此前没有该
    兜底 → 解析恒失败 → 永远退化"单步计划=原句"。优先 ```json 围栏，其次首个平衡
    {} 块；必须含非空 steps 列表才算计划。
    """
    if not text:
        return None
    candidates = _JSON_FENCE.findall(text)
    if not candidates:
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        candidates = [text[start : i + 1]]
                        break
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:  # noqa: BLE001 — 非法 JSON 跳过，继续下一个候选
            continue
        if isinstance(obj, dict) and isinstance(obj.get("steps"), list) and obj["steps"]:
            obj.setdefault("command", "create")
            return obj
    return None


def _planning_acks(ai: AIMessage) -> list[ToolMessage]:
    """给 planner 轮的**全部**工具调用补 ack 应答（planner 只解析、从不执行——
    历史里必须有 ToolMessage 配对，否则真实 provider 第二轮起 400）。

    覆盖三种情形：planning 正常调用；模型幻觉调用其他工具名（同样要应答，否则悬空）；
    **invalid_tool_calls**（JSON 参数损坏，`.tool_calls` 为空但出站序列化仍带上）——
    深研长跑中 planning 参数被截断即属此类，漏应答会让线程此后每轮 400。
    无 id 的调用由入模型前的 repair_dangling_tool_calls 投影剥除兜底。"""
    acks: list[ToolMessage] = []
    for tc in getattr(ai, "tool_calls", None) or []:
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
        if tcid:
            note = "计划已登记" if name == "planning" else "（该调用不被执行：规划阶段仅接受 planning 工具）"
            acks.append(ToolMessage(content=note, tool_call_id=str(tcid)))
    for tc in getattr(ai, "invalid_tool_calls", None) or []:
        tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
        if tcid:
            acks.append(
                ToolMessage(content="（调用参数无法解析，已忽略）", tool_call_id=str(tcid), status="error")
            )
    return acks


# ——————————————————————————————————————————————————————————————
# 状态与 reducer
# ——————————————————————————————————————————————————————————————
class SubResult(TypedDict, total=False):
    """单个并行子任务的执行结果（并回主状态的增量单元）。"""

    request_id: str  # 所属 run——同会话多次 run 的隔离键（缺失视为遗留数据）
    round: int
    branch_id: str
    task: str
    result: str
    observations: list[str]
    status: str  # finished | idle | error


def merge_sub_results(
    left: Optional[list[SubResult]], right: Optional[list[SubResult]]
) -> list[SubResult]:
    """sub_results 的并发安全 reducer：按 (request_id, round, branch_id) 去重、规范排序。

    可交换/可结合：同键只写一次（重复投递取首个），排序键确定，因此并行分支任意
    到达序合并结果一致。键含 request_id 是**同会话续聊**的关键：checkpointer 会把
    sub_results 跨 run 累积，若只按 (round, branch) 去重，新 run 的 (0, b0) 会被旧
    run 的同键结果顶掉（"取首个"），planner/summary 读到的全是上一次的旧内容。
    """
    merged: dict[tuple[Any, Any, Any], SubResult] = {}
    for item in list(left or []) + list(right or []):
        if not item:
            continue
        key = (str(item.get("request_id") or ""), item.get("round"), item.get("branch_id"))
        if key not in merged:
            merged[key] = item
    return [
        merged[k]
        for k in sorted(
            merged.keys(), key=lambda k: (k[0], k[1] if k[1] is not None else 0, str(k[2]))
        )
    ]


def results_for_round(
    sub_results: Optional[Sequence[SubResult]], request_id: str, rnd: int
) -> list[SubResult]:
    """取**本 run** 指定轮次的子任务结果（纯函数）。

    旧 run 的累积结果（含无 request_id 的遗留数据）一律排除——planner 审视的
    "上一轮执行结果"只能来自当前 run。
    """
    rid = str(request_id or "")
    return [
        r
        for r in (sub_results or [])
        if r and r.get("round") == rnd and str(r.get("request_id") or "") == rid
    ]


def build_context_digest(planner_messages: Optional[Sequence[Any]], max_chars: int = 3000) -> str:
    """从 planner 历史提取会话背景摘要（近期优先、预算截断，纯函数）。

    executor 分支按设计与会话历史隔离（子任务自包含）；但续聊场景里"把上面内容
    整理成报告"这类任务必须能看到指代对象。摘要随 Send 注入 executor 提示词——
    只取 Human/AI 的可见文本（跳过工具消息与空消息），从最新往旧填满预算后按
    时间顺序输出。
    """
    picked: list[str] = []
    used = 0
    for m in reversed(list(planner_messages or [])):
        if isinstance(m, HumanMessage):
            role = "用户"
        elif isinstance(m, AIMessage):
            role = "助手"
        else:
            continue
        content = getattr(m, "content", "")
        text = (content if isinstance(content, str) else str(content)).strip()
        if not text:
            continue
        entry = f"[{role}] {text}"
        if used + len(entry) > max_chars:
            remain = max_chars - used
            if remain > 20:
                picked.append(entry[:remain] + "…")
            break
        picked.append(entry)
        used += len(entry)
    return "\n".join(reversed(picked))


def reduce_substate(results: Sequence[SubResult]) -> str:
    """父状态归约（镜像 reduceParentState）：ERROR > IDLE > FINISHED。"""
    has_idle = False
    all_finished = True
    saw_any = False
    for r in results:
        saw_any = True
        st = (r or {}).get("status")
        if st == STATE_ERROR:
            return STATE_ERROR
        if st == STATE_IDLE:
            has_idle = True
        if st != STATE_FINISHED:
            all_finished = False
    if not saw_any:
        return STATE_FINISHED
    if has_idle:
        return STATE_IDLE
    return STATE_FINISHED if all_finished else STATE_IDLE


class PlanExecuteState(TypedDict, total=False):
    """Plan-Execute 图状态。"""

    query: str
    request_id: str
    session_id: str
    sop: str
    plan: Optional[Plan]
    round: int           # 当前 dispatch 轮（executor 子任务按此轮 tag/消费）
    step: int            # 预留
    reduced_state: str   # 上一轮并行归约结果（ERROR/IDLE/FINISHED）
    output_format: str   # M9：per-run 输出格式（planner system 拼接；镜像 sop 的注入模式）
    image_gen: bool      # B.4：生图开关（planner system 拼接生图引导）
    branches_used: int   # 已派发的执行分支累计数（全局分支预算，防步数×并行爆炸）
    planner_messages: Annotated[list, add_messages]
    sub_results: Annotated[list[SubResult], merge_sub_results]


# ——————————————————————————————————————————————————————————————
# 并行控制：信号量限宽 + 每分支超时（best-effort）
# ——————————————————————————————————————————————————————————————
async def run_branch_guarded(
    sem: asyncio.Semaphore,
    timeout: float,
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    on_start: Optional[Callable[[], Awaitable[None]]] = None,
    on_finish: Optional[Callable[[Optional[BaseException]], Awaitable[None]]] = None,
) -> Any:
    """在信号量限宽 + 超时下运行分支，并可观测实际并发槽生命周期。"""
    async with sem:
        if on_start is not None:
            await on_start()
        error: Optional[BaseException] = None
        try:
            return await asyncio.wait_for(coro_factory(), timeout)
        except BaseException as exc:
            error = exc
            raise
        finally:
            if on_finish is not None:
                await on_finish(error)


# ——————————————————————————————————————————————————————————————
# 解析与提示词
# ——————————————————————————————————————————————————————————————
def _parse_planning_call(ai: AIMessage) -> Optional[dict]:
    """从 planner 的 AIMessage 取 planning 工具调用参数（{command, title, steps}）。"""
    for tc in getattr(ai, "tool_calls", None) or []:
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        if name == "planning":
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            return dict(args or {})
    return None


def _join_results(results: Sequence[SubResult]) -> str:
    """把一轮 sub_results 拼成注记文本（按 task 顺序，镜像 joinTaskResults）。"""
    parts: list[str] = []
    for r in results:
        if not r:
            continue
        parts.append(f"{r.get('task', '')} => {r.get('result', '')}".strip())
    return "\n".join(parts)


def _plan_event_data(plan: Plan) -> dict:
    return {
        "title": plan.title,
        "steps": list(plan.steps),
        "step_status": list(plan.step_status),
        "notes": list(plan.notes),
    }


def _ai_text(ai: AIMessage) -> str:
    content = getattr(ai, "content", "")
    return content if isinstance(content, str) else str(content)


# ——————————————————————————————————————————————————————————————
# 节点工厂与图装配
# ——————————————————————————————————————————————————————————————
def build_plan_execute_graph(
    planner_model: BaseChatModel,
    executor_graph: CompiledStateGraph,
    tools: Sequence[Any],
    *,
    max_steps: int = 5,
    max_parallel: int = 2,
    sop_store: Optional[Any] = None,
    branch_timeout: float = 120.0,
    react_recursion_limit: int = 85,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    planner_system: str = PLANNER_SYSTEM,
    format_prompts: Optional[dict[str, str]] = None,
    max_total_branches: int = 0,
) -> CompiledStateGraph:
    """装配并编译 Plan-Execute 图。

    planner_system：规划器系统提示词（deep_research 变体传 RESEARCH_PLANNER_SYSTEM，
    其余拓扑/节点完全共享——同一套图的配置化变体，不是新图）。

    Args:
        planner_model: 规划器模型（产出步骤/思考；可绑定 planning 工具）。
        executor_graph: 复用的 M1 ReAct 子图（单个子任务的执行器）。
        tools: 本地工具（已绑定在 executor_graph 上；此处保留作显式声明/seam）。
        max_steps: 外层 plan→execute 循环上限（超步→STOPPED）。
        max_parallel: 并行子任务宽度上限（信号量）。
        sop_store: 可选 SopStore（recall(query)->str|None）。
        branch_timeout: 单分支超时秒数。
    """
    # 信号量按 running loop 取，确保跨 loop（多次 astream_events / 多测试）安全、不泄漏。
    _sem_by_loop: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}

    def _semaphore() -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = _sem_by_loop.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(max_parallel)
            _sem_by_loop[loop] = sem
        return sem

    # ---- sop_recall：召回 SOP 写入 state["sop"] ----
    def sop_recall(state: PlanExecuteState) -> dict:
        if state.get("sop"):
            return {}
        sop_text = ""
        if sop_store is not None:
            try:
                sop_text = sop_store.recall(state.get("query", "")) or ""
            except Exception as exc:  # noqa: BLE001 — 召回失败不应中断编排
                logger.warning("sop recall failed: %s", exc)
        return {"sop": sop_text}

    # ---- planner：create / replan + 发 plan、task 自定义事件 ----
    async def planner(state: PlanExecuteState) -> dict:
        rnd = int(state.get("round", 0))
        plan = state.get("plan")
        sub_results = state.get("sub_results") or []
        sop = state.get("sop") or ""
        query = state.get("query", "")
        # planner 只把 planning 当"结构化输出"解析、从不执行它 → 历史里的 planning
        # tool_calls 永远没有 ToolMessage 应答，真实 provider（DeepSeek/OpenAI/Anthropic）
        # 第二轮起会 400。入模型前做修复投影（补合成应答），state 原样累积不动。
        history = repair_dangling_tool_calls(list(state.get("planner_messages") or []))
        system_text = compose_planner_system(
            planner_system, sop, state.get("output_format", ""), format_prompts or {},
            image_gen=bool(state.get("image_gen", False)),
        )
        system = SystemMessage(content=system_text)

        new_msgs: list = []
        reduced: Optional[str] = None

        if plan is None:
            human = HumanMessage(
                content=f"用户任务：{query}\n请用 planning 工具创建计划（同一步骤内可用 <sep> 分隔可并行子任务）。"
            )
            ai = await planner_model.ainvoke([system, *history, human])  # → plan_thought
            new_msgs += [human, ai, *_planning_acks(ai)]
            draft = _parse_planning_call(ai) or _parse_planning_text(_ai_text(ai))
            title = (draft or {}).get("title") or "计划"
            steps = (draft or {}).get("steps") or [query or "完成任务"]
            plan = create(title, [str(s) for s in steps])
            dispatch_round = rnd  # 首轮 dispatch round = 当前 round（默认 0）
        else:
            this_round = results_for_round(sub_results, state.get("request_id", ""), rnd)
            completed_branches = len(this_round)  # 全局分支预算的计数来源（实际完成数）
            reduced = reduce_substate(this_round)
            note = _join_results(this_round)
            human = HumanMessage(content=f"上一轮执行结果：\n{note}\n请审视并推进计划。")
            ai = await planner_model.ainvoke([system, *history, human])  # → plan_thought
            new_msgs += [human, ai, *_planning_acks(ai)]
            draft = _parse_planning_call(ai) or _parse_planning_text(_ai_text(ai))

            idx = current_step_index(plan)
            if idx is not None:
                plan = set_note(plan, idx, note)
                plan = mark_step_completed(plan, idx)
            else:
                plan = ensure_executable(plan)
            # 可选 LLM replan：仅当模型显式给出 update + 新步骤且计划未完成。
            if (
                draft
                and draft.get("command") == "update"
                and draft.get("steps")
                and not all_completed(plan)
            ):
                plan = update_remaining(plan, draft.get("title"), list(draft["steps"]))
            dispatch_round = rnd + 1

        # 发计划快照（mapper 赋予 plannerRoundId，与本轮 plan_thought 同 round）。
        await adispatch_custom_event(EVENT_PLAN, _plan_event_data(plan))
        # 未完成 → 把 currentStep 按 <sep> 切分，逐个发 task。
        if not all_completed(plan):
            for sub in current_step(plan).split(SEP):
                await adispatch_custom_event(EVENT_TASK, {"text": sub})

        updates: dict = {
            "plan": plan,
            "round": dispatch_round,
            "planner_messages": new_msgs,
        }
        if reduced is not None:
            updates["reduced_state"] = reduced
            # replan 路径：累计上一轮实际完成的分支数（route 据此执行全局分支预算）。
            updates["branches_used"] = int(state.get("branches_used", 0)) + completed_branches
        return updates

    # ---- executor：信号量限宽 + 超时；复用 ReAct 子图跑单个子任务 ----
    async def executor(state: dict, config: RunnableConfig) -> dict:
        branch_id = state.get("branch_id", "b0")
        task = state.get("task", "")
        rnd = int(state.get("round", 0))
        sop = state.get("sop", "")
        request_id = state.get("request_id", "")
        session_id = state.get("session_id", "")
        plan_title = state.get("plan_title", "")
        context_digest = state.get("context_digest", "")

        parent_meta = (config or {}).get("metadata") or {}
        child_config: RunnableConfig = {
            "callbacks": (config or {}).get("callbacks"),
            "tags": list((config or {}).get("tags") or []),
            # branch_id / request_id 注入子图事件 metadata：供 mapper 命名空间化 tool_call_id 与 run 归属。
            "metadata": {**parent_meta, "branch_id": branch_id, "request_id": request_id},
            # 子线程按 run 隔离（request_id 基）：同会话第二次 run 的 (b0, r0) 不能撞上
            # 上一次 run 的同名线程（executor 子图目前无 checkpointer，此处是防御性正确）。
            "configurable": {"thread_id": f"{request_id or session_id}:{branch_id}:r{rnd}"},
            "recursion_limit": react_recursion_limit,
        }
        prompt = _executor_prompt(task, plan_title, sop, context_digest)
        sub_state = {
            "messages": [HumanMessage(content=prompt)],
            "request_id": request_id,
            "session_id": session_id,
            "query": task,
            "product_files": [],
            "is_stream": True,
            "step": 0,
        }

        sem = _semaphore()

        async def branch_start() -> None:
            await adispatch_custom_event(
                EVENT_BRANCH_START,
                {
                    "request_id": request_id,
                    "round": rnd,
                    "branch_id": branch_id,
                    "task": task,
                    "perf_counter_ns": time.perf_counter_ns(),
                },
            )

        async def branch_end(error: Optional[BaseException]) -> None:
            await adispatch_custom_event(
                EVENT_BRANCH_END,
                {
                    "request_id": request_id,
                    "round": rnd,
                    "branch_id": branch_id,
                    "task": task,
                    "perf_counter_ns": time.perf_counter_ns(),
                    "error_type": type(error).__name__ if error is not None else "",
                },
            )

        try:
            result_state = await run_branch_guarded(
                sem,
                branch_timeout,
                lambda: executor_graph.ainvoke(sub_state, child_config),
                on_start=branch_start,
                on_finish=branch_end,
            )
            final_text, observations = _extract_outcome(result_state)
            status = STATE_FINISHED
        except asyncio.TimeoutError:
            logger.warning("executor branch %s timed out after %.0fs", branch_id, branch_timeout)
            final_text, observations, status = (f"子任务超时（>{branch_timeout}s）", [], STATE_ERROR)
        except Exception as exc:  # noqa: BLE001 — 单分支失败不拖垮其他分支
            logger.warning("executor branch %s failed: %s", branch_id, exc)
            final_text, observations, status = (f"子任务异常：{exc}", [], STATE_ERROR)

        sub: SubResult = {
            "request_id": request_id,
            "round": rnd,
            "branch_id": branch_id,
            "task": task,
            "result": final_text,
            "observations": observations,
            "status": status,
        }
        return {"sub_results": [sub]}

    # ---- summary：聚合 → result 自定义事件（finish 仅在此） ----
    async def summary(state: PlanExecuteState) -> dict:
        text = _summarize(state, max_steps)
        await adispatch_custom_event(EVENT_RESULT, {"text": text})
        return {}

    def route_after_planner(state: PlanExecuteState):
        return _route_after_planner(state, max_steps, max_total_branches)

    graph = StateGraph(PlanExecuteState)
    graph.add_node("sop_recall", sop_recall)
    graph.add_node("planner", planner)
    graph.add_node("executor", executor)
    graph.add_node("summary", summary)

    graph.add_edge(START, "sop_recall")
    graph.add_edge("sop_recall", "planner")
    graph.add_conditional_edges("planner", route_after_planner, ["executor", "summary"])
    graph.add_edge("executor", "planner")
    graph.add_edge("summary", END)

    return graph.compile(checkpointer=checkpointer)


# ——————————————————————————————————————————————————————————————
# 路由（纯函数，便于单测）
# ——————————————————————————————————————————————————————————————
def _route_after_planner(state: PlanExecuteState, max_steps: int, max_total_branches: int = 0):
    """planner 之后的条件路由：完成/超步/ERROR/分支预算尽 → summary；否则按 <sep> 扇出 Send。

    max_total_branches（0=不限）：**全局**分支预算。每分支的工具调用已有 40 次硬预算，
    但实测 planner 可产出 8 步计划 × 每步多子任务 × replan → 总分支 30+，
    总调用仍爆到 1400+（拖满 RUN_TIMEOUT、烧穿搜索额度）——步数×并行的乘积必须再设一道闸。
    """
    plan = state.get("plan")
    rnd = int(state.get("round", 0))

    if all_completed(plan):
        return "summary"
    if rnd > max_steps:
        return "summary"
    if state.get("reduced_state") == STATE_ERROR:
        return "summary"

    subs = current_step(plan).split(SEP)
    used = int(state.get("branches_used", 0))
    if max_total_branches > 0 and used + len(subs) > max_total_branches:
        return "summary"  # 再派发就超全局预算：基于已有 sub_results 收口
    # 会话背景摘要：executor 分支与会话历史隔离，续聊里"整理上面内容"类任务
    # 靠它拿到指代对象（同一轮各分支共享同一份摘要）。
    digest = build_context_digest(state.get("planner_messages"))
    return [
        Send(
            "executor",
            {
                "task": sub,
                "branch_id": f"b{i}",
                "round": rnd,
                "query": state.get("query", ""),
                "sop": state.get("sop", ""),
                "request_id": state.get("request_id", ""),
                "session_id": state.get("session_id", ""),
                "plan_title": plan.title if plan else "",
                "context_digest": digest,
            },
        )
        for i, sub in enumerate(subs)
    ]


# 公开别名（供测试导入）。
def route_after_planner(state: PlanExecuteState, max_steps: int = 5, max_total_branches: int = 0):
    """对外的路由纯函数（默认 max_steps=5、分支预算不限）。"""
    return _route_after_planner(state, max_steps, max_total_branches)


# ——————————————————————————————————————————————————————————————
# 子任务提示词 / 结果抽取 / 汇总
# ——————————————————————————————————————————————————————————————
def _executor_prompt(task: str, plan_title: str, sop: str, context_digest: str = "") -> str:
    parts = [f"你的任务是：{task}"]
    if plan_title:
        parts.append(f"（所属计划：{plan_title}）")
    if sop:
        parts.append(f"\n参考 SOP：\n{sop}")
    if context_digest:
        parts.append(
            f"\n\n—— 会话背景（供参考；任务中提到「上面/之前的内容」即指这里）——\n{context_digest}"
        )
    return "".join(parts)


def _extract_outcome(result_state: dict) -> tuple[str, list[str]]:
    """从 ReAct 子图终态抽取（最终答复, 观测列表）。"""
    messages = (result_state or {}).get("messages") or []
    final_text = ""
    observations: list[str] = []
    for m in messages:
        cls = type(m).__name__
        content = getattr(m, "content", "")
        text = content if isinstance(content, str) else str(content)
        if cls == "ToolMessage":
            observations.append(text)
        elif cls in ("AIMessage", "AIMessageChunk") and not getattr(m, "tool_calls", None):
            if text:
                final_text = text
    return final_text, observations


def _summarize(state: PlanExecuteState, max_steps: int) -> str:
    """聚合 notes/sub_results → 最终答复（按终止原因渲染）。"""
    plan = state.get("plan")
    rnd = int(state.get("round", 0))
    reduced = state.get("reduced_state")

    if reduced == STATE_ERROR:
        # 带上本 run 出错分支的具体原因（超时/异常文本），不能只给一句"联系管理员"。
        rid = str(state.get("request_id") or "")
        errs = [
            f"- {r.get('task','')}：{r.get('result','')}"
            for r in (state.get("sub_results") or [])
            if str(r.get("request_id") or "") == rid and r.get("status") == STATE_ERROR
        ]
        detail = ("\n" + "\n".join(errs)) if errs else ""
        return f"任务执行异常，已终止。失败原因：{detail or '（未知）'}"
    if rnd > max_steps and not all_completed(plan):
        return "达到最大迭代次数，任务终止。"

    lines: list[str] = []
    if plan is not None:
        if plan.title:
            lines.append(f"# {plan.title}")
        for i, step in enumerate(plan.steps):
            note = plan.notes[i] if i < len(plan.notes) else ""
            if note:
                lines.append(f"- {step}：{note}")
            else:
                lines.append(f"- {step}")
    if not lines:
        # 兜底也只看本 run 的结果（旧 run 的累积不泄入）。
        rid = str(state.get("request_id") or "")
        results = [
            r for r in (state.get("sub_results") or []) if str(r.get("request_id") or "") == rid
        ]
        lines = [f"- {r.get('task','')}: {r.get('result','')}" for r in results]
    return "任务已完成。\n" + "\n".join(lines)
