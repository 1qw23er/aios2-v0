# PR #56 (rebased): LLM 执行适配器重试与自愈合 (Issue #55)

把 Issue #55 的重试逻辑重新实现在 #79 错误分类契约之上，并完整消解 Codex 在旧分支
`harden/llm-execution-retry` (head `79d607c`) 上提出的 5 条 **blocking changes required**，
以及二轮审查提出的 2 条 **blocking**。

## 设计决策（owner 已拍板）
- **重试范围**：仅 `TIMEOUT` + `NETWORK` 两类瞬时传输失败。其他类别
  (`CONFIG_MISSING` / `PROVIDER_HTTP` / `PROVIDER_STRUCTURE` / `JSON_PARSE` / `UNKNOWN`)
  一律**不重试**——重发相同 prompt 只浪费 owner 付费 token 且永不可能成功。
- **默认开启 + 可调**：默认 `MAX_RETRIES=2`（1 次初始 + 2 次重试 = 3 次出站）。
  通过 `AIOS_AGENT_MAX_RETRIES`（0 关闭）与 `AIOS_AGENT_BACKOFF`（基础秒数，指数退避封顶 30s）
  可调。owner 在 Issue #55 中显式批准了默认开启的额外成本。

## Codex 5 条 blocking 的逐条回应
1. **重试白名单不可过宽** → 仅 `_RETRYABLE_CATEGORIES = {TIMEOUT, NETWORK}`（execution.py 常量）。
2. **恢复 prompt 须脱敏** → 重试时追加 `_RETRY_RECOVERY_HINT`，内容**仅含「请重试」指令**，
   绝不携带 `str(exc)`、原始错误文本或任何 secret（测试 `test_recovery_prompt_is_sanitized_no_raw_error` 断言）。
3. **逐次计量** → `result.metadata = {"attempts": n, "max_attempts": m}`；失败时 `reason`
   含尝试次数与类别（`执行在 N 次尝试后失败（TIMEOUT）：<脱敏 detail>`），均落库可审计。
4. **非重试类别 = 单次出站** → 测试 `test_non_retryable_category_makes_single_outbound_call`
   断言 `PROVIDER_HTTP` 仅 1 次调用即抛出，无退避、无重试。
5. **owner 成本批准** → 已在 Issue #55 决策中捕获；默认开启且 `AIOS_AGENT_MAX_RETRIES=0` 可一键关闭。

## Codex 二轮 2 条 blocking 的回应（本轮修）
### (A) 重试次数须有硬上限（封顶总成本，非仅封顶单次 sleep）
- 新增冻结常量 `MAX_RETRIES_HARD_CAP = 5`。`__init__` 在解析 `AIOS_AGENT_MAX_RETRIES`
  （无论显参还是环境变量）后，从**上方**钳制：`if self.max_retries > MAX_RETRIES_HARD_CAP: self.max_retries = MAX_RETRIES_HARD_CAP`。
- 效果：`AIOS_AGENT_MAX_RETRIES=100000`（或显参 100000）→ 实际 `max_retries=5`
  → **最多 6 次出站调用**，不可能再出现 100001 次。
- 测试 `test_max_retries_hard_cap_from_explicit_argument` / `test_max_retries_hard_cap_from_env_var`
  断言钳制值与 `calls == 1 + MAX_RETRIES_HARD_CAP`。

### (B) 逐次计量必须持久化（成功与失败都落库）
- `ExecutionError` 新增 `attempts` / `max_attempts` 字段；`run()` 在最终重试耗尽失败时填入。
- `execute_task` **成功路径**：把 `result.metadata` 的 `attempts`/`max_attempts` 写入
  `Artifact.metadata_json` **与** `artifact.created` 审计 `after_snapshot`（仅当适配器确实上报，否则不污染 metadata）。
  使用 `getattr(result, "metadata", None)` 防御委托适配器的 shim（无 `metadata` 字段）。
- `execute_task` **失败路径**：经 `_mark_failed(meta=...)` 把 `attempts`/`max_attempts` 合并进
  `task.failed` 的 Event payload **与** AuditLog `after_snapshot`（除最终 reason 汇总外，另有结构化字段可查询）。
- 集成测试 `test_execute_persists_attempts_on_success` / `test_execute_persists_attempts_on_failure`
  经真实 `execute_task()` 验证 Artifact + Event + AuditLog 三处均落库。

## 其他保障
- 退避：指数 `backoff * 2**(attempt-1)` 封顶 30s；`backoff=0` 时无 sleep。
- 环境变量解析失败（非整数/浮点）回退默认，负数被钳为 0，**超上限被钳为硬上限**，杜绝异常循环。
- 失败最终抛出沿用 #79 `_redact_secrets` 脱敏，detail 不含 secret。
- 无迁移变更，Alembic head 仍为 `20260722_0007`。

## 验证
- `pytest tests/test_execution.py -q`：41 passed（含 10 条 #55 回归：原 6 + 硬上限 2 + 持久化 2）。
- `pytest tests/test_gateway_provenance.py tests/test_execution.py`：42 passed。
- `ruff check src tests alembic`（ruff==0.15.22，line-length=100）：All checks passed。
- 全量 `pytest -q`：360 passed（含本轮修复；修复前 1 例 delegated shim 缺 `metadata` 字段的
  AttributeError，已用 getattr 防御，单独复跑通过）。

## 待办（铁律）
- `@codex review` 重新审查；**仅当** Codex APPROVE + exact-head CI 绿，才由 owner 授权 squash merge。
- 绝不自动 merge。
