"""DuckDB 数据分析脚本（skill 契约：argv[1]=JSON 入参，产物写 $SKILL_OUTPUT_DIR）。

输入文件由 runner 预下载到 $SKILL_INPUT_DIR（input_files 白名单机制）；
每个文件注册为视图（文件名去扩展名），summary 概览或 query 执行 SQL。
"""

import json
import os
import re
import sys


def fail(msg: str) -> None:
    print(f"分析失败: {msg}")
    sys.exit(1)


def view_name(file_name: str) -> str:
    stem = os.path.splitext(os.path.basename(file_name))[0]
    return re.sub(r"[^\w一-鿿]", "_", stem) or "t"


def main() -> None:
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    files = list(args.get("files") or [])
    mode = str(args.get("mode") or "summary")
    sql = str(args.get("sql") or "")

    in_dir = os.environ.get("SKILL_INPUT_DIR", "")
    out_dir = os.environ.get("SKILL_OUTPUT_DIR", ".")
    if not files:
        fail("script_args.files 为空（同时也要在 input_files 里列出文件名）")
    if not in_dir:
        fail("没有输入文件目录——调用时必须带 input_files=[文件名...]")

    import duckdb

    con = duckdb.connect()
    report: list[str] = ["# 数据分析报告", ""]
    for name in files:
        path = os.path.join(in_dir, os.path.basename(name))
        if not os.path.exists(path):
            fail(f"输入目录中找不到 {name}（input_files 是否包含它？）")
        vn = view_name(name)
        lower = name.lower()
        # DDL 不支持预编译参数；path 来自 runner 自建临时目录，单引号转义后内联安全。
        p_sql = path.replace("'", "''")
        if lower.endswith(".csv"):
            con.execute(f"CREATE VIEW \"{vn}\" AS SELECT * FROM read_csv_auto('{p_sql}')")
        elif lower.endswith((".json", ".ndjson", ".jsonl")):
            con.execute(f"CREATE VIEW \"{vn}\" AS SELECT * FROM read_json_auto('{p_sql}')")
        else:
            fail(f"不支持的文件类型: {name}（仅 CSV/JSON）")
        report.append(f"## {name}（视图 `{vn}`）")
        rows = con.execute(f'SELECT COUNT(*) FROM "{vn}"').fetchone()[0]
        cols = con.execute(f'DESCRIBE "{vn}"').fetchall()
        report.append(f"- 行数: {rows}")
        report.append("- 列: " + ", ".join(f"{c[0]}({c[1]})" for c in cols))
        # 数值列统计
        nums = [c[0] for c in cols if any(t in c[1].upper() for t in ("INT", "DOUBLE", "FLOAT", "DECIMAL"))]
        for col in nums[:8]:
            mn, mx, avg = con.execute(
                f'SELECT MIN("{col}"), MAX("{col}"), AVG("{col}") FROM "{vn}"'
            ).fetchone()
            report.append(f"  - {col}: min={mn} max={mx} avg={round(avg, 4) if avg is not None else None}")
        sample = con.execute(f'SELECT * FROM "{vn}" LIMIT 5').fetchall()
        header = [c[0] for c in cols]
        report.append("")
        report.append("| " + " | ".join(header) + " |")
        report.append("|" + "---|" * len(header))
        for r in sample:
            report.append("| " + " | ".join(str(x) for x in r) + " |")
        report.append("")

    summary_line = f"已分析 {len(files)} 个文件"
    if mode == "query":
        if not sql:
            fail("mode=query 需要 script_args.sql")
        if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
            fail("仅支持 SELECT/WITH 查询")
        res = con.execute(sql)
        header = [d[0] for d in res.description]
        rows = res.fetchall()
        import csv

        with open(os.path.join(out_dir, "result.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        report += ["## 查询", "", "```sql", sql, "```", "", f"结果 {len(rows)} 行（见 result.csv）。前 10 行：", ""]
        report.append("| " + " | ".join(header) + " |")
        report.append("|" + "---|" * len(header))
        for r in rows[:10]:
            report.append("| " + " | ".join(str(x) for x in r) + " |")
        summary_line += f"；SQL 返回 {len(rows)} 行"

    with open(os.path.join(out_dir, "analysis.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(summary_line + "，报告 analysis.md 已产出。")


if __name__ == "__main__":
    main()
