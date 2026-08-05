"""MCP server 配置解析（纯逻辑，零外部依赖）。

把 `Settings.mcp_servers`（`{name: {...}}` 原始字典）解析成 `McpServerConfig` 列表：
- 三传输 stdio / sse / streamable_http，别名归一（http / streamable-http / streamablehttp → streamable_http）。
- 校验必填项：stdio 需 command；sse/streamable_http 需 base_uri（或 url 别名）。
- **超时单位统一为秒**（修正原项目分钟/秒混用的 bug）：接受 *_seconds / *_s（秒）与 *_minutes（×60）。
- enabled=false 的 server 被过滤掉（不出现在结果里）。
- endpoint 默认：sse→"/sse"，streamable_http→"/mcp"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

TRANSPORTS = ("stdio", "sse", "streamable_http")

_TRANSPORT_ALIASES = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable_http",
    "streamable_http": "streamable_http",
    "streamable-http": "streamable_http",
    "streamablehttp": "streamable_http",
}

_DEFAULT_ENDPOINT = {"sse": "/sse", "streamable_http": "/mcp"}
_DEFAULT_TIMEOUT_S = 30.0


class McpConfigError(ValueError):
    """MCP 配置非法。"""


@dataclass(frozen=True)
class McpServerConfig:
    """单个 MCP server 的归一化配置。"""

    name: str
    transport: str  # stdio | sse | streamable_http
    # —— stdio ——
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    # —— sse / streamable_http ——
    base_uri: str | None = None
    endpoint: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    resumable: bool = False
    # —— 通用 ——
    open_on_startup: bool = True
    request_timeout_s: float = _DEFAULT_TIMEOUT_S
    enabled: bool = True


def _normalize_transport(raw: str, *, name: str) -> str:
    key = str(raw or "").strip().lower()
    if key not in _TRANSPORT_ALIASES:
        raise McpConfigError(
            f"MCP server {name!r} 的 transport={raw!r} 非法，支持 {TRANSPORTS}"
        )
    return _TRANSPORT_ALIASES[key]


def _resolve_timeout_s(spec: Mapping[str, object], *, name: str) -> float:
    """把各种超时写法统一成秒。分钟优先级低于显式秒；均缺省则用默认。"""
    for key in ("request_timeout_seconds", "request_timeout_s", "timeout_seconds", "timeout_s", "timeout"):
        if key in spec and spec[key] is not None:
            return _positive_float(spec[key], key=key, name=name)
    for key in ("request_timeout_minutes", "timeout_minutes"):
        if key in spec and spec[key] is not None:
            return _positive_float(spec[key], key=key, name=name) * 60.0
    return _DEFAULT_TIMEOUT_S


def _positive_float(value: object, *, key: str, name: str) -> float:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise McpConfigError(f"MCP server {name!r} 的 {key}={value!r} 不是数字") from exc
    if f <= 0:
        raise McpConfigError(f"MCP server {name!r} 的 {key} 必须为正数")
    return f


def parse_server(name: str, spec: Mapping[str, object]) -> McpServerConfig:
    """解析单个 server 配置（不做 enabled 过滤，供细粒度测试）。"""
    if not isinstance(spec, Mapping):
        raise McpConfigError(f"MCP server {name!r} 配置必须是对象")
    transport = _normalize_transport(str(spec.get("transport", "")), name=name)
    enabled = bool(spec.get("enabled", True))
    open_on_startup = bool(spec.get("open_on_startup", True))
    timeout_s = _resolve_timeout_s(spec, name=name)

    if transport == "stdio":
        command = spec.get("command")
        if not command:
            raise McpConfigError(f"MCP server {name!r}（stdio）缺少 command")
        args = tuple(str(a) for a in (spec.get("args") or ()))
        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        return McpServerConfig(
            name=name,
            transport=transport,
            command=str(command),
            args=args,
            env=env,
            open_on_startup=open_on_startup,
            request_timeout_s=timeout_s,
            enabled=enabled,
        )

    # sse / streamable_http
    base_uri = spec.get("base_uri") or spec.get("url")
    if not base_uri:
        raise McpConfigError(f"MCP server {name!r}（{transport}）缺少 base_uri/url")
    endpoint = spec.get("endpoint") or _DEFAULT_ENDPOINT[transport]
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    return McpServerConfig(
        name=name,
        transport=transport,
        base_uri=str(base_uri),
        endpoint=str(endpoint),
        headers=headers,
        resumable=bool(spec.get("resumable", transport == "streamable_http")),
        open_on_startup=open_on_startup,
        request_timeout_s=timeout_s,
        enabled=enabled,
    )


def parse_servers(raw: Mapping[str, object]) -> list[McpServerConfig]:
    """解析全部 server 并过滤掉 enabled=false 的；顺序稳定（按输入 key 顺序）。"""
    if not raw:
        return []
    out: list[McpServerConfig] = []
    for name, spec in raw.items():
        cfg = parse_server(str(name), spec)  # type: ignore[arg-type]
        if cfg.enabled:
            out.append(cfg)
    return out
