# 数据库修复记录

## 2026-03-09: 添加user_id列到daily_balance表

### 问题
Bot异常：`table daily_balance has no column named user_id`

### 修复
```sql
ALTER TABLE daily_balance ADD COLUMN user_id INTEGER DEFAULT 1;
```

### 影响表
- `daily_balance` - 添加user_id列，支持多用户

### 相关表结构
需要user_id的表：
- `bot_state` (已有)
- `risk_state` (已有)
- `user_config` (已有)
- `user_settings` (已有)
- `user_api_keys` (已有)

### 执行时间
2026-03-09 11:20 UTC
