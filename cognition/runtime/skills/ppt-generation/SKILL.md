---
name: ppt-generation
description: 生成可下载的演示文稿（.pptx）或独立 HTML 文档（Markdown 转网页）。
---

# 文稿生成（PPT / HTML 文档）

把整理好的内容产出为可交付文件。两个脚本按需选：

## 1. PPT（build_pptx.py）

```
script_runner(
  skill="ppt-generation",
  script="build_pptx.py",
  script_args={
    "title": "季度业务汇报",
    "subtitle": "2026 Q2",
    "slides": [
      {"title": "市场概览", "bullets": ["要点一", "要点二"]},
      {"title": "数据结论", "bullets": ["同比 +12%", "环比 +3%"]}
    ]
  }
)
```
产出 `presentation.pptx`。每页 3-6 个要点为宜，内容须来自对话中已确认的信息。

## 2. HTML 文档（md_to_html.py）

```
script_runner(
  skill="ppt-generation",
  script="md_to_html.py",
  script_args={"title": "调研报告", "markdown": "# 标题\n\n正文……"}
)
```
产出 `document.html`（自包含样式的独立网页，浏览器可直接打开/打印）。
