# GitHub 抓取 URL 手册（L3 参考）

无 token 的公开 API 速率约 60 次/小时/IP——一次调研控制在 8-12 次抓取内。
配置 `COGNITION_GITHUB_TOKEN` 后升至 5000 次/小时：web_fetch 仅对
api.github.com / raw.githubusercontent.com 自动注入 Authorization，URL 写法零变化。

## 仓库发现（web_search）
- 按主题找仓库：`web_search("site:github.com <主题> <语言/框架>")`
- 已知项目名定位 owner/repo：`web_search("github <项目名>")`
- 找同类替代品：`web_search("site:github.com <项目名> alternative")`
拿到候选后从结果 URL 提取 `{owner}/{repo}`，再按端点速查表逐项抓取。

## 端点速查
| 目的 | URL 模式 |
|---|---|
| 仓库元数据 | `https://api.github.com/repos/{o}/{r}` |
| README 原文 | `https://raw.githubusercontent.com/{o}/{r}/HEAD/README.md`（大小写变体：readme.md、README.rst） |
| 目录列表 | `https://api.github.com/repos/{o}/{r}/contents/{path}`（path 空=根） |
| 单文件原文 | `https://raw.githubusercontent.com/{o}/{r}/HEAD/{path}` |
| 最近提交 | `https://api.github.com/repos/{o}/{r}/commits?per_page=5` |
| 开放 issues | `https://api.github.com/repos/{o}/{r}/issues?state=open&per_page=5` |
| Releases | `https://api.github.com/repos/{o}/{r}/releases?per_page=3` |
| 语言构成 | `https://api.github.com/repos/{o}/{r}/languages` |

## 技术栈判定文件优先级
`package.json`（Node）> `pyproject.toml`/`requirements.txt`（Python）> `go.mod`（Go）
> `Cargo.toml`（Rust）> `pom.xml`/`build.gradle`（JVM）。

## 常见失败与对策
- 404 on README.md → 试 readme.md / README.rst / docs/README.md。
- API 403（限流）→ 首选配置 `COGNITION_GITHUB_TOKEN`（60→5000 次/小时）；
  临时对策：改抓 `https://github.com/{o}/{r}` 网页版（web_fetch 会抽正文），
  或用 `web_search("github <仓库名> <要查的信息>")` 从搜索摘要侧面取证。
- 巨型文件被截断（20k 字符）→ 只结论其可见部分，注明"截断"。
