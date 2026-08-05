---
name: word-count
description: 统计给定文本的词数与字符数，并生成 JSON 产物
---
# 文本统计技能

统计一段文本的词数（按空白切分）与字符数，把结果写成 JSON 产物登记为可下载 artifact。

## 用法

调用 `script_runner(skill="word-count", script="count.py", script_args={"text": "要统计的文本"})`。

- 标准输出返回一行 `words=.. chars=..` 摘要。
- 产物 `word-count.json` 会经对象存储登记，可经 `/artifacts/{key}` 下载。

更细的输出字段说明见 `references/notes.md`（用 `skill_read` 按需读取，属渐进式披露 L3）。
