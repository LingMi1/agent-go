---
name: frontend-design
description: 生成单文件自包含的现代网页（落地页/看板/表单/原型），产出 site.html 可在产物区实时预览。
---

# 前端页面设计

把需求变成**单文件自包含 HTML**（内联 CSS/JS，零外链——产物区 iframe 沙箱可直接渲染）。

## 用法

先在回复里想清结构，再把**完整 HTML** 交给渲染脚本落盘：

```
script_runner(
  skill="frontend-design",
  script="render_page.py",
  script_args={"title": "产品落地页", "html": "<!DOCTYPE html><html>…完整文档…</html>"}
)
```
产出 `site.html`（产物区自动预览，allow-scripts 下交互可用）。

## 设计准则（生成 HTML 时遵守）

- **自包含**：所有 CSS 写 `<style>`、JS 写 `<script>`；不引外部字体/CDN（沙箱无网络凭据）。
- **现代观感**：系统字体栈；8px 间距体系；圆角 12-16px；一个主色 + 中性灰阶；
  暗色页配 `color-scheme: dark`。
- **响应式**：max-width 容器 + flex/grid；图片 `max-width:100%`。
- **可交互**：按钮 hover/表单校验/选项卡切换用原生 JS 实现（不引框架）。
- 中文排版：`font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif`；
  行高 1.6-1.8。

## 配图（把图放进网页——**任何本地图片都必须内联，禁止相对路径引用**）

沙箱预览 iframe 无网络凭据，页面里只有内联 data-URI 的图片能显示。两类图的正确写法
（`render_page.py` 自动替换成 data-URI）：

1. **生成图**：先调 `image_generate`（产出 `image-N.png`，编号在**本会话内续接**），HTML 里用
   **`<img src="generated/image-N.png">`** 引用（文件名以 image_generate 观察结果里列出的为准）。
   本会话**此前轮次**生成的图也仍在 generated/ 可直接引用——改版式/改布局无需重新生图。
2. **用户上传的图**（如原图对比场景）：`script_runner` 调用时必须传
   **`input_files=["原图文件名.jpeg"]`**（本轮或**本会话此前轮次**上传的文件名均可，无需用户重传），HTML 里用
   **`<img src="原图文件名.jpeg">`** 裸文件名引用。忘传 input_files 则无法内联，
   渲染输出会打「警告: N 个本地图片引用未找到」——看到警告必须补上 input_files 重新渲染。

绝不要在最终页面留下任何未内联的本地路径（`<img src="xxx.jpg">` 而文件不在沙箱内），
那会让预览里图片空白、用户误判任务失败。不需要真实照片时，可用纯 CSS 装饰/emoji/inline SVG 替代。
