"""输出格式注入（M9 B6）。

不变量（压测修正的核心）：SystemMessage **只活在单次模型调用里**——
state/checkpoint 永不出现它。持久化会累积互相矛盾的格式指令，且中位 system
消息会被 langchain-anthropic 拒绝（续聊切 Claude 即 400）。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from cognition.config import Settings
from cognition.graphs.nodes import format_prompt_from_config, make_think_node
from cognition.graphs.plan_execute import PLANNER_SYSTEM, compose_planner_system

PROMPTS = Settings().output_format_prompts


class _CaptureModel:
    """记录每次 invoke 收到的消息序列。"""

    def __init__(self):
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(list(messages))
        return AIMessage(content="ok")


def test_format_prompt_from_config_matrix():
    assert format_prompt_from_config({"metadata": {"output_format": "table"}}, PROMPTS) == PROMPTS["table"]
    assert format_prompt_from_config({"metadata": {"output_format": "nope"}}, PROMPTS) == ""  # 未知值忽略
    assert format_prompt_from_config({"metadata": {}}, PROMPTS) == ""
    assert format_prompt_from_config(None, PROMPTS) == ""
    assert format_prompt_from_config({"metadata": {"output_format": "html"}}, {}) == ""


def test_think_injects_ephemeral_system_only_for_this_invoke():
    model = _CaptureModel()
    think = make_think_node(model, format_prompts=PROMPTS)  # type: ignore[arg-type]
    state = {"messages": [HumanMessage(content="问题")], "step": 0}

    out = think(state, config={"metadata": {"output_format": "table"}})
    # 模型看到 leading SystemMessage + 原消息。
    sent = model.calls[0]
    assert isinstance(sent[0], SystemMessage) and "表格" in sent[0].content
    assert isinstance(sent[1], HumanMessage)
    # 零残留：节点返回的 update 只有 AIMessage，state 原样（checkpoint 不会收到 system）。
    assert all(not isinstance(m, SystemMessage) for m in out["messages"])
    assert all(not isinstance(m, SystemMessage) for m in state["messages"])

    # 第二轮无格式 run：同一图/同一 state，不再注入。
    think(state, config={"metadata": {}})
    assert all(not isinstance(m, SystemMessage) for m in model.calls[1])


def test_compose_planner_system():
    text = compose_planner_system(PLANNER_SYSTEM, "SOP内容", "ppt", PROMPTS)
    assert "SOP内容" in text and PROMPTS["ppt"] in text
    # 未知/空格式 → 原样。
    assert compose_planner_system(PLANNER_SYSTEM, "s", "nope", PROMPTS) == PLANNER_SYSTEM.replace("{{sop}}", "s")
    assert compose_planner_system(PLANNER_SYSTEM, "s", "", PROMPTS) == PLANNER_SYSTEM.replace("{{sop}}", "s")


def test_leading_prompt_combines_image_gen_and_format():
    """Y4：image_gen 指令与 output_format 合并成一条 leading system（两者正交）。"""
    from cognition.graphs.nodes import IMAGE_GEN_INSTRUCTION, leading_prompt_from_config

    # 仅生图。
    only_img = leading_prompt_from_config({"metadata": {"image_gen": "1"}}, PROMPTS)
    assert IMAGE_GEN_INSTRUCTION in only_img and PROMPTS["table"] not in only_img
    # 生图 + 格式：两段都在，空行分隔，且生图在前。
    both = leading_prompt_from_config({"metadata": {"image_gen": "1", "output_format": "html"}}, PROMPTS)
    assert IMAGE_GEN_INSTRUCTION in both and PROMPTS["html"] in both
    assert both.index(IMAGE_GEN_INSTRUCTION) < both.index(PROMPTS["html"])
    # 仅格式（image_gen 未置位/假值）。
    assert leading_prompt_from_config({"metadata": {"output_format": "table"}}, PROMPTS) == PROMPTS["table"]
    assert leading_prompt_from_config({"metadata": {"image_gen": "0"}}, PROMPTS) == ""
    assert leading_prompt_from_config(None, PROMPTS) == ""


def test_think_injects_image_gen_instruction_react():
    """Y4：react 也注入生图指令（单条 leading system），不进 state/checkpoint。"""
    from cognition.graphs.nodes import IMAGE_GEN_INSTRUCTION

    model = _CaptureModel()
    think = make_think_node(model, format_prompts=PROMPTS)  # type: ignore[arg-type]
    state = {"messages": [HumanMessage(content="给我画只猫做成网页")], "step": 0}
    think(state, config={"metadata": {"image_gen": "1", "output_format": "html"}})
    sent = model.calls[0]
    assert isinstance(sent[0], SystemMessage)
    assert IMAGE_GEN_INSTRUCTION in sent[0].content and "网页" in sent[0].content
    # 只有一条 system（合并而非两条）。
    assert sum(isinstance(m, SystemMessage) for m in sent) == 1
    assert all(not isinstance(m, SystemMessage) for m in state["messages"])
