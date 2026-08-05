"""百度免 key 搜索 provider：GET www.baidu.com/s 结果页解析。

引入动机：国内出口网络下 DDG 稳定 202 挑战、Bing 主站返回 JS 壳页（结果由前端脚本
填充，静态抓取为空）；百度结果页仍是服务端渲染，且结果容器带 mu 属性=目标真实 URL
（无需解跳转链）。auto 降级链中排在 bing 之后（海外网络 bing 质量更好且可达，
国内 bing 秒失败后由百度兜底——链式自适应网络环境，见 __init__）。

解析（标准库 html.parser，零第三方依赖）：
- 结果条目 <div class="result c-container ..." mu="<真实URL>">；
- 标题 = 条目内 <h3> 全文本；摘要 = class 含 c-abstract 或 content-right 前缀的元素文本；
- 无 mu 的条目（广告位/聚合卡）直接丢弃——宁缺毋滥，不给 LLM 百度跳转链。
parse_baidu_html 为纯函数（离线 fixture 可测）。安全验证页/零结果 → SearchError。
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional

import httpx

from cognition.providers.search.base import SearchError, SearchResult

_ENDPOINT = "https://www.baidu.com/s"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "wbr", "area", "base", "col", "embed", "source", "track"}


def _clean(chunks: list[str]) -> str:
    return " ".join("".join(chunks).split())


def _is_snippet_class(classes: list[str]) -> bool:
    """摘要元素判定：c-abstract（经典版式）或 content-right_ 前缀（新版式带 hash 后缀）。"""
    return any(c == "c-abstract" or c.startswith("content-right") for c in classes)


class _BaiduParser(HTMLParser):
    """result c-container 条目状态机：容器 mu 属性=URL → h3 标题 → 摘要元素。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._item_depth = 0
        self._url = ""
        self._in_h3 = False
        self._h3_depth = 0
        self._got_snippet = False
        self._snippet_depth = 0
        self._title: list[str] = []
        self._snippet: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in _VOID_TAGS:
            return
        d = dict(attrs)
        classes = (d.get("class") or "").split()
        if not self._item_depth:
            if tag == "div" and "result" in classes and "c-container" in classes:
                self._item_depth = 1
                self._url = d.get("mu") or ""
                self._in_h3 = self._got_snippet = False
                self._h3_depth = self._snippet_depth = 0
                self._title, self._snippet = [], []
            return
        self._item_depth += 1
        if self._snippet_depth:
            self._snippet_depth += 1
            return
        if self._in_h3:
            self._h3_depth += 1
            return
        if tag == "h3" and not self._title:
            self._in_h3 = True
            self._h3_depth = 1
        elif not self._got_snippet and _is_snippet_class(classes):
            self._snippet_depth = 1
            self._snippet = []

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in _VOID_TAGS or not self._item_depth:
            return
        if self._snippet_depth:
            self._snippet_depth -= 1
            if self._snippet_depth == 0:
                self._got_snippet = True
        elif self._in_h3:
            self._h3_depth -= 1
            if self._h3_depth == 0:
                self._in_h3 = False
        self._item_depth -= 1
        if self._item_depth == 0:  # 容器收口：仅收带 mu 真实 URL 的条目
            title = _clean(self._title)
            if title and self._url.startswith(("http://", "https://")):
                self.results.append(SearchResult(title=title, url=self._url, snippet=_clean(self._snippet)))

    def handle_data(self, data):  # noqa: ANN001
        if self._in_h3:
            self._title.append(data)
        elif self._snippet_depth:
            self._snippet.append(data)


def parse_baidu_html(html: str) -> list[SearchResult]:
    """百度结果页 → 结果列表（纯函数；安全验证页/改版 → []，尽力而为不抛）。"""
    p = _BaiduParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 — 残破 HTML 尽力而为
        pass
    return p.results


class BaiduProvider:
    """免 key 的百度 provider（国内网络兜底引擎）。构造零 I/O；transport 为测试接缝。"""

    name = "baidu"

    def __init__(self, timeout: float = 15.0, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._timeout = timeout
        self._transport = transport

    async def search(self, query: str, *, max_results: int = 6) -> list[SearchResult]:
        params = {"wd": query, "rn": max(max_results, 10)}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,  # 固定百度端点（非 LLM 可写 URL），无 SSRF 面
                headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            ) as client:
                resp = await client.get(_ENDPOINT, params=params)
                results = parse_baidu_html(resp.text) if resp.status_code == 200 else []
                if not results and "安全验证" in resp.text:
                    # 无 BAIDUID cookie 的裸请求很快触发安全验证页；同会话先 GET 首页
                    # 领 cookie（httpx cookie jar 自动携带）再重试一次。
                    await client.get("https://www.baidu.com/")
                    resp = await client.get(_ENDPOINT, params=params)
                    results = parse_baidu_html(resp.text) if resp.status_code == 200 else []
        except httpx.HTTPError as exc:
            raise SearchError(f"百度请求失败：{exc}") from exc
        if not results:
            raise SearchError("百度无结果或触发安全验证，请稍后再试或配置 Tavily key")
        return results[:max_results]
