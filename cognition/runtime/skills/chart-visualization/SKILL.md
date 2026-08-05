---
name: chart-visualization
description: 把数据渲染成图表：产出 PNG 图片与 ECharts option JSON（柱状/折线/饼图）。
---

# 图表可视化

把结构化数据渲染成图表。适用于"画个图/可视化一下/对比展示"类请求，
常与 data-analysis 的查询结果配合（先查数，再画图）。

## 用法

```
script_runner(
  skill="chart-visualization",
  script="render.py",
  script_args={
    "type": "bar",                      # bar | line | pie
    "title": "各类别销售额",
    "labels": ["水果", "蔬菜"],          # x 轴/扇区标签
    "series": [{"name": "金额", "data": [30, 5]}]   # 一或多组数据（pie 只取第一组）
  }
)
```

## 产出（双产物）

- `chart.png`：渲染好的图片（产物区可直接预览）。
- `echarts-option.json`：等价的 ECharts option（前端/网页可直接复用）。

数据必须真实来源于对话/分析结果，不要编造数字。
