---
name: data-analysis
description: 用 DuckDB 分析用户上传的 CSV/JSON 数据文件（概览统计或执行 SQL 查询），产出结果文件。
---

# 数据分析（DuckDB）

分析**用户本轮上传的数据文件**（CSV/JSON）。上传的文件名见消息中的〔用户上传附件〕注记。

## 用法

调用 `script_runner`：

```
script_runner(
  skill="data-analysis",
  script="analyze.py",
  input_files=["sales.csv"],                    # 用户上传附件的文件名（必填）
  script_args={
    "files": ["sales.csv"],                     # 参与分析的文件（同 input_files）
    "mode": "summary",                          # summary=概览（默认）| query=执行 SQL
    "sql": "SELECT 类别, SUM(金额) FROM sales GROUP BY 类别"   # mode=query 时必填
  }
)
```

- **视图名 = 文件名去扩展名**（`sales.csv` → 表 `sales`；含特殊字符时用双引号包裹）。
- `mode=summary`：每个文件输出行数、列名/类型、数值列统计（min/max/avg）、前 5 行样例。
- `mode=query`：执行任意 DuckDB SQL（SELECT），结果写 `result.csv`；同时产出 `analysis.md` 报告。
- 仅支持 CSV/JSON；Excel 请让用户转存 CSV 后再传。

## 产出

- `analysis.md`：可读的分析报告（自动登记为可下载产物）。
- `result.csv`：query 模式的结果数据。

先跑 `summary` 了解数据结构，再写 SQL——不要凭空猜列名。
