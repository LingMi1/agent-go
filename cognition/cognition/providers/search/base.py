"""联网搜索 provider 协议（结构化 duck-typing，与 image 的 provider 协议同风格）。"""

from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


class SearchResult(TypedDict):
    """单条搜索结果（工具层照此渲染观测列表与末行 sentinel JSON）。"""

    title: str
    url: str
    snippet: str


class SearchError(Exception):
    """搜索失败，携带面向用户的中文原因。

    工具层 fail-soft：捕获后转为观测文本（"搜索失败：…"）让模型自行改道
    （web_fetch 直抓已知 URL / 基于已有知识作答），绝不向图层上抛中断编排。
    """


@runtime_checkable
class SearchProvider(Protocol):
    """联网搜索统一入口。

    name：provider 标识（观测文本与 sentinel JSON 里如实标注来源引擎）。
    返回按相关性排序的结果列表（长度 ≤ max_results）；失败抛 SearchError。
    """

    name: str

    async def search(self, query: str, *, max_results: int = 6) -> list[SearchResult]: ...
