# 安全策略

> [English](SECURITY.md)

## 报告漏洞

**请不要**为安全问题开公开的 GitHub issue。

请通过以下任一方式私下报告：
- **GitHub 私密漏洞上报**（*Security* 标签页 → *Report a vulnerability*），或
- **邮件**：`LingMi1@users.noreply.github.com`

### 报告里写什么

- 问题描述及潜在影响
- 复现步骤（最小示例、涉及的端点/文件）
- 受影响的版本或 commit SHA
- 你建议的修复或缓解方案

## 响应时限

- **确认收到**：48 小时内。
- **初步评估**：5 个工作日内。
- 在公开披露前，会先和你协调修复或公告。

## 范围

agent-go 涉及多处安全敏感面，报告时请重点关注：
- **认证与授权** —— bcrypt 哈希、Bearer token、RBAC，以及所有查询上的 owner 级资源隔离（`WHERE owner_id = ?`）。
- **Prompt 注入** —— 认知面的三层防御（输入检测、prompt 隔离、输出过滤）。
- **密钥处理** —— 已存密钥的 AES-256-GCM 加密，以及 `web_fetch` 上的 SSRF 防护。
- **技能沙箱** —— 基于 Docker 的技能执行器及其启用/禁用边界。

## 生产部署注意事项

部署 agent-go 时，把密钥当敏感信息对待：
- `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` 以及 Postgres / Qdrant / Redis / MinIO 的凭据，都放进你的密钥管理，别提交进仓库或打进镜像。
- 对外暴露前，先改掉 `deploy/.env` 里的默认值。
- 控制面若对外可达，务必置于 TLS 之后。

本项目采用 MIT 许可。安全报告由维护者 LingMi1 处理。
