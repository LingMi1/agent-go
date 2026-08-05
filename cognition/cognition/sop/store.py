"""最小 SopStore：in-repo 语料 + 朴素关键词召回。

原项目 SOP 召回是一个 HTTP 服务（SopRecallService → choosed_sop_string）。这里先打通
「召回→注入 planner 提示词」的闭环：用一个小语料 + 关键词命中返回 SOP 文本。
`recall(query) -> str | None` 是稳定 seam，M4 换成 Qdrant 混合检索时只替换内部实现。

注入路径：plan_execute 的 `sop_recall` 节点调用 recall(query)，把命中的 SOP 文本写入
state["sop"]，planner 节点用它替换提示词里的 `{{sop}}` 占位符（未命中 → 替换为空串）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SopEntry:
    """一条 SOP：关键词命中任一即召回 body。"""

    name: str
    keywords: tuple[str, ...]
    body: str


# 默认 in-repo 语料：覆盖「计划/规划/报告」类查询（中英关键词）。
_DEFAULT_CORPUS: tuple[SopEntry, ...] = (
    SopEntry(
        name="planning_report",
        keywords=("plan", "计划", "规划", "报告", "report", "方案", "调研", "research"),
        body=(
            "标准作业流程（计划/报告类任务）：\n"
            "1. 先把目标拆成可并行的子任务，单个步骤内用 <sep> 分隔可并行的子任务。\n"
            "2. 每个子任务尽量自包含、可独立执行、产出明确。\n"
            "3. 需要外部网络信息时：先用 web_search 获取候选来源（标题/链接/摘要），"
            "再对关键 URL 用 web_fetch 深入阅读；结论标注来源 URL。\n"
            "4. 需要落地交付物时，使用 write_report 工具产出报告并登记 artifact。\n"
            "5. 最后汇总各子任务结论，给出结构化、可执行的结论。"
        ),
    ),
)


@dataclass
class SopStore:
    """SOP 存储与召回（朴素关键词匹配）。"""

    entries: tuple[SopEntry, ...] = field(default_factory=lambda: _DEFAULT_CORPUS)

    def recall(self, query: str | None) -> str | None:
        """返回首个关键词命中的 SOP body；未命中返回 None。"""
        if not query:
            return None
        text = query.lower()
        for entry in self.entries:
            for kw in entry.keywords:
                if kw.lower() in text:
                    return entry.body
        return None


def default_sop_store() -> SopStore:
    """默认 SopStore（含内置语料）。"""
    return SopStore()
