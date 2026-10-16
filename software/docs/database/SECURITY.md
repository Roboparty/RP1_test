# 数据库安全与 RLS

## 数据库账号

- `rp1_admin`：仅用于迁移、备份、恢复和本地运维。
- `rp1_app`：未来 FastAPI 使用，受表权限和 RLS 约束。
- `rp1_readonly`：只读诊断账号，仍受 RLS 约束。

浏览器、机器人、台架和采集器不得获得数据库账号。

## 请求上下文

后端每个事务必须设置：

```sql
SELECT iam.set_request_context(
  '<verified-user-public-id>',
  '<request-id>',
  '<change-reason>'
);
```

身份必须来自已验证的后端会话，不能直接相信客户端提交的用户 ID。

## Campaign 授权

`iam.user_campaign_access` 保存用户对 Campaign 的 `VIEW` 或 `EDIT` 权限。系统管理员自动拥有全部 Campaign；查看者永远不能写；测试执行人员需要 `EDIT` 授权才能修改。

## 部署要求

- 非本地部署必须更换 `.env` 中所有密码。
- PostgreSQL 不暴露到公共网卡。
- 生产环境应使用密钥管理或 Docker secrets，不把 `.env` 纳入版本控制。
- 后端连接池账号不得拥有 `BYPASSRLS`。
- 运维账号不得被应用服务使用。
