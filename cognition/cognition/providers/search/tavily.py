"""Tavily 搜索 provider：POST /search，Bearer 认证，JSON 响应。

请求体**只带** {"query", "max_results"}（契约测试锁死字段集，多传字段会悄悄改变
计费/行为面）；响应取 results[].{title,url,content} → {title,url,snippet}
（snippet 截 300 字符控观测预算）。构造函数零 I/O；transport 为测试接缝
（httpx.MockTransport 离线契约测试，镜像 web_fetch 的 seam 约定）。
"""

from __future__ import annotations

from typing import Optional

import httpx

from cognition.providers.search.base import SearchError, SearchResult

_ENDPOINT = "https://api.tavily.com/search"
_SNIPPET_MAX = 300


class TavilyProvider:
    """Tavily（https://app.tavily.com，免费额度）——auto 模式配了 key 优先用它。"""

    name = "tavily"

    def __init__(
        self,
        api_key: str,
        timeout: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    async def search(self, query: str, *, max_results: int = 6) -> list[SearchResult]:
        try:
            # 每次调用建临时 client（搜索是低频操作，省掉连接池生命周期管理）。
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.post(
                    _ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"query": query, "max_results": max_results},
                )
        except httpx.HTTPError as exc:
            raise SearchError(f"Tavily 请求失败：{exc}") from exc
        if resp.status_code != 200:
            raise SearchError(f"Tavily 返回 HTTP {resp.status_code}（检查 API key 与额度）")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — 非 JSON 响应
            raise SearchError("Tavily 响应不是合法 JSON") from exc
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SearchError("Tavily 响应格式异常（缺少 results 列表）")
        out: list[SearchResult] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append(
                SearchResult(
                    title=str(it.get("title") or ""),
                    url=str(it.get("url") or ""),
                    snippet=str(it.get("content") or "")[:_SNIPPET_MAX],
                )
            )
        return out[:max_results]
