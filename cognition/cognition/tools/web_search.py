"""web_search 工具：联网搜索公开网页（provider 装配期注入：tavily/ddg/fake）。

观测双通道：人类可读的编号列表（模型直接消化，snippet 截 200 字符控预算）+
**末行 sentinel JSON**（`WEB_SEARCH_RESULTS_JSON:{...}` 单行，前端据此渲染来源
卡片，模型侧无感）。provider 失败 fail-soft 成观测文本（不抛、不带 sentinel），
模型可改用 web_fetch 直抓已知 URL 或基于已有知识作答——搜索挂了不该中断编排。
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool, StructuredTool

from cognition.config import Settings
from cognition.providers.search.base import SearchError, SearchProvider, SearchResult

# 前端解析约定：观测最后一行以此前缀开头，后随单行紧凑 JSON。
WEB_SEARCH_JSON_PREFIX = "WEB_SEARCH_RESULTS_JSON:"

_SNIPPET_DISPLAY_MAX = 200  # 编号列表里的摘要预算（sentinel 保留 provider 原文 ≤300）
_MAX_RESULTS_CAP = 10


def format_observation(query: str, provider_name: str, results: list[SearchResult]) -> str:
    """搜索结果 → 观测文本（纯函数，可测）。

    编号列表在前；空一行后 sentinel JSON 恒为**最后一行且单行**（json.dumps 会把
    换行转义为 \\n，紧凑分隔符 + ensure_ascii=False 保中文可读）。空结果也带
    sentinel（results 为空数组）——前端可据此渲染"无结果"而非当作失败。
    """
    lines: list[str] = []
    if results:
        lines.append(f"搜索「{query}」共 {len(results)} 条结果（{provider_name}）：")
        for i, r in enumerate(results, 1):
            snippet = " ".join(str(r.get("snippet") or "").split())  # 空白归一防换行破排版
            lines.append(f"{i}. {r.get('title') or ''}")
            lines.append(f"   {r.get('url') or ''}")
            if snippet:
                lines.append(f"   {snippet[:_SNIPPET_DISPLAY_MAX]}")
    else:
        lines.append(
            f"搜索「{query}」未找到结果（{provider_name}）——可换关键词重试，"
            "或用 web_fetch 直接抓取已知 URL。"
        )
    payload = {
        "query": query,
        "provider": provider_name,
        "results": [
            {"title": r.get("title") or "", "url": r.get("url") or "", "snippet": r.get("snippet") or ""}
            for r in results
        ],
    }
    sentinel = WEB_SEARCH_JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "\n".join(lines) + "\n\n" + sentinel


def build_web_search_tool(provider: SearchProvider, settings: Settings) -> BaseTool:
    """构造 web_search 工具（闭包持有 provider/settings，装配期一次）。"""
    default_max = int(getattr(settings, "search_max_results", 6) or 6)
    provider_name = getattr(provider, "name", "unknown")

    async def web_search(query: str, max_results: int = default_max) -> str:
        """联网搜索公开网页，返回带标题/链接/摘要的结果列表。

        适合查最新信息、找资料来源；找到候选后用 web_fetch 深入阅读具体页面。
        """
        n = max(1, min(int(max_results), _MAX_RESULTS_CAP))
        try:
            results = await provider.search(query, max_results=n)
        except SearchError as exc:
            return f"搜索失败：{exc}。可改用 web_fetch 直接抓取已知 URL，或基于已有知识作答。"
        except Exception as exc:  # noqa: BLE001 — 任何 provider 异常都不上抛（fail-soft）
            return f"搜索失败：{exc}。可改用 web_fetch 直接抓取已知 URL，或基于已有知识作答。"
        return format_observation(query, provider_name, list(results)[:n])

    tool = StructuredTool.from_function(
        coroutine=web_search,
        name="web_search",
        description=(
            "联网搜索公开网页，返回带标题/链接/摘要的结果列表。"
            "适合查最新信息、找资料来源；找到候选后用 web_fetch 深入阅读具体页面。"
            "query 用 2~5 个关键词组合（如「DeepSeek 新模型 2026」），不要整句自然语言——"
            "关键词式查询召回明显更好；一次没找到可换关键词再搜。"
        ),
    )
    tool.metadata = {"provider": "local"}
    return tool
