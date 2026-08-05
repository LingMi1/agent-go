---
document_id: control_plane_http_api_auth_rbac
title: HTTP API 身份隔离与 RBAC
module: control-plane
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - control-plane/internal/api/auth.go
  - control-plane/internal/api/api.go
  - control-plane/internal/store/migrations/0008_users.sql
  - control-plane/internal/api/auth_test.go
---

# HTTP API 身份隔离与 RBAC

## 业务目标

控制面需要隔离会话、run、附件、知识库、产物和连接器，并为管理接口提供真实服务端角色检查，同时保留旧开发模式的低门槛身份头。

## 执行流程

中间件优先解析 `Authorization: Bearer`，查询服务端 auth_sessions 并把 user ID和 role 写入 context。`AUTH_REQUIRED=true` 时受保护端点无有效 token 返回 401并忽略 `X-User-Id`。关闭时，有 token 仍用 token，无 token 回退 `X-User-Id`。`/admin/*` 另由 requireAdmin 检查 role。

## 关键数据结构

users 保存不可变 user ID、username、bcrypt password hash 和 user/admin 角色；auth_sessions 保存随机 token、用户和过期时间，可服务端吊销。owner ID直接等于 user ID，因此历史 runs、kb 和 uploads 无需另做租户迁移。

## 失败场景

用户名冲突、弱或错误密码、过期 token 和普通用户访问 admin 会返回结构化 problem JSON。启用强制鉴权但 token 仓库不可用时受保护接口无法建立有效身份。owner 检查在 SQL 或 handler 层阻止跨用户读取。

## 限制与消歧

默认 `AUTH_REQUIRED=false` 的 `X-User-Id` 可伪造，只适合开发兼容模式，不能宣称默认生产鉴权。系统使用服务端 session token，不是 JWT/OIDC。只有 user/admin 两种角色，没有细粒度权限策略、数据库行级安全、Vault 或 KMS。
