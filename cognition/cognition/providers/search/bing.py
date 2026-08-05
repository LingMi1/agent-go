"""Bing 免 key 搜索 provider：`format=rss` 服务端渲染输出优先，结果页 HTML 兜底。

引入动机：DDG 两端点在部分网络（实测国内出口）稳定返回 202 挑战页；Bing 主站
HTML 在国内出口是 JS 壳页（结果由脚本填充，静态抓取为空），但 **RSS 输出
（search?q=…&format=rss）始终是服务端渲染的 XML**，两类网络都可解析——因此
RSS 为主路径，b_algo HTML 解析兜底（RSS 关闭/改版时仍有机会）。

解析用标准库（零第三方依赖）：RSS 走 xml.etree（Python 3.13 默认不解析外部实体，
无 XXE 面）；HTML 走 html.parser（li.b_algo → h2>a 标题 → 首个 <p> 摘要，
href 可能是 bing.com/ck/a?...&u=a1<base64url> 跳转包装，resolve_bing_url 解包）。
parse_bing_rss / parse_bing_html / resolve_bing_url 均为纯函数（离线 fixture 可测）。
被拦截/零结果 → SearchError，工具层 fail-soft。
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

from cognition.providers.search.base import SearchError, SearchResult

_ENDPOINT = "https://www.bing.com/search"
# 浏览器化 UA（默认 httpx UA 会被判 bot 跳人机验证）
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "wbr", "area", "base", "col", "embed", "source", "track"}


def resolve_bing_url(href: str) -> str:
    """Bing 结果 href → 真实 URL（纯函数）。

    组织结果常被包装为 bing.com/ck/a?...&u=a1<base64url>&...：u 值去掉前缀
    "a1" 后是 URL 的 base64url 编码（无 padding，需补齐）。解包失败返回 ""
    （调用侧丢弃该条，宁缺毋滥——不给 LLM 一个 bing 跳转链）。未包装原样返回。
    """
    if not href:
        return ""
    parsed = urlparse(href)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        u = (parse_qs(parsed.query).get("u") or [""])[0]
        if len(u) > 2 and u.startswith("a1"):
            b64 = u[2:]
            try:
                raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
                url = raw.decode("utf-8", errors="strict")
                if url.startswith(("http://", "https://")):
                    return url
            except (ValueError, UnicodeDecodeError):  # binascii.Error ⊂ ValueError；非 ASCII 入参也抛 ValueError
                pass
        return ""
    return href


def _clean(chunks: list[str]) -> str:
    return " ".join("".join(chunks).split())


class _BingParser(HTMLParser):
    """b_algo 条目状态机：li.b_algo → h2>a 标题 → 首个 <p> 摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._item_depth = 0  # 在 li.b_algo 内的嵌套深度（0=不在条目内）
        self._in_h2 = False
        self._in_title = False
        self._got_snippet = False
        self._snippet_depth = 0
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._href = ""

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in _VOID_TAGS:
            return
        d = dict(attrs)
        classes = (d.get("class") or "").split()
        if not self._item_depth:
            if tag == "li" and "b_algo" in classes:
                self._item_depth = 1
                self._in_h2 = self._in_title = self._got_snippet = False
                self._snippet_depth = 0
                self._title, self._snippet, self._href = [], [], ""
            return
        self._item_depth += 1
        if self._snippet_depth:
            self._snippet_depth += 1
            return
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._title:
            self._in_title = True
            self._href = d.get("href") or ""
        elif tag == "p" and not self._in_h2 and not self._got_snippet and self._title:
            self._snippet_depth = 1
            self._snippet = []

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in _VOID_TAGS or not self._item_depth:
            return
        if self._snippet_depth:
            self._snippet_depth -= 1
            if self._snippet_depth == 0:
                self._got_snippet = True
        if tag == "a" and self._in_title:
            self._in_title = False
        elif tag == "h2":
            self._in_h2 = False
        self._item_depth -= 1
        if self._item_depth == 0:  # </li> 收口：产出一条结果
            title = _clean(self._title)
            url = resolve_bing_url(self._href)
            if title and url:
                self.results.append(SearchResult(title=title, url=url, snippet=_clean(self._snippet)))

    def handle_data(self, data):  # noqa: ANN001
        if self._in_title:
            self._title.append(data)
        elif self._snippet_depth:
            self._snippet.append(data)


def parse_bing_html(html: str) -> list[SearchResult]:
    """Bing 结果页 → 结果列表（纯函数；拦截页/改版 → []，尽力而为不抛）。"""
    p = _BingParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 — 残破 HTML 尽力而为
        pass
    return p.results


def _strip_tags(text: str) -> str:
    """description 里可能混 <b> 等行内标签，粗剥后归一空白。"""

    class _S(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.chunks: list[str] = []

        def handle_data(self, data):  # noqa: ANN001
            self.chunks.append(data)

    s = _S()
    try:
        s.feed(text)
    except Exception:  # noqa: BLE001
        return _clean([text])
    return _clean(s.chunks)


def parse_bing_rss(xml_text: str) -> list[SearchResult]:
    """Bing RSS 输出 → 结果列表（纯函数；非法 XML / 无 item → []）。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    results: list[SearchResult] = []
    for item in root.iter("item"):
        title = _strip_tags(item.findtext("title") or "")
        url = (item.findtext("link") or "").strip()
        snippet = _strip_tags(item.findtext("description") or "")
        if not (title and url.startswith(("http://", "https://"))):
            continue
        if (urlparse(url).hostname or "").endswith("bing.com"):
            continue  # RSS 首条常是查询自引（bing.com/search?q=…），非结果
        results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


class BingProvider:
    """免 key 的 Bing provider（auto 免 key 链中优先于 DDG：国内网络可达性更好）。

    构造零 I/O；transport 为测试接缝。
    """

    name = "bing"

    def __init__(self, timeout: float = 15.0, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._timeout = timeout
        self._transport = transport

    async def search(self, query: str, *, max_results: int = 6) -> list[SearchResult]:
        count = max(max_results, 10)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,  # 固定 Bing 端点（非 LLM 可写 URL），无 SSRF 面
                headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            ) as client:
                # RSS 主路径：两类网络（国内 JS 壳页 / 海外整页）都保持服务端渲染；
                # mkt=zh-CN 显著改善中文查询相关性（实测无 mkt 时中文长查询漂移严重）
                resp = await client.get(
                    _ENDPOINT, params={"q": query, "format": "rss", "count": count, "mkt": "zh-CN"}
                )
                results = parse_bing_rss(resp.text) if resp.status_code == 200 else []
                if not results:  # RSS 关闭/改版 → 结果页 HTML 兜底
                    resp = await client.get(_ENDPOINT, params={"q": query, "count": count})
                    results = parse_bing_html(resp.text) if resp.status_code == 200 else []
        except httpx.HTTPError as exc:
            raise SearchError(f"Bing 请求失败：{exc}") from exc
        if not results:
            raise SearchError("Bing 无结果或被拦截，请稍后再试或配置 Tavily key")
        return results[:max_results]
