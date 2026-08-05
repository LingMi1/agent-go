"""联网搜索 provider（web_search 工具的引擎侧）。

协议：`search(query, max_results=6) -> list[SearchResult]`（title/url/snippet；
失败抛 SearchError，工具层 fail-soft 成观测文本）。
实现：tavily（API key，质量优先）| bing（免 key，结果页解析）| baidu（免 key，
国内网络兜底）| ddg（免 key，DuckDuckGo html/lite 双端点）| fake（确定性，测试/离线）。
"auto" = 降级链：tavily（有 key 才入链）→ bing → baidu → ddg，首个成功者胜出——
实测国内出口 DDG 稳定 202 挑战、Bing 主站是 JS 壳页（静态抓取为空），百度服务端渲染
可用；海外网络则 bing 直接胜出。链式兜底自适应网络环境，保证开箱即用。
search_provider 置空串则不注册 web_search 工具（registry 门控）。
"""

from __future__ import annotations

from typing import Any

from cognition.providers.search.base import SearchError, SearchProvider, SearchResult

__all__ = ["ChainSearchProvider", "SearchError", "SearchProvider", "SearchResult", "build_search_provider"]


class ChainSearchProvider:
    """降级链 provider：按序尝试，首个成功者胜出；全败抛最后一个 SearchError。

    `name` 动态更新为最近一次成功引擎名（观察串「（tavily/bing/ddg）」标注真实来源；
    并发 run 共用实例时该标注存在竞态，仅影响展示文案，不影响结果正确性——接受）。
    """

    def __init__(self, providers: list[SearchProvider]) -> None:
        assert providers, "降级链至少一个 provider"
        self._providers = providers
        self.name = providers[0].name

    async def search(self, query: str, *, max_results: int = 6) -> list[SearchResult]:
        last: SearchError | None = None
        for p in self._providers:
            try:
                results = await p.search(query, max_results=max_results)
            except SearchError as exc:
                last = exc
                continue
            self.name = p.name
            return results
        raise last if last is not None else SearchError("搜索链无可用 provider")


def build_search_provider(settings: Any) -> SearchProvider:
    """按 settings 构建搜索 provider（镜像 image 的集中选择模式；纯装配零 I/O）。"""
    provider = getattr(settings, "search_provider", "auto") or "auto"
    timeout = float(getattr(settings, "search_timeout_seconds", 15.0) or 15.0)
    api_key = getattr(settings, "tavily_api_key", None)
    if provider == "auto":
        from cognition.providers.search.baidu import BaiduProvider
        from cognition.providers.search.bing import BingProvider
        from cognition.providers.search.ddg import DdgProvider

        chain: list[SearchProvider] = []
        if api_key:
            from cognition.providers.search.tavily import TavilyProvider

            chain.append(TavilyProvider(api_key=api_key, timeout=timeout))
        chain.extend([BingProvider(timeout=timeout), BaiduProvider(timeout=timeout), DdgProvider(timeout=timeout)])
        return ChainSearchProvider(chain)
    if provider == "tavily":
        from cognition.providers.search.tavily import TavilyProvider

        return TavilyProvider(api_key=api_key or "", timeout=timeout)
    if provider == "bing":
        from cognition.providers.search.bing import BingProvider

        return BingProvider(timeout=timeout)
    if provider == "baidu":
        from cognition.providers.search.baidu import BaiduProvider

        return BaiduProvider(timeout=timeout)
    if provider == "ddg":
        from cognition.providers.search.ddg import DdgProvider

        return DdgProvider(timeout=timeout)
    if provider == "fake":
        from cognition.providers.search.fake import FakeSearchProvider

        return FakeSearchProvider()
    # 配置错拼在装配期就炸（fail-fast），不留到首次搜索才发现。
    raise ValueError(f"未知搜索 provider: {provider!r}（可选 auto/tavily/bing/baidu/ddg/fake，空=不注册）")
