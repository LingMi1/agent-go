---
document_id: tools_code_interpreter_sandbox_boundary
title: Code Interpreter 与脚本沙箱安全边界
module: tools
version: "1.0"
source_revision: c04ce65
status: partial
source_files:
  - cognition/cognition/tools/code_interpreter.py
  - cognition/cognition/skills/runner/docker.py
  - cognition/cognition/skills/runner/local.py
  - cognition/cognition/config.py
---

# Code Interpreter 与脚本沙箱安全边界

## 业务目标

Agent 可执行 Python 或 Skill 脚本完成计算、数据处理和产物生成，同时通过超时、输出限制和可选容器隔离降低任意代码执行风险。

## 执行流程

`code_interpreter` 默认不注册。启用后把模型代码写入临时目录，按 `skill_runner` 选择 local 子进程或一次性 Docker 容器。Docker 模式使用断网、只读根文件系统、只读代码挂载、可写输出目录、tmpfs，以及内存、CPU、pid 限制。超时会杀容器或整个本地进程组。

## 关键数据结构

代码执行默认 60 秒、最高请求 300 秒，stdout 最多保留 4000 字符，最多登记 8 个产物，单文件超过 10 MiB 不上传。local 模式只传 PATH、HOME、LANG 和输出相关白名单环境，避免暴露认知进程的模型与存储密钥。

## 失败场景

Docker 不可用、镜像缺失、解释器启动失败、脚本非零退出和超时都会返回明确 observation。产物扫描只处理输出目录顶层普通文件。过大文件可能有 ArtifactRef 但未上传，下载会失败。

## 限制与消歧

默认 `skill_runner=local`，它只是受限子进程，不是安全沙箱；Compose 中认知服务也显式使用 local，未采用 Docker-in-Docker。生产需要外置 Docker runner 或其他执行服务。gVisor、Firecracker/microVM 和 seccomp 策略未实现，状态为 unknown/planned，不能称为真强隔离平台。
