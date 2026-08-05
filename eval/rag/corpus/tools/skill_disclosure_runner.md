---
document_id: tools_skill_disclosure_runner
title: Skill 渐进式披露与脚本运行器
module: tools
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/skills/registry.py
  - cognition/cognition/skills/disclosure.py
  - cognition/cognition/skills/tools.py
  - cognition/cognition/skills/runner/request.py
---

# Skill 渐进式披露与脚本运行器

## 业务目标

Skill 以目录和 `SKILL.md` 描述可复用工作方法，避免启动时把所有参考资料和脚本内容塞进模型上下文。模型先查看目录级摘要，再按需读取正文、引用或执行声明的脚本。

## 执行流程

SkillRegistry 扫描配置目录并解析 front matter。工具层提供技能列表、读取正文/参考文件和脚本执行入口；路径访问先经过 sandbox 校验。运行请求根据 `.py`、`.js`、`.sh` 选择解释器，将参数稳定序列化为 JSON，并把输出目录中的文件映射为 ArtifactRef。

## 关键数据结构

`SkillDefinition` 保存 name、description、正文与 base path。`ScriptRunRequest` 包含 skill、script、workdir、cmd、timeout 和允许的 input files。执行时 run ID 与 tool-call ID共同构成产物 resource key。

## 失败场景

缺失或非法 front matter、未知脚本扩展名、绝对路径、`..` 穿越和 symlink 逃逸都会被拒绝。脚本启动失败返回 127，输入下载失败返回 126，超时会终止进程并标记 timed_out。stdout/stderr 被作为 observation 返回。

## 限制与消歧

Skill 是文件约定和工具包装，不是独立 LangGraph 子 Agent。渐进披露限制的是模型上下文加载，不自动保证脚本安全；隔离强度由 local 或 Docker runner 决定。技能依赖需通过 optional `skills` extra 或执行镜像提供。
