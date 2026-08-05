"""图表渲染脚本：matplotlib(Agg) 产 PNG + 等价 ECharts option JSON（双产物）。"""

import json
import os
import sys

import matplotlib

matplotlib.use("Agg")  # 必须在 pyplot 导入前：无显示环境/容器内唯一可用后端
import matplotlib.pyplot as plt  # noqa: E402

# 中文字体尽力而为：常见平台字体逐个尝试，找不到则字符缺失但不失败。
matplotlib.rcParams["font.sans-serif"] = [
    "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei", "sans-serif",
]
matplotlib.rcParams["axes.unicode_minus"] = False


def main() -> None:
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    ctype = str(args.get("type") or "bar")
    title = str(args.get("title") or "图表")
    labels = [str(x) for x in (args.get("labels") or [])]
    series = list(args.get("series") or [])
    if not labels or not series:
        print("渲染失败: labels/series 不能为空")
        sys.exit(1)

    out_dir = os.environ.get("SKILL_OUTPUT_DIR", ".")
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    if ctype == "pie":
        data = [float(x) for x in series[0].get("data", [])]
        ax.pie(data, labels=labels, autopct="%1.1f%%")
    elif ctype == "line":
        for s in series:
            ax.plot(labels, [float(x) for x in s.get("data", [])], marker="o", label=str(s.get("name", "")))
        ax.legend()
    else:  # bar
        width = 0.8 / max(len(series), 1)
        for i, s in enumerate(series):
            xs = [j + i * width for j in range(len(labels))]
            ax.bar(xs, [float(x) for x in s.get("data", [])], width=width, label=str(s.get("name", "")))
        ax.set_xticks([j + width * (len(series) - 1) / 2 for j in range(len(labels))])
        ax.set_xticklabels(labels)
        ax.legend()
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "chart.png"))

    # 等价 ECharts option（前端可直接复用）。
    if ctype == "pie":
        option = {
            "title": {"text": title},
            "series": [{"type": "pie", "data": [
                {"name": n, "value": v} for n, v in zip(labels, series[0].get("data", []))
            ]}],
        }
    else:
        option = {
            "title": {"text": title},
            "xAxis": {"type": "category", "data": labels},
            "yAxis": {"type": "value"},
            "legend": {},
            "series": [
                {"type": ctype, "name": s.get("name", ""), "data": s.get("data", [])} for s in series
            ],
        }
    with open(os.path.join(out_dir, "echarts-option.json"), "w", encoding="utf-8") as f:
        json.dump(option, f, ensure_ascii=False, indent=2)
    print(f"已渲染 {ctype} 图（{len(labels)} 个类别，{len(series)} 组数据）：chart.png + echarts-option.json")


if __name__ == "__main__":
    main()
