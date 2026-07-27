# Issue #88 实施计划（v3）— AI 员工工作日志与知识沉淀系统（MVP 三件套）

> 基于最新 `main`（`ec36d9a`，Alembic head `20260727_0008`）。
> 本文件是**实施计划**，不含任何实现代码。仅用于架构评审（Codex + owner）。
> v2：响应 PR #90 第 1 轮评审（REQUEST_CHANGES，5 个阻断点）逐条修订，摘要见 §15。
> v3：响应第 2 轮评审（REQUEST_CHANGES，3 个契约修复点）修订 §6/§7.2/§8.2/§8.3/§11，摘要见 §16。

---

## 0. 范围与铁律（来自 Issue #88 与 owner 追加约束）

MVP 仅覆盖三件套：**提交入口** / **知识沉淀（人工审核）** / **内容素材库（只读导出）**。

**必须遵守的约束（owner 明确）：**

1. 复用现有 `Artifact` / `KnowledgeCandidate` / `KnowledgeFact` / `KnowledgeService`，不重写。
2. `KnowledgeCandidate` **仍须人工审核**（owner `review_candidate`）后才能成为 `KnowledgeFact`；**不自动 APPROVE**。
3. **不引入** LLM 判定、API/MCP 自动采集、实时 Hermes 推送。
4. 明确 `Artifact`/`KnowledgeCandidate` 的 **scope 与 provenance 规则**（§4）。
5. **幂等键强制**，单一契约，不得列为「可选」（§5）。
6. **不信任** `metadata_json` 里调用者提供的 `agent_id`；必须验证 **Agent / Project / Task 的真实归属**（§6）。
7. `ContentFeed` **只能**包含允许范围内的日志 + 已 `APPROVED` 的事实，含精确的 scope/排序/去重定义（§8.3）。
8. `Agent` 字段变更须含 Alembic `upgrade`/`downgrade` + **head bump** + **兼容性测试**，降级策略明确（§3）。
9. 按 TDD 顺序：模型 → 迁移 → 服务 → 脚本 → 测试 → 验收（§9–§12、§14）。
10. **不扩大** Issue #88 的 Out of scope（§13）。

---

## 1. 复用的现有事实（代码已确认，基于 `ec36d9a`）

| 组件 | 现状 | 本计划用法 |
|------|------|-----------|
| `ArtifactType`（StrEnum） | `MARKDOWN/JSON/IMAGE/VIDEO/DATASET/LINK` | 加 `WORK_LOG = "work_log"`（**纯代码，无迁移**） |
| `Artifact` | `project_id`(NOT NULL FK)、`task_id`(nullable FK)、`type`、`metadata_json`、`review_status`(默认 `UNVERIFIED`)、`provenance_json` | 工作日志落为 `Artifact(type=WORK_LOG)`，**初始 `UNVERIFIED`**；新增 `idempotency_key` 列（§3） |
| `owner_approve_review`（`review.py:1092`） | **APPROVED 的唯一路径**：要求 `REVIEW_PASSED` + 该轮 pending `review_gate` `Approval`，落 `Approval.APPROVED` + `AuditLog(review.owner_approved)`，幂等（已 APPROVED → no-op） | 工作日志走**新的 owner-attestation 契约**（§7.2），该契约显式复用同一证据结构，不绕过 |
| `ExecutionAssignment` | `task_id` + `selected_agent_id`（**真实执行者**）+ `idempotency_key` UNIQUE | agent↔task 归属校验的**唯一依据**（§6） |
| `KnowledgeCandidate` | `artifact_id`(FK)、`project_id`(有效 scope, NULL=公司)、`source_project_id`(NOT NULL)、`statement`、`status`、`submitted_by_*`(由可信 actor 派生) | 复用；scope 由工作日志的 `project_id` 派生 |
| `KnowledgeFact` | `series_id`、`version`、`project_id`(scope)、`source_project_id`、`supersedes_fact_id`（UNIQUE）、`uq_knowledge_fact_approved_head` 部分索引（每 series 单一 APPROVED head） | 复用；仅经 owner 审核产生 |
| `KnowledgeService.submit_candidate` | 签名 `(artifact_id, statement, *, project_id, tags, actor)`；要求 `artifact.review_status==APPROVED` 且 `artifact.project_id` 非空；`(artifact_id, statement)` 幂等；`submitted_by` 由可信 `actor` 派生 | 收割直接调用；**actor 由外层传入，服务内不自铸**（§8.2） |
| `KnowledgeService.review_candidate` | 要求 `actor.kind=="owner"`；候选须已 classify；provenance 守卫 | 人工审核路径（复用，不改） |
| `ActorContext` / `authenticate_owner` | `ActorContext(kind, owner_id, agent_id)`；API 边界注入可信 owner actor | **owner 身份只能来自认证边界**，服务/脚本不得 `resolve_owner_actor()` 自铸（§6/§8.2） |
| `append_audit` | 结构化 AuditLog + `idempotency_key` 去重 | 认证（attestation）与提交动作的审计证据 |

---

## 2. 模块划分（轻量，不新建表）

新增**一个**服务模块 `src/aios/work_log.py`，内含四个职责：

- `WorkLogService.submit_work_log(...)` — 提交入口（产出 `UNVERIFIED` 日志）
- `WorkLogService.attest_work_log(...)` — **owner 认证（attestation）**：唯一使工作日志达到 `APPROVED` 的路径（§7.2）
- `ContentValueJudge.judge(...)` — 内容价值判定（纯函数，规则 + 人工覆盖）
- `KnowledgeHarvester.harvest_candidates(...)` — 知识收割（复用 `KnowledgeService`；actor 由调用边界传入）
- `ContentFeed.get_content_feed(...)` — 内容供给（只读）

不新建任何 SQLModel 表。

---

## 3. 数据模型与迁移

### 3.1 模型层改动（`src/aios/models.py`）

**(a) `ArtifactType` 加枚举值（无迁移）：** `WORK_LOG = "work_log"`。

**(b) `Artifact` 加 `idempotency_key`（需迁移）：**
```python
idempotency_key: str | None = Field(default=None)
```
> **单一 schema 表示（评审点 3）**：模型字段**不带** `unique=True`/`index=True`；唯一性**只**由迁移中的命名部分唯一索引表达：
> `CREATE UNIQUE INDEX uq_artifact_idempotency ON artifact(idempotency_key) WHERE idempotency_key IS NOT NULL`。
> 与 `uq_knowledge_fact_approved_head`/`uq_knowledge_fact_company_version` 同机制（raw `op.execute` 部分索引），SQLModel metadata 与 Alembic 不冲突。

**(c) `Agent` 加两个字段（需迁移）：**
```python
platform: str | None = Field(default=None, index=True)      # chatgpt/codex/workbuddy/hermes/coze/custom
external_ref: str | None = Field(default=None)              # 外部定位信息，可空
```

### 3.2 新迁移 `alembic/versions/20260728_0009_work_log_and_agent_platform.py`

- `revision = "20260728_0009"`，`down_revision = "20260727_0008"`。
- **upgrade()：**
  - `Agent`：`op.batch_alter_table("agent")` 加 `platform`（含索引）、`external_ref`。（`agent` 表无 AFTER 触发器，batch 重建安全。）
  - `Artifact`：raw `ALTER TABLE artifact ADD COLUMN idempotency_key VARCHAR`（**不**走 batch，避免重建 `artifact` 表惊动字面引用 `main.artifact` 的 `knowledge_candidate_validate_insert` 触发器），随后建 `uq_artifact_idempotency` 部分唯一索引。
- **downgrade()（评审点 4 —— 选定 fail-closed 策略）：**
  - **预检（任何 DDL 之前）**：若存在任一 `agent.platform IS NOT NULL OR agent.external_ref IS NOT NULL` 的行，**或**任一 `artifact.idempotency_key IS NOT NULL` 的行 → Python `raise RuntimeError`（fail-closed；SQLite 禁止 trigger 外 `SELECT RAISE()`，与 0008 同法）。schema/行/索引/revision 完整停留在 `20260728_0009`。
  - 预检通过（0009 新列全空）→ 无损降级：`DROP INDEX IF EXISTS uq_artifact_idempotency`；`ALTER TABLE artifact DROP COLUMN idempotency_key`；batch 删 `agent.platform`/`external_ref`。
  - **明确不承诺**「已填充数据的无损往返」——填充后降级即中止，这是唯一不丢数据的选择。
- **head bump（兼容性测试必需）：**
  - `tests/test_knowledge_models.py` round-trip 断言 `20260727_0008` → `20260728_0009`；
  - `tests/test_review_binding_migration.py` 的 `HEAD` 常量 → `20260728_0009`；
  - 全仓 grep `20260727_0008` 断言一并核对 bump。

---

## 4. Scope 与 Provenance 规则（强制明确）

### 4.1 工作日志 `Artifact(type=WORK_LOG)`
- **Scope = `project_id`（必填，非空）**。MVP 不接受公司级（NULL）工作日志。
- **生命周期（评审点 1）**：创建时 `review_status=UNVERIFIED`；只有经 §7.2 的 owner attestation 才变为 `APPROVED`；只有 `APPROVED` 的日志可被收割（满足 `submit_candidate` 前置且带完整证据链）。
- **Provenance（`provenance_json`，服务端根据已验证输入填写，不照搬调用者）：**
  ```json
  {
    "submitted_by": "owner:<owner_id>",
    "submitted_at": "<utc iso>",
    "produced_by_agent_id": "<经 §6 归属校验的 Agent.id 或 null>",
    "produced_by_platform": "<agent.platform 或 null>",
    "task_id": "<已验证的 task.id 或 null>",
    "execution_assignment_id": "<归属证明所用的那条 assignment.id；legacy 路径为 null>",
    "legacy_assigned_agent": "true 仅当走 §6 legacy 固定指派路径，否则 false"
  }
  ```
- `metadata_json` 结构（7 项汇报 + 内容角度；**无 `tags` 字段**——评审点 5，tags 不来自调用者）：
  ```json
  {
    "report_type": "daily | retro",
    "produced_by_agent_id": "agt_xxx | null",
    "task_ref": "tsk_xxx | null",
    "what_done": "...", "why": "...", "problem": "...",
    "solution": "...", "new_knowledge": "...",
    "content_value": "high | medium | low | none",
    "should_enter_kb": true,
    "content_angle": "..."
  }
  ```

### 4.2 `KnowledgeCandidate`（由收割产生）
- `project_id`（有效 scope）= 源工作日志的 `project_id`（复用 `submit_candidate` 守卫：project-scoped 必须等于 source campaign）。
- `source_project_id`（NOT NULL）= 源工作日志的 `project_id`。
- `statement` = `metadata.new_knowledge`。
- `submitted_by_*` 由**调用边界传入的已认证 owner actor** 派生（服务内不自铸，§8.2）。
- `tags` = §8.2 的**确定性映射**产物（固定 taxonomy，非调用者输入）。
- `status = DRAFT`，等待 owner 审核。

### 4.3 `KnowledgeFact`
- 仅经 `KnowledgeService.review_candidate(..., actor=owner)` 产生；scope/provenance 沿用候选。

---

## 5. 幂等契约（强制，单一契约 —— 评审点 3）

**唯一契约：客户端提供 `Idempotency-Key`，服务端命名空间化 + 请求指纹核对。** 不再有第二把「服务端日期/SHA-1 键」。

1. **API `POST /work-logs`**：`Idempotency-Key` 请求头**必填**（缺失 → 422，与 `execute_task` 同惯例）。
2. **服务签名收键**：`submit_work_log(..., idempotency_key: str, ...)` —— 键是必传参数，服务不自造键。
3. **存储键（命名空间化）**：
   `Artifact.idempotency_key = "work_log:{project_id}:{sha256(client_key)[:32]}"`
   —— 端点名 + **已验证的** `project_id` 为命名空间；不含不可信的 `agent_id`；不含日期（跨 UTC 午夜重试不受影响）。
4. **请求指纹**：`metadata_json._request_fingerprint = sha256(canonical_json(全部业务输入字段))`（canonical JSON：键排序、UTF-8、无空白）。
5. **语义（穷尽三种情形）**：
   - 同键 + 指纹匹配 → 返回既有 `Artifact`（200，replay no-op）；
   - 同键 + 指纹不匹配 → `ServiceError(409, "idempotency key reuse with different payload")`；
   - 并发同键双写 → 部分唯一索引 `uq_artifact_idempotency` 兜底，捕获 `IntegrityError` 后按前两条重新裁决。
6. **收割幂等**：复用 `submit_candidate` 的 `(artifact_id, statement)` 去重 + 收割前 `NOT EXISTS` 预筛；重跑不产生重复候选。
7. **attestation 幂等**：已 `APPROVED` 的日志再次 attest → no-op 返回（与 `owner_approve_review` 同语义）；审计 `idempotency_key = "audit:work_log:attest:{artifact_id}"`。

---

## 6. 身份与归属校验（不信任 `metadata.agent_id` —— 评审点 2）

**原则：owner 身份只能来自认证边界（`authenticate_owner` 注入的 `ActorContext`），服务与脚本内部一律不得调用 `resolve_owner_actor()` 自铸身份。** 所有服务方法均显式接收 `actor: ActorContext` 参数并校验 `kind=="owner"`。

`submit_work_log` 校验顺序（任一失败 → `ServiceError(422)`）：

1. **`actor` 必须是可信 owner**（由 API/脚本边界注入，服务只校验不铸造）。
2. **`project_id` 必填且 `Project` 行存在**。
3. **若提供 `task_ref`**：`Task` 存在且 `task.project_id == project_id`。
4. **若提供 `produced_by_agent_id`（provenance，仅参考）——绑定到一条精确的 `ExecutionAssignment`（第 2 轮评审点 3）：**
   - **必须同时提供 `task_ref`**，否则 422（「仅存在性」不构成归属证明——无任务锚点的 agent 声明一律拒绝）；
   - `Agent` 行必须存在；
   - **`ExecutionAssignment.task_id` 非唯一**（重试/fallback 路由可为同一 task 留下多条 durable assignment），故仅凭 `task_ref` 选取 assignment 是**歧义的**，可能把产出错误归属到旧的/fallback 候选。请求契约因此新增**可选字段 `execution_assignment_id`**，规则如下：
     - **task 存在 ≥1 条 `ExecutionAssignment`（routed task）时，`execution_assignment_id` 必填**（缺失 → 422 「ambiguous assignment: assignment id required」）。且必须**逐项验证**（任一失败 → 422）：
       1. 该 assignment 行存在；
       2. `assignment.task_id == task_ref`（拒绝跨 task 的 assignment id）；
       3. `assignment.selected_agent_id == produced_by_agent_id`（拒绝 wrong-agent / 过期 fallback 归属声明）；
       4. `task.project_id == project_id`（task 归属已验证 project，§6 第 3 步已保证，此处复核）。
     - **legacy 兼容仅限**：task **零条** `ExecutionAssignment` 且**未提供** `execution_assignment_id` 且 `task.assigned_agent_id == produced_by_agent_id`（固定指派）。若提供了 assignment id 但 task 无 assignment → 422。
     - **`preferred_agent_id` 永不接受**为执行证明。
   - 通过后将 agent 引用与作为证明的**该条** `execution_assignment_id`（legacy 路径则记 `null` + `legacy_assigned_agent: true`）写入 `provenance_json`；**绝不**把它当作提交者身份、`actor` 或权限来源。
5. `provenance_json.submitted_by` 由第 1 步的可信 actor 派生。

---

## 7. 提交与认证（评审点 1：不直接创建 APPROVED）

### 7.1 `submit_work_log` —— 只产 `UNVERIFIED`
- 执行 §6 校验；按 §5 处理幂等。
- 创建 `Artifact(type=WORK_LOG, project_id, task_id, review_status=UNVERIFIED, provenance_json=<§4.1>, metadata_json=<§4.1>, idempotency_key=<§5.3>, uri="work_log:<id>", checksum=sha256(canonical_json(metadata)))`。
- **提交 ≠ 批准**：owner 认证只证明身份，不等于内容已审。

### 7.2 `attest_work_log` —— 显式 owner-attestation 契约（APPROVED 的唯一路径）

工作日志不产自 executor 管线，没有 reviewer/review_gate 流程；为不绕过「APPROVED 必须有证据」的架构，定义**原子的 owner-attestation 契约**，持久化与 `owner_approve_review` 同构的证据：

- 签名：`attest_work_log(session, *, artifact_id, actor: ActorContext) -> Artifact`；校验 `actor.kind=="owner"`（与 `_assert_owner_actor` 同法）。
- **risk_level = `RiskLevel.L1`**（第 2 轮评审点 2：枚举实际为 `L0`–`L4`，不存在 `LOW`）。理由：owner 亲自 attest 的内部工作日志属低风险单人决策，但非零风险（进入知识收割链路），故取 `L1` 而非 `L0`；不涉及外部发布/资金/不可逆动作，无需 `L2+`。
- 前置：`artifact.type == WORK_LOG` 且 `review_status == UNVERIFIED`（其他状态见下方幂等/fail-closed 语义）。
- **单事务原子写入三件证据**：
  1. `Approval(project_id, target_artifact_id=artifact.id, action_type="work_log_attestation", risk_level=RiskLevel.L1, status=APPROVED, decided_at=now, rationale="owner attestation of work log")`；
  2. `artifact.review_status = APPROVED`；
  3. `append_audit(action="work_log.owner_attested", resource=artifact, before/after=review_status, idempotency_key="audit:work_log:attest:{artifact_id}")`。

**并发仲裁（第 2 轮评审点 2 —— 数据库级，不靠「先读后写」乐观假设）**：
`Approval` 无通用幂等键，且 `uq_approval_gate_round` 含可空 `review_policy_id`（SQLite 视 NULL 互异），无法防两个会话同时观察到 `UNVERIFIED` 后重复插入 attestation Approval；而 AuditLog 幂等键唯一，会让并发败者事务硬失败而非干净重放。仲裁方案：

1. `attest_work_log` 在读取 artifact **之前**以 `BEGIN IMMEDIATE` 开启事务（SQLite 立即取 RESERVED 写锁，串行化所有写者；经 SQLAlchemy `session.connection().exec_driver_sql("BEGIN IMMEDIATE")` 或等价 event hook 实现，仅限本方法）。
2. 取得写锁后**重读** artifact 状态与既有证据，再按当前状态裁决（见下）。
3. 并发败者（等锁期间赢者已提交）在步骤 2 会看到 `APPROVED` + 完整证据 → 走幂等 no-op 分支，**两个会话都成功返回**；全程恰好 1 条 Approval、1 条 AuditLog、1 次状态翻转。
4. 兜底：若仍发生 `IntegrityError`（如 AuditLog 幂等键撞车），捕获后回滚、重新 `BEGIN IMMEDIATE` 读取一次，按已提交状态重新裁决返回——不向调用者泄漏 IntegrityError。

**幂等与 fail-closed 语义（穷尽）**：
- `UNVERIFIED` → 执行三件套写入，返回 200/updated；
- `APPROVED` **且** 匹配的 attestation `Approval(action_type="work_log_attestation", target_artifact_id=id, status=APPROVED)` 与 `AuditLog(idempotency_key="audit:work_log:attest:{id}")` **两者俱在** → 幂等 no-op 返回既有结果；
- `APPROVED` 但证据缺失或冲突（缺 Approval、缺 AuditLog、或存在非 APPROVED 的 attestation 行）→ **fail-closed**：`ServiceError(409, "approved work log with missing/conflicting attestation evidence")`，**绝不**静默返回，也不补写证据（证据破损须人工排查）；
- 其他状态（`REVIEW_PASSED`/`REJECTED` 等）→ 409。
- **不触碰**现有 review 管线的任何不变量：不产生 `review_gate` Approval、不涉及 `ReviewAssignment`/round；`action_type` 不同故不受 review-gate 唯一约束影响；`owner_approve_review` 仍是 reviewer 管线 artifact 的唯一批准路径。
- **收割前置由此闭环**：`submit_candidate` 只见 `APPROVED` 的日志，且该状态必有 Approval + AuditLog 证据。

---

## 8. 内容价值判定 / 收割 / 供给

### 8.1 `ContentValueJudge.judge(metadata) -> (content_value, should_enter_kb, content_angle)`
- 纯函数，**无 LLM**。
- `should_enter_kb`：以 `metadata.should_enter_kb` 显式勾选为准（默认 False）。
- `content_value`：调用者显式指定则用之；否则启发式——`new_knowledge` 命中关键词（实验/踩坑/决策/数据/对比/结论）且长度 > 50 → `medium`，否则 `low`。
- `content_angle`：优先 `metadata.content_angle`，否则 `new_knowledge` 前 80 字符截断。
- 仅用于收割筛选与展示，**不影响候选→事实的人工审核**。

### 8.2 `KnowledgeHarvester.harvest_candidates(session, *, actor: ActorContext)`
- **actor 由调用边界传入**（脚本经 owner 凭证认证后注入；服务内**不**调 `resolve_owner_actor()`——评审点 2）；校验 `kind=="owner"`。
- 扫描 `Artifact(type=WORK_LOG, review_status=APPROVED)` 中 `should_enter_kb=true` 或 `content_value in (high, medium)` 且 `NOT EXISTS` 关联候选的日志。
- 对每条调用 `submit_candidate(artifact.id, statement=metadata.new_knowledge, project_id=artifact.project_id, tags=<确定性映射>, actor=actor)`。
- **tags 确定性映射（第 2 轮评审点 1 —— 只用现有 canonical registry，不扩 taxonomy）**：
  - 现实约束：`normalize_tags()` 只接受 `CANONICAL_KNOWLEDGE_TAGS`（`knowledge_tags.py`，7 个：`user_research / positioning / wechat_writing / xhs_adaptation / video_script / packaging / knowledge_capture`），任何未知 tag → 422。v2 自创的 7 个 work-log tag 全部会被拒。
  - **选定方案 A（评审推荐）：确定性映射到既有 canonical tags，不改 registry、不 bump `CAPABILITY_TAG_MAP_VERSION`**：
    - **恒有 `"knowledge_capture"`**（工作日志知识收割的本质即知识捕获）；
    - 其余 tag 仅在**已持久化的输入**明确证立时追加（纯函数、大小写不敏感的关键词匹配，作用于 `metadata.new_knowledge + content_angle` 文本）：命中 `公众号|微信|wechat` → `wechat_writing`；命中 `小红书|xhs` → `xhs_adaptation`；命中 `视频脚本|video script` → `video_script`；命中 `定位|positioning` → `positioning`；命中 `用户调研|访谈|user research` → `user_research`；命中 `包装|封面|排版|packaging` → `packaging`。
    - 输出全集 ⊆ `CANONICAL_KNOWLEDGE_TAGS`，经 `normalize_tags()` 去重排序后必然通过；映射为纯函数、可穷尽测试。
  - 显式**不做**方案 B（扩展 `CANONICAL_KNOWLEDGE_TAGS`/`CAPABILITY_KNOWLEDGE_TAGS`/map version/readiness 投影行为）——那是独立评审范围，超出 Issue #88。
  - work-log 维度信息（report_type / content_value / pitfall 等）**不进 tags**——它们已完整持久化于日志 `metadata_json`，feed 的日志条目直接展示，无须挤入 knowledge taxonomy。
- 不自动 APPROVE——只建 `DRAFT` 候选。

### 8.3 `ContentFeed.get_content_feed(session, *, actor, project_id=None, min_value="medium", limit=100, offset=0) -> list[dict]`
- **范围（精确定义——评审点 5）：**
  - `project_id=None`（公司视图）：全部 project 的合格日志 + **全部** `APPROVED` 事实（含公司 scope 与所有 project scope）；
  - `project_id=P`（项目视图）：仅 P 的合格日志 + P scope 的 `APPROVED` 事实 + **公司 scope（`project_id IS NULL`）的 `APPROVED` 事实**（公司级知识对所有项目可见，与 KnowledgeService 的 scope 语义一致）；**绝不**含其他 project 的日志或事实（无跨项目泄漏）。
  - 日志合格线：`type=WORK_LOG` 且 `review_status=APPROVED` 且 `content_value >= min_value`（序：high > medium > low > none）。
  - 事实合格线：`KnowledgeFact.status == APPROVED`（`uq_knowledge_fact_approved_head` 保证每 series 单一 APPROVED head——同 series 旧版本为 `SUPERSEDED`，不重复出现）。
- **排序（稳定）**：`created_at DESC, id DESC`（id 为决定性 tiebreaker）。
- **分页**：`limit`（默认 100，上限 500）/ `offset`；同一快照内无重复无跳漏。
- **去重**：日志按 `Artifact.id` 唯一；事实按 `KnowledgeFact.id` 唯一。
- **字段**：日志条目 `{kind:"work_log", id, project_id, report_type, content_value, content_angle, new_knowledge, created_at}`（**无 tags**——日志 metadata 无 tags 字段，评审点 5）；事实条目 `{kind:"fact", id, series_id, version, project_id, statement, tags, created_at}`（事实的 tags 来自 §8.2 确定性映射，恒 ⊆ `CANONICAL_KNOWLEDGE_TAGS`，可信）。
- **只读**，不接实时 API；导出见 §10。

---

## 9. API 端点（复用现有惯例）

- `POST /work-logs`
  - 请求体：`{project_id, report_type, task_ref?, produced_by_agent_id?, execution_assignment_id?, what_done, why, problem, solution, new_knowledge, content_value?, should_enter_kb?, content_angle?}`（`execution_assignment_id` 规则见 §6：routed task 上提供 agent provenance 时必填）
  - `Idempotency-Key` 头必填（缺失→422）；`actor = Depends(authenticate_owner)`；语义按 §5（200 replay / 201 created / 409 键复用改载荷）。
- `POST /work-logs/{artifact_id}/attest`
  - 无 body；`actor = Depends(authenticate_owner)`；调 `attest_work_log`；幂等（已 APPROVED → 200 no-op）。
- `GET /content-feed?project_id=&min_value=medium&limit=&offset=`
  - `actor = Depends(authenticate_owner)`（只读）。
- `ServiceError` → `_translate` → HTTPException（现有惯例）。

> 不在本计划内：日志修改/删除端点、候选自动审核、Hermes 推送端点。

---

## 10. 脚本（非技术可跑；均经 owner 凭证认证边界注入 actor，不自铸）

- `scripts/submit_work_log.py`：读 JSON/交互填 7 字段 → 认证 → `submit_work_log`（生成并显示所用 Idempotency-Key，支持 `--idempotency-key` 重放）。
- `scripts/attest_work_log.py`：列出 `UNVERIFIED` 日志 → owner 逐条确认 → `attest_work_log`。
- `scripts/harvest_candidates.py`：认证 → `harvest_candidates(actor=...)`，打印新建候选数。
- `scripts/export_content_feed.py`：`get_content_feed` 导出 Markdown（供 Hermes 取材，MVP 用导出）。

---

## 11. 测试清单（TDD）

**模型/枚举**
- `test_artifact_type_has_work_log`；`test_agent_has_platform_external_ref`。

**迁移（评审点 4：fail-closed 策略）**
- `test_work_log_migration_round_trip`：**空 0009 数据**时 0008→0009→0008→0009 无损往返；断言列/部分唯一索引 `uq_artifact_idempotency` 存在与消失；head bump 断言。
- `test_migration_0009_downgrade_fail_closed_populated`：分别 seed （a）`agent.platform` 非空、（b）`artifact.idempotency_key` 非空 → downgrade 在任何 DDL 前 `RuntimeError`；schema/行/索引/revision 完整停留 0009。
- `test_artifact_trigger_survives_0009`：升级后 `knowledge_candidate_validate_insert` 触发器仍在且生效（raw ALTER 未惊动 artifact 表）。

**提交入口 / 校验（评审点 1、2）**
- `test_submit_work_log_creates_unverified_artifact`：新日志 `review_status==UNVERIFIED`。
- `test_unattested_work_log_cannot_be_harvested`：`UNVERIFIED` 日志即使 `content_value=high` 也不产候选（`submit_candidate` 的 APPROVED 前置生效）。
- `test_attest_work_log_writes_evidence`：attest 后 `APPROVED` + `Approval(action_type="work_log_attestation", risk_level=L1)` + `AuditLog(work_log.owner_attested)` 齐备。
- `test_attest_work_log_idempotent`：重复 attest（证据齐备）→ no-op，Approval/AuditLog 不重复。
- `test_attest_work_log_concurrent_two_sessions`：**真实双会话并发**（两个独立 Session/连接同时 attest 同一 UNVERIFIED 日志，`BEGIN IMMEDIATE` 仲裁）→ 恰好 1 条 Approval、1 条 AuditLog、1 次状态翻转，**两个会话均成功返回**（赢者 updated、败者幂等 no-op）。
- `test_attest_fail_closed_missing_evidence`：手工把日志置 `APPROVED` 但不写 Approval/AuditLog（分别缺其一与全缺）→ 再 attest 报 409 fail-closed，**不**静默返回、**不**补写证据。
- `test_attest_requires_owner_actor`：非 owner actor → 403。
- `test_submit_rejects_unknown_project` / `test_submit_rejects_task_project_mismatch`。
- `test_agent_provenance_requires_task_ref`：给 `produced_by_agent_id` 不给 `task_ref` → 422。
- `test_agent_provenance_requires_assignment_id_when_routed`：task 有 ≥1 条 assignment 但未给 `execution_assignment_id` → 422（歧义拒绝）。
- `test_agent_provenance_accepts_exact_assignment`：提供的 `execution_assignment_id` 存在、`task_id` 匹配、`selected_agent_id == produced_by_agent_id` → 通过且 `provenance_json` 记录**该条** assignment id。
- `test_agent_provenance_multiple_assignments_fallback`：同一 task 两条 assignment（原始 + fallback）→ 只有指向实际执行 agent 的那条 id 通过；用另一条（agent 不符）→ 422。
- `test_agent_provenance_rejects_cross_task_assignment`：assignment id 属于别的 task → 422。
- `test_agent_provenance_rejects_preferred_not_selected`：仅 `preferred_agent_id` 匹配（assignment 指向他人 agent）→ 422。
- `test_agent_provenance_legacy_assigned_agent`：task 零条 assignment、未给 assignment id、`task.assigned_agent_id` 匹配 → 通过（legacy 兼容，`legacy_assigned_agent=true`）；同场景**给了** assignment id → 422。
- `test_provenance_agent_never_becomes_actor`：agent 引用只出现在 `provenance_json`，`submitted_by` 恒为 owner。

**幂等（评审点 3）**
- `test_submit_replay_same_key_same_payload`：同键同载荷 → 返回同一 Artifact，无第二行。
- `test_submit_same_key_different_payload_409`：同键改载荷 → 409。
- `test_submit_concurrent_duplicate`：预插同存储键行模拟并发 → IntegrityError 被捕获并按指纹裁决。
- `test_submit_retry_across_midnight`：冻结时钟跨 UTC 午夜重放同键 → 仍返回既有 Artifact（键不含日期）。

**判定 / 收割 / 供给**
- `test_content_value_judge_should_enter_kb_true` / `test_content_value_judge_short_text_low`。
- `test_harvest_tags_deterministic`：给定 metadata → tags 精确等于 §8.2 方案 A 映射结果，且恒含 `knowledge_capture`。
- `test_harvest_tags_all_canonical`：映射输出对任意输入恒 ⊆ `CANONICAL_KNOWLEDGE_TAGS`，`normalize_tags()` 全部通过（无 422）。
- `test_harvest_tags_keyword_hits`：文本命中 `小红书`/`公众号` 等关键词 → 对应 canonical tag 追加；无命中 → 仅 `knowledge_capture`。
- `test_harvest_creates_candidate_from_high_value` / `test_harvest_skips_none_and_false` / `test_harvest_idempotent` / `test_harvest_never_auto_approves`。
- `test_harvest_requires_injected_owner_actor`：actor 缺失/非 owner → 拒绝（服务不自铸）。
- `test_feed_project_includes_company_facts`：项目视图含公司 scope APPROVED 事实 + 本项目事实/日志。
- `test_feed_no_cross_project_leakage`：项目视图绝不含其他 project 的日志或事实。
- `test_feed_stable_order_and_pagination`：`created_at DESC, id DESC` 稳定序；分页无重复无跳漏。
- `test_feed_only_approved_facts_single_head`：SUPERSEDED 事实不出现；同 series 只出现 APPROVED head。
- `test_feed_log_entries_have_no_tags_field`：日志条目无 tags 字段。

---

## 12. 验收命令

```bash
# lint（ruff 0.15.22, line-length 100）
aios-v0/.venv/Scripts/python -m ruff check src tests alembic

# 聚焦测试
pytest tests/test_work_log.py tests/test_knowledge_models.py tests/test_review_binding_migration.py -q

# 端点冒烟（TestClient）
pytest tests/test_api_work_log.py -q

# 全量（以 exact-head CI 为准）
pytest -q
```

验收门槛：聚焦 + 全量 `pytest` 绿；`ruff` 绿；**exact-head CI 绿**；head 断言已 bump 到 `20260728_0009`。

---

## 13. Out of scope（不扩大）

- ChatGPT/Hermes/Coze 的 API/MCP 自动采集（V2）。
- `ContentValueJudge` 的 LLM 自动打分（V3）。
- AIOS 统一 Agent 中台、自动注册实体（V4）。
- 日志修改/删除端点、候选自动审核、实时 Hermes 推送、任何写外部副作用。
- 除 `ArtifactType.WORK_LOG` / `Artifact.idempotency_key` / `Agent.platform`+`external_ref` 之外的模型变更。

---

## 14. TDD 实施顺序（实现 PR 采用）

1. **模型**：`ArtifactType.WORK_LOG` + `Artifact.idempotency_key`（无约束标注）+ `Agent.platform/external_ref`。
2. **迁移** `20260728_0009`：upgrade（raw ALTER + 部分索引）/ downgrade（fail-closed 预检）+ head bump + §11 迁移测试。
3. **服务** `work_log.py`：`submit_work_log`（UNVERIFIED + 幂等契约）→ `attest_work_log`（证据三件套）→ `ContentValueJudge` → `KnowledgeHarvester`（注入 actor + 确定性 tags）→ `ContentFeed`（scope/排序/分页）。先写测试。
4. **API**：`POST /work-logs`、`POST /work-logs/{id}/attest`、`GET /content-feed`。
5. **脚本**：四个脚本（含 `attest_work_log.py`）。
6. **测试**：§11 全量 + `test_api_work_log.py`。
7. **验收**：§12 全绿 + exact-head CI 绿。

---

## 15. v2 修订摘要（对照 PR #90 评审五点）

| # | 评审阻断点 | v2 落点 |
|---|-----------|---------|
| 1 (P0) | 不得直接创建 `APPROVED` Artifact | §7：提交只产 `UNVERIFIED`；新增显式原子 owner-attestation 契约 `attest_work_log`（Approval + AuditLog + 状态翻转单事务），不绕过、不削弱现有 `owner_approve_review` 不变量 |
| 2 (P0) | 不得自铸 owner 身份；校验真实执行者 | §6/§8.2：所有服务方法显式接收边界注入的 `actor`，服务/脚本内禁用 `resolve_owner_actor()`；agent 归属证明改用 `ExecutionAssignment.selected_agent_id`（legacy `assigned_agent_id` 兼容），拒绝 `preferred_agent_id`；无 `task_ref` 的 agent 声明一律拒绝 |
| 3 (P1) | 统一幂等契约与 schema | §5：单一契约=必填 `Idempotency-Key` 传入服务，命名空间化 SHA-256 存储键 + canonical-JSON 请求指纹，replay/409/并发三情形穷尽；无日期成分；§3.1：模型无 `unique/index` 标注，唯一性只由命名部分唯一索引表达 |
| 4 (P1) | 降级不得虚构无损保留 | §3.2：选定 **fail-closed**——0009 字段有数据即在 DDL 前中止；只承诺空数据往返无损；§11 两个对应测试 |
| 5 (P1) | 消除占位符、精确定义 feed | §8.2：tags 封闭 taxonomy + 确定性纯函数映射；§8.3：项目视图含公司 scope 事实的明确规则、稳定排序、分页、去重；日志条目移除 tags 字段 |

---

## 16. v3 修订摘要（对照 PR #90 第 2 轮评审三点）

| # | 阻断点 | v3 落点 |
|---|--------|---------|
| 1 (P0) | 自创 tags 会被 `normalize_tags()` 全部 422 | §8.2：选定**方案 A**——确定性映射到既有 `CANONICAL_KNOWLEDGE_TAGS`（恒 `knowledge_capture` + 仅由已持久化文本关键词证立的其他 canonical tag），不改 registry、不 bump `CAPABILITY_TAG_MAP_VERSION`；显式排除方案 B；work-log 维度信息留在 `metadata_json` 不进 tags；§11 新增 3 个 tags 测试 |
| 2 (P0) | `RiskLevel.LOW` 不存在；attestation 无并发仲裁；APPROVED 证据缺失静默返回 | §7.2：`risk_level=RiskLevel.L1`（含理由）；`BEGIN IMMEDIATE` 写锁 → 锁下重读重裁决 → 败者幂等 no-op，`IntegrityError` 兜底重读；幂等语义穷尽四情形，`APPROVED` 但证据缺失/冲突 → **409 fail-closed** 绝不静默；§11 新增双会话并发测试 + 证据缺失 fail-closed 测试 |
| 3 (P1) | `ExecutionAssignment.task_id` 非唯一，仅凭 task 选取 assignment 歧义 | §6/§9：请求契约新增可选 `execution_assignment_id`；routed task 上提供 agent provenance 时**必填**，四项精确校验（存在 / task 匹配 / selected_agent 匹配 / project 归属）；legacy 仅限零 assignment 且未提供 id；provenance 记录该条 id + `legacy_assigned_agent` 标志；§11 新增多 assignment/fallback、跨 task id、歧义拒绝测试 |

> 本文件止步于计划。批准后由实现 PR 按 §14 顺序落地；评审通过 + exact-head CI 绿后，依铁律设 `gate:merge` 等 owner `授权合并`。
