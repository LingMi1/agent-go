#!/usr/bin/env python3
"""Pro-Agent 生产 Plan-Execute 图的可复现 Send 有界并发评测。

正式配置仅改变 build_plan_execute_graph(max_parallel=1/2/4/8)；planner 输出先由真实
DeepSeek 冻结，再由无状态 adapter 在每次运行中原样回放。并发、reducer、executor、工具、
RAG 子图与 summary 全部复用生产代码。
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
EVAL_ROOT = SCRIPT_DIR.parent
REPO_ROOT = EVAL_ROOT.parents[1]
sys.path.insert(0, str(REPO_ROOT / "cognition"))
sys.path.insert(0, str(SCRIPT_DIR))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402
from qdrant_client import models  # noqa: E402

from cognition.config import Settings  # noqa: E402
from cognition.graphs.history import HistoryPolicy  # noqa: E402
from cognition.graphs.plan_execute import (  # noqa: E402
    EVENT_BRANCH_END,
    EVENT_BRANCH_START,
    PLANNER_SYSTEM,
    SEP,
    _ai_text,
    _parse_planning_call,
    _parse_planning_text,
    build_plan_execute_graph,
    compose_planner_system,
    planning_tool,
)
from cognition.graphs.react import build_react_graph  # noqa: E402
from cognition.providers.deepseek_provider import build_deepseek_chat  # noqa: E402
from cognition.providers.fake import MessageDrivenChatModel  # noqa: E402
from cognition.rag.factory import build_embedder, build_sparse  # noqa: E402
from cognition.rag.store import DENSE_VECTOR, SPARSE_VECTOR, QdrantStore  # noqa: E402
from cognition.tools.registry import build_tool_suite  # noqa: E402

import orchestration_metrics  # noqa: E402

MODEL = "deepseek-v4-pro"
COLLECTION = "eval_orchestration_bench_v1"
KB_ID = "eval_orchestration_v1"
SCHEDULE_SEED = 20260711
PLAN_ATTEMPTS = 3
EXPLICIT_RETRY_DELAYS = (1.0, 2.0)
# 单分支超时沿用生产默认（settings.branch_timeout_seconds=300）；运行墙钟按工作量公式
# 计算（见 run_timeout_seconds），对所有配置同一公式——唯一变量仍是并发度。
BRANCH_TIMEOUT_S = 300.0
PLANNER_SLACK_S = 60.0
PRICES = {
    "currency": "USD",
    "input_cache_hit_per_million": 0.003625,
    "input_cache_miss_per_million": 0.435,
    "output_per_million": 0.87,
    "source": "https://api-docs.deepseek.com/quick_start/pricing?article_id=article_1779470751466_8",
    "retrieved_on": "2026-07-11",
}
_STABLE_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_MARKER_RE = re.compile(r"\[S(\d+)\]")
_CURRENT_RUN: contextvars.ContextVar[str] = contextvars.ContextVar("eval_run", default="unscoped")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_file(path: Path) -> None:
    """读取简单 shell .env；只注入进程环境，不打印值，已有 env 优先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        try:
            parts = shlex.split(value, comments=True, posix=True)
            parsed = parts[0] if parts else ""
        except ValueError:
            parsed = value.strip().strip("'\"")
        os.environ.setdefault(key, parsed)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def validate_tasks(tasks: list[dict]) -> None:
    if not 20 <= len(tasks) <= 30:
        raise ValueError(f"task count must be 20-30, got {len(tasks)}")
    seen: set[str] = set()
    partial = 0
    for task in tasks:
        required = {"id", "prompt", "expected_subtask_count", "dependency_type", "timeout_seconds"}
        if set(task) != required:
            raise ValueError(f"{task.get('id')}: fields must equal {sorted(required)}")
        if task["id"] in seen:
            raise ValueError(f"duplicate task id: {task['id']}")
        seen.add(task["id"])
        count = int(task["expected_subtask_count"])
        if not 3 <= count <= 8:
            raise ValueError(f"{task['id']}: expected_subtask_count outside 3-8")
        markers = [int(x) for x in _MARKER_RE.findall(task["prompt"])]
        if sorted(set(markers)) != list(range(1, count + 1)):
            raise ValueError(f"{task['id']}: prompt markers do not cover S1..S{count}")
        if task["dependency_type"] not in ("independent", "partially_dependent"):
            raise ValueError(f"{task['id']}: invalid dependency_type")
        partial += task["dependency_type"] == "partially_dependent"
    if partial < 2:
        raise ValueError("dataset must include partially_dependent tasks")


def is_retryable(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        token in text
        for token in (
            "ratelimit", "rate limit", "429", "timeout", "timed out", "connection",
            "internalserver", "server error", "500", "502", "503", "504",
        )
    )


def classify_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "429" in text or "ratelimit" in text or "rate limit" in text:
        return "rate_limit"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection" in text:
        return "connection"
    if any(x in text for x in ("500", "502", "503", "504", "server error")):
        return "server_5xx"
    return "other"


def usage_of(response: Any) -> dict:
    usage = dict(getattr(response, "usage_metadata", None) or {})
    response_usage = dict((getattr(response, "response_metadata", None) or {}).get("token_usage") or {})
    input_tokens = int(usage.get("input_tokens") or response_usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or response_usage.get("completion_tokens") or 0)
    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read")
    if cache_read is None:
        prompt_details = response_usage.get("prompt_tokens_details") or {}
        cache_read = prompt_details.get("cached_tokens")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": int(cache_read) if cache_read is not None else None,
    }


class ModelRecorder:
    """并发安全的 per-run 模型调用账本。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, list[dict]] = {}

    def add(self, item: dict) -> None:
        run_id = _CURRENT_RUN.get()
        with self._lock:
            self._records.setdefault(run_id, []).append(item)

    def reset(self, run_id: str) -> None:
        with self._lock:
            self._records[run_id] = []

    def get(self, run_id: str) -> list[dict]:
        with self._lock:
            return list(self._records.get(run_id, []))


class InstrumentedModel:
    """包装已绑定或未绑定的 Runnable：显式重试并记录所有嵌套 RAG/Executor 调用。"""

    def __init__(self, inner: Any, recorder: ModelRecorder, role: str) -> None:
        self.inner, self.recorder, self.role = inner, recorder, role

    def _record_success(self, started: float, attempt: int, response: Any) -> None:
        self.recorder.add(
            {
                "role": self.role,
                "status": "success",
                "attempt": attempt,
                "latency_s": round(time.perf_counter() - started, 6),
                **usage_of(response),
            }
        )

    def _record_error(self, started: float, attempt: int, exc: BaseException) -> None:
        self.recorder.add(
            {
                "role": self.role,
                "status": "error",
                "attempt": attempt,
                "latency_s": round(time.perf_counter() - started, 6),
                "error_type": type(exc).__name__,
                "error_class": classify_error(exc),
                "error": str(exc)[:1000],
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": None,
            }
        )

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        for attempt in range(1, len(EXPLICIT_RETRY_DELAYS) + 2):
            started = time.perf_counter()
            try:
                response = self.inner.invoke(input, config=config, **kwargs)
                self._record_success(started, attempt, response)
                return response
            except BaseException as exc:
                self._record_error(started, attempt, exc)
                if not is_retryable(exc) or attempt > len(EXPLICIT_RETRY_DELAYS):
                    raise
                time.sleep(EXPLICIT_RETRY_DELAYS[attempt - 1])
        raise AssertionError("unreachable")

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        for attempt in range(1, len(EXPLICIT_RETRY_DELAYS) + 2):
            started = time.perf_counter()
            try:
                response = await self.inner.ainvoke(input, config=config, **kwargs)
                self._record_success(started, attempt, response)
                return response
            except BaseException as exc:
                self._record_error(started, attempt, exc)
                if not is_retryable(exc) or attempt > len(EXPLICIT_RETRY_DELAYS):
                    raise
                await asyncio.sleep(EXPLICIT_RETRY_DELAYS[attempt - 1])
        raise AssertionError("unreachable")


def model_stats(calls: list[dict]) -> dict:
    successes = [c for c in calls if c["status"] == "success"]
    errors = [c for c in calls if c["status"] == "error"]
    cache_values = [c["cache_read_tokens"] for c in successes if c["cache_read_tokens"] is not None]
    cache_read = sum(cache_values) if len(cache_values) == len(successes) else None
    input_tokens = sum(int(c["input_tokens"]) for c in successes)
    output_tokens = sum(int(c["output_tokens"]) for c in successes)
    cost, method = orchestration_metrics.estimate_cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        prices=PRICES,
    )
    return {
        "llm_calls": len(successes),
        "retry_count": len(errors),
        "rate_limit_count": sum(c.get("error_class") == "rate_limit" for c in errors),
        "llm_timeout_count": sum(c.get("error_class") == "timeout" for c in errors),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "estimated_cost_usd": round(cost, 10),
        "cost_method": method,
        "llm_call_records": calls,
    }


def build_settings() -> Settings:
    return Settings(
        fake_model=False,
        model_provider="deepseek",
        planner_provider="deepseek",
        planner_model=MODEL,
        executor_provider="deepseek",
        executor_model=MODEL,
        deepseek_model=MODEL,
        pg_dsn=None,
        qdrant_collection=COLLECTION,
        rag_enabled=True,
        minio_upload_enabled=False,
        search_artifact_enabled=False,
        mcp_enabled=False,
        mcp_servers={},
        skills_enabled=False,
        skills_dirs=[],
        web_fetch_enabled=False,
    )


def ingest_fixed_corpus(settings: Settings, chunks: list[dict]) -> int:
    """把 RAG 评测的固定 chunk 原样写入隔离 collection。"""
    store = QdrantStore.from_settings(settings)
    client = store._c  # noqa: SLF001 - 评测需重建隔离集合
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    store.ensure_collection()
    embedder, sparse = build_embedder(settings), build_sparse(settings)
    dense_vecs = embedder.embed([c["text"] for c in chunks])
    sparse_vecs = sparse.embed([c["text"] for c in chunks])
    now = int(time.time())
    points = []
    for c, dense, (indices, values) in zip(chunks, dense_vecs, sparse_vecs):
        points.append(
            models.PointStruct(
                id=str(uuid.uuid5(_STABLE_NS, f"{KB_ID}|{c['chunk_id']}")),
                vector={
                    DENSE_VECTOR: dense,
                    SPARSE_VECTOR: models.SparseVector(indices=indices, values=values),
                },
                payload={
                    "kb_id": KB_ID,
                    "text": c["text"],
                    "source_id": c["document_id"],
                    "file_name": f"{c['document_id']}.md",
                    "chunk_index": c["seq"],
                    "dedup_key": c["chunk_id"],
                    "chunk_type": "text",
                    "image_url": None,
                    "created": now,
                },
            )
        )
    store.upsert(points)
    actual = int(client.count(COLLECTION, exact=True).count)
    if actual != len(chunks):
        raise RuntimeError(f"isolated corpus count {actual} != {len(chunks)}")
    return actual


def validate_frozen_plan(task: dict, title: str, steps: list[str]) -> tuple[bool, list[str], list[dict]]:
    errors: list[str] = []
    branches: list[dict] = []
    expected = int(task["expected_subtask_count"])
    for rnd, step in enumerate(steps):
        for index, text in enumerate(step.split(SEP)):
            branches.append(
                {
                    "round": rnd,
                    "branch_id": f"b{index}",
                    "task": text.strip(),
                    "depends_on": [] if rnd == 0 else [
                        f"r{rnd - 1}:b{i}" for i in range(len(steps[rnd - 1].split(SEP)))
                    ],
                }
            )
    if not title.strip():
        errors.append("blank_title")
    if len(branches) != expected:
        errors.append(f"branch_count:{len(branches)}!={expected}")
    # 标记归属制：每个分支以首个 [Sn] 为归属标记，归属集合必须恰为 S1..SN 的排列。
    # 分支正文对其他标记的引用（依赖型计划第二步必然出现"汇总 [S1]～[Sk] 结果"）不计数——
    # 旧规则按全文出现次数判重，把所有合法依赖计划误杀。
    owned: list[int] = []
    unmarked = 0
    for branch in branches:
        found = _MARKER_RE.findall(branch["task"])
        if not found:
            unmarked += 1
        else:
            owned.append(int(found[0]))
    if unmarked:
        errors.append(f"unmarked_branches:{unmarked}")
    if sorted(owned) != list(range(1, expected + 1)):
        errors.append(f"owned_markers:{sorted(owned)}")
    if task["dependency_type"] == "independent" and len(steps) != 1:
        errors.append(f"independent_rounds:{len(steps)}")
    if task["dependency_type"] == "partially_dependent":
        if len(steps) != 2:
            errors.append(f"partial_rounds:{len(steps)}")
        elif len(steps[1].split(SEP)) != 1:
            errors.append("dependent_round_must_have_one_branch")
    return not errors, errors, branches


async def freeze_plans(
    tasks: list[dict], settings: Settings, recorder: ModelRecorder, output: Path
) -> list[dict]:
    base = build_deepseek_chat(settings, model=MODEL, temperature=0, max_retries=0)
    planner = InstrumentedModel(base.bind_tools([planning_tool]), recorder, "planner_freeze")
    frozen: list[dict] = []
    for task in tasks:
        attempts: list[dict] = []
        selected: dict | None = None
        run_id = f"plan::{task['id']}"
        recorder.reset(run_id)
        token = _CURRENT_RUN.set(run_id)
        try:
            for attempt in range(1, PLAN_ATTEMPTS + 1):
                started = time.perf_counter()
                system = SystemMessage(
                    content=compose_planner_system(PLANNER_SYSTEM, "", "", {})
                )
                human = HumanMessage(
                    content=f"用户任务：{task['prompt']}\n请用 planning 工具创建计划（同一步骤内可用 <sep> 分隔可并行子任务）。"
                )
                try:
                    ai = await planner.ainvoke([system, human])
                    draft = _parse_planning_call(ai) or _parse_planning_text(_ai_text(ai))
                    title = str((draft or {}).get("title") or "")
                    steps = [str(s) for s in ((draft or {}).get("steps") or [])]
                    valid, errors, branches = validate_frozen_plan(task, title, steps)
                    rec = {
                        "attempt": attempt,
                        "latency_s": round(time.perf_counter() - started, 6),
                        "valid": valid,
                        "validation_errors": errors,
                        "title": title,
                        "steps": steps,
                        "raw_content": _ai_text(ai)[:4000],
                    }
                    attempts.append(rec)
                    if valid:
                        selected = {"title": title, "steps": steps, "branches": branches}
                        break
                except BaseException as exc:
                    attempts.append(
                        {
                            "attempt": attempt,
                            "latency_s": round(time.perf_counter() - started, 6),
                            "valid": False,
                            "validation_errors": [f"{type(exc).__name__}: {exc}"],
                            "title": "",
                            "steps": [],
                            "raw_content": "",
                        }
                    )
        finally:
            _CURRENT_RUN.reset(token)
        stats = model_stats(recorder.get(run_id))
        item = {
            "task_id": task["id"],
            "prompt_sha256": hashlib.sha256(task["prompt"].encode()).hexdigest(),
            "expected_subtask_count": task["expected_subtask_count"],
            "dependency_type": task["dependency_type"],
            "valid": selected is not None,
            "attempts": attempts,
            "selected_plan": selected,
            "planner_model": MODEL,
            "temperature": 0,
            "planner_usage": {k: v for k, v in stats.items() if k != "llm_call_records"},
        }
        frozen.append(item)
        append_jsonl(output, item)
        print(f"[freeze] {task['id']}: {'valid' if selected else 'INVALID'}", flush=True)
    return frozen


def build_frozen_planner(frozen: list[dict], task_map: dict[str, dict]) -> MessageDrivenChatModel:
    by_prompt = {
        task_map[item["task_id"]]["prompt"]: item["selected_plan"]
        for item in frozen
        if item.get("valid")
    }

    def decide(messages: list) -> AIMessage:
        if any(
            isinstance(m, AIMessage)
            and any(tc.get("name") == "planning" for tc in (m.tool_calls or []))
            for m in messages
        ):
            return AIMessage(content="冻结计划：保持 DAG 不变并推进到下一轮。")
        prompt = ""
        for message in reversed(messages):
            if isinstance(message, HumanMessage) and str(message.content).startswith("用户任务："):
                prompt = str(message.content)[len("用户任务：") :].split("\n请用 planning", 1)[0]
                break
        plan = by_prompt.get(prompt)
        if plan is None:
            raise ValueError("no valid frozen plan for prompt")
        return AIMessage(
            content="回放已冻结的真实 planner 输出。",
            tool_calls=[
                {
                    "name": "planning",
                    "args": {"command": "create", "title": plan["title"], "steps": plan["steps"]},
                    "id": "frozen_plan_" + hashlib.sha256(prompt.encode()).hexdigest()[:12],
                }
            ],
        )

    return MessageDrivenChatModel(decide=decide)


def pair_branch_intervals(starts: list[dict], ends: list[dict], fallback_end: int) -> list[dict]:
    queues: dict[tuple[int, str], list[dict]] = {}
    for item in starts:
        queues.setdefault((int(item["round"]), item["branch_id"]), []).append(item)
    end_queues: dict[tuple[int, str], list[dict]] = {}
    for item in ends:
        end_queues.setdefault((int(item["round"]), item["branch_id"]), []).append(item)
    intervals = []
    for key, items in queues.items():
        matches = end_queues.get(key, [])
        for index, start in enumerate(items):
            end = matches[index] if index < len(matches) else None
            intervals.append(
                {
                    "round": key[0],
                    "branch_id": key[1],
                    "task": start.get("task", ""),
                    "start_ns": int(start["perf_counter_ns"]),
                    "end_ns": int(end["perf_counter_ns"]) if end else fallback_end,
                    "error_type": (end or {}).get("error_type", "missing_end"),
                }
            )
    return sorted(intervals, key=lambda x: (x["round"], x["branch_id"], x["start_ns"]))


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _answer_matches_observation(answer: str, ok_obs: list[str]) -> bool:
    """最终回答里是否出现某个成功观测的数值（浮点容差吸收"保留 6 位小数"类舍入）。"""
    answer_nums = _NUM_RE.findall(answer.replace(",", "").replace("，", ""))
    obs_nums = [n for obs in ok_obs for n in _NUM_RE.findall(obs.replace(",", ""))]
    for a in answer_nums:
        for o in obs_nums:
            try:
                fa, fo = float(a), float(o)
            except ValueError:
                continue
            if fa == fo or abs(fa - fo) / max(abs(fa), abs(fo), 1.0) < 1e-6:
                return True
    return False


def check_run(task: dict, frozen: dict, intervals: list[dict], results: list[dict], aggregate: str, concurrency: int) -> dict:
    expected_branches = frozen["selected_plan"]["branches"]
    # 生产 current_step(plan).split(SEP) 不 strip，sub_results 的 task 带切分残留空白；
    # 冻结分支在冻结期已 strip——两侧统一 strip 后比对，否则同一子任务恒被记 missing+unexpected。
    expected_keys = [(b["round"], b["branch_id"], str(b["task"]).strip()) for b in expected_branches]
    actual_keys = [
        (int(r.get("round", -1)), str(r.get("branch_id", "")), str(r.get("task", "")).strip())
        for r in results
    ]
    missing = [key for key in expected_keys if key not in actual_keys]
    duplicates = sorted({key for key in actual_keys if actual_keys.count(key) > 1})
    unexpected = [key for key in actual_keys if key not in expected_keys]
    empty_or_error = [
        (int(r.get("round", -1)), str(r.get("branch_id", "")))
        for r in results
        if r.get("status") != "finished" or not str(r.get("result", "")).strip()
    ]
    aggregate_missing = [
        (int(r.get("round", -1)), str(r.get("branch_id", "")))
        for r in results
        if str(r.get("task", "")).strip() not in aggregate or str(r.get("result", "")) not in aggregate
    ]
    quality_failures = []
    tool_error_count = 0
    is_calc = task["id"].startswith("calc_")
    for result in results:
        observations = [str(x) for x in (result.get("observations") or [])]
        rnd = int(result.get("round", 0))
        error_obs = [x for x in observations if "Error:" in x or "工具执行失败" in x]
        ok_obs = [x for x in observations if x not in error_obs]
        tool_error_count += len(error_obs)
        if is_calc and (rnd == 0 or task["dependency_type"] == "independent"):
            # 质量 = 最终回答包含某个成功观测里的数值（容忍格式化舍入）。
            # 工具首调出错但自纠成功属正常 ReAct 行为，计入 tool_error_count 不判死。
            if not ok_obs or not _answer_matches_observation(
                str(result.get("result", "")), ok_obs
            ):
                quality_failures.append([rnd, result.get("branch_id"), "calculator_observation"])
        if not is_calc and rnd == 0:
            joined = "\n".join(observations) + str(result.get("result", ""))
            if not observations or "〔" not in joined:
                quality_failures.append([rnd, result.get("branch_id"), "rag_citation"])

    by_round: dict[int, list[dict]] = {}
    for item in intervals:
        by_round.setdefault(int(item["round"]), []).append(item)
    dependency_ok = True
    if task["dependency_type"] == "partially_dependent" and len(by_round) >= 2:
        rounds = sorted(by_round)
        for prior, later in zip(rounds, rounds[1:]):
            dependency_ok &= min(i["start_ns"] for i in by_round[later]) >= max(
                i["end_ns"] for i in by_round[prior]
            )
    elif task["dependency_type"] == "partially_dependent":
        dependency_ok = False

    serial_order_ok = True
    if concurrency == 1:
        for items in by_round.values():
            starts = sorted(items, key=lambda x: x["start_ns"])
            actual = [x["branch_id"] for x in starts]
            wanted = [f"b{i}" for i in range(len(starts))]
            serial_order_ok &= actual == wanted
    complete = not (missing or duplicates or unexpected or empty_or_error or aggregate_missing)
    return {
        "completeness_pass": complete,
        "dependency_order_pass": bool(dependency_ok),
        "serial_order_pass": bool(serial_order_ok),
        "quality_pass": not quality_failures,
        "tool_error_count": tool_error_count,
        "missing_subtasks": missing,
        "duplicate_subtasks": duplicates,
        "unexpected_subtasks": unexpected,
        "empty_or_error_subtasks": empty_or_error,
        "aggregate_missing_subtasks": aggregate_missing,
        "quality_failures": quality_failures,
    }


def run_timeout_seconds(frozen: dict, concurrency: int) -> float:
    """运行墙钟预算 = 规划余量 + Σ每轮 ceil(该轮分支数/并发度)×单分支超时。

    同一公式套所有配置（并发度是唯一变量），给每个配置等价的"人均分支时间预算"；
    旧规则一刀切 300s，串行跑 N 个分支的总预算只有并行的 1/N，必然被算术性误杀。
    """
    per_round: dict[int, int] = {}
    for branch in frozen["selected_plan"]["branches"]:
        rnd = int(branch["round"])
        per_round[rnd] = per_round.get(rnd, 0) + 1
    budget = PLANNER_SLACK_S
    for count in per_round.values():
        budget += math.ceil(count / max(concurrency, 1)) * BRANCH_TIMEOUT_S
    return budget


async def run_one(
    *, task: dict, frozen: dict, graph: Any, concurrency: int, repetition: int,
    is_warmup: bool, recorder: ModelRecorder,
) -> dict:
    run_id = f"orch::{task['id']}::c{concurrency}::{'w' if is_warmup else 'r'}{repetition}"
    run_timeout_s = run_timeout_seconds(frozen, concurrency)
    recorder.reset(run_id)
    token = _CURRENT_RUN.set(run_id)
    started_utc = utc_now()
    started = time.perf_counter()
    starts: list[dict] = []
    ends: list[dict] = []
    sub_results: list[dict] = []
    aggregate = ""
    exception = ""
    timed_out = False
    try:
        state = {
            "query": task["prompt"],
            "request_id": run_id,
            "session_id": run_id,
            "plan": None,
            "round": 0,
            "step": 0,
            "reduced_state": "",
            "output_format": "",
            "image_gen": False,
            "planner_messages": [],
            "sub_results": [],
        }
        config = {
            "configurable": {"thread_id": run_id},
            "metadata": {
                "request_id": run_id,
                "run_id": run_id,
                "session_id": run_id,
                "kb_id": KB_ID,
                "agent_type": "plan_solve",
            },
            "recursion_limit": 80,
        }
        async with asyncio.timeout(run_timeout_s):
            async for event in graph.astream_events(state, version="v2", config=config):
                if event.get("event") == "on_custom_event":
                    data = dict(event.get("data") or {})
                    if event.get("name") == EVENT_BRANCH_START:
                        starts.append(data)
                    elif event.get("name") == EVENT_BRANCH_END:
                        ends.append(data)
                    elif event.get("name") == "result":
                        aggregate = str(data.get("text", ""))
                if (
                    event.get("event") == "on_chain_end"
                    and (event.get("metadata") or {}).get("langgraph_node") == "executor"
                ):
                    output = (event.get("data") or {}).get("output") or {}
                    for item in output.get("sub_results", []) if isinstance(output, dict) else []:
                        key = (item.get("request_id"), item.get("round"), item.get("branch_id"))
                        if not any(
                            (x.get("request_id"), x.get("round"), x.get("branch_id")) == key
                            for x in sub_results
                        ):
                            sub_results.append(dict(item))
    except TimeoutError:
        timed_out = True
        exception = f"run timeout after {run_timeout_s:.0f}s"
    except BaseException as exc:
        exception = f"{type(exc).__name__}: {exc}"
    finally:
        _CURRENT_RUN.reset(token)
    finished_ns = time.perf_counter_ns()
    latency = time.perf_counter() - started
    intervals = pair_branch_intervals(starts, ends, finished_ns)
    checks = check_run(task, frozen, intervals, sub_results, aggregate, concurrency)
    stats = model_stats(recorder.get(run_id))
    success = bool(
        not exception
        and not timed_out
        and checks["completeness_pass"]
        and checks["dependency_order_pass"]
        and checks["serial_order_pass"]
        and checks["quality_pass"]
    )
    return {
        "run_id": run_id,
        "task_id": task["id"],
        "config": "serial_baseline" if concurrency == 1 else "send_parallel",
        "concurrency": concurrency,
        "repetition": repetition,
        "is_warmup": is_warmup,
        "started_utc": started_utc,
        "finished_utc": utc_now(),
        "latency_s": round(latency, 6),
        "run_timeout_s": run_timeout_s,
        "success": success,
        "timed_out": timed_out,
        "exception": exception,
        "expected_subtask_count": int(task["expected_subtask_count"]),
        "observed_subtask_count": len(sub_results),
        "actual_max_concurrency": orchestration_metrics.max_concurrency(intervals),
        "branch_intervals": intervals,
        "sub_results": sub_results,
        "aggregate_output": aggregate,
        **checks,
        **stats,
    }


def record_key(record: dict) -> tuple:
    return (
        record["task_id"], int(record["concurrency"]), bool(record["is_warmup"]),
        int(record["repetition"]),
    )


def docker_clock_offset_s() -> float | None:
    """MinIO/Qdrant 所在 Docker VM 与宿主机的时钟差（秒，容器-宿主）；不可测返回 None。

    宿主机休眠会让 Docker VM 时钟漂移（上一轮实验的 RequestTimeTooSkewed 根因），
    跑前应校验并在 config.json 留档。
    """
    try:
        names = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.split()
        name = next((n for n in names if "minio" in n or "qdrant" in n), None)
        if not name:
            return None
        out = subprocess.run(
            ["docker", "exec", name, "date", "-u", "+%s"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return int(out) - int(time.time())
    except Exception:  # noqa: BLE001 — best-effort 探测，失败不阻断评测
        return None


def build_config(args: argparse.Namespace, tasks: list[dict], chunks: list[dict]) -> dict:
    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    git_dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "-uno"], cwd=REPO_ROOT,
            capture_output=True, text=True,
        ).stdout.strip()
    )
    packages = {}
    for package in ("langgraph", "langchain-core", "langchain-deepseek", "qdrant-client", "fastembed"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = "missing"
    return {
        "started_utc": utc_now(),
        "git_commit": git_commit,
        "git_dirty_tracked": git_dirty,
        "tasks_file": "tasks.jsonl",
        "tasks_sha256": sha256_file(EVAL_ROOT / "tasks.jsonl"),
        "task_count": len(tasks),
        "dependency_distribution": {
            kind: sum(t["dependency_type"] == kind for t in tasks)
            for kind in ("independent", "partially_dependent")
        },
        "corpus_file": "eval/orchestration/corpus.jsonl",
        "corpus_sha256": sha256_file(EVAL_ROOT / "corpus.jsonl"),
        "corpus_chunks": len(chunks),
        "qdrant_collection": COLLECTION,
        "kb_id": KB_ID,
        "model": MODEL,
        "temperature": 0,
        "sdk_max_retries": 0,
        "explicit_retry_delays_s": list(EXPLICIT_RETRY_DELAYS),
        "concurrencies": args.concurrencies,
        "formal_runs_per_task_config": args.runs,
        "warmups_per_task_config": args.warmups,
        "schedule_seed": SCHEDULE_SEED,
        "pricing": PRICES,
        "branch_timeout_s": BRANCH_TIMEOUT_S,
        "run_timeout_formula": "60s planner slack + sum_rounds(ceil(branches/concurrency))*300s",
        "minio_upload_enabled": False,
        "docker_clock_offset_s": docker_clock_offset_s(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
        "official_complete": bool(
            len(tasks) == 20
            and args.concurrencies == [1, 2, 4, 8]
            and args.runs >= 3
            and args.warmups >= 1
            and not args.limit
        ),
        "secrets_recorded": False,
    }


async def main_async(args: argparse.Namespace) -> Path:
    # write_report 执行期走 get_settings()（环境变量），不读评测的 Settings 对象；
    # deploy/.env 里该开关为 true——必须在 load_env_file 前显式压成 false，
    # 否则评测运行会 best-effort 上传 MinIO（时钟偏差时刷 RequestTimeTooSkewed 噪声）。
    os.environ["COGNITION_MINIO_UPLOAD_ENABLED"] = "false"
    load_env_file(Path(args.env_file))
    tasks = load_jsonl(EVAL_ROOT / "tasks.jsonl")
    validate_tasks(tasks)
    if args.limit:
        tasks = tasks[: args.limit]
    chunks = load_jsonl(EVAL_ROOT / "corpus.jsonl")
    settings = build_settings()
    recorder = ModelRecorder()

    if args.resume:
        out_dir = Path(args.resume)
        config = json.loads((out_dir / "config.json").read_text(encoding="utf-8"))
    else:
        out_dir = (
            Path(args.out)
            if args.out
            else EVAL_ROOT / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        out_dir.mkdir(parents=True, exist_ok=False)
        config = build_config(args, tasks, chunks)
        (out_dir / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    frozen_path = out_dir / "frozen_plans.jsonl"
    if frozen_path.exists() and frozen_path.stat().st_size:
        frozen = load_jsonl(frozen_path)
    else:
        frozen = await freeze_plans(tasks, settings, recorder, frozen_path)
    frozen_map = {item["task_id"]: item for item in frozen}
    task_map = {task["id"]: task for task in tasks}

    print(f"[corpus] indexing {len(chunks)} fixed chunks into {COLLECTION}", flush=True)
    ingest_fixed_corpus(settings, chunks)

    rag_base = build_deepseek_chat(settings, model=MODEL, temperature=0, max_retries=0)
    rag_model = InstrumentedModel(rag_base, recorder, "rag")
    tools, _, closers = await build_tool_suite(settings, rag_model=rag_model)
    executor_base = build_deepseek_chat(settings, model=MODEL, temperature=0, max_retries=0)
    executor_model = InstrumentedModel(executor_base.bind_tools(tools), recorder, "executor")
    history_policy = HistoryPolicy(
        max_messages=settings.history_max_messages, max_chars=settings.history_max_chars
    )
    executor_graph = build_react_graph(
        executor_model, tools, max_steps=settings.max_steps, history_policy=history_policy,
        format_prompts=settings.output_format_prompts,
    )
    frozen_planner = build_frozen_planner(frozen, task_map)
    graphs = {
        concurrency: build_plan_execute_graph(
            frozen_planner,
            executor_graph,
            tools,
            max_steps=max(settings.planner_max_steps, 3),
            max_parallel=concurrency,
            branch_timeout=BRANCH_TIMEOUT_S,
            react_recursion_limit=2 * settings.max_steps + 5,
            format_prompts=settings.output_format_prompts,
        )
        for concurrency in args.concurrencies
    }

    per_run_path = out_dir / "per_run.jsonl"
    existing = load_jsonl(per_run_path) if per_run_path.exists() else []
    done = {record_key(r) for r in existing}
    # 冻结失败的任务跳过执行：失败本身已完整记录在 frozen_plans.jsonl 与 failures.jsonl,
    # 不再为其生成成堆 0 秒废记录污染各配置的成功率/延迟分布。
    skipped = [t["id"] for t in tasks if not (frozen_map.get(t["id"]) or {}).get("valid")]
    for task_id in skipped:
        print(f"[skip] {task_id}: frozen plan invalid, excluded from runs", flush=True)
    tasks = [t for t in tasks if t["id"] not in set(skipped)]
    rng = random.Random(SCHEDULE_SEED)
    try:
        phases = [(True, rep) for rep in range(1, args.warmups + 1)] + [
            (False, rep) for rep in range(1, args.runs + 1)
        ]
        for phase_index, (is_warmup, repetition) in enumerate(phases):
            ordered_tasks = list(tasks)
            rng.shuffle(ordered_tasks)
            for task_index, task in enumerate(ordered_tasks):
                shift = (task_index + phase_index) % len(args.concurrencies)
                ordered_concurrency = args.concurrencies[shift:] + args.concurrencies[:shift]
                for concurrency in ordered_concurrency:
                    key = (task["id"], concurrency, is_warmup, repetition)
                    if key in done:
                        continue
                    record = await run_one(
                        task=task,
                        frozen=frozen_map[task["id"]],
                        graph=graphs[concurrency],
                        concurrency=concurrency,
                        repetition=repetition,
                        is_warmup=is_warmup,
                        recorder=recorder,
                    )
                    append_jsonl(per_run_path, record)
                    existing.append(record)
                    done.add(key)
                    print(
                        f"[{'warmup' if is_warmup else 'formal'} r{repetition}] "
                        f"{task['id']} c={concurrency} {record['latency_s']:.2f}s "
                        f"success={record['success']} max={record['actual_max_concurrency']}",
                        flush=True,
                    )
    finally:
        for close in closers:
            try:
                await close()
            except Exception:
                pass

    metrics = orchestration_metrics.compute_metrics(existing, bootstrap_seed=SCHEDULE_SEED)
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=True), encoding="utf-8"
    )
    failures = []
    for item in frozen:
        if not item.get("valid"):
            failures.append({"type": "frozen_plan_invalid", "task_id": item["task_id"], "detail": item})
    for record in existing:
        if not record.get("success"):
            failures.append(
                {
                    "type": "run_failure",
                    "task_id": record["task_id"],
                    "concurrency": record["concurrency"],
                    "repetition": record["repetition"],
                    "is_warmup": record["is_warmup"],
                    "exception": record.get("exception", ""),
                    "timed_out": record.get("timed_out", False),
                    "rate_limit_count": record.get("rate_limit_count", 0),
                    "checks": {
                        key: record.get(key)
                        for key in (
                            "completeness_pass", "dependency_order_pass", "serial_order_pass",
                            "quality_pass", "missing_subtasks", "duplicate_subtasks",
                            "aggregate_missing_subtasks", "quality_failures",
                        )
                    },
                }
            )
    (out_dir / "failures.jsonl").write_text(
        "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in failures), encoding="utf-8"
    )
    config["finished_utc"] = utc_now()
    config["recorded_runs"] = len(existing)
    config["skipped_tasks_frozen_invalid"] = skipped
    (out_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from render_report import render_report

    render_report(out_dir)
    print(f"done -> {out_dir}", flush=True)
    return out_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrencies", nargs="+", type=int, default=[1, 2, 4, 8])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--env-file", default=str(REPO_ROOT / "deploy/.env"))
    args = parser.parse_args()
    args.concurrencies = sorted(set(args.concurrencies))
    if 1 not in args.concurrencies:
        parser.error("concurrency=1 serial baseline is required")
    if args.runs < 1 or args.warmups < 1:
        parser.error("runs and warmups must be >=1")
    return args


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
