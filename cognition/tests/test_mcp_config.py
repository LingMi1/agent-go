"""MCP 配置解析：三传输 / 单位归一 / 校验 / enabled 过滤（纯逻辑，不触网）。"""

from __future__ import annotations

import pytest

from cognition.mcp.config import McpConfigError, parse_server, parse_servers


def test_stdio_parse():
    cfg = parse_server(
        "fs",
        {"transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"], "env": {"K": "V"}},
    )
    assert cfg.transport == "stdio"
    assert cfg.command == "uvx"
    assert cfg.args == ("mcp-server-fetch",)
    assert cfg.env == {"K": "V"}
    assert cfg.request_timeout_s == 30.0  # 默认秒


def test_sse_parse_defaults():
    cfg = parse_server("web", {"transport": "sse", "url": "https://h.example"})
    assert cfg.transport == "sse"
    assert cfg.base_uri == "https://h.example"
    assert cfg.endpoint == "/sse"  # sse 默认端点


def test_streamable_http_alias_and_default_endpoint():
    for alias in ("http", "streamable-http", "streamablehttp", "streamable_http"):
        cfg = parse_server("x", {"transport": alias, "base_uri": "https://h"})
        assert cfg.transport == "streamable_http"
        assert cfg.endpoint == "/mcp"
        assert cfg.resumable is True  # streamable_http 默认可恢复


def test_timeout_unit_normalized_to_seconds():
    # 显式秒
    assert parse_server("a", {"transport": "stdio", "command": "c", "request_timeout_s": 5}).request_timeout_s == 5.0
    # 分钟 → ×60
    assert parse_server("b", {"transport": "stdio", "command": "c", "request_timeout_minutes": 2}).request_timeout_s == 120.0


def test_stdio_missing_command_raises():
    with pytest.raises(McpConfigError):
        parse_server("bad", {"transport": "stdio"})


def test_http_missing_base_uri_raises():
    with pytest.raises(McpConfigError):
        parse_server("bad", {"transport": "sse"})


def test_unknown_transport_raises():
    with pytest.raises(McpConfigError):
        parse_server("bad", {"transport": "grpc", "command": "c"})


def test_parse_servers_filters_disabled_and_keeps_order():
    raw = {
        "a": {"transport": "stdio", "command": "c"},
        "b": {"transport": "stdio", "command": "c", "enabled": False},
        "c": {"transport": "sse", "url": "https://h"},
    }
    got = parse_servers(raw)
    assert [c.name for c in got] == ["a", "c"]  # b 被过滤，顺序稳定


def test_parse_servers_empty():
    assert parse_servers({}) == []
