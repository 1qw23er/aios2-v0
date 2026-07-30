# 实施计划（修订版 v2）：#108 个人 IP 内容与变现工作流（V1.2-A）

> 关联 Issue **#108**；设计文档 `docs/plan-v1.2-ip-ops.md` §2/§6/§7。
> 状态：实现前实施计划（TDD + 门禁）。本修订版响应 owner 架构评审 **REQUEST_CHANGES（2026-07-31）**，逐条落实 7 项要求。仍为**零代码、零迁移** PR。
> 待 owner **最终架构批准** + 后续合并门禁（gate:merge）后，才开实现 PR `feat/issue-108-a-impl`。
> 协议：设计/假设/进度以 Issue·PR·仓库文档为唯一事实源；严格遵循协作协议（Codex APPROVE + exact-head CI + owner 门禁）。

## 0. 范围与边界（铁律）
- MVP 旅程：选题 → 策划 → 内容生产 → 独立审核 → **L4 人工批准** → 转化动作记录 + 复盘。
- 首个变现产品（D1）：闲鱼 / 电商自动化代运营（SOP / 模板包）。
- **铁律（本修订版强化）**：
  - 不自动发布、不自动承诺效果 / 价格、不自动收款（MVP 全手动）。
  - **内容审批绝不自动创建 `KnowledgeFact`**；内容正文 / 营销话术 / 价格 / 行动号召(CTA) / 复盘指标**不得自动注入 Agent 知识**。
  - 已批准的 `CONTENT_DRAFT` 仍是「已批准的 Artifact」，其快照（payload / checksum / review 状态）**只读冻结**，复盘指标写入独立附加记录。
  - 所有身份（actor / owner / agent / reviewer / approval）**一律服务端派生**，请求体/查询/头不得指定。

## 1. 数据模型变更（零迁移，附证明要求）
- `ArtifactType` 增 `CONTENT_DRAFT = "content_draft"`（StrEnum→VARCHAR，纯代码新增，无 Alembic 迁移，参见 `models.py:76` 注释；现有值 MARKDOWN/JSON/IMAGE/VIDEO/DATASET/LINK/WORK_LOG）。
- **草稿业务字段存于 `Artifact.metadata_json`**（`models.py:403`，JSON 列，列名 `metadata`）。注意：`Artifact` **没有 `payload` 列**（`payload` 是 `Event` 的列，`models.py:502`）；正文存于必填 `uri`（`models.py:382`，沿用 WORK_LOG 约定：正文 markdown 在 `uri`，结构化字段在 `metadata_json`）。`checksum`（`models.py:383`，必填）为 body 的 sha256，**批准后即冻结**。
  - `metadata_json` 字段：`topic`(str)、`phase`(str: `idea`→`outline`→`draft`)、`outline`(str/JSON)、`conversion_anchors`(JSON list: `[{type: consult|product|course|template, label, url}]`)、`series_id`(str, **可选，纯人工组织标签，默认 "黎叔AI创业实验室"，但绝不驱动 KnowledgeFact 创建**)、`review_provenance`(见 §4)。
- **审批状态机复用 `ArtifactReviewStatus`**（`models.py:90`）：`UNVERIFIED` → `NEEDS_REVISION`/`REVIEW_PASSED`(独立审核) → `APPROVED`/`REJECTED`(owner L4)。不新增枚举。
- **终端批准记录复用 `Approval`**（`models.py:458`，唯一约束 `uq_approval_gate_round`(target_artifact_id, review_policy_id, review_round, action_type)）：content 批准用 `target_artifact_id=content_id, review_policy_id=NULL, review_round=1, action_type="CONTENT_APPROVE"|"CONTENT_REJECT"`，重复插入触发 `IntegrityError` → 稳定 409。
- **审计复用 `AuditLog`**（`src/aios/audit.py`，`idempotency_key` 唯一；`redact_secrets` 保证无凭据泄露）：仅记状态跃迁 + 溯源引用，**不抄内容正文**。
- **复盘指标复用 `Event` 表**（`models.py:495`，`payload` JSON + `idempotency_key` 唯一 + `project_id` 作用域）：`type="content_review_metric"`，**追加写、不改动被批准的 Artifact**。零新增表、零新增枚举 → 零迁移。
- **结论**：零 Alembic 迁移，单 head `20260730_0001` 不变（与 #104/#106/#111 同构）。§6 要求用测试证明。

## 2. 服务层（新增 `src/aios/content_draft.py`）
复用 #88 `attest_work_log` 的「原子三元组」思想，但**用 SQLite `BEGIN IMMEDIATE` + CAS 替代悲观行锁表述**（见 §3）。新增：
- `create_content_draft(session, actor, *, project_id, topic, outline=None, body=None, conversion_anchors=None, phase="idea", series_id=None)` → 建 `Artifact(type=CONTENT_DRAFT, review_status=UNVERIFIED, uri=<body 或 content-draft://{id}>, checksum=sha256(body), metadata_json={...})`。`actor` 来自 `authenticate_owner`/`authenticate_agent`（服务端派生）；**producer 溯源**写入 `metadata_json.review_provenance.producer={agent_id, task_id}`（仅当 actor 为 agent）。
- `update_content_draft(...)` → 改 topic/outline/body/anchors/phase/series_id（仅 `UNVERIFIED`/`NEEDS_REVISION` 可改；改 body 重算 `checksum`）。`APPROVED`/`REVIEW_PASSED` 改 → 409。
- `submit_content_draft(session, actor, id)` → 触发**独立审核**（§4 `content_independent_review`），**永不自动 APPROVED**：通过 → `REVIEW_PASSED`；问题/异常/低置信 → `NEEDS_REVISION` + 报告入 `metadata_json.review_provenance`。均不触碰 `APPROVED`。
- `approve_content_draft(session, actor, id)` → **owner-only L4**（依赖 `actor.kind == "owner"`，由 `authenticate_owner` 保证）。**原子契约见 §3**。***不创建 `KnowledgeFact`，不使用 `series_id` 创建任何知识对象***。
- `reject_content_draft(session, actor, id, *, reason)` → **owner-only**：原子契约（§3）将 `review_status=REJECTED`，写 `Approval(REJECTED)` + `AuditLog`；reason 入 `metadata_json`。驳回后需重新 submit 才能再 approve。
- `record_review_metrics(session, actor, id, *, metrics, idempotency_key)` → **owner-only**：**不改动**被批准 Artifact 的 `metadata_json`/`checksum`/`review_status`；仅**追加写**一条 `Event(type="content_review_metric")`（§2b）。

### 2b. 追加写复盘指标记录（迁移免费现有原语 `Event`）
- **链接**：`Event.payload.content_artifact_id = <被批准 content artifact id>`。
- **指标 schema**：`Event.payload.metrics = {exposure:int, consult:int, conversion:int, period_start:iso8601|null, period_end:iso8601|null, channel:str|null}`。
- **recorded_by**：`Event.payload.recorded_by = actor.owner_id`（**服务端派生自 `authenticate_owner`**，永不取请求）。
- **recorded_at**：`Event.created_at`（服务端 UTC），并冗余 `Event.payload.recorded_at`。
- **idempotency_key**：`Event.idempotency_key`（表级 UNIQUE）。相同 key 重复写入 → `IntegrityError` → 返回既有记录（200，不新增）。
- **去重行为**：同 key 幂等，不重复插入。
- **更正行为**：**新记录** `Event(type="content_review_metric", payload.supersedes_event_id=<原 event id>, payload.correction=true, idempotency_key=新key)`；**绝不覆盖**原记录；查询返回「未被取代的最新记录」。
- **审计字段排除内容正文**：`Event.payload` 仅含 `content_artifact_id`/`metrics`/`recorded_by`/`recorded_at`/`provenance 引用`/`supersedes_event_id`；**不得复制 content body / outline / conversion_anchors 原文**。
- **作用域**：`Event.project_id` = content artifact 的 project_id，杜绝跨项目读取。

## 3. 审批原子契约（SQLite `BEGIN IMMEDIATE` + CAS，非悲观行锁）
**严禁** SELECT FOR UPDATE / 泛型「pessimistic row lock」措辞。统一用 **单条 `BEGIN IMMEDIATE` 事务**（SQLite 立即取 RESERVED 锁，写冲突串行化）。

### 3a. approve_content_draft 事务内步骤
```
connection.execution_options(isolation_level="IMMEDIATE")  # 或 exec_driver_sql("BEGIN IMMEDIATE")
art = session.get(Artifact, id)                             # 事务内重读当前态
if art is None: raise NotFound
if art.type != CONTENT_DRAFT: raise Conflict("not content draft")
if art.review_status != REVIEW_PASSED: raise Conflict("requires REVIEW_PASSED")   # 已 APPROVED/REJECTED/NEEDS_REVISION/UNVERIFIED 均拒
# 条件/CAS 更新
rc = session.execute(update(Artifact).where(Artifact.id==id, Artifact.review_status==REVIEW_PASSED).values(review_status=APPROVED)).rowcount
if rc != 1: raise Conflict("status changed concurrently")   # CAS 失败
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
- 并发第二写者：`BEGIN IMMEDIATE` 在首事务提交前阻塞；获锁后 CAS 发现 `review_status!=REVIEW_PASSED` 或 `Approval` 唯一约束命中 → 稳定 409。

### 3b. reject_content_draft 同构
- `BEGIN IMMEDIATE`；要求当前 `review_status in (REVIEW_PASSED, NEEDS_REVISION)`（已终态拒）；CAS `UPDATE ... SET review_status=REJECTED WHERE id=? AND review_status IN (REVIEW_PASSED, NEEDS_REVISION)`；插入 `Approval(action_type="CONTENT_REJECT", status=REJECTED)` + `AuditLog(action="content.reject")`；commit all-or-none。

## 4. 独立审核（content_independent_review，真正独立）
- **重命名** `content_self_review` → `content_independent_review`（它是独立审核，非自评）。
- **溯源持久化与校验**（`metadata_json.review_provenance`）：
  - `producer = {agent_id, task_id}`：草稿创建时由服务端 `ActorContext` 写入（agent 创建才记；owner 创建则 producer 为空/owner）。
  - `reviewer = {agent_id, task_id}`：submit 时由服务端 `ActorContext` 写入。
  - **reviewer 必须 ≠ producer**（当二者均为 agent 时）；相等 → 审核拒绝（provenance 校验失败）。
  - 上述身份**全部来自可信运行时 `ActorContext`，绝不取请求字段**。
- **自动化审核仅可产出**：`REVIEW_PASSED` 或 `NEEDS_REVISION`。错误 / 输出畸形 / 低置信 → `NEEDS_REVISION`。**永不可产出 `APPROVED`**。
- **适配器门控**：`ReviewAdapter.review(payload) -> ReviewResult(status, report, confidence)`。
  - 默认/测试用 **`FakeReviewAdapter`**（确定性规则，无网络、零付费调用）。
  - 若接真实 LLM：须显式凭据（secret-store ref）+ 真实模型调用开关 + **cost owner 门控**（谁承担费用，owner 授权）；仅在该配置下启用。
  - **测试与默认执行一律用 `FakeReviewAdapter`，绝不发起付费模型调用**。

## 5. 端点鉴权矩阵（`src/aios/api/app.py`，沿用 `@application.X`）
| 端点 | 鉴权 | 身份来源 | 作用域 |
|---|---|---|---|
| `POST /content-drafts` | owner **或** 注册 Agent | `authenticate_owner` / `authenticate_agent` → `ActorContext` | agent 限其 project/scope |
| `GET /content-drafts` | owner 或 Agent（已认证） | `ActorContext` | 按 actor.project 过滤，无跨项目泄露 |
| `GET /content-drafts/{id}` | 同上 | `ActorContext` | 跨项目→404/403 |
| `PATCH /content-drafts/{id}` | owner 或 producer Agent | `ActorContext` | 仅未批准态；scope 一致 |
| `POST /content-drafts/{id}/submit` | owner 或 producer Agent | `ActorContext` | scope 一致；触发独立审核 |
| `POST /content-drafts/{id}/approve` | **`authenticate_owner` 强制** | owner `ActorContext` | — |
| `POST /content-drafts/{id}/reject` | **`authenticate_owner` 强制** | owner `ActorContext` | — |
| `POST /content-drafts/{id}/metrics` | **`authenticate_owner` 强制** | owner `ActorContext` | — |
- **硬规则**：请求体/查询/头**不得**指定 `actor`/`owner_id`/`agent_id`/`reviewer_id`/审批身份；一切服务端派生。违者忽略或 422。
- owner-only 端点缺 `authenticate_owner` → 401/403；非 owner → 403。

## 6. 零迁移假设的证明（测试必须）
计划要求如下检查/测试，证明 SQLite DDL、触发器、枚举持久化接受 `CONTENT_DRAFT` 而**无需 schema 变更**：
- **全新库测试**：建库 → 应用现有迁移（head `20260730_0001`）→ 插入 `Artifact(type=CONTENT_DRAFT)` → 读回断言 type 保留。
- **存量库升级路径测试**：从既有迁移状态起，应用**零新增迁移** → 断言 `CONTENT_DRAFT` 被接受。
- **枚举往返**：`CONTENT_DRAFT` create/read round-trip，断言无 CHECK/触发器拒绝（含核对外部触发器 `knowledge_candidate_validate_insert` 仅 guard `WHEN NEW.type='knowledge_candidate'`，不拒绝 `content_draft`；在触发器激活下插入 `CONTENT_DRAFT` 成功）。
- **Alembic head**：`alembic heads` == 1 且 == `20260730_0001`；`migrations/versions/` **无新增迁移文件**。
- 在 `tests/test_content_draft_zero_migration.py` 落实上述断言。

## 7. TDD 测试计划（`tests/test_content_draft.py` + `tests/test_content_draft_zero_migration.py`）
- T1 create → `Artifact(type=CONTENT_DRAFT, review_status=UNVERIFIED)`，`uri`/`checksum` 已填，`metadata_json` 含 topic/anchors/phase=idea。
- T2 update phase idea→outline→draft（仅 UNVERIFIED/NEEDS_REVISION）；`APPROVED`/`REVIEW_PASSED` 改 → 409。
- T3 submit → 触发 `content_independent_review`；`review_status ∈ {NEEDS_REVISION, REVIEW_PASSED}` 且**绝不 APPROVED**；provenance 已记。
- T4 独立审核异常/畸形/低置信 → fail-closed `NEEDS_REVISION`（无自动批准）。
- T5 reviewer==producer → 审核拒绝（provenance 校验失败）。
- T6 approve(owner) → `APPROVED` + `Approval(APPROVED)` + `AuditLog` 三元组（all-or-none）。**`KnowledgeFact` 未创建**；`series_id` 未用于知识对象。
- T7 approve 幂等 → 已 APPROVED 再批准 → 稳定 409（`uq_approval_gate_round` / CAS）。
- T8 reject(owner, reason) → `REJECTED` + `Approval(REJECTED)` + `AuditLog`；需 resubmit 方可再 approve。
- T9 非 owner approve/reject/metrics → 401/403（`authenticate_owner` 强制）。
- T10 metrics(owner) → 追加 `Event(type=content_review_metric)`；被批准 Artifact 的 `metadata_json`/`checksum`/`review_status` **不变**（不可变快照）。
- T11 metrics 幂等 → 同 `idempotency_key` → 返回既有 Event（不重复插入）。
- T12 metrics 更正 → 新 superseding Event（`supersedes_event_id`）；原记录**未覆盖**；查询返回最新未取代记录。
- T13 metrics 审计字段排除正文 → Event.payload 无 body/outline/anchors 原文。
- T14 KnowledgeFact 隔离 → content 审批创建 0 条 `KnowledgeFact`；独立 `KnowledgeCandidate` 提交路径仅由 owner 手动发起、且仅提交人工提炼的 lesson，绝不自动注入正文/话术/价格/CTA/指标。
- T15 未认证 create/read/update/submit/approve/reject/metrics → 401。
- T16 跨项目访问（agent A 读/改 agent B 草稿）→ 403/404，无泄露。
- T17 请求体/查询/头试图指定 actor/owner_id/agent_id/reviewer_id/审批身份 → 被忽略或 422（服务端派生）。
- T18 producer/reviewer 分离已持久化并校验。
- T19 SQLite `BEGIN IMMEDIATE` 并发 approve/approve → 恰 1 条 APPROVED，第二者 409，无重复 Approval/AuditLog。
- T20 SQLite 并发 approve/reject → 确定性终态，无部分写入，败者稳定 409。
- T21 SQLite 回滚 → 任一步（CAS/Approval/AuditLog）后失败 → 全回滚，无残留 Approval/状态/AuditLog。
- T22 无真实模型调用 → 测试用 `FakeReviewAdapter`；默认执行发起 0 次付费 LLM 调用（断言 `adapter.is_fake` 或零网络）。
- T23 无自动发布/定价承诺/收款/客户动作 → approve/submit/metrics 路径不含此类副作用（代码审查 + 测试断言）。
- T24 零迁移证明（见 §6）：fresh / upgrade / 枚举往返 / 无 CHECK·触发器拒绝 / 单 head `20260730_0001` / 无新增迁移文件。
- T25 exact-head CI 绿 + ruff 清；既有 tests 无回归。

## 8. 验收标准（合并门禁）
- [ ] 所有 TDD 测试通过（exact-head CI 绿），含 §6/§7 全部新增用例。
- [ ] ruff 清。
- [ ] **零 Alembic 迁移**，单 head `20260730_0001` 不变。
- [ ] owner-only L4 批准强制（非 owner 拒绝）。
- [ ] **content 审批不创建 `KnowledgeFact`**，内容/话术/价格/CTA/指标不自动注入知识。
- [ ] 已批准快照不可变；复盘指标为追加写 `Event`，更正走 superseding 记录。
- [ ] 独立审核 reviewer≠producer，仅产 REVIEW_PASSED/NEEDS_REVISION，默认 Fake 适配器零付费调用。
- [ ] SQLite `BEGIN IMMEDIATE`+CAS 原子契约；无 SELECT FOR UPDATE / 悲观行锁措辞；并发/回滚测试覆盖。
- [ ] 全端点鉴权矩阵落实；无跨项目泄露；身份全服务端派生。
- [ ] Codex(`gpt-5.6-sol`) APPROVE。

## 9. 不在范围 / 开放问题
- 不在：自动发布到公众号/小红书、自动报价/收款、客户承诺、客服全自动、`KnowledgeFact` 自动生成。
- 开放（impl 定）：`Event` 的 `status` 取值（建议 `EventStatus.DONE` 且不被事件处理器消费）；真实 LLM 审核适配器的凭据与 cost owner 落地细节（V4 secret-store）。

## 10. 分支与评审
- 实现分支：`feat/issue-108-a-impl`（base `main` = `fd808df`），**本 PR 不创建**。
- 流程：TDD 实现 → `codex review` APPROVE → exact-head CI 绿 → 设 `gate:merge`/`next:owner`/`status:blocked` → owner 授权 squash-merge（精确 head 锁）→ 关 #108、清门禁标签。
- **本修订版（v2）状态**：plan-only，响应式 REQUEST_CHANGES；已移除 `gate:merge`，待 owner 最终架构批准后再进入合并门禁。
