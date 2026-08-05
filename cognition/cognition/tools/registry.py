"""工具注册表。

`get_local_tools()` 返回本地工具集合（provider="local"）。`build_tool_suite()` 在装配期把
本地 + MCP + Skill 工具聚合成一份统一 `BaseTool` 列表，并给出：
- `provider_map`：工具名 → provider（local/mcp/skill），注入 EventMapper 让事件带上 tool_provider；
- `closers`：需在停机时优雅关闭的资源（如 MCP worker task / 子进程）。

所有工具都是 LangChain `StructuredTool`，因此经 `get_local_tools` seam 注入 `bind_tools`/`ToolNode`
即可复用现有事件、产物、回放链路（proto/Go 零改）。
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from langchain_core.tools import BaseTool

from cognition.config import Settings, get_settings
from cognition.mcp.naming import dedup
from cognition.tools.calculator import calculator
from cognition.tools.report import write_report

logger = logging.getLogger(__name__)

# 工具提供方标记（事件契约里 tool_provider 字段的来源）。
LOCAL_PROVIDER = "local"

Closer = Callable[[], Awaitable[None]]


def get_local_tools() -> list[BaseTool]:
    """返回本地工具列表（calculator + write_report）。"""
    return [calculator, write_report]


def _build_script_runner(settings: Settings):
    if settings.skill_runner == "docker":
        from cognition.skills.runner.docker import DockerScriptRunner

        return DockerScriptRunner(settings.skill_runner_image, settings=settings)
    from cognition.skills.runner.local import LocalSubprocessScriptRunner

    return LocalSubprocessScriptRunner(settings)


async def build_tool_suite(
    settings: Settings | None = None,
    *,
    rag_model=None,
) -> tuple[list[BaseTool], dict[str, str], list[Closer]]:
    """聚合 local + MCP + Skill 工具，返回 (tools, provider_map, closers)。

    ``rag_model`` 是评测/诊断接缝：传入时让 knowledge_search 内部 RAG 子图复用调用方
    的模型包装器（统一 temperature、retry、usage 统计）；生产默认 None，行为不变。
    """
    settings = settings or get_settings()
    tools: list[BaseTool] = list(get_local_tools())
    closers: list[Closer] = []

    # —— MCP：装配期预热（fail-soft 在 registry 内） ——
    if settings.mcp_enabled and settings.mcp_servers:
        from cognition.mcp.config import parse_servers
        from cognition.mcp.registry import McpRegistry

        registry = McpRegistry()
        cfgs = parse_servers(settings.mcp_servers)
        mcp_tools = await registry.preload(cfgs)
        tools.extend(mcp_tools)
        closers.append(registry.aclose)
        if registry.errors:
            logger.warning("MCP 部分 server 预热失败: %s", registry.errors)

    # —— RAG：把 Agentic RAG 子图包成 knowledge_search 工具 ——
    if getattr(settings, "rag_enabled", False):
        from cognition.rag.graph import build_rag_subgraph
        from cognition.tools.knowledge_search import build_knowledge_search_tool

        subgraph = build_rag_subgraph(
            settings, model=rag_model
        )  # 装配期编译一次（含 store/embedder/rerank 预热）
        tools.append(build_knowledge_search_tool(subgraph, settings))

    # —— 图像生成：provider 配置非空才注册（镜像 rag_enabled 门控）——
    if getattr(settings, "image_gen_provider", ""):
        from cognition.providers.image import build_image_provider
        from cognition.tools.image_generate import build_image_generate_tool

        tools.append(build_image_generate_tool(build_image_provider(settings), settings))

    # —— M12：web_fetch（默认开，SSRF 服务端封禁）与 code_interpreter（沙箱执行）——
    if getattr(settings, "web_fetch_enabled", True):
        from cognition.tools.web_fetch import build_web_fetch_tool

        tools.append(build_web_fetch_tool(settings))

    # —— 联网搜索：provider 配置非空才注册（默认 auto=开——ddg 免 key 可用，装配零
    # I/O，运行时失败在工具内 fail-soft；镜像 image_gen_provider 的空串门控）——
    if getattr(settings, "search_provider", ""):
        from cognition.providers.search import build_search_provider
        from cognition.tools.web_search import build_web_search_tool

        tools.append(build_web_search_tool(build_search_provider(settings), settings))
    if getattr(settings, "code_interpreter_enabled", False):
        from cognition.tools.code_interpreter import build_code_interpreter_tool

        unsafe = (
            getattr(settings, "skill_runner", "local") != "docker"
            and "code_interpreter" not in set(getattr(settings, "approval_tools", []) or [])
        )
        if unsafe:
            logger.warning(
                "code_interpreter 已开启但既非 docker 运行器也未列入 approval_tools——"
                "这是认知面主机上的无沙箱任意代码执行，仅限可信本地环境！"
            )
        tools.append(build_code_interpreter_tool(settings))

    # —— Skill：扫描 SKILL.md + 构建工具 ——
    if settings.skills_enabled and settings.skills_dirs:
        from cognition.skills.registry import SkillRegistry
        from cognition.skills.tools import build_skill_tools

        skill_registry = SkillRegistry()
        skill_registry.refresh(settings.skills_dirs)
        runner = _build_script_runner(settings)
        tools.extend(
            build_skill_tools(
                skill_registry,
                runner,
                settings=settings,
                max_body_chars=settings.skill_disclosure_max_chars,
                default_timeout=settings.skill_default_timeout,
            )
        )

    tools = dedup(tools)
    provider_map = {t.name: (t.metadata or {}).get("provider", LOCAL_PROVIDER) for t in tools}
    return tools, provider_map, closers
