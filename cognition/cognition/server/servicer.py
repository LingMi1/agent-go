"""CognitionService gRPC servicer 实现（grpc.aio，server-streaming）。

按 `RunRequest.agent_type` 路由：
- "plan_solve" → Plan-Execute 图（plan→executor 子图→summary，含 replan 与并行子任务）。
- 其它（默认 "react"）→ M1 ReAct 图。

Run 是异步生成器：
1. 由 RunRequest 按 agent_type 装配初始 State。
2. graph.astream_events(version="v2", config={configurable:{thread_id:session_id}, recursion_limit, metadata})。
3. 逐事件喂 EventMapper，yield Event.to_proto()。
4. 客户端取消（CancelledError）→ 干净停止；节点异常 → 发终态 result(finish, error) 关闭流。
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import HumanMessage

from cognition._genproto import agent_pb2, agent_pb2_grpc
from cognition.attachments import attachment_note, build_attachment_message, normalize_attachments
from cognition.config import Settings
from cognition.events.mapper import EventMapper
from cognition.observability.langfuse_seam import build_langfuse_callbacks
from cognition.observability.otel_seam import current_trace_id

logger = logging.getLogger(__name__)

AGENT_TYPE_PLAN_SOLVE = "plan_solve"
AGENT_TYPE_DEEP_RESEARCH = "deep_research"


class ForkSeedError(RuntimeError):
    """分叉播种失败（定位不到分叉点 / 分叉点无可继承记忆）。

    docs/14 §2 红线：此类失败必须让 run 显式以错误收尾——静默降级成"空记忆会话"
    是最坏结果（用户在一个自称继承了历史、实则失忆的会话里继续对话，M7 教训）。
    """


def resolve_kb_id(request) -> str:
    """解析本 run 的知识库归属（纯函数，单点解析后经 config metadata 供
    knowledge_search 与附件入库共用）。

    owner 级优先（`owner:{owner_id}`，Go 经 RunRequest.metadata["owner_id"] 传入）：
    用户上传的文档跨会话可检索（"用户自有知识库"语义）。无 owner（旧 Go/直连
    gRPC）回退会话级 `sess:{session_id}`。**绝不返回空串**——kb_id=="" 在检索层
    意味着无隔离全库查询。
    """
    meta = getattr(request, "metadata", None) or {}
    owner = str(meta.get("owner_id", "") or "")
    if owner:
        return f"owner:{owner}"
    sid = str(getattr(request, "session_id", "") or getattr(request, "run_id", "") or "run")
    return f"sess:{sid}"


class CognitionServicer(agent_pb2_grpc.CognitionServiceServicer):
    """一次 run = 一个 server-streaming RPC。按 agent_type 选图。"""

    def __init__(
        self,
        react_graph,
        settings: Settings,
        plan_graph=None,
        research_graph=None,
        tool_providers=None,
        ingest_attachments_fn=None,
    ) -> None:
        self.react_graph = react_graph
        self.plan_graph = plan_graph
        # plan 家族路由表：deep_research 缺省回退 plan_solve 图（提示词非研究版但可用）。
        self.plan_graphs = {
            k: g
            for k, g in {
                AGENT_TYPE_PLAN_SOLVE: plan_graph,
                AGENT_TYPE_DEEP_RESEARCH: research_graph or plan_graph,
            }.items()
            if g is not None
        }
        self.settings = settings
        # 工具名 → provider（local/mcp/skill），装配期从工具集构建后注入 EventMapper。
        self.tool_providers = dict(tool_providers or {})
        # 附件入库（同步可调用 (att_dicts, kb_id)->list[入库文件名]；Run 内经 to_thread 调用）。
        self.ingest_attachments_fn = ingest_attachments_fn

    def _build(self, request, ingested_names: tuple[str, ...] = ()):
        """返回 (graph, initial_state, recursion_limit)。"""
        run_id = request.run_id or "unknown"
        session_id = request.session_id or run_id
        max_steps = request.max_steps or self.settings.max_steps
        agent_type = request.agent_type or "react"
        attachments = normalize_attachments(getattr(request, "attachments", []) or [])

        if agent_type in self.plan_graphs:
            # plan 家族（plan_solve/deep_research）不做图片多模态（planner 无 messages
            # 接缝，见 docs/08 §4 已知限制）：附件以短注记进 query，文本类已入知识库。
            query = request.query
            if attachments:
                query = f"{request.query}\n{attachment_note(attachments, ingested_names)}"
            state = {
                "query": query,
                "request_id": run_id,
                "session_id": session_id,
                "plan": None,
                "round": 0,
                "step": 0,
                # 显式清残留：同会话上一次 run 若以 ERROR 收场，reduced_state 会随
                # checkpoint 延续，新 run 会被 route_after_planner 直接送去 summary。
                "reduced_state": "",
                "output_format": dict(getattr(request, "metadata", {}) or {}).get("output_format", ""),
                # B.4：生图开关也进 plan state，planner 据此把生图步骤纳入规划（executor
                # 侧已由 config.metadata spread 覆盖；planner 节点只读 state，故走 state）。
                "image_gen": str(dict(getattr(request, "metadata", {}) or {}).get("image_gen", "")).lower() in ("1", "true", "yes"),
                "planner_messages": [],
                "sub_results": [],
            }
            # 外层循环 + 并行分支 join 占用 superstep，留足余量。
            # recursion 预算按 agent_type 取各自轮次上限——deep_research 轮次更多，
            # 硬绑 planner_max_steps 会在研究后期 GraphRecursionError。
            steps_budget = (
                int(self.settings.research_max_steps)
                if agent_type == AGENT_TYPE_DEEP_RESEARCH
                else int(self.settings.planner_max_steps)
            )
            recursion = 4 * steps_budget + 25
            return self.plan_graphs[agent_type], state, recursion

        if attachments:
            # 附件消息：文本块（query+清单注记）+ 图片 pro_attachment 引用块
            #（checkpoint 只存引用，base64 在 think 投影期按需展开）。
            human = build_attachment_message(request.query, attachments, ingested_names)
        else:
            human = HumanMessage(content=request.query)
        state = {
            "messages": [human],
            "request_id": run_id,
            "session_id": session_id,
            "query": request.query,
            "product_files": attachments,  # M1 起预留的 seam，本期起实装
            "is_stream": True,
            "step": 0,
        }
        recursion = 2 * int(max_steps) + 5
        return self.react_graph, state, recursion

    async def IngestDocument(self, request, context):  # noqa: N802 (gRPC 方法名固定)
        """Files 面板"上传即入库"（UX-1）：不经对话轮，直接把已上传对象送入 owner 知识库。

        复用 Run 前置入库的同一条管线（build_ingestor：下载→提取→分块→内容寻址幂等
        upsert），语义与"随消息附件入库"完全一致——同一文件两种入口不会产生重复向量。
        kb 归属由服务端从 owner_id 推导，客户端不可指定（与 kb config 优先同一条纪律）。
        """
        owner = str(request.owner_id or "")
        if not owner:
            return agent_pb2.IngestDocumentResponse(ok=False, message="缺少 owner_id")
        if self.ingest_attachments_fn is None:
            return agent_pb2.IngestDocumentResponse(ok=False, message="RAG 未启用（COGNITION_RAG_ENABLED=false）")
        kb_id = f"owner:{owner}"
        atts = normalize_attachments([request.attachment])
        try:
            # 与 Run 前置入库同款 to_thread：同步下载/嵌入不得占 grpc.aio 事件循环。
            names = await asyncio.to_thread(self.ingest_attachments_fn, atts, kb_id)
        except Exception as exc:  # noqa: BLE001 — 管理操作失败以 message 上浮，不抛 gRPC 错
            logger.warning("IngestDocument failed for %s: %s", kb_id, exc)
            return agent_pb2.IngestDocumentResponse(ok=False, kb_id=kb_id, message=f"入库失败: {exc}")
        if not names:
            # names=[] 有两因：非文本类（永久，跳过合理）或下载/提取失败（可重试，
            # build_ingestor 内部吞掉了）——RPC 层无法区分，文案两者都覆盖以免误导。
            return agent_pb2.IngestDocumentResponse(
                ok=False,
                kb_id=kb_id,
                message="未入库：仅支持文本/Markdown/CSV/JSON/PDF/扫描版 PDF（需 vision key）；"
                        "若确为受支持类型，可能是下载/解析失败，请重试",
            )
        logger.info("document ingested into %s: %s", kb_id, list(names))
        return agent_pb2.IngestDocumentResponse(ok=True, kb_id=kb_id)

    async def Run(self, request, context):  # noqa: N802 (gRPC 方法名固定)
        run_id = request.run_id or "unknown"
        session_id = request.session_id or run_id
        agent_type = request.agent_type or "react"
        # 结构化日志：run_id/session_id/agent_type 关联键（与 Go 侧一致，跨进程串同一 run）。
        # 启用 OTel 时再补 trace_id（server span 由 aio 拦截器建，与 Go 根 span 同一 trace）：
        # 可用同一 trace_id grep 两面 stdout JSON（docs/18 §3.4）。未启用/未装 SDK 时
        # current_trace_id() 返回 None，不落 trace_id，保持零行为变化。
        adapter_extra = {"run_id": run_id, "session_id": session_id, "agent_type": agent_type}
        trace_id = current_trace_id()
        if trace_id:
            adapter_extra["trace_id"] = trace_id
        log = logging.LoggerAdapter(logger, adapter_extra)

        mapper = EventMapper(run_id, self.tool_providers)
        kb_id = resolve_kb_id(request)

        # —— M11 HITL：审批决议恢复 run（审批=run 边界；决议乘 metadata 走既有 Run RPC）——
        req_meta = dict(getattr(request, "metadata", {}) or {})
        if req_meta.get("approval_resume_id"):
            async for out in self._resume_approval(request, req_meta, mapper, session_id, log):
                yield out
            return

        # —— docs/14 会话分叉：分叉会话的第一条 run 先把父 thread 分叉点的记忆播种进
        # 新 thread，再照常起图（拦截位置对齐 _resume_approval 先例：metadata 特殊键在
        # 构建 state 之前处理；fork 键由 Go 只在"有 fork 登记且尚无 own run"时附带）。
        if req_meta.get("fork_from_session_id"):
            try:
                await self._seed_forked_thread(
                    src_session=req_meta["fork_from_session_id"],
                    src_run=req_meta.get("fork_from_run_id", ""),
                    new_session=session_id,
                    log=log,
                )
            except ForkSeedError as exc:
                # 诚实报错：定位不到分叉点绝不静默降级成空记忆会话（docs/14 §2 红线）。
                log.warning("fork seed failed: %s", exc)
                yield mapper.error_result(str(exc)).to_proto()
                return
            except Exception as exc:  # noqa: BLE001 — 播种崩溃同样诚实收尾，不冒充继承成功
                log.exception("fork seed crashed")
                yield mapper.error_result(f"分叉播种失败: {exc}").to_proto()
                return

        # —— 附件入库预步（run 前同步：read-your-writes，刚上传就能问到）——
        # 必须 to_thread：embedder/下载是同步阻塞，裸调会冻结 grpc.aio 单事件循环上的
        # 全部并发 run。整体 best-effort：入库失败不阻断 run（附件仍以注记/引用块在场）。
        ingested: tuple[str, ...] = ()
        attachments = list(getattr(request, "attachments", []) or [])
        if attachments and req_meta.get("image_gen"):
            # 生图 run（评审#23）：附件是生成素材（底图/蒙版）而非用户知识——跳过自动
            # 入库：省去逐附件的 vision OCR 调用，也不向知识库堆蒙版类垃圾文档（蒙版
            # 字节每次都不同，无幂等收敛；生图会话本就一次性隔离，docs/12 §4.6）。
            # 附件白名单/image_generate 的下载路径不受影响。
            log.info("image_gen run: skip attachment auto-ingest (%d attachment(s))", len(attachments))
        elif attachments and self.ingest_attachments_fn is not None:
            from cognition.attachments import normalize_attachments

            try:
                names = await asyncio.to_thread(
                    self.ingest_attachments_fn, normalize_attachments(attachments), kb_id
                )
                ingested = tuple(names or ())
                if ingested:
                    log.info("attachments ingested into %s: %s", kb_id, list(ingested))
            except Exception as exc:  # noqa: BLE001 — 入库是增强路径，绝不拖垮 run
                log.warning("attachment ingest failed: %s", exc)

        # —— 会话历史附件（Go 聚合 runs.attachments 经 metadata 下发）——
        # 白名单合并在 _attachments_from_config；此处再给模型一条可见注记：
        # 没有它，模型不知道「此前上传的文件仍可按文件名引用」，会让用户重传（实测缺陷）。
        session_atts_raw = req_meta.get("session_attachments", "")
        history_names: list[str] = []
        if session_atts_raw:
            try:
                import json as _json2

                from cognition.attachments import normalize_attachments as _norm2

                cur_keys = {a.get("resource_key") for a in _norm2(attachments)}
                for a in _json2.loads(session_atts_raw):
                    # 历史名单是 Go 原样透传的 camelCase（resourceKey/fileName），两种键形都认
                    if isinstance(a, dict) and (a.get("resource_key") or a.get("resourceKey")) not in cur_keys:
                        name = str(a.get("file_name") or a.get("fileName") or "")
                        if name and name not in history_names:
                            history_names.append(name)
            except Exception:  # noqa: BLE001 — 历史名单解析失败按无历史处理（白名单侧同样容错）
                history_names = []
        if history_names:
            shown = "、".join(history_names[:8])
            more = f" 等 {len(history_names)} 个" if len(history_names) > 8 else ""
            request.query = (
                f"{request.query}\n（本会话此前上传的文件仍可直接按文件名使用，无需重传：{shown}{more}）"
            )

        graph, state, recursion = self._build(request, ingested)
        # kb_id 单点解析进 metadata：knowledge_search（config 优先）与附件入库共用；
        # attachments 白名单供 script_runner 的 input_files 按文件名解析（M9）；
        # plan_solve 的 executor 分支经 child_config metadata spread 自动透传。
        metadata = {
            "request_id": run_id,
            "run_id": run_id,
            "session_id": session_id,
            "kb_id": kb_id,
            "agent_type": agent_type,  # 研究模式的提示门与检索产物例外共用（M9）
        }
        req_meta = dict(request.metadata)
        output_format = req_meta.get("output_format", "")
        if output_format:
            # per-run 输出格式走 config（react think 调用期临时前置 system；
            # plan 的 executor 分支经 metadata spread 同机制获得）——不进 checkpoint。
            metadata["output_format"] = output_format
        # 生图开关同机制透传：leading_prompt_from_config 读 config.metadata.image_gen
        # 决定是否前置生图指令。漏传则整条生图开关链路成空操作（headline 功能失效）。
        if req_meta.get("image_gen"):
            metadata["image_gen"] = req_meta["image_gen"]
        if attachments:
            import json as _json

            from cognition.attachments import normalize_attachments as _norm

            metadata["attachments"] = _json.dumps(_norm(attachments), ensure_ascii=False)
        if session_atts_raw:
            # 原样透传（已在上方 best-effort 校验过可解析）；消费方 _attachments_from_config
            # 与本轮 attachments 合并成白名单（resource_key 去重，本轮优先）。
            metadata["session_attachments"] = session_atts_raw
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": recursion,
            "metadata": metadata,
        }
        # 可选 Langfuse trace（默认关、未装即 no-op）。
        callbacks = build_langfuse_callbacks(self.settings)
        if callbacks:
            config["callbacks"] = callbacks
            metadata["langfuse_session_id"] = session_id

        log.info("run start")
        try:
            async for ev in graph.astream_events(state, version="v2", config=config):
                for out in mapper.handle(ev):
                    yield out.to_proto()
            # —— M11 HITL 挂起检测（门控三条件：流尽 + 未发终态 + react 主图）——
            # plan 家族/异常 EOF 不探测，防误判；受保护工具 interrupt 时 GraphInterrupt
            # 被带 checkpointer 的根图吞掉，流自然结束且无 RESULT。
            if not mapper.finished and agent_type not in self.plan_graphs:
                async for out in self._emit_pending_approval(graph, config, mapper, log):
                    yield out
            log.info("run done")
        except asyncio.CancelledError:
            log.info("run cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — 节点异常兜底，保证流干净关闭
            log.exception("run failed")
            yield mapper.error_result(str(exc)).to_proto()

    async def _seed_forked_thread(self, src_session: str, src_run: str, new_session: str, log) -> None:
        """把父会话 thread 在"分叉点那一轮结束时"的 messages 通道播种进新 thread。

        checkpoint 深度机制（docs/14 §4，本方法是"时间旅行"的全部认知面实现）：

        1. **轮边界定位靠 checkpoint metadata 的 run_id**：Run() 一直把 run_id 写进
           config["metadata"]，LangGraph 的 get_checkpoint_metadata 会把它合并进该次
           执行产生的**每个** checkpoint 的 metadata 落库（PG 为 JSONB 列）。于是
           "run K 结束时的状态" == 父 thread 中 metadata.run_id==K 的最新 checkpoint，
           aget_state_history(filter={"run_id": K}, limit=1) 一击命中（history 新→旧）。
           这正是零 proto 字段、零 runs 表加列就能锚定轮边界的关键：框架落 metadata
           是既有行为，"发现它可当分叉锚"是设计。

        2. **分叉点快照天然不可变**：checkpoint 链只追加不改写，父会话在分叉之后继续
           演化（新轮次、新分支）不影响这里定位到的历史快照——两条时间线从此独立。

        3. **只搬 messages 通道**（会话记忆的本体，think 节点的修复→裁剪→附件展开三道
           投影全部吃它）：plan 家族的 reduced_state/sub_results 设计上按 run 隔离，
           播过去等于把修过的"跨 run 残留"bug 请回来（docs/14 §4.3）。

        4. **必须用 react 图做定位与播种**：三图共享同一 checkpointer 实例、同 thread_id，
           但 snapshot.values / aupdate_state 都按**图自己的 state schema** 过滤通道——
           实测（langgraph 1.2.7）plan 图没有 messages 通道，用它 aupdate_state(
           {"messages": ...}) 会**静默丢弃**（不报错、什么也没写），aget_state_history
           的 values 里同样看不到 messages。react 图写下的 messages 会随 checkpoint
           保留在 thread 的通道并集里（另一实测：plan run 跑过同 thread 后 messages
           原样保留），因此无论分叉会话首 run 是什么模式，这里统一走 react 图。

        5. **pending interrupt 天然不迁移**：aupdate_state 只写通道值，不搬 pending
           tasks/writes——父会话停在审批卡时，其挂起决议留在父时间线；新时间线的模型
           只"记得曾请求审批"，续聊重新走工具（docs/14 §4.4，刻意语义而非缺陷）。
        """
        graph = self.react_graph

        # —— 幂等闸（第二道保险；第一道在 Go：仅"有 fork 登记且无 own run"才附 fork 键）——
        # 目标 thread 已有任何 checkpoint（并发首消息/客户端重试已播种过）→ 直接跳过，
        # 绝不重复追加消息。空 thread 的判据：values 为空 且 config 无 checkpoint_id。
        new_cfg = {"configurable": {"thread_id": new_session}}
        snapshot = await graph.aget_state(new_cfg)
        existing_ckpt = ((getattr(snapshot, "config", None) or {}).get("configurable") or {}).get(
            "checkpoint_id"
        )
        if (getattr(snapshot, "values", None) or {}) or existing_ckpt:
            log.info("fork seed skipped: thread %s already has checkpoint", new_session)
            return

        # —— 定位：父 thread 中 metadata.run_id == src_run 的最新 checkpoint（该轮末快照）——
        located = None
        async for s in graph.aget_state_history(
            {"configurable": {"thread_id": src_session}}, filter={"run_id": src_run}, limit=1
        ):
            located = s
            break
        if located is None:
            # metadata 机制启用前的远古 thread / checkpoint 被清理 / FAKE 模式重启后
            # InMemorySaver 记忆丢失——均无法定位，诚实报错。
            raise ForkSeedError(
                f"无法定位分叉点（会话过旧或无 checkpoint 记录：run {src_run or '?'}）。"
                "该分叉会话无法继承记忆，请从较新的轮次重新分叉或直接新建会话。"
            )
        messages = (getattr(located, "values", None) or {}).get("messages") or []
        if not messages:
            # plan-only 老会话等：该轮 checkpoint 没有 messages 通道——没有可继承的
            # 对话记忆，同样诚实报错（播出一个空 thread 就是在制造"假继承"）。
            raise ForkSeedError(
                "分叉点没有可继承的对话记忆（该轮可能由纯规划模式产生），无法分叉播种。"
            )

        # —— 播种：经 add_messages reducer 把整段消息写成新 thread 的首个 checkpoint ——
        # 实测（langgraph 1.2.7）：**空 thread 上 aupdate_state 不带 as_node 不抛
        # ambiguous update**——无既有 checkpoint 时更新按 START 语义写入（落库 checkpoint
        # 的 metadata.source=="update"），多节点 react 图也无歧义；若未来版本收紧，
        # 回退方案是显式 as_node="__start__"。附件引用块（占位+key 形态）随消息一起
        # 被搬，但下载白名单仍按"本 run attachments"校验——继承块只是模型上下文，
        # 不赋予新下载权限（安全边界不变，docs/14 §4.3）。
        await graph.aupdate_state(new_cfg, {"messages": messages})
        log.info(
            "forked thread seeded: %s <- %s@%s (%d messages)",
            new_session,
            src_session,
            src_run,
            len(messages),
        )

    async def _emit_pending_approval(self, graph, config, mapper, log):
        """流尽未终态时探测 pending interrupt：发 approval_request + 挂起 RESULT 收尾。"""
        from cognition.approval import first_interrupt_payload

        try:
            snapshot = await graph.aget_state(config)
        except Exception as exc:  # noqa: BLE001 — 探测失败按普通未终态处理
            log.warning("interrupt probe failed: %s", exc)
            return
        payload = first_interrupt_payload(snapshot)
        if payload is None:
            return
        log.info("run paused for approval %s (tool=%s)", payload.get("approval_id"), payload.get("tool"))
        yield mapper.approval_request(payload).to_proto()
        yield mapper.plain_result(
            f"⏸ 已挂起等待人工审批：{payload.get('tool', '')}。批准或拒绝后将自动继续。"
        ).to_proto()

    async def _resume_approval(self, request, req_meta, mapper, session_id, log):
        """审批决议恢复：校验 pending interrupt 匹配 → Command(resume) 续图 → 继续检测链。

        interrupt 态在 checkpoint（thread=session）持久——跨断连/重启/隔夜均可恢复；
        决议 resume 值恒为字符串（dict 会被 langgraph 解释为 interrupt-id 映射）。
        """
        from langgraph.types import Command

        from cognition.approval import first_interrupt_payload, make_decision

        graph = self.react_graph  # 受保护工具仅 react 主图（见 approval.py 模块注释）
        resume_id = req_meta.get("approval_resume_id", "")
        approved = req_meta.get("approval_decision", "") == "approved"
        comment = req_meta.get("approval_comment", "")
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": 2 * int(self.settings.max_steps) + 5,
            "metadata": {
                "request_id": mapper.run_id,
                "run_id": mapper.run_id,
                "session_id": session_id,
                "kb_id": resolve_kb_id(request),
                "agent_type": "react",
            },
        }
        try:
            snapshot = await graph.aget_state(config)
        except Exception as exc:  # noqa: BLE001
            yield mapper.error_result(f"审批恢复失败: {exc}").to_proto()
            return
        payload = first_interrupt_payload(snapshot)
        if payload is None or payload.get("approval_id") != resume_id:
            # 已被处理/会话已继续（新消息重启图丢弃 pending task）/伪造 id → 优雅收尾。
            log.info("no matching pending approval (want=%s)", resume_id)
            yield mapper.plain_result("没有待审批的请求（可能已被处理，或会话已继续对话）。").to_proto()
            return
        # 恢复 run 的 config 必须重建挂起时的 per-run 上下文，否则 ToolNode 重放期
        # script_runner 读不到附件白名单（数据分析审批必失败）。attachments 从 checkpoint
        # 的 state.product_files 恢复（挂起前已入 state）；决议乘 metadata 走既有 Run RPC。
        product = (getattr(snapshot, "values", None) or {}).get("product_files") or []
        if product:
            import json as _json

            from cognition.attachments import normalize_attachments as _norm

            config["metadata"]["attachments"] = _json.dumps(_norm(list(product)), ensure_ascii=False)
        # output_format 走 config 不进 checkpoint（设计如此）→ 恢复轮无法还原，
        # 最终答复回落自由格式（已知限制，审批场景罕见带格式）。
        verdict = "已批准 ✅" if approved else "已拒绝 ⛔"
        note = f"人工审批{verdict}：{payload.get('tool', '')}" + (f"（备注：{comment}）" if comment else "")
        yield mapper.info_event(note).to_proto()
        log.info("resuming after approval %s approved=%s", resume_id, approved)
        try:
            async for ev in graph.astream_events(
                Command(resume=make_decision(approved, comment)), version="v2", config=config
            ):
                for out in mapper.handle(ev):
                    yield out.to_proto()
            # 恢复后可能再次撞审批门（链式审批自然组合）。
            if not mapper.finished:
                async for out in self._emit_pending_approval(graph, config, mapper, log):
                    yield out
        except asyncio.CancelledError:
            log.info("resume cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("resume failed")
            yield mapper.error_result(str(exc)).to_proto()
