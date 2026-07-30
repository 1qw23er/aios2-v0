# 实施计划（修订版 v3）：#108 个人 IP 内容与变现工作流（V1.2-A）

> 关联 Issue **#108**；设计文档 `docs/plan-v1.2-ip-ops.md` §2/§6/§7。
> 状态：**APPROVED FOR IMPLEMENTATION**（owner CONDITIONAL APPROVE 2026-07-31，确认 3 项契约后批准）。仍为**零代码、零迁移** PR。
> 协议：设计/假设/进度以 Issue·PR·仓库文档为唯一事实源；严格遵循协作协议（Codex APPROVE + exact-head CI + owner 门禁）。
> 流程：本计划经 `gate:merge` 由 owner 授权 squash-merge 到 `main` 后，**方可**开实现 PR `feat/issue-108-a-impl`（TDD + Codex + 门禁）；未合并前不实现。

## 0. 范围与边界（铁律）
- MVP 旅程：选题 → 策划 → 内容生产 → 独立审核 → **L4 人工批准** → 转化动作记录 + 复盘。
- 首个变现产品（D1）：闲鱼 / 电商自动化代运营（SOP / 模板包）。
- **铁律**：
  - 不自动发布、不自动承诺效果 / 价格、不自动收款（MVP 全手动）。
  - **内容审批绝不自动创建 `KnowledgeFact`**；内容正文 / 营销话术 / 价格 / 行动号召(CTA) / 复盘指标**不得自动注入 Agent 知识**。复盘经验只能由 owner 手动提 `KnowledgeCandidate`（走既有独立审核路径）。
  - 已批准的 `CONTENT_DRAFT` 仍是「已批准的 Artifact」，其快照（metadata_json / checksum / review 状态）**只读冻结**。
  - 所有身份（actor / owner / agent / producer / reviewer / approval / assignment）**一律服务端派生**，请求体/查询/头不得指定。

## 1. 数据模型变更（零迁移，附证明要求）
- `ArtifactType` 增 `CONTENT_DRAFT = "content_draft"`（StrEnum→VARCHAR，纯代码新增，无 Alembic 迁移；现有值 MARKDOWN/JSON/IMAGE/VIDEO/DATASET/LINK/WORK_LOG）。
- **草稿业务字段存于 `Artifact.metadata_json`**（`models.py:403`，JSON 列，列名 `metadata`）。注意 `Artifact` **没有 `payload` 列**（`payload` 是 `Event` 的列，`models.py:502`）；正文存于必填 `uri`（`models.py:382`）；`checksum`（`models.py:383`，必填）为 body 的 sha256，**批准后即冻结**。
  - `metadata_json` 字段：`topic`(str)、`phase`(str: `idea`→`outline`→`draft`)、`outline`(str/JSON)、`conversion_anchors`(JSON list: `[{type: consult|product|course|template, label, url}]`)、`series_id`(str, **可选，纯人工组织标签，默认 "黎叔AI创业实验室"，但绝不驱动 KnowledgeFact 创建**)、`independent_review`(见 §4)、`review_history`(见 §2)。
- **修订/状态版本复用现有 `Artifact.revision_count`**（`models.py:392`，`int = Field(default=0)`）：任何允许的内容编辑原子递增该值，作为「被审核修订」的版本号，**零迁移**（列已存在）。
- **审批状态机复用 `ArtifactReviewStatus`**（`models.py:90`）：`UNVERIFIED` → `NEEDS_REVISION`/`REVIEW_PASSED`(独立审核) → `APPROVED`/`REJECTED`(owner L4)。不新增枚举。
- **终端批准记录复用 `Approval`**（`models.py:458`，唯一约束 `uq_approval_gate_round`(target_artifact_id, review_policy_id, review_round, action_type)）：content 批准用 `target_artifact_id=content_id, review_policy_id=NULL, review_round=1, action_type="CONTENT_APPROVE"|"CONTENT_REJECT"`，重复插入触发 `IntegrityError` → 稳定 409。
- **审计复用 `AuditLog`**（`src/aios/audit.py`，append-only 惰性表）：
  - 用于批准跃迁（§3）**与**追加写复盘指标（§2b）。
  - 精确字段（`audit.py`）：`id, actor, action(index), resource_type, resource_id, project_id(nullable,index), task_id(nullable), before_snapshot(JSON), after_snapshot(JSON), idempotency_key(UNIQUE,index), created_at`。**无 `status`/`attempt_count`/`processed_at`/`last_error`** → 纯追加、不可变、无消费者（惰性）。`redact_secrets` 保证无凭据泄露，且不误伤内容正文。
- **复盘指标原语：弃用 `Event`，改用 `AuditLog`（惰性）**。理由（`models.py:495`）：`Event` 含 `status=EventStatus.PENDING`(默认) + `attempt_count` + `processed_at` + `last_error` → 具**投递/重试/outbox 语义**（疑似 worker 消费），违反契约 2「不触发编排/投递」。改用 `AuditLog`（无交付语义）→ 零迁移、惰性、满足全部保证。
- **结论**：零 Alembic 迁移，单 head `20260730_0001` 不变。§6 用测试证明。

## 2. 服务层（新增 `src/aios/content_draft.py`）
复用 #88 `attest_work_log` 的「原子三元组」思想，但**用 SQLite `BEGIN IMMEDIATE` + CAS 替代悲观行锁表述**（见 §3）。
- `create_content_draft(session, actor, *, project_id, topic, outline=None, body=None, conversion_anchors=None, phase="idea", series_id=None)` → 建 `Artifact(type=CONTENT_DRAFT, review_status=UNVERIFIED, revision_count=0, uri=<body 或 content-draft://{id}>, checksum=sha256(body), metadata_json={...})`。`actor` 来自 `authenticate_owner`/`authenticate_agent`（服务端派生）；**producer 溯源**写入 `metadata_json.independent_review.producer` 仅在 submit 时（create 时 producer 为创建者身份，待 submit 审核时落库）。
- `update_content_draft(...)` → 改 topic/outline/body/anchors/phase/series_id（仅 `UNVERIFIED`/`NEEDS_REVISION` 可改）。**原子编辑契约（契约 1）**：
  1. 更新 `metadata_json` 业务字段（payload）；
  2. 重算 `checksum = sha256(body)`；
  3. **递增 `revision_count += 1`**；
  4. **重置 `review_status = UNVERIFIED`**；
  5. 将既有 `metadata_json.independent_review`（若有）**移入 `metadata_json.review_history`（追加数组，保留但不可用于批准）并清空 `independent_review`**。
  全程在单事务内 all-or-none。`APPROVED`/`REVIEW_PASSED` 改 → 409。
- `submit_content_draft(session, actor, id)` → 触发**独立审核**（§4 `content_independent_review`），将独立审核记录持久化到 `metadata_json.independent_review`（含 reviewed_checksum/reviewed_revision 等），**永不自动 APPROVED**：通过 → `REVIEW_PASSED`；问题/异常/低置信 → `NEEDS_REVISION` + 报告入 `metadata_json`。均不触碰 `APPROVED`。
- `approve_content_draft(session, actor, id)` → **owner-only L4**（依赖 `actor.kind == "owner"`，由 `authenticate_owner` 保证）。**原子契约见 §3，绑定精确修订（契约 1）**。**不创建 `KnowledgeFact`，不使用 `series_id` 创建任何知识对象**。
- `reject_content_draft(session, actor, id, *, reason)` → **owner-only**：原子契约（§3）将 `review_status=REJECTED`，写 `Approval(REJECTED)` + `AuditLog`；reason 入 `metadata_json`。驳回后需重新 submit 才能再 approve。
- `record_review_metrics(session, actor, id, *, metrics, idempotency_key)` → **owner-only**：**不改动**被批准 Artifact 的 `metadata_json`/`checksum`/`review_status`；仅**追加写**一条 `AuditLog(action="content_review_metric")`（`type` 语义见 §2b）。

### 2b. 追加写复盘指标记录（惰性原语 `AuditLog`）
写入前**校验该 Artifact 为 `APPROVED`**（服务层强制「仅引用已批准 content Artifact」）；非 APPROVED → 409/422。
- **链接**：`AuditLog.resource_type="artifact"`, `AuditLog.resource_id = <被批准 content artifact id>`。
- **指标 schema**：`AuditLog.after_snapshot.metrics = {exposure:int, consult:int, conversion:int, period_start:iso8601|null, period_end:iso8601|null, channel:str|null}`。
- **recorded_by**：`AuditLog.actor = actor.owner_id`（**服务端派生自 `authenticate_owner`**，永不取请求）。
- **recorded_at**：`AuditLog.created_at`（服务端 UTC），并冗余 `after_snapshot.recorded_at`。
- **idempotency_key**：`AuditLog.idempotency_key`（表级 UNIQUE） → 同 key 重复写入 → `IntegrityError` → 返回既有记录（200，不新增）。
- **去重行为**：同 key 幂等，不重复插入。
- **更正行为**：**新记录** `AuditLog(action="content_review_metric", after_snapshot.supersedes_audit_id=<原 audit id>, after_snapshot.correction=true, idempotency_key=新key)`；**原记录不可变**（无 UPDATE 路径）；查询返回「未被取代的最新记录」。
- **审计字段排除内容正文**：`after_snapshot` 仅含 `resource_id`/`metrics`/`recorded_by`/`recorded_at`/`supersedes_audit_id`；**不得复制 content body / outline / conversion_anchors 原文**。
- **作用域**：`AuditLog.project_id` = content artifact 的 project_id，杜绝跨项目读取。
- **惰性契约保证（契约 2）**：
  - 插入**不触发**任务编排、下游 ready 事件、重试、outbox 投递或其他消费者（`AuditLog` 无任何消费者 worker；代码仅有 `append_audit` 写路径，无 UPDATE/DELETE）；
  - `idempotency_key` 数据库唯一强制（去重/幂等）；
  - 插入后**不可变**（无 UPDATE 路径）；
  - **仅引用 APPROVED** content Artifact（服务层校验）；
  - **不改** Artifact 的 `metadata_json`/`checksum`/`review_status` 或 `Approval`；
  - 更正用**不可变向后 supersession 引用**（`supersedes_audit_id`）；
  - 要求 superseded 记录 `resource_id` 与更正记录**同一 content Artifact**；
  - **拒绝分支/环/覆盖**（不可变 + 引用约定强制；无覆盖路径）。
  - §7 以测试断言以上全部（尤其「无编排/投递副作用」「不可变」）。

## 3. 审批原子契约（SQLite `BEGIN IMMEDIATE` + CAS，非悲观行锁）
**严禁** SELECT FOR UPDATE / 泛型「pessimistic row lock」措辞。统一用 **单条 `BEGIN IMMEDIATE` 事务**（SQLite 立即取 RESERVED 锁，写冲突串行化）。

### 3a. approve_content_draft 事务内步骤（绑定精确修订，契约 1）
```
connection.execution_options(isolation_level="IMMEDIATE")  # 或 exec_driver_sql("BEGIN IMMEDIATE")
art = session.get(Artifact, id)                             # 事务内重读当前态
if art is None: raise NotFound
if art.type != CONTENT_DRAFT: raise Conflict("not content draft")
# —— 精确修订绑定（契约 1）——
ir = art.metadata_json.get("independent_review")
if ir is None: raise Conflict("no independent review")                 # 未审核
if art.checksum != ir["reviewed_checksum"]: raise Conflict("stale checksum")     # 409 陈旧
if art.revision_count != ir["reviewed_revision"]: raise Conflict("stale revision") # 409 陈旧
if art.review_status != REVIEW_PASSED: raise Conflict("requires REVIEW_PASSED")  # 已 APPROVED/REJECTED/NEEDS_REVISION/UNVERIFIED 均拒
# 条件/CAS 更新（防御性再校验，确保提交瞬间仍一致）
rc = session.execute(
    update(Artifact).where(
        Artifact.id==id,
        Artifact.review_status==REVIEW_PASSED,
        Artifact.checksum==ir["reviewed_checksum"],
        Artifact.revision_count==ir["reviewed_revision"],
    ).values(review_status=APPROVED)
).rowcount
if rc != 1: raise Conflict("status/checksum/revision changed concurrently")  # CAS 失败
session.add(Approval(target_artifact_id=id, review_policy_id=None, review_round=1,
                     action_type="CONTENT_APPROVE", status=APPROVED, ...))   # 重复→IntegrityError→409
session.add(AuditLog(actor=actor.owner_id, action="content.approve", resource_type="artifact",
                     resource_id=id, project_id=art.project_id,
                     before_snapshot={"review_status":"review_passed"},
                     after_snapshot={"review_status":"approved"},
                     idempotency_key=<唯一>))                 # 仅状态跃迁+溯源，无正文
# commit all-or-none；异常→rollback→无残留
```
- 失败任一步（CAS/Approval/AuditLog）→ 整事务回滚，无部分 `Approval`/状态/`AuditLog`。
- 陈旧审核（checksum 或 revision 不匹配）→ 稳定 409。
- 并发第二写者：`BEGIN IMMEDIATE` 在首事务提交前阻塞；获锁后 CAS 发现状态/校验和不匹配或 `Approval` 唯一约束命中 → 稳定 409。

### 3b. reject_content_draft 同构
- `BEGIN IMMEDIATE`；要求当前 `review_status in (REVIEW_PASSED, NEEDS_REVISION)`；CAS `UPDATE ... SET review_status=REJECTED WHERE id=? AND review_status IN (REVIEW_PASSED, NEEDS_REVISION) [AND checksum/revision 匹配]`；插入 `Approval(action_type="CONTENT_REJECT", status=REJECTED)` + `AuditLog(action="content.reject")`；commit all-or-none。

## 4. 独立审核（content_independent_review，真正独立）
- **重命名** `content_self_review` → `content_independent_review`（它是独立审核，非自评）。
- **持久化独立审核记录**（`metadata_json.independent_review`，契约 1 要求字段）：
  - `artifact_id`（冗余，= 本 artifact id）；
  - `reviewed_checksum`（审核时 `art.checksum`）；
  - `reviewed_revision`（审核时 `art.revision_count`）；
  - `producer = {agent_id, task_id}`（草稿创建者，服务端 `ActorContext` 派生）；
  - `reviewer = {agent_id, task_id}`（**assigned reviewer**，服务端 `ActorContext` 派生）；
  - `result ∈ {REVIEW_PASSED, NEEDS_REVISION}`；
  - `confidence: float`（有界，0..1）；
  - `bounded_reason: str`（长度上限，防注入过载）；
  - `reviewed_at: iso8601`。
- **reviewer 必须 ≠ producer**（二者均为 agent 时）；相等 → 审核拒绝（provenance 校验失败）。
- **reviewer 为 assigned reviewer**：审核由服务端指派的可信适配器身份执行，**非请求指定**；reviewer≠producer 强制（契约 3 要求「assigned reviewer only, reviewer different from producer」）。
- 上述身份**全部来自可信运行时 `ActorContext`，绝不取请求字段**。
- **自动化审核仅可产出**：`REVIEW_PASSED` 或 `NEEDS_REVISION`。错误 / 输出畸形 / 低置信 → `NEEDS_REVISION`。**永不可产出 `APPROVED`**。
- **适配器门控**：`ReviewAdapter.review(payload) -> ReviewResult(status, report, confidence)`。
  - 默认/测试用 **`FakeReviewAdapter`**（确定性规则，无网络、零付费调用）。
  - 若接真实 LLM：须显式凭据（secret-store ref）+ 真实模型调用开关 + **cost owner 门控**（谁承担费用，owner 授权）；仅在该配置下启用。
  - **测试与默认执行一律用 `FakeReviewAdapter`，绝不发起付费模型调用**。

## 5. 端点鉴权矩阵（`src/aios/api/app.py`，per-Artifact，同项目内）
| 端点 | 鉴权 | 身份来源 | 作用域（同一 project 内） |
|---|---|---|---|
| `POST /content-drafts` | owner **或** 注册 Agent | `authenticate_owner` / `authenticate_agent` → `ActorContext` | agent 限其 project/scope |
| `GET /content-drafts` | **owner 或 显式相关 producer/reviewer** | `ActorContext` | **同项目无关 Agent → 403，且不得经 list 学到任何草稿正文** |
| `GET /content-drafts/{id}` | **owner 或 显式相关 producer/reviewer** | `ActorContext` | 跨项目/无关 → 403/404，无泄露 |
| `PATCH /content-drafts/{id}` | **owner 或 可信 producer/assigned producer** | `ActorContext` | 仅未批准态；scope 一致 |
| `POST /content-drafts/{id}/submit` | **owner 或 可信 producer/assigned producer** | `ActorContext` | scope 一致；触发独立审核 |
| `POST /content-drafts/{id}/approve` | **`authenticate_owner` 强制** | owner `ActorContext` | — |
| `POST /content-drafts/{id}/reject` | **`authenticate_owner` 强制** | owner `ActorContext` | — |
| `POST /content-drafts/{id}/metrics` | **`authenticate_owner` 强制** | owner `ActorContext` | — |
| （review，内部） | submit 内部触发，**assigned reviewer only**（reviewer ≠ producer） | 服务端指派适配器身份 | 非请求端点 |
- **硬规则**：请求体/查询/头**不得**指定 `actor`/`owner_id`/`agent_id`/`reviewer_id`/`producer`/`assignment`/`project`/审批身份；一切服务端派生。违者忽略或 422。
- owner-only 端点缺 `authenticate_owner` → 401/403；非 owner → 403。
- **同项目无关 Agent**：即便同 project，只要不是该 Artifact 的 owner / producer / reviewer，读(list/get)/改(PATCH)/submit 一律 403，且 list 响应过滤掉其无权访问的草稿（不得泄露正文或元数据）。

## 6. 零迁移假设的证明（测试必须）
计划要求如下检查/测试，证明 SQLite DDL、触发器、枚举持久化接受 `CONTENT_DRAFT` 而**无需 schema 变更**：
- **全新库测试**：建库 → 应用现有迁移（head `20260730_0001`）→ 插入 `Artifact(type=CONTENT_DRAFT)` → 读回断言 type 保留。
- **存量库升级路径测试**：从既有迁移状态起，应用**零新增迁移** → 断言 `CONTENT_DRAFT` 被接受。
- **枚举往返**：`CONTENT_DRAFT` create/read round-trip，断言无 CHECK/触发器拒绝（含核对外部触发器 `knowledge_candidate_validate_insert` 仅 guard `WHEN NEW.type='knowledge_candidate'`，不拒绝 `content_draft`；在触发器激活下插入 `CONTENT_DRAFT` 成功）。
- **Alembic head**：`alembic heads` == 1 且 == `20260730_0001`；`migrations/versions/` **无新增迁移文件**。
- **惰性证明**：`record_review_metrics` 插入后断言 (a) 被引用 Artifact 的 `metadata_json`/`checksum`/`review_status` 不变；(b) 无新增 `Event`/`Task`/`DelegatedRun`（无编排/投递副作用）；(c) `AuditLog` 仅增 1 行且 `after_snapshot` 不含正文。
- 在 `tests/test_content_draft_zero_migration.py` 落实上述断言。

## 7. TDD 测试计划（`tests/test_content_draft.py` + `tests/test_content_draft_zero_migration.py`）
- T1 create → `Artifact(type=CONTENT_DRAFT, review_status=UNVERIFIED, revision_count=0)`，`uri`/`checksum` 已填，`metadata_json` 含 topic/anchors/phase=idea。
- T2 update phase idea→outline→draft（仅 UNVERIFIED/NEEDS_REVISION）；`APPROVED`/`REVIEW_PASSED` 改 → 409；编辑后 `revision_count` 递增、`checksum` 重算、`review_status=UNVERIFIED`、旧 `independent_review` 移入 `review_history` 且 `independent_review` 清空。
- T3 submit → 触发 `content_independent_review`；`review_status ∈ {NEEDS_REVISION, REVIEW_PASSED}` 且**绝不 APPROVED**；`independent_review` 已记（含 reviewed_checksum/reviewed_revision/producer/reviewer/confidence/bounded_reason/reviewed_at）。
- T4 独立审核异常/畸形/低置信 → fail-closed `NEEDS_REVISION`（无自动批准）。
- T5 reviewer==producer → 审核拒绝（provenance 校验失败）。
- T6 approve(owner) → `APPROVED` + `Approval(APPROVED)` + `AuditLog` 三元组（all-or-none）。**`KnowledgeFact` 未创建**；`series_id` 未用于知识对象。
- T7 approve 幂等 → 已 APPROVED 再批准 → 稳定 409（`uq_approval_gate_round` / CAS）。
- T8 reject(owner, reason) → `REJECTED` + `Approval(REJECTED)` + `AuditLog`；需 resubmit 方可再 approve。
- T9 非 owner approve/reject/metrics → 401/403（`authenticate_owner` 强制）。
- T10 metrics(owner) → 追加 `AuditLog(action="content_review_metric")`；被批准 Artifact 的 `metadata_json`/`checksum`/`review_status` **不变**（不可变快照）。
- T11 metrics 幂等 → 同 `idempotency_key` → 返回既有 AuditLog（不重复插入）。
- T12 metrics 更正 → 新 superseding AuditLog（`supersedes_audit_id`）；原记录**未覆盖**；查询返回最新未取代记录；两记录 `resource_id` 相同。
- T13 metrics 审计字段排除正文 → `after_snapshot` 无 body/outline/anchors 原文。
- T14 KnowledgeFact 隔离 → content 审批创建 0 条 `KnowledgeFact`；独立 `KnowledgeCandidate` 提交路径仅由 owner 手动发起。
- T15 未认证 create/read/update/submit/approve/reject/metrics → 401。
- T16 跨项目访问（agent A 读/改 agent B 草稿）→ 403/404，无泄露。
- T17 请求体/查询/头试图指定 actor/owner_id/agent_id/reviewer_id/producer/assignment/project/审批身份 → 被忽略或 422（服务端派生）。
- T18 producer/reviewer 分离已持久化并校验（reviewer≠producer）。
- T19 SQLite `BEGIN IMMEDIATE` 并发 approve/approve → 恰 1 条 APPROVED，第二者 409，无重复 Approval/AuditLog。
- T20 SQLite 并发 approve/reject → 确定性终态，无部分写入，败者稳定 409。
- T21 SQLite 回滚 → 任一步（CAS/Approval/AuditLog）后失败 → 全回滚，无残留 Approval/状态/AuditLog。
- T22 无真实模型调用 → 测试用 `FakeReviewAdapter`；默认执行发起 0 次付费 LLM 调用。
- T23 无自动发布/定价承诺/收款/客户动作 → approve/submit/metrics 路径不含此类副作用。
- T24 零迁移证明（见 §6）：fresh / upgrade / 枚举往返 / 无 CHECK·触发器拒绝 / 单 head `20260730_0001` / 无新增迁移文件 / AuditLog 惰性（无编排副作用）。
- T25 exact-head CI 绿 + ruff 清；既有 tests 无回归。
- **T26（契约 1）edit after REVIEW_PASSED invalidates approval eligibility**：submit→`REVIEW_PASSED`；`update_content_draft`（新 body→新 checksum + revision_count+1）→ 随后 `approve` 返回 409（stale checksum/revision，`independent_review` 已被清空）。
- **T27（契约 1）concurrent update versus approve**：两并发事务（一 edit 一 approve）经 `BEGIN IMMEDIATE` 串行化；确定性结果——edit 胜则 review 陈旧、approve 409；approve 胜则后续 edit 因 `review_status=APPROVED`→409；无部分状态。
- **T28（契约 1）no stale checksum/revision can be approved**：构造 `metadata_json.independent_review.reviewed_checksum/reviewed_revision` 与 `art.checksum`/`revision_count` 故意不一致 → `approve` 稳定 409。
- **T29（契约 2）AuditLog inert**：插入 `content_review_metric` AuditLog 后断言无新 `Event`/`Task`/`DelegatedRun`（无编排/重试/outbox 副作用）；`audit_log` 仅 +1 行。
- **T30（契约 2）AuditLog metrics immutable**：表中无 UPDATE 路径；更正只新增 superseding 行，原行不变。
- **T31（契约 2）AuditLog references only APPROVED**：`record_review_metrics` 引用非 `APPROVED` Artifact → 拒绝（409/422）。
- **T32（契约 3）same-project unauthorized read**：同 project 无关 Agent `GET /content-drafts` 与 `GET /{id}` → 403，且响应不含草稿正文/元数据。
- **T33（契约 3）same-project unauthorized update**：同 project 无关 Agent `PATCH /{id}` → 403。
- **T34（契约 3）same-project unauthorized submit**：同 project 无关 Agent `POST /{id}/submit` → 403。

## 8. 验收标准（合并门禁）
- [ ] 所有 TDD 测试通过（exact-head CI 绿），含 §6/§7 全部新增用例（T1–T34）。
- [ ] ruff 清。
- [ ] **零 Alembic 迁移**，单 head `20260730_0001` 不变。
- [ ] owner-only L4 批准强制（非 owner 拒绝）。
- [ ] **content 审批不创建 `KnowledgeFact`**，内容/话术/价格/CTA/指标不自动注入知识。
- [ ] 已批准快照不可变；复盘指标为追加写惰性 `AuditLog`，更正走 superseding 记录。
- [ ] **精确修订绑定**：approve 校验 `checksum`+`revision_count`+`REVIEW_PASSED`+无终态 Approval；陈旧审核稳定 409（契约 1）。
- [ ] 独立审核 reviewer≠producer（assigned reviewer only），仅产 REVIEW_PASSED/NEEDS_REVISION，默认 Fake 适配器零付费调用（契约 3 review）。
- [ ] SQLite `BEGIN IMMEDIATE`+CAS 原子契约；无 SELECT FOR UPDATE / 悲观行锁措辞；并发/回滚测试覆盖。
- [ ] **per-Artifact 鉴权（同项目内）**：get/list 限 owner 或显式相关 producer/reviewer；同项目无关 Agent 读/改/submit → 403 且不泄露（契约 3）。
- [ ] `Event` 未被用于指标（已改 `AuditLog`），`AuditLog` 惰性契约经测试断言。
- [ ] Codex(`gpt-5.6-sol`) APPROVE。

## 9. 不在范围 / 开放问题
- 不在：自动发布到公众号/小红书、自动报价/收款、客户承诺、客服全自动、`KnowledgeFact` 自动生成。
- 已解决（v3）：复盘指标原语由 `Event`（具投递语义）改为惰性 `AuditLog`（契约 2）；精确修订绑定与 stale-409（契约 1）；per-Artifact 同项目鉴权收紧（契约 3）。
- 开放（impl 定）：真实 LLM 审核适配器的凭据与 cost owner 落地细节（V4 secret-store）；`reviewed_at` 时区统一 UTC。

## 10. 分支与评审
- 状态：**APPROVED FOR IMPLEMENTATION**（2026-07-31 owner CONDITIONAL APPROVE 确认 3 契约）。
- 本 PR：`feat/issue-108-a-plan` → `main`，**仅 `docs/issue-108-a-plan.md`，零代码零迁移**。
- 合并门禁：已重设 `gate:merge`/`next:owner`/`status:blocked`；owner 授权 squash-merge（精确 head 锁）→ 删分支 → 清标签 → 关 #108。
- 实现分支：`feat/issue-108-a-impl`（base `main`），**本计划 merge 后才创建**：TDD 实现 → `codex review` APPROVE → exact-head CI 绿 → 设 `gate:merge`/`next:owner`/`status:blocked` → owner 授权 squash-merge → 关 #108、清门禁标签。**绝不自动 merge**。
