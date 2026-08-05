"""SOP 召回与注入 planner 提示词。"""

from cognition.graphs.plan_execute import PLANNER_SYSTEM
from cognition.sop import SopEntry, SopStore, default_sop_store


def test_recall_hits_planning_keywords():
    store = default_sop_store()
    assert store.recall("帮我做个调研报告") is not None
    assert store.recall("制定一个计划") is not None
    assert store.recall("make a research plan") is not None


def test_recall_miss_returns_none():
    store = default_sop_store()
    assert store.recall("今天天气怎么样") is None
    assert store.recall("") is None
    assert store.recall(None) is None


def test_custom_corpus():
    store = SopStore(entries=(SopEntry(name="x", keywords=("退款",), body="退款 SOP"),))
    assert store.recall("我要退款") == "退款 SOP"
    assert store.recall("计划") is None  # 自定义语料不含默认条目


def test_sop_injected_into_planner_prompt():
    sop = default_sop_store().recall("写个报告计划")
    assert sop  # 命中
    prompt = PLANNER_SYSTEM.replace("{{sop}}", sop)
    assert "{{sop}}" not in prompt
    assert sop in prompt


def test_sop_miss_replaced_with_empty():
    prompt = PLANNER_SYSTEM.replace("{{sop}}", "")
    assert "{{sop}}" not in prompt
