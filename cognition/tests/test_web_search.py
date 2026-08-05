"""web_search 工具与搜索 provider：观测格式/sentinel、fake 确定性、DDG 解析、
Tavily 契约（MockTransport 离线）、工厂解析与装配门控。"""

from __future__ import annotations

import json

import httpx
import pytest

from cognition.config import Settings
from cognition.providers.search import ChainSearchProvider, build_search_provider
from cognition.providers.search.baidu import BaiduProvider, parse_baidu_html
from cognition.providers.search.base import SearchError, SearchResult
from cognition.providers.search.bing import BingProvider, parse_bing_html, parse_bing_rss, resolve_bing_url
from cognition.providers.search.ddg import DdgProvider, parse_ddg_html, parse_ddg_lite
from cognition.providers.search.fake import FakeSearchProvider
from cognition.providers.search.tavily import TavilyProvider
from cognition.tools.registry import build_tool_suite
from cognition.tools.web_search import (
    WEB_SEARCH_JSON_PREFIX,
    build_web_search_tool,
    format_observation,
)

# —— format_observation 纯逻辑 ——

# 前端 vitest fixture 会镜像此观测串——改这里必须同步前端。
_FIXTURE_RESULTS: list[SearchResult] = [
    {
        "title": "LangGraph Checkpointing 官方文档",
        "url": "https://langchain-ai.github.io/langgraph/concepts/persistence/",
        "snippet": "LangGraph 通过 checkpointer 在每个 super-step 持久化图状态，支持恢复、时间旅行与人工介入。",
    },
    {
        "title": "Checkpointer 源码解析",
        "url": "https://example.com/langgraph-checkpoint",
        "snippet": "PostgresSaver 将 channel 值序列化后写入 checkpoints 表。",
    },
]

_FIXTURE_EXPECTED = (
    "搜索「LangGraph 检查点机制」共 2 条结果（tavily）：\n"
    "1. LangGraph Checkpointing 官方文档\n"
    "   https://langchain-ai.github.io/langgraph/concepts/persistence/\n"
    "   LangGraph 通过 checkpointer 在每个 super-step 持久化图状态，支持恢复、时间旅行与人工介入。\n"
    "2. Checkpointer 源码解析\n"
    "   https://example.com/langgraph-checkpoint\n"
    "   PostgresSaver 将 channel 值序列化后写入 checkpoints 表。\n"
    "\n"
    'WEB_SEARCH_RESULTS_JSON:{"query":"LangGraph 检查点机制","provider":"tavily","results":'
    '[{"title":"LangGraph Checkpointing 官方文档","url":"https://langchain-ai.github.io/langgraph/concepts/persistence/",'
    '"snippet":"LangGraph 通过 checkpointer 在每个 super-step 持久化图状态，支持恢复、时间旅行与人工介入。"},'
    '{"title":"Checkpointer 源码解析","url":"https://example.com/langgraph-checkpoint",'
    '"snippet":"PostgresSaver 将 channel 值序列化后写入 checkpoints 表。"}]}'
)


def test_format_observation_exact_fixture():
    """锁死观测串逐字形态（前端 vitest fixture 镜像用）。"""
    assert format_observation("LangGraph 检查点机制", "tavily", _FIXTURE_RESULTS) == _FIXTURE_EXPECTED


def test_format_observation_sentinel_last_line_single_line_roundtrip():
    obs = format_observation("LangGraph 检查点机制", "tavily", _FIXTURE_RESULTS)
    lines = obs.split("\n")
    # sentinel 是最后一行、单行、且前有空行分隔。
    assert lines[-1].startswith(WEB_SEARCH_JSON_PREFIX)
    assert lines[-2] == ""
    assert WEB_SEARCH_JSON_PREFIX not in "\n".join(lines[:-1])
    # JSON 可 round-trip 且 ensure_ascii=False（中文原样，不出现 \uXXXX 转义）。
    payload = json.loads(lines[-1][len(WEB_SEARCH_JSON_PREFIX):])
    assert payload["query"] == "LangGraph 检查点机制"
    assert payload["provider"] == "tavily"
    assert payload["results"] == [dict(r) for r in _FIXTURE_RESULTS]
    assert "检查点" in lines[-1] and "\\u" not in lines[-1]


def test_format_observation_numbered_list_and_truncation():
    long_snippet = "长" * 300
    obs = format_observation("q", "ddg", [{"title": "T", "url": "https://a.example/", "snippet": long_snippet}])
    body = obs.split("\n\n")[0]
    assert body.splitlines()[1] == "1. T"
    # 列表里的 snippet 截 200；sentinel 保留 provider 原文。
    assert body.splitlines()[3] == "   " + "长" * 200
    payload = json.loads(obs.splitlines()[-1][len(WEB_SEARCH_JSON_PREFIX):])
    assert payload["results"][0]["snippet"] == long_snippet
    # snippet 内换行被归一为空格，不破编号列表排版；sentinel 仍是单行（json 转义）。
    obs2 = format_observation("q", "ddg", [{"title": "T", "url": "u", "snippet": "第一行\n第二行"}])
    assert "   第一行 第二行" in obs2
    assert obs2.splitlines()[-1].startswith(WEB_SEARCH_JSON_PREFIX)


def test_format_observation_empty_results():
    obs = format_observation("冷门问题", "ddg", [])
    assert "未找到结果" in obs and "web_fetch" in obs
    last = obs.splitlines()[-1]
    assert last.startswith(WEB_SEARCH_JSON_PREFIX)
    assert json.loads(last[len(WEB_SEARCH_JSON_PREFIX):])["results"] == []


# —— fake provider 确定性 ——

async def test_fake_provider_deterministic():
    p = FakeSearchProvider()
    a = await p.search("深度学习")
    b = await p.search("深度学习")
    assert a == b and len(a) == 3
    assert a[0]["title"] == "关于「深度学习」的资料 1"
    assert (await p.search("另一个主题")) != a
    assert len(await p.search("深度学习", max_results=2)) == 2


# —— 工具层：tool_call 式 ainvoke + fail-soft + 截幅 ——

async def test_web_search_tool_ainvoke_returns_list_and_sentinel():
    tool = build_web_search_tool(FakeSearchProvider(), Settings())
    msg = await tool.ainvoke(
        {"args": {"query": "向量数据库选型"}, "id": "ws1", "name": "web_search", "type": "tool_call"},
        config={"metadata": {"request_id": "run-ws"}},
    )
    assert "搜索「向量数据库选型」共 3 条结果（fake）：" in msg.content
    assert "1. 关于「向量数据库选型」的资料 1" in msg.content
    payload = json.loads(msg.content.splitlines()[-1][len(WEB_SEARCH_JSON_PREFIX):])
    assert payload["provider"] == "fake" and len(payload["results"]) == 3
    assert {"title", "url", "snippet"} == set(payload["results"][0])


class _BoomProvider:
    name = "boom"

    async def search(self, query: str, *, max_results: int = 6):
        raise SearchError("DDG 暂时限流/拦截，请稍后再试或配置 Tavily key")


async def test_web_search_tool_fail_soft_no_sentinel():
    tool = build_web_search_tool(_BoomProvider(), Settings())
    msg = await tool.ainvoke(
        {"args": {"query": "x"}, "id": "ws2", "name": "web_search", "type": "tool_call"}
    )
    assert msg.content == (
        "搜索失败：DDG 暂时限流/拦截，请稍后再试或配置 Tavily key。"
        "可改用 web_fetch 直接抓取已知 URL，或基于已有知识作答。"
    )
    assert WEB_SEARCH_JSON_PREFIX not in msg.content


class _RecordingProvider:
    name = "rec"

    def __init__(self) -> None:
        self.seen: list[int] = []

    async def search(self, query: str, *, max_results: int = 6):
        self.seen.append(max_results)
        return []


async def test_web_search_tool_clamps_max_results():
    rec = _RecordingProvider()
    tool = build_web_search_tool(rec, Settings())
    await tool.ainvoke({"args": {"query": "q", "max_results": 99}, "id": "a", "name": "web_search", "type": "tool_call"})
    await tool.ainvoke({"args": {"query": "q", "max_results": 0}, "id": "b", "name": "web_search", "type": "tool_call"})
    await tool.ainvoke({"args": {"query": "q"}, "id": "c", "name": "web_search", "type": "tool_call"})
    assert rec.seen == [10, 1, 6]  # 上钳 10 / 下钳 1 / 默认 settings.search_max_results


# —— DDG 解析纯函数（离线 fixture）——

_DDG_HTML_FIXTURE = """
<html><body><div id="links" class="results">
  <div class="result results_links results_links_deep web-result">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fzh.wikipedia.org%2Fwiki%2F%E6%B7%B1%E5%BA%A6%E5%AD%A6%E4%B9%A0&amp;rut=abc123">深度学习 - 维基百科</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">深度学习是<b>机器学习</b>的分支。</a>
  </div>
  <div class="result">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="https://www.deeplearningbook.org/">Deep Learning Book</a>
    </h2>
    <div class="result__snippet">An MIT Press book by <b>Ian Goodfellow</b>.</div>
  </div>
</div></body></html>
"""


def test_parse_ddg_html_two_results_with_redirect_decode():
    results = parse_ddg_html(_DDG_HTML_FIXTURE)
    assert len(results) == 2
    # uddg 重定向包装解开 + 百分号编码的中文路径还原。
    assert results[0]["title"] == "深度学习 - 维基百科"
    assert results[0]["url"] == "https://zh.wikipedia.org/wiki/深度学习"
    assert results[0]["snippet"] == "深度学习是机器学习的分支。"
    assert results[1]["title"] == "Deep Learning Book"
    assert results[1]["url"] == "https://www.deeplearningbook.org/"
    assert results[1]["snippet"] == "An MIT Press book by Ian Goodfellow."


_DDG_LITE_FIXTURE = """
<html><body><table>
  <tr><td>1.&nbsp;</td><td><a rel="nofollow" href="https://pytorch.org/" class="result-link">PyTorch 官网</a></td></tr>
  <tr><td>&nbsp;</td><td class="result-snippet">开源深度学习框架。</td></tr>
  <tr><td>2.&nbsp;</td><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tensorflow.org%2F&amp;rut=x">TensorFlow</a></td></tr>
  <tr><td>&nbsp;</td><td class="result-snippet">Google 的机器学习平台。</td></tr>
</table></body></html>
"""


def test_parse_ddg_lite_rows():
    results = parse_ddg_lite(_DDG_LITE_FIXTURE)
    assert results == [
        {"title": "PyTorch 官网", "url": "https://pytorch.org/", "snippet": "开源深度学习框架。"},
        {"title": "TensorFlow", "url": "https://www.tensorflow.org/", "snippet": "Google 的机器学习平台。"},
    ]


def test_parse_ddg_challenge_and_empty_yield_no_results():
    challenge = '<html><body><div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div></body></html>'
    assert parse_ddg_html(challenge) == []
    assert parse_ddg_lite(challenge) == []
    assert parse_ddg_html("") == []
    assert parse_ddg_lite("") == []


# —— DDG provider：html→lite 降级与限流报错（MockTransport 离线）——

async def test_ddg_provider_falls_back_to_lite_then_errors():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(202, text="challenge")  # 限流/人机验证
        return httpx.Response(200, text=_DDG_LITE_FIXTURE)

    p = DdgProvider(timeout=5.0, transport=httpx.MockTransport(handler))
    results = await p.search("深度学习", max_results=1)
    assert calls == ["html.duckduckgo.com", "lite.duckduckgo.com"]
    assert len(results) == 1 and results[0]["title"] == "PyTorch 官网"

    blocked = DdgProvider(timeout=5.0, transport=httpx.MockTransport(lambda r: httpx.Response(202, text="x")))
    with pytest.raises(SearchError, match="限流"):
        await blocked.search("q")


# —— Tavily 契约（MockTransport 离线锁请求形状）——

async def test_tavily_contract():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"title": "T1", "url": "https://a.example/", "content": "C1 " + "x" * 400, "score": 0.9},
        ]})

    p = TavilyProvider(api_key="tvly-test", timeout=5.0, transport=httpx.MockTransport(handler))
    results = await p.search("量子计算", max_results=4)
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["auth"] == "Bearer tvly-test"
    assert seen["body"] == {"query": "量子计算", "max_results": 4}  # 字段集锁死，不多不少
    assert results[0]["title"] == "T1" and results[0]["url"] == "https://a.example/"
    assert results[0]["snippet"] == ("C1 " + "x" * 400)[:300]  # content→snippet 且截 300


async def test_tavily_non_200_and_malformed_raise():
    p500 = TavilyProvider("k", timeout=5.0, transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(SearchError, match="HTTP 500"):
        await p500.search("q")
    bad = TavilyProvider("k", timeout=5.0, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"foo": 1})))
    with pytest.raises(SearchError, match="results"):
        await bad.search("q")


# —— 工厂解析与装配门控 ——

def test_factory_resolution(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("COGNITION_TAVILY_API_KEY", raising=False)
    # auto = 降级链：无 key → [bing, baidu, ddg]；有 key → tavily 头部入链
    chain = build_search_provider(Settings())
    assert isinstance(chain, ChainSearchProvider)
    assert [type(p) for p in chain._providers] == [BingProvider, BaiduProvider, DdgProvider]
    chain_keyed = build_search_provider(Settings(tavily_api_key="tvly-x"))
    assert isinstance(chain_keyed, ChainSearchProvider)
    assert [type(p) for p in chain_keyed._providers] == [TavilyProvider, BingProvider, BaiduProvider, DdgProvider]
    assert isinstance(build_search_provider(Settings(search_provider="ddg", tavily_api_key="tvly-x")), DdgProvider)
    assert isinstance(build_search_provider(Settings(search_provider="tavily", tavily_api_key="k")), TavilyProvider)
    assert isinstance(build_search_provider(Settings(search_provider="bing")), BingProvider)
    assert isinstance(build_search_provider(Settings(search_provider="baidu")), BaiduProvider)
    assert isinstance(build_search_provider(Settings(search_provider="fake")), FakeSearchProvider)
    with pytest.raises(ValueError, match="未知搜索 provider"):
        build_search_provider(Settings(search_provider="google"))


# —— Bing 免 key provider ——


def _b64u(url: str) -> str:
    import base64

    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def test_resolve_bing_url_matrix():
    direct = "https://example.com/page"
    assert resolve_bing_url(direct) == direct
    wrapped = f"https://www.bing.com/ck/a?!&&p=xx&u=a1{_b64u('https://example.com/文章')}&ntb=1"
    assert resolve_bing_url(wrapped) == "https://example.com/文章"
    assert resolve_bing_url("https://www.bing.com/ck/a?u=a1%%%bad") == ""  # 坏 base64 → 丢弃
    assert resolve_bing_url(f"https://www.bing.com/ck/a?u=a1{_b64u('javascript:alert(1)')}") == ""  # 非 http(s)
    assert resolve_bing_url("") == ""


_BING_HTML = f"""
<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&u=a1{_b64u("https://zhuanlan.zhihu.com/p/1")}&ntb=1">DeepSeek 新模型<b>解读</b></a></h2>
  <div class="b_caption"><p>2026 年 DeepSeek 发布的新一代模型，<b>推理能力</b>大幅提升。</p></div></li>
<li class="b_algo"><h2><a href="https://direct.example.com/a">直接链接结果</a></h2>
  <p class="b_lineclamp2">无跳转包装的摘要。</p></li>
</ol></body></html>
"""


def test_parse_bing_html_fixture():
    results = parse_bing_html(_BING_HTML)
    assert results == [
        SearchResult(title="DeepSeek 新模型解读", url="https://zhuanlan.zhihu.com/p/1", snippet="2026 年 DeepSeek 发布的新一代模型，推理能力大幅提升。"),
        SearchResult(title="直接链接结果", url="https://direct.example.com/a", snippet="无跳转包装的摘要。"),
    ]
    assert parse_bing_html("<html><body>验证</body></html>") == []


_BING_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>q - Bing</title>
<item><title>DeepSeek 新模型&lt;b&gt;解读&lt;/b&gt;</title><link>https://zhuanlan.zhihu.com/p/1</link>
  <description>2026 年 DeepSeek 发布的&lt;b&gt;新一代&lt;/b&gt;模型。</description></item>
<item><title>次条</title><link>https://example.com/2</link><description>摘要二</description></item>
<item><title>坏链接</title><link>javascript:x</link><description>丢弃</description></item>
</channel></rss>"""


def test_parse_bing_rss_fixture():
    results = parse_bing_rss(_BING_RSS)
    assert results == [
        SearchResult(title="DeepSeek 新模型解读", url="https://zhuanlan.zhihu.com/p/1", snippet="2026 年 DeepSeek 发布的新一代模型。"),
        SearchResult(title="次条", url="https://example.com/2", snippet="摘要二"),
    ]  # 非 http(s) 链接丢弃
    assert parse_bing_rss("<html>不是 XML 的壳页</html>") == []


async def test_bing_provider_rss_first_then_html_fallback():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.bing.com"
        assert request.url.params["q"] == "测试"
        if request.url.params.get("format") == "rss":
            calls.append("rss")
            return httpx.Response(200, text=_BING_RSS)
        calls.append("html")
        return httpx.Response(200, text=_BING_HTML)

    provider = BingProvider(transport=httpx.MockTransport(handler))
    results = await provider.search("测试", max_results=1)
    assert calls == ["rss"] and results[0]["url"] == "https://zhuanlan.zhihu.com/p/1"

    def rss_dead(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("format") == "rss":
            return httpx.Response(200, text="<html>JS 壳页</html>")
        return httpx.Response(200, text=_BING_HTML)

    fallback = BingProvider(transport=httpx.MockTransport(rss_dead))
    results = await fallback.search("测试", max_results=2)
    assert [r["url"] for r in results] == ["https://zhuanlan.zhihu.com/p/1", "https://direct.example.com/a"]

    blocked = BingProvider(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>验证</html>")))
    with pytest.raises(SearchError):
        await blocked.search("测试")


# —— 百度免 key provider ——

_BAIDU_HTML = """
<html><body><div id="content_left">
<div class="result c-container new-pmd" mu="https://news.example.cn/deepseek">
  <h3 class="c-title"><a href="https://www.baidu.com/link?url=xxx">DeepSeek 发布<em>新模型</em></a></h3>
  <span class="content-right_2s-H4">2026 年 DeepSeek 新一代模型，推理与工具调用大幅增强。</span></div>
<div class="result c-container" mu="https://tech.example.cn/qwen">
  <h3><a href="https://www.baidu.com/link?url=yyy">通义千问更新</a></h3>
  <div class="c-abstract">阿里发布通义千问新版本。</div></div>
<div class="result c-container"><h3><a href="https://www.baidu.com/link?url=ad">无 mu 广告位</a></h3></div>
</div></body></html>
"""


def test_parse_baidu_html_fixture():
    results = parse_baidu_html(_BAIDU_HTML)
    assert results == [
        SearchResult(title="DeepSeek 发布新模型", url="https://news.example.cn/deepseek", snippet="2026 年 DeepSeek 新一代模型，推理与工具调用大幅增强。"),
        SearchResult(title="通义千问更新", url="https://tech.example.cn/qwen", snippet="阿里发布通义千问新版本。"),
    ]  # 无 mu 条目（广告位）被丢弃
    assert parse_baidu_html("<html><body>百度安全验证</body></html>") == []


async def test_baidu_provider_transport_and_blocked():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.baidu.com"
        assert request.url.params["wd"] == "测试"
        return httpx.Response(200, text=_BAIDU_HTML)

    provider = BaiduProvider(transport=httpx.MockTransport(ok))
    results = await provider.search("测试", max_results=1)
    assert len(results) == 1 and results[0]["url"] == "https://news.example.cn/deepseek"

    blocked = BaiduProvider(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<p>百度安全验证</p>")))
    with pytest.raises(SearchError):
        await blocked.search("测试")


# —— 降级链 ——


class _FailingProvider:
    name = "failer"

    async def search(self, query, *, max_results=6):  # noqa: ANN001
        raise SearchError("挂了")


async def test_chain_provider_falls_through_and_updates_name():
    chain = ChainSearchProvider([_FailingProvider(), FakeSearchProvider()])
    results = await chain.search("链路", max_results=3)
    assert results and chain.name == "fake"  # 胜出者名字用于观察串标注

    all_fail = ChainSearchProvider([_FailingProvider(), _FailingProvider()])
    with pytest.raises(SearchError, match="挂了"):
        await all_fail.search("x")


async def test_suite_gating_web_search():
    base = dict(mcp_enabled=False, skills_dirs=[], minio_upload_enabled=False)
    tools, _, _ = await build_tool_suite(Settings(**base, search_provider=""))
    assert "web_search" not in {t.name for t in tools}  # 空串=关闭
    tools, provider_map, _ = await build_tool_suite(Settings(**base))
    assert "web_search" in {t.name for t in tools}  # 默认 auto=开（ddg 免 key）
    assert provider_map["web_search"] == "local"
