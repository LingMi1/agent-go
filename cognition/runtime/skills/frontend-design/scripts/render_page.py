"""前端页面落盘脚本：接收完整 HTML 写为 site.html 产物（补最小骨架/校验非空）。

B.1：把 HTML 里对本地图片的引用替换成内联 data-URI——沙箱 iframe 带不了 X-User-Id
拿不到 /artifacts，唯有 data-URI 才能在预览里真正显示图片。两类来源：
- src="generated/NAME"：image_generate 暂存图（SKILL_GENERATED_DIR 只读挂进沙箱）；
- src="NAME"（裸文件名）：用户上传附件，经 script_runner 的 input_files 暂存到
  SKILL_INPUT_DIR（缺陷回归：原图 <img src="1.jpeg"> 相对引用未内联 → 预览中原图空白，
  用户误判任务失败）。找不到的引用原样保留并打印警告，模型可据此自纠（补 input_files）。
"""

import base64
import json
import mimetypes
import os
import re
import sys

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".ico"}


def _inline_local_images(html: str) -> tuple[str, int, int, list[str]]:
    """本地图片引用 → data:URI。返回 (html, 生成图内联数, 上传图内联数, 未命中名单)。"""
    gen_dir = os.environ.get("SKILL_GENERATED_DIR") or ""
    in_dir = os.environ.get("SKILL_INPUT_DIR") or ""
    gen_count = 0
    input_count = 0
    missing: list[str] = []

    def _read(name: str, dirs: list[str]) -> "tuple[bytes, str] | None":
        base = os.path.basename(name)
        for d in dirs:
            if not d or not os.path.isdir(d):
                continue
            path = os.path.join(d, base)
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as f:
                        return f.read(), mimetypes.guess_type(path)[0] or "image/png"
                except OSError:
                    return None
        return None

    def repl(m: "re.Match[str]") -> str:
        nonlocal gen_count, input_count
        quote, ref = m.group(1), m.group(2)
        # 只处理本地图片引用：跳过 data:/http(s)/协议相对/锚点，以及非图片扩展名（如 <script src>）
        if ref.startswith(("data:", "http:", "https:", "//", "#")):
            return m.group(0)
        norm = ref[2:] if ref.startswith("./") else ref
        if os.path.splitext(norm)[1].lower() not in _IMG_EXTS:
            return m.group(0)
        if norm.startswith("generated/"):
            got = _read(norm[len("generated/") :], [gen_dir])
            is_gen = True
        else:
            got = _read(norm, [in_dir, gen_dir])  # 上传图优先 input 目录，生成目录兜底
            is_gen = False
        if got is None:
            missing.append(norm)
            return m.group(0)  # 找不到原样保留（模型可能引了不存在的名）
        data, mime = got
        if is_gen:
            gen_count += 1
        else:
            input_count += 1
        b64 = base64.b64encode(data).decode("ascii")
        return f"src={quote}data:{mime};base64,{b64}{quote}"

    new_html = re.sub(r"""src=(["'])([^"']+)\1""", repl, html)
    return new_html, gen_count, input_count, missing


def main() -> None:
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    title = str(args.get("title") or "页面")
    html = str(args.get("html") or "")
    if not html.strip():
        print("渲染失败: html 不能为空（需传完整 HTML 文档或片段）")
        sys.exit(1)
    # 片段则补最小骨架（保证 iframe 可独立渲染）。
    if "<html" not in html.lower():
        html = (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{title}</title>"
            "</head><body>" + html + "</body></html>"
        )
    html, gen_inlined, input_inlined, missing = _inline_local_images(html)
    out_dir = os.environ.get("SKILL_OUTPUT_DIR", ".")
    path = os.path.join(out_dir, "site.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    extra = f"，内联生成图 {gen_inlined} 张" if gen_inlined else ""
    extra += f"，内联上传图 {input_inlined} 张" if input_inlined else ""
    print(f"已生成 site.html（{os.path.getsize(path)} 字节，标题：{title}{extra}）")
    if missing:
        names = ", ".join(sorted(set(missing))[:5])
        print(
            f"警告: {len(missing)} 个本地图片引用未找到、未内联（预览中将显示空白）: {names}。"
            "上传图需在 script_runner 调用时传 input_files=[\"文件名\"] 后用 <img src=\"文件名\"> 引用；"
            "生成图用 <img src=\"generated/image-N.png\">。请修正后重新渲染。"
        )


if __name__ == "__main__":
    main()
