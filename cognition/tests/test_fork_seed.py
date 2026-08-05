"""会话分叉/时间旅行（docs/14）：servicer 级 fork 播种全链路（fake 模型 + InMemorySaver）。

架构不变量：分叉 = 新会话（新 thread）。Go 在分叉会话首 run 附
metadata["fork_from_session_id"/"fork_from_run_id"]；servicer 用 checkpoint metadata 的
run_id 定位父 thread"那一轮结束时"的快照（aget_state_history filter），把 **messages
通道**播种进新 thread（aupdate_state，不带 as_node——实测 langgraph 1.2.7 空 thread 无
歧义）。幂等双保险（Go 只在无 own run 时附键 + Python 见 checkpoint 即跳过）；定位失败
显式报错（绝不静默降级成空记忆会话）；pending interrupt 不迁移。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from cognition._genproto import agent_pb2
from cognition.approval import first_interrupt_payload, wrap_with_approval
from cognition.config import Settings
from cognition.graphs.react import build_react_graph
from cognition.providers.fake import MessageDrivenChatModel
from cognition.server.servicer import CognitionServicer
from langgraph.checkpoint.memory import MemorySaver

from cognition.tools.calculator import calculator


def _decide_factory(seen: list):
    """无状态按最后一条 human 内容作答的 fake 模型；seen 记录每次模型看到的完整上下文
    （证实 fork 后续聊的模型上下文含轮 1、不含轮 2——比只查 thread state 更接近本体）。"""

    def decide(messages):
        seen.append(list(messages))
        text = "".join(str(getattr(m, "content", "")) for m in messages)
        last = str(getattr(messages[-1], "content", "")) if messages else ""
        if "我叫什么" in last:
            return AIMessage(content="你叫小明" if "小明" in text else "我不知道你的名字")
        if "我在哪" in last:
            return AIMessage(content="你在北京" if "北京" in text else "我不知道你在哪")
        return AIMessage(content=f"收到：{last}")

    return decide


def _servicer(decide) -> CognitionServicer:
    model = MessageDrivenChatModel(decide=decide)
    graph = build_react_graph(model, [calculator], checkpointer=MemorySaver(), max_steps=4)
    return CognitionServicer(react_graph=graph, settings=Settings())


def _req(run_id: str, session: str, query: str, metadata: dict | None = None):
    return agent_pb2.RunRequest(
        run_id=run_id, session_id=session, query=query, agent_type="react",
        metadata=metadata or {},
    )


def _fork_meta(src_session: str, src_run: str) -> dict:
    return {"fork_from_session_id": src_session, "fork_from_run_id": src_run}


async def _drain(servicer, request):
    return [p async for p in servicer.Run(request, None)]


def _contents(msgs) -> list[str]:
    return [str(getattr(m, "content", "")) for m in msgs]


async def _thread_messages(servicer, thread_id: str):
    snap = await servicer.react_graph.aget_state({"configurable": {"thread_id": thread_id}})
    return (snap.values or {}).get("messages") or []


async def _seed_two_turns(servicer):
    """会话 sA 两轮：轮1 记名字、轮2 记城市（分叉点将取轮 1）。"""
    p1 = await _drain(servicer, _req("r1", "sA", "我叫小明"))
    p2 = await _drain(servicer, _req("r2", "sA", "我在北京"))
    assert p1[-1].finish and p2[-1].finish


async def test_fork_seeds_run1_snapshot_exactly():
    """播种正确性：新 thread 的继承前缀 == 父 thread run1 末快照的 messages（内容与条数）。"""
    seen: list = []
    s = _servicer(_decide_factory(seen))
    await _seed_two_turns(s)

    protos = await _drain(s, _req("rf", "sF", "我叫什么", metadata=_fork_meta("sA", "r1")))
    assert protos[-1].finish and "你叫小明" in protos[-1].result.text

    # 对照父 thread run1 末 checkpoint（同一定位配方）。
    hist = [
        snap
        async for snap in s.react_graph.aget_state_history(
            {"configurable": {"thread_id": "sA"}}, filter={"run_id": "r1"}, limit=1
        )
    ]
    assert len(hist) == 1
    run1_msgs = _contents(hist[0].values["messages"])
    forked = _contents(await _thread_messages(s, "sF"))
    assert forked[: len(run1_msgs)] == run1_msgs  # 继承前缀逐条一致
    assert forked[len(run1_msgs):] == ["我叫什么", "你叫小明"]  # 其后是分叉会话自己的轮
    assert all("北京" not in c for c in forked)  # 轮 2 不在新时间线

    # 父 thread 不受影响：两轮 4 条消息原样在场（分叉点快照不可变，两线独立演化）。
    parent = _contents(await _thread_messages(s, "sA"))
    assert len(parent) == 4 and any("北京" in c for c in parent)


async def test_fork_then_continue_context_has_turn1_not_turn2():
    """fork 后续聊（第二 run 无 fork metadata）：模型实看上下文含轮 1、不含轮 2。"""
    seen: list = []
    s = _servicer(_decide_factory(seen))
    await _seed_two_turns(s)
    await _drain(s, _req("rf", "sF", "我叫什么", metadata=_fork_meta("sA", "r1")))

    seen.clear()
    protos = await _drain(s, _req("rf2", "sF", "我在哪"))  # 续聊，不带 fork 键
    assert protos[-1].finish
    assert "我不知道你在哪" in protos[-1].result.text  # 轮 2（北京）没被继承 → 诚实不知道
    assert len(seen) == 1
    ctx = "".join(_contents(seen[0]))
    assert "小明" in ctx and "北京" not in ctx


async def test_fork_seed_idempotent_on_retry():
    """幂等第二道保险：目标 thread 已有 checkpoint 时重复携带 fork 键 → 跳过播种，不重复追加。"""
    seen: list = []
    s = _servicer(_decide_factory(seen))
    await _seed_two_turns(s)
    await _drain(s, _req("rf", "sF", "我叫什么", metadata=_fork_meta("sA", "r1")))
    before = len(await _thread_messages(s, "sF"))

    # Go 侧竞态/客户端重试的模拟：第二条 run 仍带 fork 键。
    protos = await _drain(s, _req("rf2", "sF", "我叫什么", metadata=_fork_meta("sA", "r1")))
    assert protos[-1].finish and "运行出错" not in protos[-1].result.text
    after = _contents(await _thread_messages(s, "sF"))
    assert len(after) == before + 2  # 只多出本轮 human+ai，继承段没有第二份
    assert after.count("我叫小明") == 1


async def test_fork_locate_failure_errors_honestly():
    """定位失败（run_id 无记录/会话过旧）→ run 显式报错收尾，且新 thread 保持未播种。"""
    seen: list = []
    s = _servicer(_decide_factory(seen))
    await _seed_two_turns(s)

    protos = await _drain(s, _req("rf", "sF", "我叫什么", metadata=_fork_meta("sA", "ghost")))
    assert len(protos) == 1 and protos[0].finish
    assert "运行出错" in protos[0].result.text and "无法定位分叉点" in protos[0].result.text
    assert await _thread_messages(s, "sF") == []  # 绝不产出半播种的假继承 thread
    assert seen == [] or all("我叫什么" not in "".join(_contents(m)) for m in seen)  # 图未起跑

    # 源会话整个不存在（无任何 checkpoint）同样诚实报错。
    protos2 = await _drain(s, _req("rg", "sG", "hi", metadata=_fork_meta("no-such-session", "r1")))
    assert len(protos2) == 1 and "无法定位分叉点" in protos2[0].result.text


async def test_fork_does_not_migrate_pending_interrupt():
    """interrupt 态不迁移（docs/14 §4.4）：父会话停在审批卡，分叉出的新 thread 无挂起
    审批；继承的悬空 tool_calls 由 think 的 repair 投影自愈，新时间线正常续聊。"""

    def decide(messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="工具完成")
        last = str(getattr(messages[-1], "content", "")) if messages else ""
        if "算一下" in last:
            return AIMessage(
                content="需要计算",
                tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "tc-f", "type": "tool_call"}],
            )
        return AIMessage(content=f"好的：{last}")

    model = MessageDrivenChatModel(decide=decide)
    tools = wrap_with_approval([calculator], ["calculator"], reason="需要审批")
    graph = build_react_graph(model, tools, checkpointer=MemorySaver(), max_steps=4)
    s = CognitionServicer(react_graph=graph, settings=Settings())

    # 轮 1 撞审批门挂起（pending interrupt 落在父 thread 的 checkpoint 里）。
    p1 = await _drain(s, _req("r1", "sA", "帮我算一下"))
    assert any(p.type == agent_pb2.EVENT_TYPE_APPROVAL_REQUEST for p in p1)

    # 从挂起轮分叉：播种成功、fork run 正常收尾（不是挂起文案）。
    p2 = await _drain(s, _req("rf", "sF", "换个话题", metadata=_fork_meta("sA", "r1")))
    assert p2[-1].finish and "挂起" not in p2[-1].result.text

    # 新 thread 无挂起审批；父 thread 的审批原地保留（决议属父时间线）。
    snap_new = await graph.aget_state({"configurable": {"thread_id": "sF"}})
    assert first_interrupt_payload(snap_new) is None
    snap_parent = await graph.aget_state({"configurable": {"thread_id": "sA"}})
    assert first_interrupt_payload(snap_parent) is not None
