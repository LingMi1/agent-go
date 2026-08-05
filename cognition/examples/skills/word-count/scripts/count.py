"""统计文本词数/字符数，把结果写入 SKILL_OUTPUT_DIR/word-count.json。

约定（与脚本运行器一致）：
- args 作为 argv[1]（JSON）传入；
- 产物写到环境变量 SKILL_OUTPUT_DIR 指向的目录，由运行器扫描并登记为 artifact。
"""

import json
import os
import sys

args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
text = str(args.get("text", ""))
result = {"words": len(text.split()), "chars": len(text)}

out_dir = os.environ.get("SKILL_OUTPUT_DIR", ".")
with open(os.path.join(out_dir, "word-count.json"), "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False)

print(f"words={result['words']} chars={result['chars']}")
