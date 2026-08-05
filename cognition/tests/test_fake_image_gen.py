"""fake ReAct 模型的生图分支（E2E 出图链路的模型侧下单，见 providers/fake.py）。

钉住三件事：
1. 标记子串与 graphs/nodes.py 的 IMAGE_GEN_INSTRUCTION 保持同步（marker 漂移即测试红）。
2. 有生图标记 → 第一步调 image_generate，prompt/mask/source_images 按生图工作区
   query 模板（web/src/components/GenerateWorkspace.tsx）+ 附件清单注记解析正确；
   工具结果回来后收尾、不再调工具。
3. 无标记 → 行为不变：仍先调 calculator(2*(3+4))、再答"答案是 14。"。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cognition.attachments import attachment_note
from cognition.graphs.nodes import IMAGE_GEN_INSTRUCTION
from cognition.providers.fake import IMAGE_GEN_MARKER, build_fake_model


def test_marker_stays_in_sync_with_instruction():
    """fake 的判定子串必须存在于 think 节点注入的生图指令里（同步锚）。"""
    assert IMAGE_GEN_MARKER in IMAGE_GEN_INSTRUCTION


def test_no_marker_behavior_unchanged_calculator_then_answer():
    model = build_fake_model()
    first = model.invoke([HumanMessage(content="帮我算一下 2*(3+4) 等于多少")])
    assert len(first.tool_calls) == 1
    tc = first.tool_calls[0]
    assert tc["name"] == "calculator"
    assert tc["args"] == {"expression": "2*(3+4)"}

    final = model.invoke(
        [
            HumanMessage(content="帮我算一下 2*(3+4) 等于多少"),
            first,
            ToolMessage(content="14", tool_call_id=tc["id"]),
        ]
    )
    assert final.content == "答案是 14。"
    assert not final.tool_calls


def test_marker_text_to_image_calls_image_generate():
    """纯文生图：prompt=最后一条 human 文本，无 source_images/mask。"""
    model = build_fake_model()
    query = "生成图片（尺寸 1024x1024，共 2 张）：一只戴帽子的橘猫"
    msgs = [SystemMessage(content=IMAGE_GEN_INSTRUCTION), HumanMessage(content=query)]
    msg = model.invoke(msgs)
    assert len(msg.tool_calls) == 1
    tc = msg.tool_calls[0]
    assert tc["name"] == "image_generate"
    assert tc["args"]["prompt"] == query
    assert "source_images" not in tc["args"]
    assert "mask" not in tc["args"]

    final = model.invoke([*msgs, msg, ToolMessage(content="已生成 2 张图片", tool_call_id=tc["id"])])
    assert not final.tool_calls
    assert final.content  # 收尾出结论


def test_marker_inpaint_parses_mask_and_source_images():
    """图生图+蒙版：mask 名取自「使用蒙版文件 X」，底图名取自附件清单注记（剔除蒙版）。"""
    model = build_fake_model()
    atts = [
        {"resource_key": "uploads/u/s/e2e-source.png", "file_name": "e2e-source.png", "mime_type": "image/png", "size": 12345},
        {"resource_key": "uploads/u/s/mask-a1b2c3.png", "file_name": "mask-a1b2c3.png", "mime_type": "image/png", "size": 678},
    ]
    # 逐字对齐 GenerateWorkspace.tsx 的 query 模板 + servicer 的附件注记拼接。
    query = (
        "生成图片（尺寸 1024x1024，共 1 张）：把画面中间换成一朵花"
        "。以我上传的图片为底图进行修改（图生图）。"
        "使用蒙版文件 mask-a1b2c3.png 对底图做局部重绘（inpaint），蒙版透明区域=需要重绘的区域。"
        f"\n\n{attachment_note(atts)}"
    )
    # human 消息按 build_attachment_message 展开后的形态给 content blocks（text 块拼接）。
    human = HumanMessage(content=[{"type": "text", "text": query}])
    msg = model.invoke([SystemMessage(content=IMAGE_GEN_INSTRUCTION), human])
    tc = msg.tool_calls[0]
    assert tc["name"] == "image_generate"
    assert tc["args"]["mask"] == "mask-a1b2c3.png"
    assert tc["args"]["source_images"] == ["e2e-source.png"]
    assert "一朵花" in tc["args"]["prompt"]


def test_marker_image_to_image_without_mask():
    """仅底图（无蒙版）：source_images 有值、mask 缺省。"""
    model = build_fake_model()
    atts = [
        {"resource_key": "uploads/u/s/e2e-source.png", "file_name": "e2e-source.png", "mime_type": "image/png", "size": 12345},
    ]
    query = (
        "生成图片（尺寸 1024x1024，共 1 张）：整体重绘为水彩风"
        "。以我上传的图片为底图进行修改（图生图）。"
        f"\n\n{attachment_note(atts)}"
    )
    msg = model.invoke([SystemMessage(content=IMAGE_GEN_INSTRUCTION), HumanMessage(content=query)])
    tc = msg.tool_calls[0]
    assert tc["name"] == "image_generate"
    assert tc["args"]["source_images"] == ["e2e-source.png"]
    assert "mask" not in tc["args"]


def test_stateless_parallel_safe_repeat_invocations():
    """无状态：同一实例交替喂不同阶段消息，互不串号（并发/长驻安全的最小锚）。"""
    model = build_fake_model()
    calc_msgs = [HumanMessage(content="算一下")]
    img_msgs = [
        SystemMessage(content=IMAGE_GEN_INSTRUCTION),
        HumanMessage(content="生成图片（尺寸 1024x1024，共 1 张）：一朵花"),
    ]
    for _ in range(3):
        assert model.invoke(calc_msgs).tool_calls[0]["name"] == "calculator"
        assert model.invoke(img_msgs).tool_calls[0]["name"] == "image_generate"
    done = model.invoke(
        [*calc_msgs, AIMessage(content="", tool_calls=[{"name": "calculator", "args": {}, "id": "c1"}]), ToolMessage(content="14", tool_call_id="c1")]
    )
    assert done.content == "答案是 14。"
