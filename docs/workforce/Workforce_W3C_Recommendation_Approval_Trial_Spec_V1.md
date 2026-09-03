# Workforce W3-C Spec V1 — Recommendation / Approval / Trial

> **状态**：设计文档（本轮只做设计，不含实现代码 / migration / 测试代码）。
> **基线**（已冻结，禁止改写语义）：
> - W3-A landed：`c367de9`（Evaluation 单环）
> - W3-B exact-head：`6c08d044d2049f7fb48467e2e89c0b30baecba75`，landed：`0a72c45`（W3-B tree 与 exact-head 字节级一致，CI 全绿）
> - 本次 W3-C 仅基于上述冻结态做**新增（additive）**设计。

## 0. 闭环与目标

```
W3-B:  Match / Ranking  ──▶  W3-C:  Recommendation  ──▶  Approval(L4)  ──▶  Trial
 (compute_match / rank_candidates, 已冻结)      (本 Spec)      (owner_inbox 人类闸)  (本 Spec, W3 终点)
```

W3-C 完成「Match/Ranking → Recommendation → Approval(L4) → Trial」闭环。
**W3 止于 Trial**：Employee / Training / Performance 归 W4，本 Spec 不建、不改。

---

## 1. 硬约束满足矩阵

| # | 硬约束 | W3-C 满足机制 |
|---|---|---|
| 1 | W3-A/W3-B 冻结，禁改语义 | 仅新增表/列/枚举值；`CandidateLifecycle` 仅**扩展边**，不删改既有边；不碰 `evaluate_candidate` / `compute_match` / `run_benchmark` / `candidate.evaluation_context`。 |
| 2 | Recommendation 必须建立在可解释 Match/Ranking 之上 | `recommend_candidate` **只读** `Match`（score/breakdown/evidence_refs/evaluated_fields/status），原样快照进 `Recommendation`，**不重算** capability_fit / benchmark_score。 |
| 3 | 禁止虚构 reliability / historical / cost | `Recommendation.breakdown` 直接复制 `Match.breakdown`（其 `excluded` 已含 `reliability/historical/cost`）；W3-C 不产生任何新评分维度。 |
| 4 | score 必须保留 `breakdown + evidence_refs` | `Recommendation` 强制含 `breakdown`(JSON) + `evidence_refs`(JSON)，且 `evidence_refs` 直接转发 `Match.evidence_refs`。 |
| 5 | `Approval(owner_inbox / L4)` 是 Recommendation→Trial 唯一人类闸 | Trial **仅**在 `Approval.status=APPROVED` 路径内创建；Approval 以 `risk_level=L4` 经现有 `OwnerInboxService.decide` / `services.decide_approval` 决策（人类专属）。 |
| 6 | 禁止任何自动绕过 Approval 创建 Trial | 所有 Trial 创建函数入口断言 `Approval.status == APPROVED`，否则 422/409（F-R4）；无其它 Trial 创建入口。 |
| 7 | Recommendation/Trial 须幂等、审计、fail-closed | 见 §9 幂等、§10 fail-closed、§11 审计。 |
| 8 | W3 止于 Trial | `Trial` 由 W3-C 建为 `PROPOSED`；ACTIVE/COMPLETED/… 归 W4。 |
| 9 | Employee/Training/Performance 留 W4 | 不建相关表/列；W3-C 仅提供 W4 入口（§8）。 |
| 10 | 不接 ai-arena、不改 ruleset | 不引用 `BenchmarkAdapter` 外部适配；不碰 `ruleset`/RLS。 |
| 11 | 复用 Scheduler/Execution/Budget/Audit/Approval，不重造 | **复用**：`AuditLog`(append_audit)、`Approval`(owner_inbox/L4)、`CandidateLifecycle`。**递延 W4**：Scheduler/Execution/Budget（见 §12 说明）。 |

---

## 2. Recommendation 数据模型

新增表 `recommendation`（additive，不影响既有表）：

```text
recommendation
  id                         PK  (rec_<12hex>)
  candidate_id              FK -> candidate.id        ON DELETE CASCADE
  job_version_id            FK -> job_version.id       ON DELETE CASCADE
  match_id                  FK -> match.id             ON DELETE RESTRICT  (溯源，不重算)
  score                     float                     (原样 = Match.score，不重算)
  weights_version           str                       (原样 = Match.weights_version)
  breakdown                 JSON                      (原样复制 Match.breakdown，含 excluded[])
  evaluated_fields          JSON                      (原样复制 Match.evaluated_fields)
  evidence_refs             JSON                      (原样复制 Match.evidence_refs)
  match_blocked_reason      str | None                (原样复制 Match.match_blocked_reason，fail-closed 留痕)
  status                    RecommendationStatus       default PROPOSED
  rationale                 str | None                (owner 决策备注，可选)
  recommended_by            str = "workforce_recommend"
  created_at / updated_at   datetime
  UNIQUE(candidate_id, job_version_id)               ← 幂等身份
```

`RecommendationStatus`(StrEnum, 存为 VARCHAR)：
- `PROPOSED` — 已建，待 Approval。
- `APPROVED` — L4 闸 APPROVED → 已建 Trial。
- `REJECTED` — L4 闸 REJECTED。
- `WITHDRAWN` — 被同 candidate+job_version 的更新版 Recommendation 取代（可选，W3-C 至少保留枚举）。

**可解释性**：`breakdown` / `evidence_refs` / `evaluated_fields` 全部来自 W3-B `Match`，W3-C 不生成新证据、不重算分数。

---

## 3. Trial 数据模型

新增表 `trial`（additive）：

```text
trial
  id                         PK  (trial_<12hex>)
  candidate_id              FK -> candidate.id        ON DELETE CASCADE
  job_version_id            FK -> job_version.id       ON DELETE CASCADE
  recommendation_id         FK -> recommendation.id    ON DELETE RESTRICT
  approval_id               FK -> approval.id          ON DELETE RESTRICT
  status                    TrialStatus                default PROPOSED
  trial_plan_ref            JSON | None               (W4 激活时填充，W3-C 留空)
  started_at / ended_at     datetime | None
  created_at / updated_at   datetime
  UNIQUE(approval_id)                                ← 一个 Approval 只产一个 Trial（幂等）
```

`TrialStatus`(StrEnum)：
- `PROPOSED` — **W3-C 唯一设置的态**；Trial 创建即此态。
- `ACTIVE` / `COMPLETED` / `CANCELLED` / `FAILED` — **词汇预留，W3-C 不进入**；归 W4（同 W3-A 预留 `RECOMMENDED` 模式）。

`candidate.status` 在 W3-C 进入 `TRIALING`（新增枚举值，**无需 migration**，列是 `sa.String()`）。

---

## 4. Approval 复用（L4 人类闸）

**复用既有 `Approval` 表**，仅做 additive 扩展，不新建 `workforce_approval`：

- 给 `Approval` 增加可空列 `recommendation_id`（`FK -> recommendation.id ON DELETE RESTRICT`，additive ALTER），并加 `UNIQUE(recommendation_id)`（一个 Recommendation 仅一个 Approval，并发收敛）。
- `request_recommendation_approval` 创建：
  ```text
  Approval(
    recommendation_id = <id>,
    action_type       = "workforce_recommendation",
    risk_level        = RiskLevel.L4,          # 人类专属闸
    status            = ApprovalStatus.PENDING,
    project_id        = None,                  # Workforce 当前无 Project 关联（见 §12）
    task_id           = None,
    target_artifact_id= None,
    review_policy_id  = None,
    review_round      = 1,
  )
  ```
- 决策走**既有** `services.decide_approval(...)` / `OwnerInboxService.decide(...)`——L4 闸已强制人类决策；非人类 actor 调用决策路径返回 403/409（F-R5）。
- APPROVED → 触发 `create_trial_from_approval`（§6）；REJECTED → `Recommendation.REJECTED` + `Candidate RECOMMENDED→REJECTED`。

---

## 5. CandidateLifecycle 扩展（W3-C 仅加边）

`CandidateStatus` 新增 `TRIALING = "trialing"`（列是 `sa.String()`，**无 migration**）。
`CandidateLifecycle.ALLOWED` 在 W3-C **仅扩展**以下边（不删改既有边）：

| 源 | 新增目标 | 触发函数 |
|---|---|---|
| `EVALUATED` | `RECOMMENDED` | `recommend_candidate` |
| `RECOMMENDED` | `REJECTED` | Approval REJECTED |
| `RECOMMENDED` | `TRIALING` | Approval APPROVED → `create_trial_from_approval` |

残留 `RECOMMENDED: set()`（W3-A）→ 变为 `{REJECTED, TRIALING}`。
`TRIALING` 的后续边（`TRIALING→EMPLOYED` 等）归 W4，本 Spec 不定义。

---

## 6. 状态机

### 6.1 Recommendation 状态机
```
PROPOSED --(Approval APPROVED)--> APPROVED --(建 Trial)--> [终态, W3-C]
PROPOSED --(Approval REJECTED)--> REJECTED --[终态]
PROPOSED --(被新推荐取代)--> WITHDRAWN (可选)
```

### 6.2 Approval → Trial 状态流
```
Recommendation(PROPOSED)
   │ request_recommendation_approval
   ▼
Approval(PENDING, L4)
   │ OwnerInboxService.decide  (人类)
   ├─ APPROVED ──▶ Recommendation.APPROVED
   │                 + Candidate RECOMMENDED→TRIALING
   │                 + create_trial_from_approval → Trial(PROPOSED)
   └─ REJECTED ──▶ Recommendation.REJECTED
                     + Candidate RECOMMENDED→REJECTED
                     + 不建 Trial
```

### 6.3 Candidate 生命周期（W3-C 切片）
```
POOLED → EVALUATING → EVALUATED → RECOMMENDED → TRIALING → (W4: EMPLOYED)
                  ↘ REJECTED      ↘ REJECTED
```

---

## 7. 与 W3-B Match/Ranking 的接口契约

**读依赖（仅只读）**：
- `Match`：`score`, `weights_version`, `breakdown`, `evaluated_fields`, `evidence_refs`, `status`, `match_blocked_reason`, `benchmark_version_id`。
- `Candidate`：`status`（须 `EVALUATED`）、`candidate_id`。
- `CandidateLifecycle`：状态迁移校验。

**显式不依赖 / 不改动**：
- 不调用 `compute_match` / `rank_candidates` 内部的 capability/benchmark 计算；
- 不写 `Candidate.evaluation_context`；
- 不改 `Match` 任何字段；
- 不接 `BenchmarkAdapter` / ai-arena。

`recommend_candidate(candidate_id, job_version_id)` 契约：
- 仅当存在 `Match(candidate_id, job_version_id)` 且 `Match.status == COMPUTED` 时建 Recommendation；
- `rank_candidates` 的排序结果可作为 `recommend_top_k` 的入参（可选，不在必须集）。

---

## 8. W4 Employee 接口边界

W3-C 是 W3 终点，为 W4 提供清晰交接：
- **交接标记**：`Candidate.status = TRIALING` 是 W3→W4 的唯一握手态。
- **W4 入口**：`SELECT * FROM trial WHERE status = 'proposed'`（或按 `candidate_id`/`job_version_id` 取）。
- **W4 职责（本 Spec 之外）**：Trial `PROPOSED→ACTIVE`；运行试用；成功则 `Candidate.TRIALING→EMPLOYED` 并建 `Employee`（W4 表）；失败则回退/标记。
- **W3-C 禁做**：不建 `Employee`、不写 Trial 的 ACTIVE+ 流转、不触碰 `Agent`/调度执行实际工作。

---

## 9. 幂等规则

| 操作 | 幂等身份 | 并发收敛 |
|---|---|---|
| `recommend_candidate` | `UNIQUE(candidate_id, job_version_id)` 或 key `rec:<cand>:<jv>` | SAVEPOINT 吸收 UNIQUE 冲突，回读已存在行 |
| `request_recommendation_approval` | `UNIQUE(recommendation_id)` on `Approval` 或 key `recappr:<rec_id>` | 已存在则返回既有 Approval（PENDING/已决） |
| `create_trial_from_approval` | `UNIQUE(approval_id)` on `trial` 或 key `trial:<approval_id>` | 已存在则返回既有 Trial |

全部沿用 W3-B 的 SAVEPOINT 模式（见 `compute_match` 的 `begin_nested`）。

---

## 10. Fail-closed 规则

- **F-R1**：无 `Match(candidate_id, job_version_id)` → `ServiceError(422)`（不评估就不能推荐）。
- **F-R2**：`Match.status == BLOCKED`（capability_gap）→ 拒推荐 `ServiceError(422)`；`blocked` 永远不可入围。
- **F-R3**：`Candidate.status != EVALUATED` → `ServiceError(422)`。
- **F-R4**：Trial 创建**仅**在 `Approval.status == APPROVED` 分支内；任何其它路径（含直接调用）持 PENDING/REJECTED Approval → `ServiceError(409/422)`。
- **F-R5**：非人类 actor 不可 APPROVE；`risk_level=L4` + `OwnerInboxService.decide` 强制人类；程序化 APPROVE → 403/409。
- **F-R6**：`breakdown` / `evidence_refs` 缺失或为空 → `ServiceError(422)`（绝不存不可解释的评分）。

---

## 11. Audit / Evidence 要求

复用 `append_audit(session, *, actor, action, resource_type, resource_id, project_id, task_id, before, after, idempotency_key)`（签名见 `aios/audit.py:110`）：

| 动作 | action | before / after |
|---|---|---|
| 建 Recommendation | `recommendation.created` | `{}` / `{score, breakdown, evaluated_fields, evidence_refs, status}` |
| 重评估刷新快照 | `recommendation.refreshed` | 旧 `{score,status}` / 新 |
| 请求 Approval | `recommendation.approval_requested` | `{}` / `{approval_id, risk_level}` |
| Approval 决策（既有 decide 已记） | `approval.decided` | 既有 / 链接 recommendation_id |
| 建 Trial | `trial.created` | `{}` / `{trial_id, approval_id, candidate_id, job_version_id, status}` |
| Candidate 迁移 | `candidate.transition` | `{from}` / `{to}` |

- `idempotency_key` 命名空间：`rec:` / `recappr:` / `trial:`。
- Evidence：`Recommendation.evidence_refs` 非空且等于 `Match.evidence_refs`（契约测试断言）；不生成新证据。

---

## 12. Migration 必要性分析

**需要 migration（单 head，additive，可逆）**：
1. 新建表 `recommendation`（含 `UNIQUE(candidate_id, job_version_id)`）。
2. 新建表 `trial`（含 `UNIQUE(approval_id)`）。
3. `Approval` 加可空列 `recommendation_id`（`FK->recommendation.id ON DELETE RESTRICT`）+ `UNIQUE(recommendation_id)`。

**不需要 migration**：
- `CandidateStatus.TRIALING` 与 `RecommendationStatus` / `TrialStatus` 枚举值——`candidate.status` / 新列均为 `sa.String()`（VARCHAR），新增枚举值零 schema 改动（参照 W3-A `RECOMMENDED` 同款做法）。
- `CandidateLifecycle.ALLOWED` 边扩展——纯代码。

**Budget 复用说明（关键决策）**：当前 Workforce 链（BusinessGoal→RequiredWork→Job→JobVersion→Candidate）**无 `Project` 外键**，而 `check_budget(session, project, estimated_cost)` 需 `Project`。因此 **W3-C 不调用 `check_budget`**——Trial 创建（PROPOSED）本身不产生花费；Budget 闸门**递延至 W4** Trial 激活（实际委派/执行时）再复用。Scheduler/Execution 同理归 W4。此为有意边界，非遗漏（GO WITH CONDITIONS 条件①待 R7 确认）。

---

## 13. 完整测试计划（供后续实现，本轮不写代码）

**Recommendation**
1. `recommend_candidate` 正常：EVALUATED + COMPUTED Match → 建 Recommendation，Candidate `EVALUATED→RECOMMENDED`，audit 含 breakdown/evidence_refs。
2. 推荐 BLOCKED Match → 422（capability_gap 拒入）。
3. 无 Match → 422。
4. 非 EVALUATED Candidate → 422。
5. 幂等：重放返回同一 Recommendation（无复制、无重算）。
6. 重评估新 attempt → 刷新 Recommendation 快照 + audit before/after。
7. `recommendation.evidence_refs` 非空且 == `Match.evidence_refs`；`breakdown.excluded` 含 reliability/historical/cost。

**Approval（L4）**
8. `request_recommendation_approval` 建 `Approval(risk_level=L4, status=PENDING)` 且绑定 recommendation；幂等。
9. APPROVED → 建 Trial(PROPOSED) + Candidate `RECOMMENDED→TRIALING` + audit。
10. REJECTED → Recommendation REJECTED + Candidate `RECOMMENDED→REJECTED` + **不建 Trial**。
11. Trial 幂等：一个 Approval 仅一个 Trial。

**Fail-closed**
12. F-R4：持 PENDING/REJECTED Approval 直接 `create_trial` → 409/422（静态 + 契约双重）。
13. F-R5：非人类 actor 调 APPROVE → 403/409。
14. F-R6：`breakdown`/`evidence_refs` 缺失 → 422。

**状态机边界**
15. `EVALUATED→RECOMMENDED` 允许；`RECOMMENDED→TRIALING` / `RECOMMENDED→REJECTED` 允许；非法（`POOLED→RECOMMENDED`、`EVALUATED→TRIALING`）→ 409。

**Migration / 回归**
16. Alembic 单 head、additive、`downgrade` 可逆（drop recommendation+trial 表 + Approval.recommendation_id 列）；head 常量断言。
17. W3-A+W3-B 回归 246+ 全绿（零语义改动）。
18. 边界静态检查：W3-C **不写** `candidate.evaluation_context`、**不调** `compute_match`/`run_benchmark`、**不建** Employee、**不碰** ai-arena/ruleset、**不调** Scheduler/Execution/Budget（递延 W4）。

---

## 14. 结论

**GO WITH CONDITIONS**

设计内在一致，复用既有 `Approval`(L4)/`AuditLog`，严守 11 条硬约束，fail-closed + 幂等 + 可解释齐备，migration 为单 head additive 可逆。以下为待 R7 确认的 5 项条件（均为确认项，无需设计返工）：

1. **Budget 递延 W4**：W3-C 建 Trial(PROPOSED) 不调 `check_budget`（Workforce 无 Project 关联）；若 R7 要求 W3-C 内加 Budget 闸，则需先给 Job/BusinessGoal 接 Project（超出本轮）。
2. **`TRIALING` 为 W3→W4 握手态**，W4 拥有 `TRIALING→EMPLOYED` + `Employee` 创建。
3. **Approval 复用方式**：扩展既有 `Approval` 加 `recommendation_id` 列（单 head），而非新建 `workforce_approval` 表。
4. **TrialStatus 词汇预留**：W3-C 仅置 `PROPOSED`，`ACTIVE/COMPLETED/CANCELLED/FAILED` 归 W4（同 W3-A 预留 `RECOMMENDED`）。
5. **Recommendation 纯只读 Match**：不写 `evaluation_context`、不重算 capability_fit/benchmark_score。

**未授权前不进入 W3-C 实现。**
