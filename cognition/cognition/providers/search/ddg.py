"""DuckDuckGo 免 key 搜索 provider：html 端点优先，lite 端点兜底。

POST html.duckduckgo.com/html/（表单 q=<query>，浏览器 UA），非 200/零结果再退
lite.duckduckgo.com/lite/。解析用标准库 html.parser（镜像 web_fetch._TextExtractor
的免第三方依赖取向，**不引入 ddgs 包**）；结果链接常被包装成
//duckduckgo.com/l/?uddg=<pct-url>&rut=…，经 parse_qs 解出真实 URL。
parse_ddg_html / parse_ddg_lite 均为纯函数（离线 fixture 可测）。
被限流/人机验证（202/挑战页/两端点皆空）→ SearchError，工具层 fail-soft。
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

from cognition.providers.search.base import SearchError, SearchResult

_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
_LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"
# 浏览器化 UA：默认 httpx UA 会被 DDG 直接判 bot（202 挑战页概率大增）。
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 无闭合的空元素：深度计数必须跳过，否则 <br> 会让 snippet 捕获状态永不归零。
_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "wbr", "area", "base", "col", "embed", "source", "track"}


def resolve_ddg_url(href: str) -> str:
    """DDG 结果 href → 真实 URL（纯函数）。

    html 端点的链接被包装为 //duckduckgo.com/l/?uddg=<pct-url>&rut=…；
    parse_qs 取 uddg（其值已完成百分号解码，等价 unquote，不再二次解码——
    防 %25 双重解码损坏）。未包装的 href 原样返回，scheme-relative 补 https:。
    """
    if not href:
        return ""
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = (parse_qs(parsed.query).get("uddg") or [""])[0]
        if uddg:
            return uddg
    if href.startswith("//"):
        return "https:" + href
    return href


def _clean(chunks: list[str]) -> str:
    """文本块合并 + 空白归一（换行/连续空格折叠为单空格）。"""
    return " ".join("".join(chunks).split())


class _DdgHtmlParser(HTMLParser):
    """html 端点结果页解析：class=result__a 锚（标题+href）+ result__snippet 摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._in_title = False
        self._snippet_depth = 0  # 摘要元素可嵌套 <b> 等行内标签，按深度配对
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._href = ""

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in _VOID_TAGS:
            return
        if self._snippet_depth:
            self._snippet_depth += 1
            return
        d = dict(attrs)
        classes = (d.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._in_title = True
            self._href = d.get("href") or ""
            self._title = []
        elif "result__snippet" in classes:  # <a> 或 <div> 皆有出现，按 class 认
            self._snippet_depth = 1
            self._snippet = []

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in _VOID_TAGS:
            return
        if self._in_title and tag == "a":
            self._in_title = False
            title = _clean(self._title)
            url = resolve_ddg_url(self._href)
            if title and url:
                self.results.append(SearchResult(title=title, url=url, snippet=""))
        elif self._snippet_depth:
            self._snippet_depth -= 1
            if self._snippet_depth == 0 and self.results:
                self.results[-1]["snippet"] = _clean(self._snippet)

    def handle_data(self, data):  # noqa: ANN001
        if self._in_title:
            self._title.append(data)
        elif self._snippet_depth:
            self._snippet.append(data)


class _DdgLiteParser(HTMLParser):
    """lite 端点结果表解析：rel="nofollow" 锚（标题+href）+ class=result-snippet 单元格。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._in_link = False
        self._snippet_depth = 0
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._href = ""

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in _VOID_TAGS:
            return
        if self._snippet_depth:
            self._snippet_depth += 1
            return
        d = dict(attrs)
        if tag == "a" and (d.get("rel") or "") == "nofollow":
            self._in_link = True
            self._href = d.get("href") or ""
            self._title = []
        elif tag == "td" and "result-snippet" in (d.get("class") or "").split():
            self._snippet_depth = 1
            self._snippet = []

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in _VOID_TAGS:
            return
        if self._in_link and tag == "a":
            self._in_link = False
            title = _clean(self._title)
            url = resolve_ddg_url(self._href)
            if title and url:
                self.results.append(SearchResult(title=title, url=url, snippet=""))
        elif self._snippet_depth:
            self._snippet_depth -= 1
            if self._snippet_depth == 0 and self.results:
                self.results[-1]["snippet"] = _clean(self._snippet)

    def handle_data(self, data):  # noqa: ANN001
        if self._in_link:
            self._title.append(data)
        elif self._snippet_depth:
            self._snippet.append(data)


def parse_ddg_html(html: str) -> list[SearchResult]:
    """html 端点结果页 → 结果列表（纯函数；挑战页/残破 HTML → []，尽力而为不抛）。"""
    p = _DdgHtmlParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 — 残破 HTML 尽力而为
        pass
    return p.results


def parse_ddg_lite(html: str) -> list[SearchResult]:
    """lite 端点结果页 → 结果列表（纯函数；挑战页/残破 HTML → []）。"""
    p = _DdgLiteParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        pass
    return p.results


class DdgProvider:
    """免 key 的 DuckDuckGo provider（auto 模式无 Tavily key 时的默认引擎）。

    构造零 I/O；transport 为测试接缝（httpx.MockTransport 离线验证降级链路）。
    """

    name = "ddg"

    def __init__(self, timeout: float = 15.0, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._timeout = timeout
        self._transport = transport

    async def search(self, query: str, *, max_results: int = 6) -> list[SearchResult]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,  # 固定 DDG 端点（非 LLM 可写 URL），无 SSRF 面
                headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            ) as client:
                resp = await client.post(_HTML_ENDPOINT, data={"q": query})
                results = parse_ddg_html(resp.text) if resp.status_code == 200 else []
                if not results:  # 202/挑战页/零结果 → lite 端点兜底
                    resp = await client.post(_LITE_ENDPOINT, data={"q": query})
                    results = parse_ddg_lite(resp.text) if resp.status_code == 200 else []
        except httpx.HTTPError as exc:
            raise SearchError(f"DDG 请求失败：{exc}") from exc
        if not results:
            raise SearchError("DDG 暂时限流/拦截，请稍后再试或配置 Tavily key")
        return results[:max_results]
