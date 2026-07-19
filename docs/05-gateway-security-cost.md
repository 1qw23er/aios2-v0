# 外部 Agent 网关：安全与成本模型

> 关联 Issue：#105（本文档）/ #57（Agent Interop Gateway 史诗）/ #103（网关首片）/ #104（网关加固）
> 状态：Accepted（随 #104 落地，全量 pytest 190 passed）
> 适用范围：所有经 `DelegatedRun` 委托给**外部** Agent（Hermes 远程 API / 扣子工作站等）的执行路径。

## 背景

AIOS 通过适配器把任务委托给外部 Agent。这引入了四类原生风险，是 V0 内部 Agent 路径不存在的：

1. **凭证泄漏**——外部 Agent 需要 `secret_ref` 才能调用远端，但解析后的密钥一旦进入 `TaskContext` / `Artifact` / `Event` / `AuditLog`，就会随上下文共享、落库、被人工可见。
2. **上下文过度暴露**——外部 Agent 不应拿到 AIOS 的知识库内部上下文（已批准事实、决策、策略），否则信息外泄且超出任务所需。
3. **成本失控**——远端调用按量计费，缺乏预算闸门会导致单次委托或连续委托耗尽项目算力预算。
4. **不可信执行**——experimental 级外部 Agent 尚未验证，直接委托等同于把生产任务交给未知能力。

`docs/04-security.md` 已规定 V0 总体治理（Secrets 不进 Prompt/日志/Context Store、最小权限、三层成本预算、外部回传须 schema 校验）。本文档把其中**面向外部委托**的部分落成可执行的网关机制。

## 决策

网关加固（`src/aios/delegation.py` + `src/aios/audit.py`）落地五项机制，全部**复用既有** `Task` / `Artifact` / `AuditLog` / `Approval`，不引入新权限框架：

### 1. 凭证引用边界（Secret reference boundary）

- 委托记录只持久化 `DelegatedRun.secret_ref`（如 `secret://hermes-prod`）这一**不透明句柄**，绝不写入解析后的密钥值。
- 适配器在**执行边界**才通过 `resolve_secret(ref)` 解析；解析结果只在内存中用于本次 HTTP 调用，不回写任何 DB 载荷。
- 测试保证：`secret_ref` 之外的密钥值不出现在 `DelegatedRun` / `Artifact` / `AuditLog` 任何字段。

### 2. 审计脱敏（Audit redaction）

- `redact_secrets(value)` 递归处理载荷，三类 secret 全部脱敏为 `[REDACTED]`：
  - **按 key**：`SECRET_KEYS = {secret, token, password, credential, api_key, api-key}`；
  - **按 header key**：`SECRET_HEADER_KEYS = {authorization, proxy-authorization, cookie, set-cookie}`；
  - **按值形态**：`Bearer <t>` / `Basic <b64>` / `sk-...` / `AKIA...` / `nvapi-...` / `xox*-...` 等带前缀的密钥，以及**高熵** 40+ 字符（同时含小写+大写+数字）的令牌。
- **刻意不过度脱敏**：纯 hex / 纯小写的内容哈希（如 `context_hash`）被视为内容寻址而非凭证，**原样保留**在审计轨迹中——否则会破坏审计完整性（回归测试 `test_controlled_retention_deletes_with_minimal_audit` 已锁定此行为）。
- `append_audit` 持久化前对 `before/after` 快照统一脱敏。

### 3. 上下文最小权限（Context least privilege）

- `project_external_context(task_context)` 以**严格 allowlist** 投影：`{objective, instructions, acceptance_criteria, dependency_outputs}`，并显式剥离内部键 `approved_facts / relevant_decisions / applicable_policies / project_context / source_references / agent_profile`。
- 投影结果再过一遍 `redact_secrets`，兜底任何残留凭证值。
- 外部 Agent 只收到任务所需的、已批准上下文，看不到知识库内部。

### 4. 预算强阻断（Budget enforcement）

- `Project.budget_limit`（默认 `0.0` = 不强制）+ 新增 `Project.budget_used`（累计已花费）。
- `check_budget(session, project, estimated_cost)`：当 `budget_limit > 0` 且 `used + estimated > limit`，抛 `BudgetExceededError`（属 `DelegatedExecutionError`），**在远端调用前**硬阻断，安全失败并带显式 reason（`"budget exceeded"`）。
- 成功执行后 `_accrue_budget(run)` 从持久化 `DelegatedRun.cost` 累加进 `Project.budget_used`（重新查 DB 读取，避免内存对象 detached/stale）。
- `budget_limit == 0.0` 时不强制，保持既有行为。

### 5. Agent 信任等级（Agent trust level）

- 单一信任轴 `AgentTrustLevel`（非通用权限框架）：`internal` / `verified_external` / `experimental`。
- `assert_trust_delegable(agent)`：`experimental` 直接抛 `DelegatedExecutionError`，禁止委托；`internal` 与 `verified_external` 允许。
- 这是**有意收窄**的设计：用一条可信轴限制外部执行能力，而不是建一套 RBAC / 通用权限系统（满足约束 "no generic permission framework"）。

## 约束遵守

- **no vendor-specific logic**：脱敏前缀列表与信任轴都是形态/语义层面的通用机制，不含任何厂商 SDK 或平台特化分支。
- **no new agent marketplace**：仅复用 `Agent` registry 既有字段，未新增市场/目录。
- **no generic permission framework**：信任等级是单一轴，不是通用权限系统。
- **reuse existing Task / Artifact / AuditLog / Approval**：五项机制全部构建在既有模型与迁移之上，迁移 `20260719_0002` 仅新增 `agent.trust_level` 与 `project.budget_used` 两列。

## 后果

- 网关可安全地把任务委托给外部 Agent：凭证零落库、上下文最小暴露、成本有硬顶、不可信 Agent 被挡在委托之外。
- 回归面：审计脱敏必须精细——过度脱敏会破坏审计完整性（已用测试锁定 `context_hash` 等合法哈希的保留）；预算累计必须读持久化 cost，避免 stale。
- 下一步：#105 之后如需更细能力限制，应在信任轴内扩展（如 `verified_external` 分级），而非引入通用权限框架。
