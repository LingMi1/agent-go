---
name: github-deep-research
description: 深度调研一个 GitHub 仓库（README/结构/关键源码/活跃度），产出研究报告。纯提示词技能，依赖 web_fetch 工具。
---

# GitHub 深度调研

调研一个 GitHub 仓库并产出结构化报告。**全程用 `web_fetch` 抓公开页面**（无需 token；
配置 `COGNITION_GITHUB_TOKEN` 后 web_fetch 对 api.github.com / raw.githubusercontent.com
自动携带认证，限流 60→5000 次/小时，抓取写法不变）。

## 调研流程（按序执行，每步都要真实抓取）

0. **定位仓库**（仅当用户没给出明确的 owner/repo 时）：`web_search("site:github.com <主题关键词>")`
   → 从结果标题/链接确定 `{owner}/{repo}` 再进入后续步骤；仓库已明确则跳过本步。
1. **仓库概览**：`web_fetch("https://api.github.com/repos/{owner}/{repo}")`
   → JSON 含 stars/forks/语言/描述/最近推送时间/开源协议。
2. **README**：`web_fetch("https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md")`
   → 项目定位、用法、架构说明。
3. **目录结构**：`web_fetch("https://api.github.com/repos/{owner}/{repo}/contents/")`
   → 顶层文件/目录 JSON；对关键子目录可再抓 `/contents/{path}`。
4. **关键源码**（按需 2-4 个文件）：
   `web_fetch("https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}")`
   —— 选入口文件/核心模块/配置（package.json、pyproject.toml、go.mod 判断技术栈）。
5. **活跃度**：`web_fetch("https://api.github.com/repos/{owner}/{repo}/commits?per_page=5")`
   → 最近提交时间与主题；`/issues?state=open&per_page=5` 看开放问题。

## 产出要求

调用 `write_report` 产出 markdown 报告，结构：
`定位与解决的问题 → 技术栈与架构（含目录导览）→ 核心实现要点（引用真实源码路径）
→ 活跃度与成熟度评估 → 借鉴点/风险点`。
所有结论必须来自抓取到的真实内容并注明来源 URL；抓取失败的部分如实说明，不要编造。

详细 URL 模式与备选端点见 `references/playbook.md`（用 skill_read 查看）。
