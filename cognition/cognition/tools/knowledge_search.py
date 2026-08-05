"""knowledge_search 工具：把 Agentic RAG 子图包成一个本地工具暴露给外层图。

外层 ReAct/Plan-Execute 只看到一次 tool_call/tool_result；答案作 observation（内联〔n〕）。
产物策略（M9 UX 修正）：**默认不登记 artifact**——检索是过程证据不是交付物，ReAct 一轮
6-8 次检索会把产物区刷成 8 份同名文件（业界一致做法是折叠为步骤摘要）。例外：
- settings.search_artifact_enabled=True 显式开启；
- deep_research 模式（检索证据属于研究交付物），且用 tool_call_id 后缀唯一命名防同名刷屏。
子图内部的 route/expand/retrieve/reflect/rerank/generate 事件不外泄（proto/Go 零改）。
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolCallId, StructuredTool

from cognition.config import Settings
from cognition.rag.citation import sources_to_artifact_md
from cognition.tools.report import _maybe_upload, _run_id_from_config

_FILE_NAME = "search-results.md"
_MIME = "text/markdown"


def _kb_id_from_config(config: Optional[RunnableConfig]) -> str:
    """从 RunnableConfig.metadata 取当前 run 的 kb_id（servicer 注入，工具层只读）。"""
    if not config:
        return ""
    meta = config.get("metadata") or {}
    return str(meta.get("kb_id") or "")


def _agent_type_from_config(config: Optional[RunnableConfig]) -> str:
    if not config:
        return ""
    meta = config.get("metadata") or {}
    return str(meta.get("agent_type") or "")


def search_artifact_file_name(agent_type: str, tool_call_id: str) -> Optional[str]:
    """产物登记决策（纯函数）：None=不登记；deep_research 用 tcid 唯一命名。

    取**尾** 6 位：DeepSeek 的 id 形如 call_00_<rand>，前缀几乎恒为 "call_0"，
    取头部会让所有检索产物撞成同一个名字（实测踩坑）。"""
    if agent_type == "deep_research":
        suffix = (tool_call_id or "tc")[-6:]
        return f"search-results-{suffix}.md"
    return None


def build_knowledge_search_tool(subgraph: Any, settings: Settings) -> BaseTool:
    """用编译好的 RAG 子图构建 knowledge_search 工具（装配期一次）。"""

    async def knowledge_search(
        query: str,
        kb_id: str = "",
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
        config: RunnableConfig = None,  # type: ignore[assignment]
    ) -> tuple[str, Optional[dict]]:
        """在知识库中检索并基于证据作答。当问题需要事实/文档依据时使用。

        kb_id 通常无需填写：系统会自动定位当前用户的知识库（上传的文档都在其中）。
        """
        # 安全：config（servicer 注入的 owner kb）优先于 LLM 填的入参——kb_id 是模型
        # 可见参数，被提示注入/幻觉填成他人 kb 也不能越权检索；无 config 时（离线
        # 测试/脚本直调）才用入参。
        kb = _kb_id_from_config(config) or kb_id
        result = await subgraph.ainvoke({"query": query, "kb_id": kb})
        answer = str(result.get("answer", "") or "")
        sources = result.get("sources", []) or []

        # 产物门：默认不登记（过程≠交付物）；显式开关或 deep_research 例外。
        tcid = tool_call_id or "tc"
        file_name: Optional[str] = None
        if getattr(settings, "search_artifact_enabled", False):
            file_name = _FILE_NAME
        else:
            file_name = search_artifact_file_name(_agent_type_from_config(config), tcid)

        if file_name is None:
            summary = answer + (f"\n（依据 {len(sources)} 条来源）" if sources else "")
            return summary, None

        run_id = _run_id_from_config(config)
        resource_key = f"{run_id}/{tcid}/{file_name}"
        body = sources_to_artifact_md(query, answer, sources).encode("utf-8")
        _maybe_upload(settings, resource_key, body, _MIME)

        artifact = {
            "resource_key": resource_key,
            "name": file_name,
            "file_name": file_name,
            "mime_type": _MIME,
            "size": len(body),
            "download_url": f"/artifacts/{resource_key}",
            "preview_url": f"/artifacts/{resource_key}",
            "missing": False,
        }
        summary = answer + (f"\n（依据 {len(sources)} 条来源，详见 {file_name}）" if sources else "")
        return summary, artifact

    tool = StructuredTool.from_function(
        coroutine=knowledge_search,
        name="knowledge_search",
        description="在知识库中做混合检索并基于证据作答（返回答案摘要 + 来源产物）。当问题需要事实/文档依据时使用。",
        response_format="content_and_artifact",
    )
    tool.metadata = {"provider": "local"}
    return tool
