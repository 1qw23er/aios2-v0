# series_id 升级前预检（Pre-upgrade Precheck）

**关联**：DSH 审计 5 条非阻塞 follow-up 之 #①（升级首过窗口预检）。
**脚本**：`scripts/precheck_series_id_metadata.py`
**测试**：`tests/test_series_id_metadata_precheck.py`

## 背景

`series_id` 回填迁移（`20260820_0001_series_id`）及其加固迁移
（`20260824_0001_series_id_json_guard`）会读取 `artifact.metadata` 作为 JSON。
在带 `json_valid()` 守卫的版本之前（即 `≤ 20260812` 的首过窗口），若某行的
`metadata` 是非 JSON 的普通字符串 / 空串，SQLite 会在 `json_extract` 时抛出
`malformed JSON`，**使整条迁移事务中止**。

`20260824` 的 `json_valid()` 守卫已能在迁移运行时安全跳过这些行（fail-closed），
但运维侧仍应在升级前显式发现脏数据，而不是等迁移中途失败。

## 预检做什么

- 扫描三张 owner-inbox 项表：`artifact` / `cs_suggestion` / `knowledge_candidate`。
- **仅对带 `metadata` 列的表**执行校验（`artifact` 有；另两张派生 `series_id`、
  从不解析 JSON，因此**跳过**）。
- 统计 `metadata IS NOT NULL AND NOT json_valid(metadata)` 的行。
- `NULL` 的 `metadata` 视为合法（即"未归类"哨兵，回填时由
  `WHERE series_id IS NULL` 跳过），不计入。

## 退出码

| 码 | 含义 |
|----|------|
| `0` | 无非法 metadata，可安全升级 |
| `1` | 发现非法 metadata，**升级前必须先修复**（置为合法 JSON 或 NULL） |
| `2` | 用法 / 连接 / schema 错误 |

## 用法

```bash
# 使用环境变量中的库（默认 sqlite:///data/aios.db）
python scripts/precheck_series_id_metadata.py

# 或显式传库 URL
python scripts/precheck_series_id_metadata.py "sqlite:////path/to/aios.db"
```

## 升级流程建议（运维侧闭合 ≤20260812 死角）

1. 升级前先跑预检：`python scripts/precheck_series_id_metadata.py $DB_URL`
2. 若返回 `1`：定位脏行，将 `metadata` 修正为合法 JSON 或置 `NULL`，重跑预检至 `0`。
3. 再执行迁移升级：`alembic upgrade head`（或应用正常的发布流程）。
4. 升级后预检应持续返回 `0`。

> 注意：已发布的迁移**不可修改**。本预检是独立运维工具，不与任何迁移耦合；
> 它只是把"首过窗口可能遇到的 malformed JSON"提前暴露出来。
