"""web_fetch 工具（M12）：抓取网页并抽取可读文本，供深度研究/资料核对。

安全边界（SSRF）：URL 解析后逐个校验解析出的 IP——私网/环回/链路本地/元数据地址
一律拒绝（工具参数是 LLM 可写面，提示注入可让模型去打内网；封禁在服务端做）。
重定向**手动逐跳**（禁 httpx 自动跟随）：每跳发请求前先校验，杜绝"请求已打到内网才校验落点"。
产出限幅：正文截断 MAX_TEXT_CHARS；仅 http/https。
已知残留（单用户本地平台可接受，生产应加出口代理/pin-resolver）：校验用 getaddrinfo 与
httpx 连接时的再解析之间存在 DNS-rebinding 窗口（需攻击者控制低 TTL DNS）。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import httpx
from langchain_core.tools import BaseTool, StructuredTool

MAX_TEXT_CHARS = 20_000
_TIMEOUT_S = 20.0
_MAX_BYTES = 2 * 1024 * 1024  # 2MB 响应上限

_MAX_REDIRECTS = 4
_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "iframe", "template"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "pre", "blockquote"}
# 行内元素之间补空格，避免 "<b>foo</b><b>bar</b>" 粘成 "foobar"（丢词边界）。
_INLINE_SEP_TAGS = {"a", "b", "i", "em", "strong", "span", "code", "td", "th", "label", "small"}


class _TextExtractor(HTMLParser):
    """标准库 html.parser 的轻量正文抽取（免第三方依赖；不求完美，够研究场景）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        elif tag in _INLINE_SEP_TAGS:
            self._chunks.append(" ")  # 词边界

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):  # noqa: ANN001
        if self._in_title and not self.title:
            self.title = data.strip()
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", raw)).strip()


def _decode_body(body: bytes, content_type: str, http_charset: Optional[str]) -> str:
    """按优先级选编码解码：HTTP 头 charset > HTML <meta charset> > utf-8（纯函数，可测）。"""
    enc = http_charset
    if not enc:
        m = re.search(rb'charset=["\']?([\w-]+)', body[:2048], re.IGNORECASE)
        if m:
            enc = m.group(1).decode("ascii", errors="ignore")
    try:
        return body.decode(enc or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return body.decode("utf-8", errors="replace")


def html_to_text(html: str) -> tuple[str, str]:
    """HTML → (title, 可读文本)（纯函数，可测）。"""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 — 残破 HTML 尽力而为
        pass
    return p.title, p.text()


def is_private_host(host: str) -> bool:
    """host（域名或 IP 字面量）是否解析到私网/环回/链路本地/元数据地址（纯逻辑+DNS）。

    任一解析结果命中即拒绝——DNS 多记录里藏一个内网地址是经典 SSRF 绕过。
    """
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # 解析失败按拒绝（fail-closed）
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str.split("%")[0])
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def validate_fetch_url(url: str) -> Optional[str]:
    """URL 是否允许抓取；返回 None=允许，否则返回拒绝原因（纯函数+DNS）。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "URL cannot be parsed"
    if parsed.scheme not in ("http", "https"):
        return "Only http/https supported"
    if not parsed.hostname:
        return "URL has no hostname"
    if is_private_host(parsed.hostname):
        return "Target address unreachable (private/loopback/reserved addresses blocked)"
    return None


# GitHub token 只对这两个主机注入（**精确匹配**，非后缀匹配——api.github.com.evil.com
# 之类的伪装域拿不到 token；github.com 网页版无需认证也不给，最小暴露面）。
_GITHUB_AUTH_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})


def auth_headers_for(host: str, token: Optional[str]) -> dict[str, str]:
    """按主机名给出请求头（纯函数，可测）：命中 GitHub API/Raw 才带 Bearer token。

    调用方在重定向循环里**逐跳重算**——302 跳出 GitHub 后 token 绝不跟随第三方。
    """
    if token and host in _GITHUB_AUTH_HOSTS:
        return {"Authorization": f"Bearer {token}"}
    return {}


def build_web_fetch_tool(settings=None, *, transport=None) -> BaseTool:  # noqa: ANN001
    """构造 web_fetch 工具。

    settings 可选（加性默认，旧调用零改）：提供 github_token 时对 api.github.com /
    raw.githubusercontent.com 注入认证（限流 60→5000 次/小时）。transport 为测试
    接缝（httpx.MockTransport 离线验证请求头/逐跳行为）。
    """
    github_token = getattr(settings, "github_token", None) if settings is not None else None

    async def web_fetch(url: str) -> str:
        """抓取一个公开网页并返回其标题与正文文本（截断至 2 万字符）。

        用于查资料/读文档/GitHub 页面等；不能访问内网地址；只支持 http/https。
        """
        # 关键：**禁 httpx 自动重定向**，手动逐跳——每一跳在发请求前都过 SSRF 校验，
        # 否则 302→内网地址的请求会先打出去（blind SSRF）再校验落点，为时已晚。
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_S, follow_redirects=False,
                headers={"User-Agent": "agent-go/1.0 (+research)"},
                transport=transport,
            ) as client:
                cur = url
                resp = None
                for _ in range(_MAX_REDIRECTS + 1):
                    reason = validate_fetch_url(cur)  # 发请求前校验本跳目标
                    if reason:
                        return f"Fetch rejected: {reason} ({cur})"
                    # 认证头按**当前跳**主机名逐跳重算，且只随本次请求发送（绝不设到
                    # client 级）——重定向落到非 GitHub 主机时 token 不跟随；token 也
                    # 绝不出现在观测文本/日志里。
                    hop_headers = auth_headers_for(urlparse(cur).hostname or "", github_token)
                    async with client.stream("GET", cur, headers=hop_headers) as r:
                        if r.is_redirect:
                            loc = r.headers.get("location", "")
                            cur = str(r.next_request.url) if r.next_request else loc
                            continue
                        if r.status_code >= 400:
                            return f"Fetch failed: HTTP {r.status_code} ({cur})"
                        body = b""
                        async for chunk in r.aiter_bytes():
                            body += chunk
                            if len(body) > _MAX_BYTES:
                                break
                        resp = r
                        break
                else:
                    return "Fetch failed: too many redirects"
                if resp is None:
                    return "Fetch failed: no response obtained"
        except httpx.HTTPError as exc:
            return f"Fetch failed: {exc}"

        ctype = resp.headers.get("content-type", "")
        text = _decode_body(body, resp.headers.get("content-type", ""), resp.charset_encoding)
        if "html" in ctype or text.lstrip()[:1] == "<":
            title, extracted = html_to_text(text)
            head = f"Title: {title}\n" if title else ""
            out = head + extracted
        else:
            out = text  # json/纯文本等原样
        if len(out) > MAX_TEXT_CHARS:
            out = out[:MAX_TEXT_CHARS] + "\n…(truncated)"
        return out or "(Page has no readable text)"

    tool = StructuredTool.from_function(
        coroutine=web_fetch,
        name="web_fetch",
        description="Fetch a public web page and return its title and body text (look up info, read online docs, GitHub pages). Cannot access internal networks.",
    )
    tool.metadata = {"provider": "local"}
    return tool
