"""build_tool_suite 装配：local + Skill 聚合，provider_map 正确（不接真实 MCP）。"""

from __future__ import annotations

from cognition.config import Settings
from cognition.tools.registry import build_tool_suite


def _write_skill(root):
    d = root / "chart"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: chart\ndescription: 画图\n---\n正文", encoding="utf-8")


async def test_suite_aggregates_local_and_skill(tmp_path):
    _write_skill(tmp_path)
    settings = Settings(
        mcp_enabled=False,           # 不接真实 MCP（stdio/sse 需外部进程）
        skills_enabled=True,
        skills_dirs=[str(tmp_path)],
        skill_runner="local",
        minio_upload_enabled=False,
    )
    tools, provider_map, closers = await build_tool_suite(settings)
    names = {t.name for t in tools}

    # 本地工具在
    assert {"calculator", "write_report"} <= names
    # Skill 工具在
    assert {"skill", "skill_read", "script_runner"} <= names
    # provider_map 正确
    assert provider_map["calculator"] == "local"
    assert provider_map["write_report"] == "local"
    assert provider_map["skill"] == "skill"
    assert provider_map["script_runner"] == "skill"
    # 无 MCP → 无需关闭资源
    assert closers == []


async def test_suite_defaults_local_only():
    settings = Settings(mcp_servers={}, skills_dirs=[], minio_upload_enabled=False)
    tools, provider_map, closers = await build_tool_suite(settings)
    # M12：web_fetch 默认开，code_interpreter 默认关（危险原语 opt-in）；
    # web_search 默认开（search_provider=auto → 免 key ddg，装配零 I/O，失败 fail-soft）。
    assert {t.name for t in tools} == {"calculator", "write_report", "web_fetch", "web_search"}
    assert set(provider_map.values()) == {"local"}
    assert closers == []


async def test_suite_includes_knowledge_search_when_rag_enabled():
    # 离线：fake 模型 + :memory: qdrant + fake embedding，构建不触网、不需 key
    settings = Settings(
        mcp_enabled=False, skills_dirs=[], rag_enabled=True, fake_model=True,
        qdrant_url=":memory:", embedding_provider="fake", sparse_provider="fake",
        embedding_dimension=64, minio_upload_enabled=False,
    )
    tools, provider_map, _ = await build_tool_suite(settings)
    names = {t.name for t in tools}
    assert "knowledge_search" in names
    assert provider_map["knowledge_search"] == "local"
