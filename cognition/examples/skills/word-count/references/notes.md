# word-count 输出字段说明（渐进式披露 L3）

`word-count.json` 结构：

| 字段 | 含义 |
| --- | --- |
| `words` | 词数（按任意空白切分后的 token 数） |
| `chars` | 字符数（含空白，Unicode 码点计数） |

说明：中文文本没有空格分词，`words` 会偏小；如需按字符粒度统计中文，请看 `chars`。
这类"更细的用法/边界"正是应该放到 references 里、按需用 `skill_read` 读取的内容，
而不是塞进 L1 目录或 L2 正文，以节省上下文预算。
