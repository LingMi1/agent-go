"""write_report 产物工具：ArtifactRef 形状与 resource_key 规则（不触网，upload 关闭）。"""

from __future__ import annotations

from cognition.config import Settings
from cognition.tools.report import build_report_artifact


def test_build_report_artifact_shape():
    settings = Settings(minio_upload_enabled=False)  # 不触网
    summary, art = build_report_artifact(
        title="销售周报", content="本周合计 14", run_id="r1", tool_call_id="tc1", settings=settings
    )
    assert art["resource_key"] == "r1/tc1/销售周报.md"
    assert art["download_url"] == "/artifacts/r1/tc1/销售周报.md"
    assert art["preview_url"] == "/artifacts/r1/tc1/销售周报.md"
    assert art["file_name"].endswith(".md")
    assert art["mime_type"] == "text/markdown"
    assert art["missing"] is False
    assert art["size"] > 0
    assert "销售周报" in summary


def test_slugify_unsafe_title():
    settings = Settings(minio_upload_enabled=False)
    _, art = build_report_artifact(
        title="a/b c?d", content="x", run_id="r2", tool_call_id="tc9", settings=settings
    )
    # 文件名安全化：非字词字符转 -，仍以 .md 结尾，且 key 前缀为 run/tool_call。
    assert art["resource_key"].startswith("r2/tc9/")
    assert "/" not in art["file_name"]
    assert art["file_name"].endswith(".md")
