"""write_report 工具：生成 Markdown 报告并（惰性、可降级地）上传 MinIO，登记 artifact。

工具返回 `(summary_str, artifact_dict)`（`response_format="content_and_artifact"`）：
- summary_str 进入 ToolMessage.content（observation 文本）。
- artifact_dict 进入 ToolMessage.artifact，由 EventMapper 转成 tool_result 的 ArtifactRef。

ArtifactRef 形状沿用原项目 8 字段：
- resource_key = f"{run_id}/{tool_call_id}/{file_name}"（run+tool_call 归属，跨工具可复用）。
- download_url / preview_url = f"/artifacts/{resource_key}"（**Go 代理**端点：Go 持鉴权、
  resourceKey 内嵌 runId → 校验 owner → 流式回传；比 presigned 更稳更安全）。
- missing = False（对象存在性由 Go 代理在取用时判定）。

run_id 通过 RunnableConfig.metadata.request_id 注入（executor 分支会带上）；tool_call_id 通过
LangChain 的 InjectedToolCallId 注入。MinIO 上传是**惰性 import + 可降级**：仅当
`settings.minio_upload_enabled` 为真才尝试，且任何异常都被吞掉（best-effort），因此单测无需
MinIO 也能跑（默认关闭）。
"""

from __future__ import annotations

import io
import logging
import re
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from pydantic import BaseModel, Field

from cognition.config import Settings, get_settings

logger = logging.getLogger(__name__)

_MIME_MARKDOWN = "text/markdown; charset=utf-8"


class ReportArgs(BaseModel):
    """write_report 的入参 schema。

    注意：tool_call_id 必须以 InjectedToolCallId 注解**出现在本 schema 里**——ToolNode
    按 args_schema（而非函数签名）判定可注入参数；只写在函数签名上不会被注入，会在
    运行期炸 TypeError（线上踩坑：fake ReAct 只调 calculator，该路径直到真实模型调
    write_report 才暴露）。LLM 可见 schema（tool_call_schema）会自动剔除注入字段。
    """

    title: str = Field(description="报告标题。")
    content: str = Field(description="报告正文（纯文本/Markdown）。")
    tool_call_id: Annotated[str, InjectedToolCallId]


def _slugify(title: str) -> str:
    """把标题转成安全文件名（保留中英文与数字，其余转 -）。"""
    base = re.sub(r"[^\w一-鿿]+", "-", (title or "report").strip()).strip("-")
    return (base or "report")[:64]


def _upload_object(
    settings: Settings, resource_key: str, data: bytes, content_type: str
) -> None:
    """上传到 MinIO（惰性 import）。确保 bucket 存在后 put_object。失败由调用方降级处理。"""
    from minio import Minio  # 惰性：单测/无 MinIO 环境不需要该依赖

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)
    client.put_object(
        settings.minio_bucket,
        resource_key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def _maybe_upload(settings: Settings, resource_key: str, data: bytes, content_type: str) -> None:
    """惰性 + 可降级上传：仅在开启时尝试，任何异常都不影响工具返回。"""
    if not settings.minio_upload_enabled:
        return
    try:
        _upload_object(settings, resource_key, data, content_type)
    except Exception as exc:  # noqa: BLE001 — 上传是 best-effort，失败不阻断
        logger.warning("write_report: MinIO upload failed for %s: %s", resource_key, exc)


def _run_id_from_config(config: Optional[RunnableConfig]) -> str:
    """从 RunnableConfig 取 run_id（request_id）：优先 metadata，其次 configurable。"""
    if not config:
        return "run"
    meta = config.get("metadata") or {}
    rid = meta.get("request_id") or meta.get("run_id")
    if rid:
        return str(rid)
    conf = config.get("configurable") or {}
    return str(conf.get("request_id") or conf.get("run_id") or "run")


def build_report_artifact(
    *,
    title: str,
    content: str,
    run_id: str,
    tool_call_id: str,
    settings: Optional[Settings] = None,
) -> tuple[str, dict[str, Any]]:
    """构造报告正文 + ArtifactRef（并按需上传）。抽出便于单测，不依赖 LangChain 注入。"""
    settings = settings or get_settings()
    file_name = f"{_slugify(title)}.md"
    body = f"# {title}\n\n{content}\n".encode("utf-8")
    resource_key = f"{run_id}/{tool_call_id}/{file_name}"

    _maybe_upload(settings, resource_key, body, _MIME_MARKDOWN)

    artifact = {
        "resource_key": resource_key,
        "name": title or file_name,
        "file_name": file_name,
        "mime_type": "text/markdown",
        "size": len(body),
        "download_url": f"/artifacts/{resource_key}",
        "preview_url": f"/artifacts/{resource_key}",
        "missing": False,
    }
    summary = f"已生成报告「{title}」（{len(body)} 字节），文件 {file_name} 已登记。"
    return summary, artifact


@tool("write_report", args_schema=ReportArgs, response_format="content_and_artifact")
def write_report(
    title: str,
    content: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> tuple[str, dict[str, Any]]:
    """生成一份 Markdown 报告并登记为可下载的 artifact（产物落对象存储）。

    当任务需要产出可交付的文档/报告时使用。返回报告摘要，并在结果里附带 artifact 引用。
    """
    run_id = _run_id_from_config(config)
    return build_report_artifact(
        title=title, content=content, run_id=run_id, tool_call_id=tool_call_id
    )
