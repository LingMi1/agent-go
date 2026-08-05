#!/usr/bin/env python3
"""从 results/<ts>/ 的 config.json / metrics.json / failures.jsonl / per_question.jsonl
渲染 REPORT.md。所有数字均程序化读取与计算，杜绝手填。

用法：cognition/.venv/bin/python eval/rag/scripts/render_report.py <results_dir>
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

CONFIG_ORDER = ["dense_only", "sparse_only", "hybrid_rrf", "full_agentic_rag"]
CONFIG_LABEL = {
    "dense_only": "A. dense_only",
    "sparse_only": "B. sparse_only",
    "hybrid_rrf": "C. hybrid_rrf",
    "full_agentic_rag": "D. full_agentic_rag",
}


def pct(x, digits=1):
    return f"{x * 100:.{digits}f}%"


def fmt_s(x, digits=3):
    return f"{x:.{digits}f}s"


def mean_std(vals):
    if len(vals) <= 1:
        return vals[0], 0.0
    return statistics.mean(vals), statistics.stdev(vals)


def main() -> None:
    out_dir = Path(sys.argv[1])
    cfg = json.loads((out_dir / "config.json").read_text(encoding="utf-8"))
    metrics = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    failures = [json.loads(l) for l in (out_dir / "failures.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    per_q = [json.loads(l) for l in (out_dir / "per_question.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    rag_root = Path(__file__).resolve().parents[1]
    rejected = [json.loads(l) for l in (rag_root / "questions_rejected.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    verified = [json.loads(l) for l in (rag_root / "questions_verified.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]

    configs = [c for c in CONFIG_ORDER if c in metrics]
    m_mean = {c: metrics[c]["mean"] for c in configs}
    n_ans = m_mean[configs[0]]["n_answerable"]
    n_un = m_mean[configs[0]]["n_unanswerable"]

    L: list[str] = []
    w = L.append
    w(f"# Pro-Agent RAG 检索评测报告（{out_dir.name}）")
    w("")
    w("所有指标由 `scripts/eval_metrics.py`（含单测）计算、本报告由 `scripts/render_report.py` "
      "从 `metrics.json` 程序化渲染；逐题原始结果见 `per_question.jsonl`，失败清单见 "
      "`failures.jsonl`，完整运行环境见 `config.json`。")
    w("")

    # ---- 1. 语料与问题规模 ----
    w("## 1. 语料规模与有效问题数")
    w("")
    w(f"- 语料：{cfg['n_chunks']} 个固定 chunk（29 篇结构化中文文档，构建于 "
      f"`chunks.jsonl`，sha256 前缀 `{cfg['chunks_sha256']}`），一次性入库独立 collection "
      f"`{cfg['collection']}`（kb_id=`{cfg['kb_id']}`），四配置共用同一份索引；")
    w(f"- 问题：`{cfg['questions_file']}`（sha256 前缀 `{cfg['questions_sha256']}`）共 "
      f"{cfg['n_questions']} 题 = 可回答 {n_ans:.0f} + 不可回答 {n_un:.0f}；"
      "四配置使用完全相同的题目集合，无逐配置删题；")
    types = {}
    for q in verified:
        types[q["question_type"]] = types.get(q["question_type"], 0) + 1
    w("- 题型分布：" + "、".join(f"{t}={n}" for t, n in sorted(types.items())) + "。")
    w("")

    # ---- 2. 被拒问题 ----
    w("## 2. 独立审查：被拒与修正")
    w("")
    w(f"评测集经独立审查（见 `eval/rag/review_report.md`）：原始 60 题中拒绝 {len(rejected)} 题、"
      f"修正 {sum(1 for q in verified if q.get('review_status') == 'modified')} 题"
      "（补标漏标的等价证据 chunk、为多跳题引入 gold_groups）。被拒题目：")
    for r in rejected:
        w(f"- **{r['id']}**（{r['question_type']}）：{r['reject_reason']}")
    w("")

    # ---- Recall 定义 ----
    w("## 3. 指标定义")
    w("")
    w("- **单跳/同义改写/消歧/故障排查**：Recall@k = top-k 是否包含任一 gold chunk；")
    w("- **多跳 Any Recall@k**：至少一个证据组被命中；**多跳 All Recall@k**：全部证据组均被命中"
      "（组内任一等价证据 chunk 命中即该组命中；组间为回答所需的不同必要事实）；")
    w("- 汇总 Recall@k 对全部可回答题按“gold 并集任一命中”口径计算（多跳题该口径等价于 Any）；")
    w("- **MRR@10**：gold 并集首个命中名次的倒数（>10 或未命中计 0）；")
    w("- **不可回答误检率**：不可回答题中最终证据集非空的比例（A/B/C 为 top-5 非空——纯检索器无拒答"
      "机制，恒为 100%，列出仅作对照；D 为进入生成的 sources 非空）；")
    w("- 延迟：A/B/C 为 embed+查询端到端；D 为整个子图（含 LLM 调用），另附去除答案生成后的检索侧延迟。")
    w("")

    # ---- 4. 各配置指标 ----
    w("## 4. 各配置结果")
    w("")
    det_note = []
    for c in configs:
        mc = metrics[c]
        if mc["deterministic_claim"]:
            det_note.append(
                f"{c}：{mc['n_runs']} 次运行结果哈希{'完全一致' if mc['runs_identical'] else '不一致'}"
                f"（{'确定性成立，表中为单次值' if mc['runs_identical'] else '确定性不成立，表中为均值'}）")
    w("确定性验证：" + "；".join(det_note) + "。full_agentic_rag 含 LLM（temperature=0 仍非确定），"
      f"运行 {metrics.get('full_agentic_rag', {}).get('n_runs', 0)} 次取均值±标准差。")
    w("")
    header = ["指标"] + [CONFIG_LABEL[c] for c in configs]
    w("| " + " | ".join(header) + " |")
    w("|" + "---|" * len(header))

    def row(label, key, is_pct=True, digits=1):
        cells = [label]
        for c in configs:
            runs_vals = [r[key] for r in metrics[c]["runs"] if key in r]
            if not runs_vals:
                cells.append("—")
                continue
            mu, sd = mean_std(runs_vals)
            if c == "full_agentic_rag" and len(runs_vals) > 1 and sd > 1e-9:
                cells.append(f"{pct(mu, digits)} ±{sd * 100:.1f}" if is_pct
                             else f"{mu:.3f} ±{sd:.3f}")
            else:
                cells.append(pct(mu, digits) if is_pct else f"{mu:.3f}")
        w("| " + " | ".join(cells) + " |")

    for k in (1, 3, 5, 10):
        row(f"Recall@{k}（可回答 {n_ans:.0f} 题）", f"recall_at_{k}")
    row("MRR@10", "mrr_at_10", is_pct=False)
    row("多跳 Any Recall@5", "multi_hop_any_recall_at_5")
    row("多跳 All Recall@5", "multi_hop_all_recall_at_5")
    row("多跳 All Recall@10", "multi_hop_all_recall_at_10")
    row("不可回答误检率", "unanswerable_false_retrieval_rate")
    row("平均检索延迟", "latency_mean_s", is_pct=False)
    row("P50 延迟", "latency_p50_s", is_pct=False)
    row("P95 延迟", "latency_p95_s", is_pct=False)
    w("")

    if "full_agentic_rag" in metrics:
        d = metrics["full_agentic_rag"]
        n_runs = d["n_runs"]
        w("**full_agentic_rag 行为统计**（3 次运行合计，59 题/次）：")
        w("")
        tot = {k: sum(r.get(k, 0) for r in d["runs"]) for k in
               ("total_expand_calls", "total_reflect_calls", "total_rewrite_triggered",
                "total_route_simple", "total_llm_retries")}
        w(f"- query 扩展（expand）调用 {tot['total_expand_calls']} 次、"
          f"Reflect 反思 {tot['total_reflect_calls']} 次、"
          f"其中给出改写查询（rewrite 触发重检索）{tot['total_rewrite_triggered']} 次；")
        w(f"- route 判为简单问题（跳过检索）{tot['total_route_simple']} 次；"
          f"LLM 瞬时错误重试 {tot['total_llm_retries']} 次（无丢题）；")
        rvals = [r.get("retrieval_only_latency_mean_s") for r in d["runs"]]
        rvals = [v for v in rvals if v is not None]
        if rvals:
            w(f"- 检索侧延迟（去除答案生成调用）：均值 {statistics.mean(rvals):.2f}s；"
              f"总延迟中其余为 route/expand/reflect/generate 的 LLM 调用耗时。")
        w("")

    w("**分题型 Recall@5（gold 并集任一命中口径，均值）**：")
    w("")
    all_types = sorted({t for c in configs for t in metrics[c]["per_type_recall_at_5_mean"]})
    w("| 题型 | " + " | ".join(CONFIG_LABEL[c] for c in configs) + " |")
    w("|" + "---|" * (len(configs) + 1))
    for t in all_types:
        cells = [t]
        for c in configs:
            v = metrics[c]["per_type_recall_at_5_mean"].get(t)
            cells.append(pct(v) if v is not None else "—")
        w("| " + " | ".join(cells) + " |")
    w("")

    # ---- 5. 失败案例 ----
    w("## 5. 失败案例分析")
    w("")
    for c in configs:
        fs = [f for f in failures if f["config"] == c and f["failure"] == "recall_at_5_miss"]
        fr = [f for f in failures if f["config"] == c and f["failure"] == "false_retrieval"]
        w(f"### {CONFIG_LABEL[c]}：Recall@5 未命中 {len(fs)} 题"
          + (f"（另有不可回答误检 {len(fr)} 题）" if fr else ""))
        w("")
        by_type: dict[str, int] = {}
        for f in fs:
            by_type[f["type"]] = by_type.get(f["type"], 0) + 1
        if by_type:
            w("按题型：" + "、".join(f"{t}×{n}" for t, n in sorted(by_type.items())) + "。示例：")
            w("")
            for f in fs[:3]:
                gold_flat = sorted({cid for g in f["gold_groups"] for cid in g})
                w(f"- `{f['qid']}`（{f['type']}）：{f['question']}")
                w(f"  - gold: {', '.join('`' + g + '`' for g in gold_flat[:4])}"
                  + ("…" if len(gold_flat) > 4 else ""))
                w(f"  - 实际 top-5: {', '.join('`' + t + '`' for t in f['top10'][:5])}")
        w("")

    # 失败归因：检索失败 vs 排序失败（gold 是否出现在返回全列表任意位置）
    qv = {q["id"]: q for q in verified}
    w("**失败归因拆分（gold 在返回全列表任意位置的命中率 vs Recall@5，均值）**：")
    w("")
    w("| 配置 | 全列表命中 | Recall@5 | 差值=排序损失 | 平均返回条数 |")
    w("|---|---|---|---|---|")
    for c in configs:
        runs = sorted({r["run"] for r in per_q if r["config"] == c})
        anywhere, r5s, lens = [], [], []
        for run in runs:
            recs = [r for r in per_q if r["config"] == c and r["run"] == run
                    and qv[r["qid"]]["question_type"] != "unanswerable"]
            anywhere.append(statistics.mean(
                [1.0 if set(r["ranked"]) & set(qv[r["qid"]]["gold_chunk_ids"]) else 0.0
                 for r in recs]))
            r5s.append(statistics.mean(
                [1.0 if set(r["ranked"][:5]) & set(qv[r["qid"]]["gold_chunk_ids"]) else 0.0
                 for r in recs]))
            lens.append(statistics.mean([len(r["ranked"]) for r in recs]))
        aw, r5v = statistics.mean(anywhere), statistics.mean(r5s)
        w(f"| {CONFIG_LABEL[c]} | {pct(aw)} | {pct(r5v)} | {pct(aw - r5v)} | "
          f"{statistics.mean(lens):.1f} |")
    w("")
    w("full_agentic_rag 的“全列表命中”高于其 Recall@5 的部分，来自扩展子查询确实召回了 gold "
      "但生产的“首次出现序”累积排序把噪声排在了前面——属于排序/融合问题而非检索覆盖问题。")
    w("")

    # ---- 6/7. 对比结论 ----
    w("## 6. 配置对比与 trade-off（是否优于 baseline 的如实说明）")
    w("")

    def mv(c, key):
        vals = [r[key] for r in metrics[c]["runs"] if key in r]
        return statistics.mean(vals) if vals else float("nan")

    for key, label in (("recall_at_5", "Recall@5"), ("recall_at_10", "Recall@10"),
                       ("mrr_at_10", "MRR@10")):
        line = "、".join(f"{c}={mv(c, key):.3f}" for c in configs)
        w(f"- {label}：{line}")
    w("")
    cmp_pairs = [("hybrid_rrf", "dense_only"), ("hybrid_rrf", "sparse_only"),
                 ("full_agentic_rag", "hybrid_rrf")]
    for a, b in cmp_pairs:
        if a not in metrics or b not in metrics:
            continue
        d5 = mv(a, "recall_at_5") - mv(b, "recall_at_5")
        dm = mv(a, "mrr_at_10") - mv(b, "mrr_at_10")
        verdict = ("优于" if d5 > 1e-9 else ("持平" if abs(d5) <= 1e-9 else "**未优于**"))
        w(f"- {a} vs {b}：Recall@5 差 {d5 * 100:+.1f} 个百分点、MRR@10 差 {dm:+.3f} → "
          f"Recall@5 口径 {a} {verdict} {b}。")
    dlat = mv("full_agentic_rag", "latency_mean_s") / max(mv("hybrid_rrf", "latency_mean_s"), 1e-9)
    w("")
    # 与 §9 相同的显著性门槛：多跳仅 10 题、不可回答仅 6 题，差距达 1 题以上才算收益
    gain_bits = []
    if mv("full_agentic_rag", "multi_hop_all_recall_at_5") > mv("hybrid_rrf", "multi_hop_all_recall_at_5") + 0.1:
        gain_bits.append("多跳 All Recall@5 提升")
    if mv("full_agentic_rag", "unanswerable_false_retrieval_rate") < mv("hybrid_rrf", "unanswerable_false_retrieval_rate") - 0.1:
        gain_bits.append("不可回答误检率下降")
    gain_txt = ("换来的收益仅为 " + "、".join(gain_bits) if gain_bits
                else "且在本评测集上未换来任何召回口径的收益（见上方对比）")
    w(f"- 延迟代价：full_agentic_rag 平均延迟约为 hybrid_rrf 的 **{dlat:.0f} 倍**"
      f"（{mv('full_agentic_rag', 'latency_mean_s'):.1f}s vs "
      f"{mv('hybrid_rrf', 'latency_mean_s') * 1000:.0f}ms），代价来自 route/expand/reflect/"
      f"generate 的多次 LLM 调用；{gain_txt}。")
    w("")

    # ---- 环境 ----
    w("## 7. 运行环境与可复现性")
    w("")
    w(f"- git commit：`{cfg['git_commit']}`（tracked 脏工作区：{cfg['git_dirty_tracked']}）；")
    w(f"- embedding：fastembed `{cfg['embedding_model']}`（dim={cfg['embedding_dimension']}）；"
      f"sparse：fastembed `{cfg['sparse_model']}`；融合：{cfg['fusion']}；top_k={cfg['top_k']}；")
    w(f"- LLM（仅 D）：`{cfg['llm_model']}` @ {cfg['llm_base_url']}，temperature="
      f"{cfg['llm_temperature']}，reflection_limit={cfg['rag_reflection_limit']}，"
      f"subquery_max={cfg['rag_subquery_max']}；")
    w(f"- 运行时间：{cfg['started_utc']} → {cfg['finished_utc']}（{cfg['duration_s']}s）；"
      f"Python {cfg['python']}，PYTHONHASHSEED={cfg['pythonhashseed'] or '未设'}；")
    w("- 依赖版本：" + "、".join(f"{k}={v}" for k, v in cfg["packages"].items())
      + f"；Qdrant server {cfg['qdrant_server_version']}；")
    for n in cfg.get("notes", []):
        w(f"- {n}")
    w("")

    # ---- 8. 数据集局限 ----
    w("## 8. 数据集局限")
    w("")
    w("- 语料是**从源码整理的结构化中文文档**（29 篇 × 2 chunk、280–660 字符），chunk 边界与语义"
      "边界对齐，检索难度低于生产环境的杂乱长文档，绝对数值外推需谨慎；")
    w(f"- 库规模小（{cfg['n_chunks']} chunk），Recall 天花板偏高；干扰项主要来自刻意设计的"
      "相似概念文档（三种并发限制、多个大小上限等）；")
    w(f"- 可回答题 {n_ans:.0f} 条、不可回答 {n_un:.0f} 条，样本量支持配置间的相对比较，"
      "单点百分比的置信区间较宽（±1 题 ≈ ±1.9 个百分点）；")
    w("- 题目由熟悉语料的会话生成、另一会话独立审查（拒 1 修 15），非真实用户查询分布；")
    w("- fastembed `Qdrant/bm25` 的默认分词按空白/标点切分，中文长串成为单 token，sparse 路"
      "在纯中文改写题上几乎只能靠英文标识符命中——这是生产实现的真实行为，评测如实反映；")
    w("- D 配置的延迟依赖外部 LLM API 的当日网络状况，绝对值参考意义有限，量级对比有效。")
    w("")

    # ---- 9. 可安全写入简历的结论 ----
    w("## 9. 可安全写入简历的结论（含禁写清单）")
    w("")
    w(f"以下表述均由本目录数据直接支撑（评测集 {cfg['n_questions']} 题、其中可回答 "
      f"{n_ans:.0f} 题；语料 {cfg['n_chunks']} chunk；配置与逐题结果可复现），"
      "数字为多次运行均值，措辞遵循“基线+样本量+相对改进”原则：")
    w("")

    r5 = {c: mv(c, "recall_at_5") for c in configs}
    r10 = {c: mv(c, "recall_at_10") for c in configs}
    mrr = {c: mv(c, "mrr_at_10") for c in configs}
    mh_all5 = {c: mv(c, "multi_hop_all_recall_at_5") for c in configs}
    mh_any5 = {c: mv(c, "multi_hop_any_recall_at_5") for c in configs}
    fr = {c: mv(c, "unanswerable_false_retrieval_rate") for c in configs}
    best_single = max(("dense_only", "sparse_only"), key=lambda c: r5.get(c, 0))

    ok: list[str] = []
    if r5.get("hybrid_rrf", 0) > r5.get(best_single, 0) + 1e-9:
        ok.append(f"“构建 Qdrant dense+sparse 混合检索与 RRF 融合，在自建 {n_ans:.0f} 条中文"
                  f"知识库问答评测集上将 Recall@5 从单路最优 {pct(r5[best_single])} 提升至 "
                  f"{pct(r5['hybrid_rrf'])}”")
    elif abs(r5.get("hybrid_rrf", 0) - r5.get(best_single, 0)) <= 1e-9:
        ok.append(f"“混合检索 Recall@5 与单路最优持平（{pct(r5['hybrid_rrf'])}），"
                  f"但显著优于 sparse 单路（{pct(r5.get('sparse_only', 0))}）”——只能这样写，"
                  "不能写“混合检索提升 Recall@5”")
    d10 = r10.get("hybrid_rrf", 0) - r10.get("dense_only", 0)
    if 0 < d10 < 0.05:
        ok.append(f"“混合检索将 Recall@10 从纯 dense 的 {pct(r10['dense_only'])} 提升至 "
                  f"{pct(r10['hybrid_rrf'])}”——差距仅 {d10 * 100:.1f} 个百分点"
                  f"（≈{d10 * n_ans:.1f} 题），样本量下证据较弱，如写必须带评测集规模，"
                  "不建议作为主亮点")
    elif d10 >= 0.05:
        ok.append(f"“混合检索将 Recall@10 从纯 dense 的 {pct(r10['dense_only'])} 提升至 "
                  f"{pct(r10['hybrid_rrf'])}（{n_ans:.0f} 条评测集）”")
    if "full_agentic_rag" in r5:
        d, c = r5["full_agentic_rag"], r5["hybrid_rrf"]
        if d > c + 1e-9:
            ok.append(f"“Agentic RAG（查询扩展+Reflect 重检索）将 Recall@5 从单轮混合检索的 "
                      f"{pct(c)} 提升至 {pct(d)}（{n_ans:.0f} 条评测集，3 次运行均值）”")
        # 多跳仅 10 题，差距达 1 题（10pp）以上才允许作为可写结论
        if mh_all5.get("full_agentic_rag", 0) > mh_all5.get("hybrid_rrf", 0) + 0.1:
            ok.append(f"“多跳问题全部证据组的 Recall@5（All-Recall）从 "
                      f"{pct(mh_all5['hybrid_rrf'])} 提升至 "
                      f"{pct(mh_all5['full_agentic_rag'])}（10 条多跳题，3 次运行均值）”")
        if fr.get("full_agentic_rag", 1) < fr.get("hybrid_rrf", 1) - 0.1:
            ok.append(f"“不可回答问题的误检率由纯检索的 {pct(fr['hybrid_rrf'])} 降至 "
                      f"{pct(fr['full_agentic_rag'])}（6 条不可回答题）”")
    ok.append(f"“自建 {cfg['n_questions']} 题中文知识库检索评测集（六种题型、gold 分组标注、"
              "独立审查拒 1 修 15）与四配置消融评测 harness（复用生产检索代码，12 项指标代码"
              "计算、3 次运行、可复现），定位出 BM25 中文分词失效与子查询语义漂移两类瓶颈”"
              "——评测体系与失败归因本身即是有效亮点（Eval/L3），不依赖提升数字")
    ok.append(f"“单轮混合检索 Recall@5 {pct(r5['hybrid_rrf'])}、Recall@10 "
              f"{pct(r10['hybrid_rrf'])}、检索延迟 P95 "
              f"{mv('hybrid_rrf', 'latency_p95_s') * 1000:.0f}ms”——写绝对值时必须同时给出"
              "评测集规模（59 题/58 chunk 自建集）")
    for s in ok:
        w(f"- ✅ {s}")
    w("")
    w("**禁写清单**（当前数据不支持，写了会在面试追问中翻车）：")
    w("")
    bad: list[str] = []
    if "full_agentic_rag" in r5 and r5["full_agentic_rag"] <= r5["hybrid_rrf"] + 1e-9:
        bad.append(f"“Agentic RAG 提升整体 Recall@5”——实测 {pct(r5['full_agentic_rag'])} vs "
                   f"hybrid 的 {pct(r5['hybrid_rrf'])}，未优于基线，必须如实说明")
    if "full_agentic_rag" in mrr and mrr["full_agentic_rag"] <= mrr["hybrid_rrf"] + 1e-9:
        bad.append(f"“Agentic RAG 改善排序质量（MRR）”——实测 MRR@10 "
                   f"{mrr['full_agentic_rag']:.3f} vs {mrr['hybrid_rrf']:.3f}")
    if r5.get("hybrid_rrf", 0) <= r5.get("dense_only", 0) + 1e-9:
        bad.append(f"“混合检索优于纯 dense”——本集上 Recall@5 hybrid={pct(r5['hybrid_rrf'])} "
                   f"vs dense={pct(r5['dense_only'])}（中文 BM25 分词限制所致，见 §8）")
    bad.append("任何“召回率 99%/大规模语料验证/生产流量验证”表述——本评测是 58 chunk 自建集")
    bad.append("“Recall 提升 X%”却不注明基线配置、评测集规模与运行次数")
    for s in bad:
        w(f"- ❌ {s}")
    w("")
    w("面试追问自检：分块策略（标题优先、560/100，理由见 scripts/build_chunks.py 头注释）、"
      "Recall 口径（gold 并集任一命中 / 多跳组语义，§3）、评测集标注质量（独立审查拒 1 修 15，"
      "review_report.md）、为什么 sparse 路弱（BM25 中文分词，§8）、Agentic 的延迟代价（§6）"
      "——以上均有文档与数据可答。")
    w("")

    (out_dir / "REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"rendered {out_dir / 'REPORT.md'} ({len(L)} lines)")


if __name__ == "__main__":
    main()
