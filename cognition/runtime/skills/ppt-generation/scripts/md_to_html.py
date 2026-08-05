"""Markdown → 自包含 HTML 文档（零第三方依赖的轻量转换，覆盖常用语法）。"""

import html
import json
import os
import re
import sys


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    in_code = False
    in_list = False
    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
            else:
                out.append("<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        if in_list and not re.match(r"^\s*[-*] ", line):
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        if not line.strip():
            out.append("")
            continue
        out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _inline(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    return t


_TEMPLATE = """<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ max-width: 860px; margin: 40px auto; padding: 0 20px; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; line-height: 1.7; color: #1f2328; }}
h1, h2, h3 {{ border-bottom: 1px solid #eee; padding-bottom: .3em; }}
pre {{ background: #f6f8fa; padding: 12px; border-radius: 8px; overflow-x: auto; }}
code {{ background: #f6f8fa; padding: 1px 4px; border-radius: 4px; }}
</style>
<h1>{title}</h1>
{body}
"""


def main() -> None:
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    title = str(args.get("title") or "文档")
    md = str(args.get("markdown") or "")
    if not md.strip():
        print("生成失败: markdown 不能为空")
        sys.exit(1)
    doc = _TEMPLATE.format(title=html.escape(title), body=md_to_html(md))
    out_dir = os.environ.get("SKILL_OUTPUT_DIR", ".")
    with open(os.path.join(out_dir, "document.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"已生成 document.html（{len(doc)} 字符）")


if __name__ == "__main__":
    main()
