# 实施计划（v4 · REQUEST_CHANGES 修订）：#110 使用反馈闭环（V1.2-C）

> 关联 Issue **#110**；设计文档 `docs/plan-v1.2-ip-ops.md` §4/§6/§7。
> 状态：**待评审（PLAN PR）** — 本 PR 仅含本计划文档，零代码、零迁移。
> 协议：设计/假设/进度以 Issue·PR·仓库文档为唯一事实源；严格遵循协作协议（Codex APPROVE + exact-head CI + owner 门禁）。
> 流程：本计划经 `gate:merge` 由 owner 授权 squash-merge 到 `main` 后，**方可**开实现 PR `feat/issue-110-feedback-loop-impl`（TDD + Codex + 门禁）；未合并前不实现。
> 本版（v4）响应 owner `REQUEST_CHANGES` 六条要求：①完整阶段机契约 ②owner 审批绑定唯一 solution 修订 ③源反馈/工作流元数据/分析三分离 + 校验和域 ④聚类摘要完全可追溯且确定性 ⑤反馈视作不可信外部输入 ⑥扩展 TDD。

## 0. 范围与边界（铁律）

- MVP 旅程：非技术 owner 简单入口（自然语言）→ 自动记录原话/场景/期望/风险标签/时间 → `Feedback` Artifact → 周期性聚类/去重/摘要 → 看板状态机 → **优先级建议只读**。
- 设计 §5「明确不在首个 MVP 实现」与 §4「不自动」：
  - **优先级建议只读**：**不得**自动修改生产系统、接入付费服务、或改变工作流架构。建议落库后，仅 owner 人工将看板推到 `DEVELOP` 阶段时由人工创建关联 `Task`。
  - 不自动发布、不自动回帖、不自动改生产、不接付费模型（默认用确定性聚类，零付费）。
- **铁律（复用 #108-A 三契约）**：
  - 反馈原文 / 场景 / 期望 / 风险标签 / 解决方案 **绝不自动注入 `KnowledgeFact`、绝不自动创建知识对象**。
  - 聚类摘要与优先级建议为**惰性、不可变、只读**记录，写入 `AuditLog`（复用 #108-A §2b 惰性原语），**无编排/投递/重试副作用**。
  - 所有身份（actor / owner / agent / 提交者 / 转移者）**一律服务端派生**，请求体/查询/头不得指定。
- **本版新增铁律（响应 REQUEST_CHANGES）**：
  - **端点绝不接受任意阶段字符串**：状态跃迁一律通过「命名转移动词（`FeedbackTransition` 枚举）」驱动，动词→(所需当前阶段, 目标阶段) 由服务端 `ALLOWED_TRANSITIONS` 版本化白名单决定；非法动词/非法源阶段→稳定 409，缺 owner 身份→403。
  - **owner 审批绑定唯一 solution 修订**：进入 `AWAIT_OWNER_APPROVE` 即冻结提交时 `checksum`+`revision_count`；`DEVELOP` 转移在单 `BEGIN IMMEDIATE` 事务内重读并强制 `checksum`/`revision` 相等 + 无冲突终态 `Approval`；提交后任何 solution 编辑使待审失效并退回 `SOLUTION`/`CLARIFY`。
  - **反馈即不可信外部输入**：UTF-8 归一 + 字段上限；不落任何 header/cookie/凭据/环境变量/密文；`AuditLog` 绝不存完整反馈正文；反馈文本是数据不是指令；默认确定性假适配器、零付费调用。

## 1. 数据模型与校验和域（零迁移，附证明要求）

### 1.1 复用原语（`models.py` / `audit.py`）

- `ArtifactType` 增 `FEEDBACK = "feedback"`（StrEnum→VARCHAR，纯代码新增，**无 Alembic 迁移**，与 `WORK_LOG`/`CONTENT_DRAFT` 同机制）。
- **反馈业务字段存于 `Artifact.metadata_json`**（`models.py:408`，JSON 列，列名 `metadata`）。`Artifact` 无 `payload` 列；原文存于必填 `uri`；**`Artifact.checksum`（`models.py:388`，必填）= 内容校验和**（定义见 §1.3），**不是**阶段/指派字段。
- **复用现有 `Artifact.revision_count`**（`models.py:397`，`int=0`）：**仅当源反馈/解决方案内容被编辑时原子递增**，作为「内容修订版本」。**阶段跃迁不递增 `revision_count`、不改变 `checksum`**。
- **审计复用 `AuditLog`**（`src/aios/audit.py`，append-only 惰性表，`append_audit(session, *, actor, action, resource_type, resource_id, project_id, task_id, before, after, idempotency_key)`）：
  - 看板阶段转移：写 `AuditLog(action="feedback.stage_transition")`（`before/after` 含 `stage` 转移 + 转移动词）。
  - owner 审批绑定：写 `AuditLog(action="feedback.owner_approve")` 与 `feedback.submit_for_approval`，`after` 含绑定 `checksum`/`revision`/`approval_id`。
  - 聚类摘要：写 `AuditLog(action="feedback.cluster_summary")`（见 §4）。
  - 精确字段：`id, actor, action, resource_type, resource_id, project_id, task_id, before_snapshot, after_snapshot, idempotency_key, created_at`。**无 `status`/`processed_at`/`supersedes_audit_id` 列** → 纯追加、不可变、无消费者；`supersedes_audit_id` 仅作为更正/重跑记录的 `after_snapshot` 内嵌键（零迁移）。
  - **`append_audit` 内部已调用 `redact_secrets`**（`audit.py:130-131`）：自动 redact `secret/token/password/credential/api_key/authorization/cookie` 等键，及高熵 token（`sk-/AKIA-/nvapi-/xox*-`、40+ 混合熵串）。聚类/审批摘要进入 `after_snapshot` 前同样经 `redact_secrets`，且**绝不带完整反馈正文**。
- **owner 审批复用现有 `Approval`**（`models.py`，`__tablename__="approval"`，`uq_approval_gate_round(target_artifact_id, review_policy_id, review_round, action_type)`，`status∈{pending,approved,rejected,expired}`，`target_artifact_id` 外键 `artifact.id`）：**零迁移**新增绑定记录，`action_type="feedback.develop"`，`review_round=1`。

### 1.2 `metadata_json` 规范 schema（精确字段）

`Feedback` Artifact 的 `metadata_json`（JSON 列）含三类明确分区（见 §3 三分离）：

**A. 源反馈内容（参与内容校验和，见 §1.3）**
- `original_text`(str, 必填, ≤16 KiB UTF-8 NFC)
- `scenario`(str|null, ≤4 KiB)
- `expected_outcome`(str|null, ≤4 KiB)
- `risk_tags`(list[str], ≤16 项, 取值于 `{data_loss,ux,perf,billing,security,other}`)
- `solution_text`(str|null, ≤16 KiB) — 提出的解决方案（进入 `AWAIT_OWNER_APPROVE` 前必填）

**B. 工作流元数据（不参与内容校验和；阶段机版本化）**
- `stage`(str, 看板阶段，取值见 §3.1 `FeedbackStage` v1)
- `submitted_by`(服务端派生 actor 串，经 `actor.derive_submitted_by()`)
- `channel`(str, 如 `owner_console`/`cli`/`wechat`，记录来源，**非内容**)
- `cluster_id`(str|null，聚类归属，报告用，**非内容**)
- `pending_approval`(dict|null，仅 `AWAIT_OWNER_APPROVE` 期存在；见 §2.2)
- `duplicate_of`(str|null，仅 `DUPLICATE` 期指向规范 feedback id)
- `workflow_revision`(str，固定 `"fsm-v1"`，阶段机版本；**非内容**，单独文档化)
- `transition_seq`(int，阶段转移单调计数器，初值 0；每次成功 `apply_transition` 原子递增，用于审计 `idempotency_key` 唯一性，使「同一源→目标 转移」在重复周期/重开后仍产生不同键，**非内容**)
- `corrections`(list[dict]，编辑历史，**非内容**；见 §3.3)

**C. 分析/摘要（只读，存于 `AuditLog` 不在此）**
- `metadata_json` **不含** `suggested_priority`/聚类摘要（避免与惰性契约冲突）。优先级与聚类摘要仅存 §4 的 `AuditLog.after_snapshot`。

### 1.3 校验和域（显式定义）

- **内容校验和 `Artifact.checksum`**：
  `sha256:" + sha256(canonical_json(_feedback_content_payload(artifact)))`
  其中 `_feedback_content_payload` = `{original_text, scenario, expected_outcome, risk_tags, solution_text}`（**仅 A 区**）。**明确排除** B 区全部字段（stage/submitted_by/channel/cluster_id/pending_approval/duplicate_of/workflow_revision/corrections）。
- **不变性保证**：
  1. 相同规范内容（A 区同值，NFC 归一、键排序）恒得相同 `checksum`（复用 `content_draft._compute_checksum` 的 `canonical_json(sort_keys=True)` 机制）。
  2. **阶段跃迁（`stage` 变动）不改变 `checksum`**，因为 `stage` ∈ B 区被排除 → 满足「阶段转移不得意外使内容引用失效，除非内容本身变更」。
  3. 仅当 A 区内容编辑时重算 `checksum` 并 `revision_count += 1`。
- **工作流版本单独文档化**：`workflow_revision="fsm-v1"` 标识阶段机版本，不参与内容校验和；若未来阶段机变更，仅改该常量与白名单，不触动内容引用。

### 1.4 结论

零 Alembic 迁移，单 head `20260730_0001` 不变。§6/§7 用测试证明「fresh / upgrade / 单 head / 无新增迁移文件 / 内容校验和不受阶段影响」。

## 2. 服务层（新增 `src/aios/feedback.py`，仿 `work_log.py` / `content_draft.py`）

复用 #88 `attest_work_log` 与 #108-A「原子三元组 + `BEGIN IMMEDIATE` + CAS」思想，用 SQLite `BEGIN IMMEDIATE` 替代悲观行锁（**精确复用 `content_draft.py:604-605`**：`session.rollback()` + `session.connection().exec_driver_sql("BEGIN IMMEDIATE")`）。

- `create_feedback(session, actor, *, project_id, original_text, scenario=None, expected_outcome=None, risk_tags=None, channel="owner_console")` → 建 `Artifact(type=FEEDBACK, review_status=UNVERIFIED, revision_count=0, uri=original_text, checksum=content_checksum, metadata_json={A区+B区 stage:"COLLECTED", workflow_revision:"fsm-v1", transition_seq:0, submitted_by:actor.derive_submitted_by(), ...})`；`actor` 来自 `authenticate_owner`/`authenticate_agent`（服务端派生）；`original_text` 必填 + UTF-8 NFC 归一 + 上限校验（超长→422）。
- `get_feedback` / `list_feedback` → 同项目 per-Artifact 鉴权（§5）：owner 或显式相关提交者可见；无关 agent → 403，且不经 list 学到任何反馈正文。
- `amend_feedback(session, actor, id, *, reason: str, scenario=None, expected_outcome=None, risk_tags=None, solution_text=None)` → **`reason` 为必填参数**（记录于 `corrections` 编辑历史，无静默覆盖，缺省→422）；仅 `COLLECTED`/`CLARIFY`/`SOLUTION` 可改（**仅 A 区内容，不改阶段**）；**BEGIN IMMEDIATE** 内：重读→鉴权（owner 或 submitted_by）→改 A 区→重算 `checksum`→`revision_count += 1`→**保留 `corrections` 编辑历史（见 §3.3，无静默覆盖）**→写 `AuditLog(feedback.amend)`。`AWAIT_OWNER_APPROVE` 阶段的内容修订**不**经此函数，统一走命名动词 `INVALIDATE_PENDING`（§3.2/§2.2，原子编辑+清 pending+退 SOLUTION）；终态（`DEVELOP`/`TEST`/`SHIPPED`/`REJECTED`/`DUPLICATE`）→ 409。
- `apply_transition(session, actor, id, *, transition: FeedbackTransition, reason=None, canonical_feedback_id=None, scenario=None, expected_outcome=None, risk_tags=None, solution_text=None)` → **唯一阶段机入口**；接受命名动词枚举（**绝不接受任意阶段字符串**）。内部查 `ALLOWED_TRANSITIONS[v1][transition]` → 得 `(required_actor, required_current_stage, target_stage, preconditions)`，校验后走 §3.2 原子契约。**内容编辑参数（`reason`/`scenario`/`expected_outcome`/`risk_tags`/`solution_text`）仅由携带编辑的动词（如 `INVALIDATE_PENDING`）消费**——`reason` 对所有动词可选（除 `INVALIDATE_PENDING` 等明确要求者必填，缺→422），内容编辑字段对非编辑动词忽略。非法动词→422；源阶段不符→409；缺 owner→403；缺必填 `reason`→422。
- `record_cluster_run(session, actor, *, project_id, window_start, window_end, idempotency_key)` → 触发聚类（§4）；仅追加写 `AuditLog(action="feedback.cluster_summary")`；幂等同 `idempotency_key`（UNIQUE）；**绝不**改任何 `Feedback` Artifact 的业务字段或阶段、绝不建 `KnowledgeFact`、绝不触发编排。

### 2.1 聚类摘要（惰性原语 `AuditLog`，完全可追溯 + 确定性，见 §4）

- 聚类纯函数 `cluster_feedback(items) -> list[ClusterSummary]`：**默认确定性规则**（关键词/哈希分桶 + 频次计数），**零 LLM、零付费**。若接真实语义聚类：须显式凭据 + cost owner 门控 + 真实模型调用开关；测试与默认执行一律用确定性规则。
- 写入：`AuditLog(action="feedback.cluster_summary", resource_type="project", resource_id=project_id, project_id=project_id, actor=actor.derive_submitted_by(), before={}, after={cluster_key, member_ids(sorted), member_revisions(list of {id,content_checksum,revision}), policy_version, summary(bounded), suggested_priority(read-only), window_start, window_end, risk_tags, supersedes_audit_id?}, idempotency_key=<确定性>)`。
- **惰性契约保证（复用 #108-A §2b 契约 2 全部条款）**：插入不触发编排/投递/重试；`idempotency_key` 表级 UNIQUE 去重；插入后不可变（无 UPDATE 路径）；**不引用/不改动任何 `Feedback` Artifact**（绝不写 `Feedback.metadata_json` 任何字段，含 `suggested_priority`）；**不创建 `KnowledgeFact`**；更正/重跑用**不可变 superseding 记录**（`after_snapshot.supersedes_audit_id` 内嵌，**零迁移**）。优先级建议**仅**存 `AuditLog.after_snapshot.suggested_priority`，**绝不回写** Feedback。

### 2.2 owner 审批绑定唯一 solution 修订（响应要求 ②）

- **进入 `AWAIT_OWNER_APPROVE`（动词 `SUBMIT_FOR_APPROVAL`，源 `SOLUTION`）**：在 `BEGIN IMMEDIATE` 内：重读→校验 `solution_text` 非空→**服务端派生递增轮次** `new_round = max(既存 Approval.review_round for this artifact, (pending_approval.review_round if any)) + 1`（首提为 1）→置 `metadata_json.stage=AWAIT_OWNER_APPROVE` 并写 `metadata_json.pending_approval = {checksum: artifact.checksum, revision: artifact.revision_count, submitted_at: now_utc().isoformat(), submitted_by: actor.derive_submitted_by(), action_type: "feedback.develop", review_round: new_round}`；写 `AuditLog(feedback.submit_for_approval, after={stage, pending_approval})`；commit。
- **owner 审批转移 `AWAIT_OWNER_APPROVE → DEVELOP`（动词 `APPROVE_SOLUTION`，owner-only，源 `AWAIT_OWNER_APPROVE`）** —— 单 `BEGIN IMMEDIATE` 事务 **all-or-none**：
  1. `self.session.rollback()` + `exec_driver_sql("BEGIN IMMEDIATE")`；
  2. 事务内重读 `fb = session.get(Artifact, id)`；
  3. 要求 `fb.metadata_json["stage"] == AWAIT_OWNER_APPROVE`，否则 409；
  4. 取 `pending = fb.metadata_json["pending_approval"]`；要求 `fb.checksum == pending["checksum"]` 且 `fb.revision_count == pending["revision"]`，否则 409（stale/编辑后失效）；
  5. 要求**同轮无冲突终态 `Approval`**：`SELECT Approval WHERE target_artifact_id=id AND action_type="feedback.develop" AND review_round=pending["review_round"] AND status IN (approved,rejected)` 为空，否则 409（旧轮 REJECTED 不影响新轮）；
  6. 插入 `Approval(project_id, target_artifact_id=id, review_policy_id=None, review_round=pending["review_round"], action_type="feedback.develop", risk_level=..., status=APPROVED, decided_at=now_utc(), rationale=reason)`；**并发安全不依赖** `uq_approval_gate_round`（该约束含 `target_artifact_id`/`review_policy_id` 两个 NULLable 列，SQLite 中 NULL 互不判等、非主键级并发栅栏）——**主保护来自本事务的 `BEGIN IMMEDIATE` 写串行化 + 步骤 5 的事务内「同轮无冲突终态 Approval」重读校验**：首个获锁者插入 `APPROVED` 并提交；第二个获锁者重读发现同轮已存在终态 `Approval`→步骤 5 直接 409，绝不会插入第二条。`uq_approval_gate_round` 仅作**幂等/防呆二级护栏**（重复同元组不会双写），非并发控制主路径；轮次不同则元组本身不同，不冲突。
  7. 置 `fb.metadata_json.stage=DEVELOP` 并清 `pending_approval`；
  8. `append_audit(session, action="feedback.owner_approve", before={stage:AWAIT_OWNER_APPROVE}, after={stage:DEVELOP, approval_id, bound_checksum, bound_revision}, idempotency_key=f"audit:feedback:approve:{id}:{revision}")`；
  9. `session.commit()`；任一步 `ServiceError/IntegrityError/Exception` → `session.rollback()` 后重抛（**无部分 Approval/阶段/AuditLog**）。
- **提交后 solution 编辑使待审失效**：在 `AWAIT_OWNER_APPROVE` 阶段若需改内容，必须走**命名转移动词 `INVALIDATE_PENDING`（见 §3.2 表）**，该动词**自身携带编辑参数**（`reason` 必填 + `scenario`/`expected_outcome`/`risk_tags`/`solution_text`），在单 `BEGIN IMMEDIATE` 内原子执行：①对 A 区内容做编辑（重算 `checksum`、`revision_count += 1`、保留 `corrections`、记录 `reason`）；②清 `pending_approval`；③置 `stage=SOLUTION`（**确定性单目标，不依 reason 选 CLARIFY/SOLUTION**）；④`append_audit(feedback.invalidate_pending, after={stage:SOLUTION, cleared_pending:round, reason})`。**绝不靠 `amend_feedback` 隐式改阶段**——`amend_feedback` 只允许 `COLLECTED`/`CLARIFY`/`SOLUTION` 改 A 区内容（§2 服务层约定），`AWAIT` 阶段内容修订统一经 `INVALIDATE_PENDING` 动词。原待审因 `checksum`/`revision` 失配而**自动失效**（后续 `APPROVE_SOLUTION` 步骤 4/5 409）。**无 DEVELOP 状态可于无匹配 `Approval`+`AuditLog` 下存在**。
- **owner 拒绝解决方案（动词 `REJECT_SOLUTION`，owner-only，源 `AWAIT_OWNER_APPROVE`）**：`BEGIN IMMEDIATE` 内插入 `Approval(status=REJECTED, review_round=本轮)` + 清 `pending_approval` + 置 `stage=SOLUTION` + `AuditLog(feedback.owner_reject)`。**拒绝后「重做 submit」必须推进轮次**：`SUBMIT_FOR_APPROVAL` 在进入 `AWAIT_OWNER_APPROVE` 时，**服务端派生递增轮次** `new_round = (当前 Feedback 已见最大 review_round) + 1`（从 `pending_approval.review_round` 或既存 `Approval.review_round` 取 max，初值为 1→2），并写入 `pending_approval.review_round = new_round`；后续 `APPROVE_SOLUTION` 步骤 5 的「无冲突终态 `Approval`」校验**仅针对同一 `review_round`**（即 `WHERE target_artifact_id=id AND action_type="feedback.develop" AND review_round=<本轮> AND status IN (approved,rejected)`）。这样被拒后可推进到 round 2 重新 submit→approve，旧 round 1 的 REJECTED 不阻塞新轮；且旧轮 Approval 永不批准新轮修订（绑定校验要求 `pending_approval.review_round == 本轮`，owner approve 插入 `review_round=本轮`）。

## 3. 阶段机契约（版本化白名单 + 命名转移动词，响应要求 ①）

**严禁** SELECT FOR UPDATE / 泛型悲观行锁。统一 `BEGIN IMMEDIATE` 单事务（复用 #108-A §3）。**任何端点只接受 `FeedbackTransition` 枚举动词，不接受裸阶段字符串**；动词→(所需 actor, 所需当前阶段, 目标阶段) 由 `ALLOWED_TRANSITIONS["fsm-v1"]` 决定。

### 3.1 `FeedbackStage` v1（阶段枚举）

`COLLECTED, CLARIFY, SOLUTION, AWAIT_OWNER_APPROVE, DEVELOP, TEST, SHIPPED, DEFERRED, REJECTED, DUPLICATE`
- 终态（不可逆）：`SHIPPED`、`REJECTED`、`DUPLICATE`。
- 挂起（可重开）：`DEFERRED`。
- 活动（可正常前向）：其余（`COLLECTED`/`CLARIFY`/`SOLUTION`/`AWAIT_OWNER_APPROVE`/`DEVELOP`/`TEST`）。

### 3.2 `FeedbackTransition` v1 允许转移全表

每条含：动词 / 允许可信 actor / 所需当前阶段 / 前置条件 / 原子写 / 审计事件 / 稳定 409/403 / 是否终态 / 重开规则。

| 动词 | actor | 所需当前 | 目标 | 前置条件 | 原子写 | 审计事件 | 409/403 | 终态 | 重开 |
|---|---|---|---|---|---|---|---|---|---|
| `CLARIFY_REQUESTED` | owner 或 submitted_by | `COLLECTED` | `CLARIFY` | — | stage=CLARIFY | `feedback.stage_transition` | 非源阶段→409；非 actor→403 | 否 | — |
| `CLARIFIED` | owner 或 submitted_by | `CLARIFY` | `SOLUTION` | — | stage=SOLUTION | `feedback.stage_transition` | 同上 | 否 | — |
| `RETURN_TO_CLARIFY` | owner 或 submitted_by | `SOLUTION` | `CLARIFY` | — | stage=CLARIFY | `feedback.stage_transition` | 同上 | 否 | 可经 `CLARIFIED` 回 SOLUTION |
| `SUBMIT_FOR_APPROVAL` | owner 或 submitted_by | `SOLUTION` | `AWAIT_OWNER_APPROVE` | `solution_text` 非空 | stage=AWAIT_OWNER_APPROVE + `pending_approval{checksum,revision,...}` | `feedback.submit_for_approval` | 同上；缺 solution→409 | 否 | 可经 `REJECT_SOLUTION`/`INVALIDATE_PENDING` 退回 |
| `INVALIDATE_PENDING` | owner 或 submitted_by | `AWAIT_OWNER_APPROVE` | `SOLUTION` | 携带内容编辑参数 `reason`(必填)+`scenario`/`expected_outcome`/`risk_tags`/`solution_text`(可选) | 在单 `BEGIN IMMEDIATE` 内原子执行：①对 A 区内容做编辑(重算 checksum+revision_count+1+corrections，reason 必填)；②清 `pending_approval`；③置 `stage=SOLUTION`（确定性单目标，不依 reason 选 CLARIFY）；④`append_audit(feedback.invalidate_pending, after={stage:SOLUTION, cleared_pending:round, reason})`。**该动词自身携带编辑参数，不另调 `amend_feedback`**（避免阶段变更绕过命名动词） | `feedback.invalidate_pending` | 非源阶段→409；非 actor→403；缺 `reason`→422 | 否 | 重新 `SUBMIT_FOR_APPROVAL` 派生新轮绑定 |
| `REJECT_SOLUTION` | **owner** | `AWAIT_OWNER_APPROVE` | `SOLUTION` | — | 插 `Approval(REJECTED, review_round=本轮)` + 清 `pending_approval` + stage=SOLUTION | `feedback.owner_reject` | 非 owner→403；非源阶段→409 | 否 | 重做 submit 时 `SUBMIT_FOR_APPROVAL` 服务端派生递增轮次（round+1）绑定新轮 |
| `APPROVE_SOLUTION` | **owner** | `AWAIT_OWNER_APPROVE` | `DEVELOP` | 绑定校验（§2.2 4-5） | 插 `Approval(APPROVED)` + stage=DEVELOP + 清 `pending_approval` + `AuditLog` | `feedback.owner_approve` | 非 owner→403；checksum/revision 失配或已终态 Approval→409 | 否 | 不可直接重开（需新 submit 轮） |
| `START_TEST` | **owner** | `DEVELOP` | `TEST` | — | stage=TEST | `feedback.stage_transition` | 非 owner→403；非源→409 | 否 | — |
| `TEST_FAILED` | **owner** | `TEST` | `DEVELOP` | — | stage=DEVELOP | `feedback.stage_transition` | 同上 | 否 | — |
| `SHIP` | **owner** | `TEST` | `SHIPPED` | — | stage=SHIPPED | `feedback.stage_transition` | 同上 | **是** | 不可重开 |
| `DEFER` | **owner** | 任意活动 | `DEFERRED` | — | stage=DEFERRED；**若源阶段为 `AWAIT_OWNER_APPROVE` 则原子清 `pending_approval`**（消除遗留 solution 绑定，pending_approval 仅存活于 AWAIT 期） | `feedback.stage_transition` | 非 owner→403；终态→409 | 否（挂起） | 经 `REOPEN`→`SOLUTION`；后续 `SUBMIT_FOR_APPROVAL` 重新派生递增轮次绑定新轮（无遗留旧轮绑定） |
| `REOPEN` | **owner** | `DEFERRED` | `SOLUTION` | — | stage=SOLUTION | `feedback.stage_transition` | 非 owner→403；非 DEFERRED→409 | 否 | — |
| `REJECT_FEEDBACK` | **owner** | `COLLECTED`/`CLARIFY`/`SOLUTION` | `REJECTED` | — | stage=REJECTED | `feedback.stage_transition` | 非 owner→403；非源→409 | **是** | 不可重开 |
| `MARK_DUPLICATE` | **owner** | 任意活动 | `DUPLICATE` | `canonical_feedback_id` 存在且同项目 | stage=DUPLICATE + `duplicate_of` | `feedback.stage_transition` | 非 owner→403；canonical 非法→409；终态→409 | **是** | 不可重开（改操作规范 feedback） |

- **优先级建议永不改变阶段**：聚类/建议路径（§4）不调用 `apply_transition`，任何 `suggested_priority` 写入均不触碰 `stage`。
- **端点约束**：`POST /feedback/{id}/transition` 请求体含 `{transition: FeedbackTransition, reason?, canonical_feedback_id?, scenario?, expected_outcome?, risk_tags?, solution_text?}`；若 `transition` 非枚举值→422；若枚举合法但源阶段不符白名单→409；`INVALIDATE_PENDING` 缺 `reason`→422；非编辑动词的编辑字段被忽略。

### 3.3 源反馈保留 + 编辑历史（响应要求 ③「无静默覆盖」）

- `amend_feedback` 编辑 A 区字段时，**先追加一条 `corrections` 项**再改当前值：
  `corrections.append({field, original_value, corrected_value, reason, actor: actor.derive_submitted_by(), timestamp: now_utc().isoformat(), revision: new_revision_count})`。
- 原始 `original_text` 永远保留在 `corrections[0].original_value`（首条创建时亦记一笔 `original_value=null→首值` 或创建即存原文）；后续任何修正均留痕，**绝不静默覆盖**。
- `corrections` 属 B 区（工作流元数据），**不参与内容校验和**（见 §1.3），但其存在保证编辑可审计、可回看原始。

### 3.4 阶段机原子契约伪码（统一）

```
self.session.rollback()
self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
try:
    fb = self.session.get(Artifact, id)                  # 事务内重读当前态
    if fb is None: raise ServiceError(404, "Artifact not found")
    if fb.type != ArtifactType.FEEDBACK: raise ServiceError(409, "not feedback")
    rule = ALLOWED_TRANSITIONS["fsm-v1"][transition]
    if actor.kind != "owner" and not (rule.actor == "owner_or_submitter"
            and actor.derive_submitted_by() == fb.metadata_json["submitted_by"]):
        raise ServiceError(403, "transition requires owner or submitter")
    if fb.metadata_json["stage"] != rule.required_current_stage:
        raise ServiceError(409, "illegal transition")     # 跳级/回退/终态→409
    # owner-only 动词额外 _assert_owner_actor(actor)
    ... # 动词特定前置（如绑定校验 §2.2、canonical 校验）
    new_md = {**fb.metadata_json, "stage": rule.target_stage,
              "transition_seq": int(fb.metadata_json.get("transition_seq", 0)) + 1}
    fb.metadata_json = new_md
    self.session.add(fb)
    append_audit(self.session, actor=actor.derive_submitted_by(),
                 action="feedback.stage_transition", resource_type="artifact",
                 resource_id=fb.id, project_id=fb.project_id, task_id=fb.task_id,
                 before={"stage": cur}, after={"stage": rule.target_stage, "transition": transition},
                 idempotency_key=f"audit:feedback:stage:{fb.id}:{cur}->{rule.target_stage}:{new_md['transition_seq']}")
    self.session.commit()                                # all-or-none
except (ServiceError, IntegrityError, Exception):
    self.session.rollback(); raise
```
- 并发第二写者：`BEGIN IMMEDIATE` 在首事务提交前阻塞；获锁后重读发现阶段已变/owner 校验失败/绑定失配 → 稳定 409。
- **审计写入一律经 `append_audit(..., before=..., after=...)`**（audit.py 原始语），绝不构造 `AuditLog(before=..., after=...)`（模型字段为 `before_snapshot`/`after_snapshot`，由 `append_audit` 内部 redact 后写入）。

## 4. 聚类摘要完全可追溯且确定性（响应要求 ④）

### 4.1 每条 `feedback.cluster_summary`（`AuditLog.after_snapshot`）必含

- `cluster_key`(str)：确定性派生 `sha256(sorted(member_ids) + "|" + policy_version)` —— 同成员集合+同策略恒得同 key；成员变动→新 key（非 supersession，是新聚类）。
- `member_ids`(list[str], **升序排序**)：本次聚类的 Feedback Artifact id。
- `member_revisions`(list[dict])：每项 `{id, content_checksum, revision}`，聚类时**只读快照**（不回写 Feedback）。
- `policy_version`(str)：聚类/策略版本，如 `"det-1.0"`。
- `summary`(str, **≤512 字符有界**)：去重/摘要文本。
- `suggested_priority`(str|null ∈ `{P0,P1,P2,P3}`)：**只读建议**，永不改变阶段。
- `actor`(服务端派生 `actor.derive_submitted_by()`) + `created_at`(now_utc)：服务器派生时间戳。
- `idempotency_key`(str，**完全确定性，无运行序号**)：`f"cluster:{project_id}:{window_start}:{window_end}:{cluster_key}:{policy_version}"`。**确定性 + 真正幂等语义**：受 `AuditLog.idempotency_key` UNIQUE 约束，`record_cluster_run` 先按此**确定性 key 查重**：
  - **若已存在同 key 记录 R**：进一步比对本次聚类「实质内容」（`member_ids` + `member_revisions` + `summary` + `suggested_priority` + `risk_tags`，即 `after_snapshot` 中除 `actor`/`created_at`/`supersedes` 外的全部字段）与 R 是否**逐字段相等**。
    - 相等 → **真正幂等**：直接返回既有 R，**不插入新行、不 supersede**（重试零副作用，满足「retry 不产生新记录」）。
    - 不等（内容确有变化）→ 走 **superseding 路径**：查当前头 H（§4.2），插入新行 `supersedes_audit_id=H.id`，新行 `idempotency_key` 为 `f"...:{cluster_key}:{policy_version}:{H.id}"`（确定性且唯一）。
  - **若同 key 不存在** → 插入首行（确定性 key）。
  - 因此：同输入重试恒返回同记录（零新行）；仅内容变化的重跑才产生 superseding 新行。**绝不**为「相同聚类」创建两条记录，也**绝不**覆盖旧行（旧行不可变）。
- `supersedes_audit_id`(str|null)：可选，指向上一条被取代的聚类摘要 audit id。

### 4.2 强制不变式

- **仅同项目成员**：所有 `member_ids` 的 `project_id == 聚类 project_id`，否则拒绝（测试：跨项目聚类拒绝）。
- **仅现存且已授权的 Feedback Artifact**：所有 `member_ids` 必须存在、`type==FEEDBACK`、同项目，否则拒绝。
- **无分支/无环/无跨聚类 supersession（单一、一致的「当前头」规则）**：定义**当前头 H** = 唯一满足「`action="feedback.cluster_summary"` 且 `after_snapshot.cluster_key=新 cluster_key` 且 `after_snapshot.policy_version=新 policy_version` 且 **不存在任何其它行的 `after_snapshot.supersedes_audit_id == 该行.id`**」的 `AuditLog` 行（即「无人指向它」）。该定义**自洽且唯一**：在单向线性链 `H0←H1←…←Hn` 中，仅尾节点 `Hn` 满足（无任何它行 `supersedes==Hn.id`）；链中其它节点（含根 `H0`，其 `supersedes_audit_id` 为 `None`）**因被其后继指向**而不满足——这与「H 是否自身有 supersedes 值无关」。**关键**：判定当前头**只看「是否有人指向」**，绝不要求 `H.supersedes_audit_id is None`（尾节点 Hn 的 supersedes 指向 Hn-1，非 None，仍合法为头）。**写入规则**：在 `BEGIN IMMEDIATE` 内原子核验 (a) H 存在且 `H.after_snapshot.cluster_key == 新 cluster_key`（禁跨聚类）；(b) 新行 `supersedes_audit_id = H.id`（新行成为新尾，指向旧尾 H）；(c) 新行 `supersedes_audit_id != 新行.id`（禁自环）。写入后旧 H 被新行指向→降级为历史，新行成为新尾。**显式允许 N 次更正链**（H0←H1←…←Hn，每轮新行指旧尾）；**禁止**：指向已被取代节点（分支，该节点已非当前头、有人指向）、指向自身（环）、指向异 cluster_key（跨聚类）、或同 `idempotency_key` 重复（UNIQUE 兜底）。**确定性 latest 解析** = 查询当前头 H（唯一「无人指向」的行），无歧义。
- **既有记录不可变**：`AuditLog` 无 UPDATE 路径；更正/重跑只追加新行（superseding），原行永不覆盖。
- **确定性 latest 解析（与「当前头 H」定义完全一致）**：取某 `(project_id, cluster_key, policy_version)` 的最新摘要 = 唯一满足「`action="feedback.cluster_summary"` AND `project_id=?` AND `after_snapshot.cluster_key=?` AND `after_snapshot.policy_version=?` AND **无任何其它行的 `after_snapshot.supersedes_audit_id == 该行.id`**」的行（即「无人指向它」= 当前头 H，与上文定义同构）。在合法单向线性链 `H0←H1←…←Hn` 中即尾节点 `Hn`（不被任何行指向）；绝不依赖 `H.supersedes_audit_id is None`（Hn 自身指 Hn-1，非 None，仍合法为 latest）。**确定性无歧义**。
- **绝不突变**任何 `Feedback` Artifact / `stage` / `Task` / `Approval` / `KnowledgeFact` / `Event` 投递 / 生产状态。

### 4.3 为何 `AuditLog` 已足够（明确回答 owner 设问，不误用 `Event`）

- `AuditLog` 已具备支撑上述契约的全部能力：append-only 惰性（无消费者/无编排）、`idempotency_key` UNIQUE（幂等去重）、`before/after_snapshot` JSON（可承载 `cluster_key/member_ids/member_revisions/policy_version/summary/suggested_priority/supersedes_audit_id` 全量归因）、`actor`+`created_at`（服务器派生可追溯）、`project_id` 索引（同项目约束可查）。
- `supersedes_audit_id` 内嵌 `after_snapshot`（**零迁移**），supersession/分支/成环约束在写入前由服务层校验（§4.2），无需新列。
- **刻意不使用 `Event` 表**：`Event`（`models.py:event`，`EventStatus{pending,processed}`）语义为「待处理/已处理的执行事件」，隐含投递/消费副作用；将其用于惰性分析会误用执行语义、且 `pending` 状态会诱使消费者改动生产。聚类摘要纯惰性、零副作用，与 `Event` 语义冲突，故**明确排除**。
- 结论：**无需新增任何 inert 持久化原语，亦无需迁移**；沿用 `AuditLog` 即可满足 §4 全部契约。

## 5. 反馈视作不可信外部输入（响应要求 ⑤）

- **per-Artifact 同项目鉴权（明确 403，非静默过滤）**：`get_feedback(id)` / `list_feedback()` 的可见性判定为——`actor.kind=="owner"`（同项目 owner 可见全部）**或** `actor.derive_submitted_by() == 该 Feedback.metadata_json["submitted_by"]`（提交者本人可见自己那条）。**任何其它 actor（含同项目无关 Agent、异项目 Agent）对单条 `get_feedback(id)` → 403**（不是 404、不是静默过滤——owner 要求「same-project unrelated Agent 403」）；`list_feedback` 同理：无关 Agent 的 list 请求整体 403（不得经 list 响应学到任何反馈原文/元数据，而非「过滤后返回空集」这种可能被利用的语义）。**绝不**因「同项目」而放行无关 Agent。该规则与 §2 服务层 `get_feedback`/`list_feedback` 一致，并在 T27/T32 断言。
- **绝不落敏感数据（密文 + PII）**：反馈/聚类/审计**不写入**任何 HTTP header、cookie、凭据、环境变量；`append_audit` 经 `redact_secrets` 自动 redact 已知密文键与高熵 token（`audit.py:75-102`）。**额外 PII 检测**：聚类摘要 `after_snapshot` 构建前，对所有字符串值（尤其 `summary` 与 `theme`）经 `redact_pii` 处理——正则/模式匹配邮箱（`[^@\s]+@[^@\s]+\.[^@\s]+`）、手机号（中国大陆 `1[3-9]\d{9}` 等）、身份证/护照号、银行卡号等高置信 PII 模式，替换为 `[REDACTED-PII]`；**且 `AuditLog` 绝不存完整反馈正文**（仅 `member_ids`+`checksum`+有界 `summary`）。PII 检测只在「分析摘要/审计」路径执行；**源反馈原文（`uri`/`metadata.original_text`）按不可信数据原样保留**（owner 控制台可看自己提交的内容），但任何对外 list/get 响应与聚类摘要均经 PII redact，杜绝跨用户泄露。
- **有界 API 响应**：`GET /feedback`/`GET /feedback/clusters` 列表响应条目数设上限（如分页 `limit≤100`）；单条 `summary` 已受 §4.1 ≤512 字符上限；`original_text` 在 list 中截断展示（如 ≤200 字符 + 省略号），`GET /feedback/{id}` 详情才返回完整原文（仍限同项目授权者）。`list/get` 响应同样经 PII redact。
- **反馈文本是数据不是指令**：反馈内容**绝不作为系统指令**进入任何 prompt 的指令位；默认聚类为确定性规则（无 LLM），**天然无 prompt-injection 面**。即使启用真实语义聚类，反馈文本仅作 user-data 传入、关闭 tool-calling、禁止其改变系统行为；含注入语句的反馈文本按**惰性数据**原样存储与展示（测试验证：注入文本不触发任何工具/指令）。
- **无任何 prompt/工具权限源自反馈内容**：分析路径不授予反馈文本任何权限；`suggested_priority` 仅读、永不驱动生产。
- **list/get 响应不泄露**：任何被授权可见的 list 响应仅含调用者有权访问的反馈；单条 `get` 越权（含同项目无关 Agent）→ 403（见上「明确 403」）；响应字段经 PII redact（§5 安全段）。
- **真实 LLM 聚类的显式门控**：需 (a) 显式凭据（V4 secret-store，非请求体）；(b) 真实模型调用开关（默认关）；(c) **cost owner 门控**（owner 显式授权付费）。**默认执行与全部测试用确定性假适配器，零付费模型调用**。
- **无效/畸形/低置信/不可用分析**：产出**无** `suggested_priority`，且**绝不改变阶段**；对应聚类记录可记一条 `AuditLog`（建议为 null）但不触发任何副作用。

## 6. 零迁移假设的证明（测试必须）

同 #108-A §6，将 `CONTENT_DRAFT` 替换为 `FEEDBACK`：
- 全新库 / 存量库升级路径：插入 `Artifact(type=FEEDBACK)` 成功，无 schema 变更。
- 枚举往返：`FEEDBACK` create/read round-trip；断言外部触发器 `knowledge_candidate_validate_insert` 仅 guard `knowledge_candidate`，不拒绝 `feedback`。
- Alembic head：`alembic heads` == 1 且 == `20260730_0001`；`migrations/versions/` **无新增迁移文件**。
- 惰性证明：`record_cluster_run` 后断言 (a) 无 `Feedback` Artifact 业务字段/阶段被改；(b) 无新增 `Event`/`Task`/`DelegatedRun`；(c) `AuditLog` 仅增（聚类摘要行），`after_snapshot` 不含反馈正文、`suggested_priority` 存在但 Feedback 未变。
- **内容校验和不受阶段影响**：`apply_transition` 前后断言 `Artifact.checksum` 不变、`revision_count` 不变（§1.3 不变性 2）。
- 落 `tests/test_feedback_zero_migration.py`。

## 7. TDD 测试计划（`tests/test_feedback.py` + `tests/test_feedback_zero_migration.py`）

**阶段机（要求 ①）**
- T1 每个**允许**转移动词成功：`CLARIFY_REQUESTED`→`CLARIFIED`→`SUBMIT_FOR_APPROVAL`(owner/submitter, solution 非空)→`APPROVE_SOLUTION`(owner)→`START_TEST`(owner)→`SHIP`(owner, 终态)；`TEST_FAILED` 回 `DEVELOP`；`DEFER`→`REOPEN`；`REJECT_FEEDBACK` 终态；`MARK_DUPLICATE`(带 canonical) 终态。
- T2 每个**禁止**转移→409：跳级（`COLLECTED→DEVELOP`）、终态再转移（`SHIPPED→*`）、`AWAIT_OWNER_APPROVE→DEVELOP` 缺 `pending_approval`、非源阶段动词。
- T3 端点**不接受任意阶段字符串**：传裸 `stage` 字段或非法枚举→422。
- T4 owner-only 动词（`APPROVE_SOLUTION`/`START_TEST`/`SHIP`/`DEFER`/`REOPEN`/`REJECT_FEEDBACK`/`MARK_DUPLICATE`）非 owner → 401/403（`_assert_owner_actor`）。
- T5 `SOLUTION→CLARIFY`（`RETURN_TO_CLARIFY`）与 `AWAIT_OWNER_APPROVE→SOLUTION`（`REJECT_SOLUTION`，插 `Approval(REJECTED)`）成功。

**owner 审批绑定唯一修订（要求 ②）**
- T6 `SUBMIT_FOR_APPROVAL` 冻结 `pending_approval{checksum,revision}`；`APPROVE_SOLUTION` 成功写 `Approval(APPROVED)`+`stage=DEVELOP`+`AuditLog`。
- T7 **stale solution approval → 409**：submit 后编辑 solution（checksum/revision 变）→ 再 `APPROVE_SOLUTION` → 409。
- T8 **并发 edit vs approval → 409**：编辑事务与审批事务并发；胜者提交后，败者 `BEGIN IMMEDIATE` 重读发现 checksum 失配→409（无 DEVELOP 无 Approval）。
- T9 **并发 approve vs reject → 确定性单终态**：两事务并发 `APPROVE_SOLUTION`/`REJECT_SOLUTION`；主保护为 `BEGIN IMMEDIATE` 写串行化 + 步骤 5 事务内「同轮无冲突终态 Approval」重读校验——首个获锁者提交终态 `Approval`；第二个获锁者重读发现同轮已存在终态→步骤 5 直接 409；`uq_approval_gate_round` 仅作二级幂等护栏。断言恰 1 条终态 `Approval`、败者 409、无重复 AuditLog/双重终态。
- T10 **任一部分写失败→全回滚**：CAS/Approval/AuditLog 任一步异常→无残留 `Approval`/阶段/`AuditLog`；**无 `DEVELOP` 状态脱离匹配 `Approval`+`AuditLog`**。
- T11 submit 后编辑必须走命名动词 `INVALIDATE_PENDING`（源码阶段 `AWAIT_OWNER_APPROVE`，目标 `SOLUTION` 确定性单目标，不依 reason 选 CLARIFY）：断言①动词**自身携带** `reason`(必填)+内容编辑参数，在单 `BEGIN IMMEDIATE` 原子执行内容编辑(checksum/revision 变、corrections 追加含 reason) + 清 `pending_approval` + `stage=SOLUTION` + `AuditLog(feedback.invalidate_pending)`；②`INVALIDATE_PENDING` 缺 `reason` → 422；③直接调 `amend_feedback`(无动词)于 `AWAIT_OWNER_APPROVE` → 409（不经命名动词不得改）；④原待审因 checksum/revision 失配自动失效（后续 `APPROVE_SOLUTION` 步骤4/5→409）；⑤无 DEVELOP 状态脱离匹配 Approval+AuditLog。
- T11b **拒绝后重做 submit 推进轮次**：`REJECT_SOLUTION`（插 `Approval(REJECTED, round=1)`）→ 再 `SUBMIT_FOR_APPROVAL` 派生 `review_round=2` 并冻结 `pending_approval.review_round=2` → `APPROVE_SOLUTION` 同一 `BEGIN IMMEDIATE` 校验「同轮无冲突终态」通过 → 成功写 `Approval(APPROVED, round=2)` + `stage=DEVELOP`；断言旧 round=1 REJECTED 不阻塞（按轮作用域），且新轮元组 `(target, NULL, 2, feedback.develop)` 与旧轮 `(target, NULL, 1, feedback.develop)` 不同、不冲突。
- T11c **DEFER 清 pending_approval**：`AWAIT_OWNER_APPROVE` 态 `DEFER` → `stage=DEFERRED` 且 `pending_approval` 被原子清除（无遗留 solution 绑定）；`REOPEN`→`SOLUTION` 后 `pending_approval` 仍为空，须重新 `SUBMIT_FOR_APPROVAL` 派生新轮；断言若直接 `APPROVE_SOLUTION` 因缺 `pending_approval`/非源阶段 → 409。

**三分离 + 校验和（要求 ③）**
- T12 创建 `Artifact.checksum`==内容校验和；`original_text`/`scenario`/`expected_outcome`/`risk_tags`/`solution_text` 入 A 区。
- T13 **阶段转移不改 `checksum`/`revision_count`**：任意 `apply_transition` 前后 `checksum` 恒定（§1.3 不变性 2）。
- T14 **相同规范内容恒得相同 `checksum`**（NFC+键排序幂等）。
- T15 `amend` 改 A 区→`checksum` 变、`revision_count+1`、**`corrections` 追加原始值**（无静默覆盖）；`list_feedback` 可读出 `corrections[].original_value`。
- T16 `metadata_json` B 区字段（stage/submitted_by/channel/cluster_id/pending_approval/workflow_revision）**不参与** `checksum`（改 B 区不影响内容引用）。

**聚类可追溯/确定性（要求 ④）**
- T17 `cluster_summary` `after_snapshot` 含全部字段（`cluster_key`/`member_ids` 升序/`member_revisions`/`policy_version`/`summary` 有界/`suggested_priority`/`actor`/`created_at`/`idempotency_key`）。
- T18 **确定性 + 真正幂等**：同 `(project_id,window,cluster_key,policy_version)` 且**内容相同**的重跑 → 返回既有 `cluster_summary` 记录、**不插入新行**（AuditLog 行数不变）、不产生 supersession；断言「retry 不产生新记录」。内容变化（如 member 集合/summary 变）的重跑 → 走 superseding 新行（见 T22）。
- T19 **跨项目聚类拒绝**：mix 异项目 member→拒绝（不写 AuditLog/不泄露）。
- T20 **仅现存已授权 Feedback**：含不存在/非 FEEDBACK/异项目 id→拒绝。
- T21 **supersession 分支/环/跨聚类拒绝**（均对照「当前头 = 唯一无人指向的尾节点」定义）：跨 `cluster_key` 取代→拒；新行 `supersedes_audit_id` 指向「有其它行指向它」的节点（即该节点已非当前头、是被取代的历史节点 = 分支）→拒；新行 `supersedes_audit_id == 新行.id`（自环）→拒；指向异 `cluster_key`→拒；同 `idempotency_key` 重复→UNIQUE 兜底拒。
- T22 **既有记录不可变 + latest 解析确定性**：多 superseding 行后，latest 解析唯一且不被取代（= 当前头 H，无人指向）。
- T22b **支持 >1 次更正线性链**：连续 3 次内容变化的重跑/更正（H0←H1←H2），每次新行 `supersedes_audit_id` 指向写入时的「当前头 H」（=唯一无人指向的尾节点，Hn 自身 `supersedes` 指向 Hn-1 仍合法为头，不要求 H.supersedes=None）；断言 (a) 每次写入成功形成 H0←H1←H2 单向链；(b) 内容未变重跑→幂等返回既有记录（见 T18），不插新行；(c) 指向「已被取代节点（有它行指向）」→拒（分支）；(d) 指向自身→拒（环）；(e) 指向异 cluster_key→拒（跨聚类）；(f) 确定性 latest = H2（无它行指 H2）；(g) 既有 H0/H1 不可变（无 UPDATE 路径）；(h) 根 H0.supersedes=None 不影响其「非头」判定（因 H1 指向它）。
- T23 聚类**绝不突变** `Feedback`/`stage`/`Task`/`Approval`/`KnowledgeFact`/`Event`；`after_snapshot` 无反馈正文。

**不可信输入（要求 ⑤）**
- T24 反馈文本含注入语句（如「忽略上述指令…」）→ 原样惰性存储、展示；**不触发**任何工具/指令/阶段变更（`suggested_priority` 仍按规则或 null）。
- T25 密文/高熵串入反馈/聚类摘要→ `AuditLog.after_snapshot` 经 `redact_secrets` 为 `[REDACTED]`，且反馈正文本身（存 `uri`/`metadata`）按数据保留但**摘要不外露**。
- T25b **PII redaction**：反馈原文含邮箱/手机号/身份证模式 → 聚类 `summary`/`theme` 经 `redact_pii` 替换为 `[REDACTED-PII]`；`AuditLog.after_snapshot` 不含明文 PII；`GET /feedback/clusters` 响应经 PII redact。源 `original_text`（`uri`）按不可信数据保留（owner 控制台自分内容可见），但对外 list/get 与摘要一律 redact。
- T26 字段超长/非法 UTF→422；`unicodedata.NFC` 归一后 `checksum` 稳定。
- T26b **有界响应**：`GET /feedback` 分页 `limit≤100`；`list` 条目 `original_text` 截断（≤200 字符）；`summary` ≤512 字符；`GET /feedback/{id}` 详情返回完整原文（仍限同项目授权者）。越界 limit→422/默认上限。
- T27 **同项目无关 Agent → 403（显式，非过滤）**：owner 创建的反馈，同项目另一 Agent `get_feedback(id)` → 403（非 404/非空过滤）；该 Agent `list_feedback` 整体 → 403（不得经 list 学到任何原文/元数据）。提交者本人 `get/list` 自己的反馈 → 200/可见。
- T28 真实 LLM 聚类默认关闭→测试 0 次付费模型调用；启用路径需显式凭据+开关+cost owner 门控（impl 落地，测试仅断言默认零调用）。
- T29 无效/畸形/低置信/不可用分析→无 `suggested_priority`、阶段不变。

**隔离/惰性/零迁移（综合）**
- T30 **聚合隔离断言（显式，非推断）**：反馈创建/聚类/转移/审批/拒绝全程断言——`KnowledgeFact` 行数 0 增；`Event` 行数 0 增；`DelegatedRun` 行数 0 增；`Task` 行数 **0 增**（绝不自动建 Task）；`task_context` 无新增；`Publication`/`Payment`/`Deployment` 类实体无改动（仓库无此类表，断言等价于「无新增生产副作用表写入」）；任何 `artifact` 行（除本 Feedback 自身创建/状态变更）**无**内容/阶段被改；无任何 `knowledge_candidate`/`review_*` 行因反馈流程新增。该测试直接核实「零自动动作」不变量，而非仅从「Event/DelegatedRun 零增 + 生产 Artifact 未变」推断。
- T31 零迁移证明（见 §6）：fresh / upgrade / 枚举往返 / 无触发器拒绝 / 单 head `20260730_0001` / 无新增迁移文件 / 内容校验和不受阶段影响 / `AuditLog` 惰性。
- T32 SQLite `BEGIN IMMEDIATE` 并发 transition → 恰 1 条终态，第二者 409，无重复 AuditLog/部分阶段。
- T32b **转移审计键跨重复周期唯一**：`SOLUTION→AWAIT→SOLUTION→AWAIT`（经 REJECT_SOLUTION 循环）两次 submit 的 `feedback.stage_transition` `AuditLog.idempotency_key` 因 `transition_seq` 递增而不同（UNIQUE 不冲突）；断言两次转移均成功落 AuditLog、无 `IntegrityError`。
- T33 exact-head CI 绿 + ruff 清；既有 tests 无回归。

## 8. 验收标准（合并门禁）

- [ ] 所有 TDD 测试通过（exact-head CI 绿），含 §7 全部新增用例（T1–T33）。
- [ ] ruff 清。
- [ ] **零 Alembic 迁移**，单 head `20260730_0001` 不变。
- [ ] **端点只接受 `FeedbackTransition` 命名动词，不接受任意阶段字符串**（非法枚举/裸 stage→422）。
- [ ] owner-only 看板门禁强制（`APPROVE_SOLUTION` 等 owner 动词非 owner 拒绝）。
- [ ] **owner 审批绑定唯一 solution 修订**：`checksum`/`revision` 失配或已终态 Approval → 409；提交后编辑自动失效退回。
- [ ] **反馈/聚类绝不创建 `KnowledgeFact`**，原文/场景/期望/风险/解决方案不自动注入知识。
- [ ] 聚类摘要为追加写惰性 `AuditLog`，完全可追溯 + 确定性，更正走 superseding 记录；**优先级建议只读**（不建 Task/不改生产/不接付费/不改架构）。
- [ ] 阶段机原子契约：`BEGIN IMMEDIATE`+CAS；无 SELECT FOR UPDATE / 悲观行锁；并发/回滚测试覆盖。
- [ ] **源反馈保留 + 编辑历史无静默覆盖**（`corrections`）；内容校验和不受阶段影响。
- [ ] **反馈视作不可信输入**：UTF-8 归一+字段上限；`AuditLog` 不存完整正文且 redact 密文；注入文本按惰性数据；默认零付费 LLM 调用。
- [ ] `Event` 未被用于聚类/建议（已用 `AuditLog`）；`AuditLog` 惰性契约经测试断言。
- [ ] Codex(`gpt-5.6-sol`) APPROVE。

## 9. 不在范围 / 开放问题

- 不在：自动发布反馈处理结果、自动建 Task/改生产、接付费模型、跨渠道自动同步、客服全自动。
- 已对齐（v4）：复用 #108-A 三契约；阶段机版本化白名单 + 命名转移动词（无裸 stage）；owner 审批绑定唯一修订（`pending_approval` 冻结 + `BEGIN IMMEDIATE` 绑定校验）；源/工作流/分析三分离 + 显式校验和域；聚类摘要完全可追溯且确定性（沿用 `AuditLog`，不误用 `Event`）；反馈不可信输入安全模型。
- 开放（impl 定）：真实语义聚类（LLM）的凭据与 cost owner 落地（V4 secret-store）；`submitted_at` 时区统一 UTC；owner 控制台入口形态（自然语言录入 UI）；`risk_level` 取值映射（Approval 必填，impl 据 `risk_tags` 推导）。

## 10. 分支与评审

- 状态：**待评审（PLAN PR）**。
- 本 PR：`feat/issue-110-plan` → `main`，**仅 `docs/issue-110-feedback-loop-plan.md`，零代码零迁移**。
- 评审门禁：Codex(`gpt-5.6-sol`) APPROVE → 设 `gate:merge`/`next:owner`/`status:blocked` + assignee `QLM1234` → owner 授权 squash-merge（精确 head 锁）→ 删分支 → 清标签 → **保持 #110 open**（实现 PR 闭环时才关）。
- 实现分支：`feat/issue-110-feedback-loop-impl`（base `main`），**本计划 merge 后才创建**：TDD 实现 → `codex review` APPROVE → exact-head CI 绿 → 设 `gate:merge`/`next:owner`/`status:blocked` → owner 授权 squash-merge → 关 #110、清门禁标签。**绝不自动 merge**。
