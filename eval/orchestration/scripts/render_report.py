#!/usr/bin/env python3
"""从编排评测原始产物程序化渲染 REPORT.md；不接受手填指标。"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def pct(value: float) -> str:
    return "—" if value is None or math.isnan(value) else f"{value * 100:.1f}%"


def sec(value: float) -> str:
    return "—" if value is None or math.isnan(value) else f"{value:.2f}s"


def num(value: float, digits: int = 2) -> str:
    return "—" if value is None or math.isnan(value) else f"{value:.{digits}f}"


def render_report(out_dir: Path) -> None:
    cfg = json.loads((out_dir / "config.json").read_text(encoding="utf-8"))
    metrics = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (out_dir / "per_run.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failures = [
        json.loads(line)
        for line in (out_dir / "failures.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    frozen = [
        json.loads(line)
        for line in (out_dir / "frozen_plans.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    configs = metrics["configs"]
    ordered = sorted(configs, key=int)
    formal = [r for r in records if not r["is_warmup"]]
    warmups = [r for r in records if r["is_warmup"]]

    lines: list[str] = []
    w = lines.append
    w(f"# Pro-Agent 多子任务编排性能评测报告（{out_dir.name}）")
    w("")
    w("本报告完全由 `config.json`、`frozen_plans.jsonl`、`per_run.jsonl`、"
      "`metrics.json` 与 `failures.jsonl` 程序化生成；失败运行保留在统计中。")
    if not cfg.get("official_complete"):
        w("")
        w("> **非正式结果**：当前运行不是完整的 20 任务 × 4 配置 × 3 次正式评测，不得用于简历。")
    w("")

    w("## 1. 任务集设计")
    w("")
    dist = cfg["dependency_distribution"]
    w(f"- 固定任务 {cfg['task_count']} 条：independent={dist['independent']}、"
      f"partially_dependent={dist['partially_dependent']}；每条含 3–8 个带稳定标记的子任务。")
    w("- 任务构成：10 条 calculator/只读计算与 10 条虚构非敏感知识库检索；知识库包含"
      f" `{cfg['corpus_chunks']}` 个固定 chunk，写入隔离 collection `{cfg['qdrant_collection']}`。")
    w(f"- tasks sha256=`{cfg['tasks_sha256'][:16]}`，corpus sha256=`{cfg['corpus_sha256'][:16]}`。")
    w("")

    w("## 2. 控制变量")
    w("")
    w(f"- 模型：`{cfg['model']}`，temperature={cfg['temperature']}；SDK 隐式 retry="
      f"{cfg['sdk_max_retries']}，显式 retry 等待={cfg['explicit_retry_delays_s']} 秒。")
    w("- 每个任务先由真实 planner 冻结一次计划，serial/parallel 回放完全相同的 steps、"
      "`<sep>` 分支和依赖 DAG；正式执行禁止动态改写冻结计划。")
    w(f"- 并发度：{cfg['concurrencies']}；每个 task/config 预热 {cfg['warmups_per_task_config']} 次、"
      f"正式 {cfg['formal_runs_per_task_config']} 次；预热 {len(warmups)} 条不进入指标，正式 {len(formal)} 条。")
    w(f"- 顺序采用固定 seed `{cfg['schedule_seed']}` 轮换配置，工具、提示词、语料、超时与输入保持一致。")
    w("")

    w("## 3. 串行与各并发度结果")
    w("")
    w("| concurrency | config | 正式运行 | 成功率 | 实际峰值均值/最大 | LLM calls | tokens(in/out) | 成本(USD) |")
    w("|---:|---|---:|---:|---:|---:|---:|---:|")
    for key in ordered:
        m = configs[key]
        label = "serial_baseline" if key == "1" else "send_parallel"
        w(f"| {key} | {label} | {m['formal_runs']} | {pct(m['success_rate'])} | "
          f"{m['actual_max_concurrency_mean']:.2f}/{m['actual_max_concurrency_max']} | "
          f"{m['llm_calls']} | {m['input_tokens']}/{m['output_tokens']} | "
          f"{m['estimated_cost_usd']:.6f} |")
    w("")

    w("## 4. 平均耗时与 P95")
    w("")
    w("| concurrency | mean | P50 | P95 | 成功运行 mean |")
    w("|---:|---:|---:|---:|---:|")
    for key in ordered:
        m = configs[key]
        w(f"| {key} | {sec(m['mean_latency_s'])} | {sec(m['p50_latency_s'])} | "
          f"{sec(m['p95_latency_s'])} | {sec(m['successful_mean_latency_s'])} |")
    w("")

    w("## 5. Speedup 与延迟降幅")
    w("")
    w("Speedup 主口径为 `serial mean / config mean`；另列同 task/repetition 配对均值和"
      "按 task 聚类 bootstrap 的 latency reduction 95% CI。")
    w("")
    w("| concurrency | ratio-of-means speedup | 配对 speedup | latency reduction | reduction 95% CI |")
    w("|---:|---:|---:|---:|---:|")
    for key in ordered:
        m = configs[key]
        ci = m["paired_reduction_ci95"]
        w(f"| {key} | {num(m['speedup_ratio_of_means'])}× | {num(m['paired_speedup_mean'])}× | "
          f"{pct(m['latency_reduction_ratio'])} | [{pct(ci[0])}, {pct(ci[1])}] |")
    w("")

    w("## 6. 失败率、限流与重试")
    w("")
    w("| concurrency | 失败率 | 超时率 | 限流运行率 | retry 次数 | 限流事件 |")
    w("|---:|---:|---:|---:|---:|---:|")
    for key in ordered:
        m = configs[key]
        w(f"| {key} | {pct(1 - m['success_rate'])} | {pct(m['timeout_rate'])} | "
          f"{pct(m['rate_limit_run_rate'])} | {m['retry_count']} | {m['rate_limit_count']} |")
    w("")
    w(f"- `failures.jsonl` 共 {len(failures)} 条（包含预热失败）；正式失败没有从延迟或成功率统计中删除。")
    w("")

    w("## 7. 输出完整性与质量")
    w("")
    w("| concurrency | 完整性 | 依赖顺序 | 期望/实际子任务 |")
    w("|---:|---:|---:|---:|")
    for key in ordered:
        m = configs[key]
        w(f"| {key} | {pct(m['completeness_pass_rate'])} | "
          f"{pct(m['dependency_order_pass_rate'])} | {m['expected_subtasks']}/{m['observed_subtasks']} |")
    valid_plans = sum(bool(x.get("valid")) for x in frozen)
    w("")
    w(f"- 冻结计划有效 {valid_plans}/{len(frozen)}；检查包括分支唯一性、遗漏/重复、非空结果、"
      "summary 全覆盖、calculator observation、RAG 引用与 partially-dependent 跨轮顺序。")
    w("")

    w("## 8. 最佳并发度")
    w("")
    best = metrics["best_concurrency"]
    best_c = str(best["concurrency"])
    if best["safe_parallel_gain"]:
        w(f"按预先锁定的成功率、超时/限流、完整性、成本和 CI 安全门，最佳并发度为 "
          f"**{best_c}**；其平均耗时 {sec(configs[best_c]['mean_latency_s'])}。")
    else:
        w("没有并行配置同时通过全部安全门，最佳并发度保守记为 **1**；不能宣称安全的并行收益。")
    rejected = {k: v for k, v in best.get("rejections", {}).items() if v}
    if rejected:
        w("- 未通过项：" + "；".join(f"c={k}: {', '.join(v)}" for k, v in rejected.items()) + "。")
    w("")

    w("## 9. 可安全写入简历的结论")
    w("")
    if cfg.get("official_complete") and best["safe_parallel_gain"]:
        serial = configs["1"]
        target = configs[best_c]
        w("> 设计基于 LangGraph 的 ReAct / Plan-Execute 双编排引擎，基于 Send 与异步信号量实现"
          f"有界并行；在 20 组冻结计划多子任务评测、每配置 3 次运行中，concurrency={best_c} "
          f"相比串行将平均执行耗时降低 {pct(target['latency_reduction_ratio'])}，P95 从 "
          f"{sec(serial['p95_latency_s'])} 降至 {sec(target['p95_latency_s'])}，输出完整性 "
          f"{pct(target['completeness_pass_rate'])}、成功率 {pct(target['success_rate'])}。")
    elif cfg.get("official_complete"):
        w("> 本次完整评测未发现同时满足稳定性、完整性、成本和置信区间安全门的并行配置；"
          "简历不应写入并行降时比例。")
    else:
        w("> 当前为非正式冒烟结果，不生成简历量化结论。")
    w("")

    w("## 10. 实验局限")
    w("")
    w("- planner 只在冻结阶段真实调用一次；正式 latency 衡量冻结 DAG 从进入生产图到 summary 的"
      "编排端到端耗时，不包含面向用户请求时的实时规划延迟。")
    w("- temperature=0 仍不能消除远端模型、网络、服务端排队与磁盘 prompt cache 的随机性；"
      "轮换顺序与三次重复只能降低而不能消除该影响。")
    w("- 数据集覆盖 calculator 与虚构固定知识库检索，不代表写文件、生图、外部 MCP、网页抓取或"
      "有副作用工具的性能。")
    w("- token 来自 provider usage；若未返回 cache-read 明细，成本按全部 input cache miss 计算，"
      "属于保守上界而非账单金额。")
    w("- 本实验隔离 Send 分支并发收益，动态 replan 机制由生产回归测试覆盖，但不评价其规划质量。")
    w("")

    (out_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    render_report(Path(sys.argv[1]))
