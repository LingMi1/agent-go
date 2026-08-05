"""确定性假搜索 provider：同 query 恒同结果（单测/离线 e2e 用，不触网）。"""

from __future__ import annotations

import hashlib

from cognition.providers.search.base import SearchResult


class FakeSearchProvider:
    """URL 由 query 哈希决定：确定性 + 不同 query 可区分（镜像 FakeImageProvider 取向）。"""

    name = "fake"

    async def search(self, query: str, *, max_results: int = 6) -> list[SearchResult]:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        results = [
            SearchResult(
                title=f"关于「{query}」的资料 {i}",
                url=f"https://example.com/{digest[:12]}/{i}",
                snippet=f"这是与「{query}」相关的第 {i} 条离线搜索结果摘要（fake provider，测试确定性）。",
            )
            for i in (1, 2, 3)
        ]
        return results[: max(1, max_results)]
