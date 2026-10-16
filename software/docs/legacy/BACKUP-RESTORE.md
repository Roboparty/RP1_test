# 备份与恢复

## 策略

只保留一个最新有效备份：`backups/latest.dump`。

备份脚本先写临时文件，执行 `pg_restore --list` 和 SHA-256 校验后，再原子替换 `latest.dump`。如果新备份失败，旧备份不会被覆盖。

## 备份

```bash
./scripts/backup-postgres.sh
```

同时生成：

- `backups/latest.dump`
- `backups/latest.dump.sha256`
- `backups/latest.contents`

## 非破坏性恢复演练

以下命令会把最新备份恢复到一个临时数据库，执行 Alembic 版本检查和完整数据库验证，然后自动删除临时数据库，不触碰当前运行库：

```bash
./scripts/verify-backup-restore.sh
```

## 恢复

恢复是破坏性操作，脚本要求显式环境变量确认：

```bash
RP1_CONFIRM_RESTORE=YES ./scripts/restore-postgres.sh backups/latest.dump
```

恢复前应停止未来 API/Worker，避免写入竞争。恢复脚本校验 dump 后清理目标数据库对象并恢复，随后重新执行 Alembic head 检查和数据库验证。
