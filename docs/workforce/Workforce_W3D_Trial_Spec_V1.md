# Workforce W3-D — Trial Spec V1

> 状态：**设计稿，待 R7 确认 §12 条件后进入实现**
> 上游基线：`docs/workforce/Workforce_W3C_Recommendation_Approval_Spec_V4.md`（W3-C 已冻结落地，main `9364a67`）
> 代码基线：`main @ 9364a67`（PR #7 落地后）

---

## 0. 定位与边界来源

W3-D 是 Workforce 闭环的第 5 环：

```
Candidate(POOLED)
  → EVALUATING → EVALUATED          [W3-A]
  → Match / Benchmark / Ranking     [W3-B]
  → Recommendation(PROPOSED)        [W3-C]
  → 人类 L4 闸 APPROVED             [W3-C, decide_recommendation]
  → Trial(PROPOSED)                 [W3-D  ← 本轮]
  → Employee                        [W4]
```

**权威边界 = W3-C Spec V4 §11**（`Workforce_W3C_Recommendation_Approval_Spec_V4.md:608-623`）。该节逐条划定：

| 边界项 | W3-C（已落地） | W3-D（本轮） | W4+ |
|---|---|---|---|
| Trial 实体 | 不建表、不建函数 | **建 `trial` 表 + `create_trial_from_approval`** | — |
| Trial 守卫 | `assert_trial_eligible` 已实现（含惰性 F-R8） | **本轮唯一前置调用点** | 激活前须复检 |
| `CandidateStatus.TRIALING` | 明确不新增 | **新增枚举 + 受控加边** | 后续出边 |
| Employee / Training / Performance | 不建 | 不建 | W4 建 |
| Budget / Scheduler / Execution | 零调用 | **零调用** | W4 在激活时复用 |

### 0.1 与早期 Trial 草稿的偏离（**必读，防止照抄旧稿**）

`docs/workforce/Workforce_W3C_Recommendation_Approval_Trial_Spec_V1.md`（299 行，早期 W3-C+Trial 合并稿）是本轮唯一前身文档。它提出的设计**已被 W3-C V4 推翻**，本轮**不继承**：

| 早期草稿条款 | V4 裁决 | W3-D 处理 |
|---|---|---|
| §3 `trial.approval_id` → `approval.id` RESTRICT | **Q1=B：不物理复用 `approval` 表** | ❌ 不建该列、不建该 FK |
| §3 `UNIQUE(approval_id)` 作幂等锚 | `Recommendation.approval_id` V1 恒 `None`（`models.py:1919`），**不存在任何 Approval 行** | ⚠️ **锚点失效**；改 `UNIQUE(recommendation_id)`（§3-Q1） |
| §4 给 `Approval` 加 `recommendation_id` 列 | Q1=B 否决，V4 §12.2 明列 `approval` 表「不需要 migration」 | ❌ 不碰 `approval` 表 |
| §3 `trial_plan_ref` / `started_at` / `ended_at` | W4 才填充，W3-D 恒空 | ❌ 不建（死列，§3-Q4） |
| §5 `CandidateStatus.TRIALING` 在 W3-C 进入 | V4 §11：W3-C 止于 `RECOMMENDED` | ✅ 挪到 W3-D（与 V4 一致） |

> **一句话**：早期草稿假设"Trial 挂在 Approval 行上"；V4 的真实落地是"L4 决策落在 `Recommendation.decided_by` 上，Approval 表根本没被用"。
> 因此 W3-D 的幂等锚必须从 `approval_id` 迁移到 `recommendation_id`。这是本轮**最重要的单点偏离**。

---

## 1. 代码考古结论（只读事实，全部带行号出处）

| # | 事实 | 出处 |
|---|---|---|
| F-1 | `assert_trial_eligible(session, recommendation_id) -> bool` 已实现；语义 = `status == APPROVED and bool(decided_by)` | `workforce_recommendation.py:637-659` |
| F-2 | 该函数**不纯读**：先调 `_reconcile_drift` 做惰性 F-R8 撤回（可写 `WITHDRAWN` + 审计 + 释放 candidate），再 `session.refresh()` 复检 | `workforce_recommendation.py:650-659` |
| F-3 | `_reconcile_drift` 是 `WITHDRAWN` 的唯一写入者；CAS + SAVEPOINT，不自行 commit | `workforce_recommendation.py:550-634` |
| F-4 | `_detect_drift`：`match` 缺失 / attempt 不可解析 / attempt 漂移 → 均判漂移（不可验证即不信） | `workforce_recommendation.py:159-176` |
| F-5 | **`assert_trial_eligible` 在生产代码中零调用点**，仅 `tests/test_workforce_recommendation_w3c.py` 引用 | 全仓 grep 确认 |
| F-6 | `_sync_candidate_back` 对 `status != RECOMMENDED` 的 candidate **静默 no-op** | `workforce_recommendation.py:457-459` |
| F-7 | `decide_recommendation` 是唯一 L4 人类闸；`actor` keyword-only 无默认；非 owner → 403 | `workforce_recommendation.py:467-542` |
| F-8 | `purge_recommendation` 只放行终态 `WITHDRAWN` / `REJECTED`，否则 409；owner-only + 全列快照审计 | `workforce_recommendation.py:667-731` |
| F-9 | `Recommendation` 三父 FK（candidate / job_version / match）**全 RESTRICT**（DR-1） | `models.py:1846+`；迁移 `20260902_0001_...` 文件头注释 |
| F-10 | `Recommendation.approval_id: str \| None = None`，无 FK，V1 恒 None（前向列） | `models.py:1919` |
| F-11 | `UNIQUE(candidate_id, job_version_id)`；`_rebuild_recommendation` 是**原地 UPDATE**（同一行 id 复用） | `workforce_recommendation.py:355+` |
| F-12 | `CandidateStatus` 列是 `sa.String()`，**加枚举成员零 migration** | `models.py:1542`，`RECOMMENDED` 在 1575 |
| F-13 | `CandidateLifecycle.ALLOWED[RECOMMENDED] = {EVALUATED}`；`TRIALING` 不存在 | `workforce.py:479-497` |
| F-14 | `audit_log.idempotency_key` 是 `unique=True` 且 NOT NULL | `audit.py:71` |
| F-15 | `append_audit(..., actor, action, resource_type, resource_id, project_id, task_id, before, after, idempotency_key)` 全关键字 | `audit.py:110-122` |
| F-16 | `redact_secrets` 对 before/after 递归脱敏；**值模式匹配存在但 `sk-live-xxx` 因连字符打断 `[A-Za-z0-9]{8,}` 而漏网** | `audit.py:37-56, 100-101` |
| F-17 | `_assert_owner_actor`：非 owner / owner 缺 `owner_id` → 403 | `actor.py:79-91` |

### 1.1 由考古推出的三条推论（决定本轮设计）

**推论 A（由 F-5）**：F-R8 漂移只在"被查询时"惰性观测。W3-D 创建 Trial 后若不再调用 `assert_trial_eligible`，Recommendation 不会被撤回。
⇒ 这是**安全**的：撤回的目的是阻止基于失效证据的**后续动作**，而后续动作（W4 激活）必须复检该闸（§6 F-T3）。创建 Trial 本身不会让失效证据造成新后果。

**推论 B（由 F-6 + F-13）**：一旦 candidate 进入 `TRIALING`，F-R8 撤回**不会**把它拉回 `EVALUATED`（`_sync_candidate_back` 静默 no-op）。
⇒ 已知行为缺口，**不在本轮修**（修 = 改 W3-C 冻结代码 + 定义 Trial 取消语义 = W4 职责）。登记为 §12 C-5。

**推论 C（由 F-8 + F-9）**：`APPROVED` 的 Recommendation **已经是 Job 删除的永久锁**（W3-C 既有行为 DR-3：活跃行不得被静默抹除，且无"关闭已批准推荐"的路径）。
⇒ W3-D **不改变**这一既有锁；但**不得再叠加一把自己也解不开的锁**，也**不得**为了形式上"配解锁"而交付一个不可达的 purge 函数 —— 完整论证见 §3-Q2 与 §12 C-2。

---

## 2. 范围

### 2.1 本轮闭环（W3-D）

1. `trial` 表（additive，1 张）
2. `TrialStatus` 枚举
3. `create_trial_from_approval()` —— **唯一**的 Trial 创建者，也是**唯一**把 `candidate.status` 写成 `TRIALING` 的地方
4. `CandidateStatus.TRIALING` + `CandidateLifecycle` 受控加边（**唯一的 `workforce.py` 触碰点**）
5. additive alembic 迁移 1 个（单 head）
6. 契约测试（§11）

> **本轮不交付 `purge_trial`** —— 见 §2.2 与 §12 C-2 的完整论证。

### 2.2 明确不做（W4+）

| 不做 | 归谁 | 理由 |
|---|---|---|
| Trial 激活 / 完成 / 取消 / 失败（`ACTIVE`/`COMPLETED`/`CANCELLED`/`FAILED`） | W4 | V1 无转移入口，建了是死词汇 |
| `TRIAL_ALLOWED` 边表 + `_transition_trial_status` | W4 | V1 零转移 → 死代码；W4 加第一个转移时同步引入 |
| `purge_trial()` / `${ANY}` Trial 删除路径 | W4 | **§3-Q2**：V1 内任何解锁路径要么不可达、要么制造 orphan-`TRIALING`，均劣于一条记录在案的 W4 义务 |
| `Employee` 表 | W4 | V4 §11 明裁 |
| Budget（`check_budget`）/ Scheduler / Execution | W4 | V4 §11 明裁「不重写，只复用」 |
| `trial_plan_ref` / `started_at` / `ended_at` | W4 | W3-D 恒空 = 死列 |
| `approval` 表物理复用 | W4（A′ 升级路径） | Q1=B |
| 修改 `workforce_recommendation.py` | — | W3-C 已冻结；本轮**纯新增调用**，不改其任何定义 |

---

## 3. 设计裁决（Q1–Q7）

### Q1 — 幂等锚：`UNIQUE(recommendation_id)`

**否决**早期草稿的 `UNIQUE(approval_id)`：`approval_id` 在 W3-C 落地后恒为 `None`（F-10），且 SQLite 的 UNIQUE 把多个 NULL 视为互不相同，**该约束在 V1 完全不生效**，是假的幂等保证。

**采纳** `UNIQUE(recommendation_id)`：

- 语义正确 —— Trial 是"某一条人类批准"的物理后果，一批准最多一 Trial。
- 与 F-11 相容：Recommendation 是 `UNIQUE(candidate_id, job_version_id)` 且 `_rebuild_recommendation` 原地 UPDATE，**同一个 (candidate, job_version) 终身只有一行 Recommendation**，因此本约束与 `UNIQUE(candidate_id, job_version_id)` 基数等价，但语义锚点更精确。
- 撤回后重建再批准 → 重放命中既有 Trial 行，直接返回（§7）。

### Q2 — 外键策略：**三个父全 RESTRICT，本轮不交付解锁路径**

| 列 | 策略 | 理由 |
|---|---|---|
| `trial.candidate_id` | **RESTRICT** | 镜像 W3-C DR-1：Trial 是在用的雇佣证据，不得随 Job/Candidate 删除而消失 |
| `trial.job_version_id` | **RESTRICT** | 同上 |
| `trial.recommendation_id` | **RESTRICT** | 镜像 C4：删除仍支撑 Trial 的批准 = 悬空试用期，必须拒绝 |

**为什么不用 CASCADE**：candidate/job_version 的 CASCADE 在**实践中永不触发**（Recommendation 已用 RESTRICT 挡住了父行删除），声明一个永不触发的级联是**说谎**；`recommendation_id` 的 CASCADE 则会在 `purge_recommendation` 时静默抹除 Trial 且不产生 `trial.deleted` 审计行。

**为什么本轮不交付 `purge_trial`**（在 W3-C 的 D-R1 先例下必须显式论证）：

1. **它在 V1 无法真正解锁任何东西。** 推论 C 已确立：`APPROVED` 的 Recommendation 本身就是 Job 删除的永久锁（`purge_recommendation` 只放行终态）。解锁链是 `purge_trial → purge_recommendation → 删 Job`，第二步在 V1 **必然 409**。交付 `purge_trial` 得到的是一段**不可达代码**。
2. **它会制造一个更糟的不一致。** 删掉 Trial 行后，candidate 滞留在 `TRIALING`（该节点在 W3-D 出边为空集，见 §5.1），形成"无任何 Trial 却标记试用中"的孤儿状态；且 `create_trial_from_approval` 无法重放（`TRIALING → TRIALING` 非法）。
3. **唯一能真正解决它的地方是 W4。** 解锁路径的正确形态依赖 Trial 的**取消语义**（`TRIALING` 的出边、candidate 的释放目标），而这正是 §12 C-5 已划给 W4 的东西。

> **结论**：RESTRICT 本身就是**fail-closed 方向** —— 拒绝删除是安全的，解锁只是便利性。
> 本轮选择「交付一个**记录在案的 W4 义务**」而非「交付一个半吊子函数」：前者是设计不完整（可辩护），后者是已知正确性缺陷（不可接受）。
> 登记为 §12 **C-2**。

### Q3 — `TrialStatus`：**单成员 `PROPOSED`**

```python
class TrialStatus(StrEnum):
    PROPOSED = "proposed"
```

- W3-D 只写一个值；单成员让"本轮只有一个状态"成为**结构事实**而非口头约定。
- 不预留 `ACTIVE`/`COMPLETED`/`CANCELLED`/`FAILED` 死词汇 —— W4 加成员时**零 migration**（列是 `sa.String()`，同 F-12）。
- 因此 **V1 不建 `TRIAL_ALLOWED` 边表、不建 `_transition_trial_status`**（零转移 → 死代码）。
- 不变式：`trial.status` 的唯一写入者是 `create_trial_from_approval` 的构造默认值。**W4 引入第一个转移时，必须同步引入 `TRIAL_ALLOWED` + 单一状态写入器**（登记 §12 C-4）。

### Q4 — 列集：**7 列最小集**

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `str` PK | `new_id("trial")` → `trial_<12hex>` |
| `candidate_id` | `str` FK RESTRICT | |
| `job_version_id` | `str` FK RESTRICT | |
| `recommendation_id` | `str` FK RESTRICT | **UNIQUE** |
| `status` | `TrialStatus` | 默认 `PROPOSED` |
| `created_at` | `datetime` | `default_factory=now_utc` |
| `updated_at` | `datetime` | `default_factory=now_utc` |

**刻意不建**：`approval_id`（无 Approval 行）、`trial_plan_ref` / `started_at` / `ended_at`（W4 才填）、`created_by`（审计行已记 actor，`decided_by` 在 Recommendation 上）。
Trial 在 W3-D 的语义就是**一张交接凭证**："这条人类批准已进入试用流程"。实质内容由 W4 填充 —— 与 V4 §11 的分期原则一致。

### Q5 — actor：**owner-only，keyword-only，无默认**

```python
def create_trial_from_approval(
    session: Session,
    recommendation_id: str,
    *,
    actor: ActorContext,
) -> Trial: ...
```

- 与 `decide_recommendation`（F-7）/ `purge_recommendation`（F-8）同构：`actor` keyword-only、**无默认**（P2-1 先例）；非 owner → 403（F-17）。
- **为什么不用 `recommend_candidate` 的 `recommender: str` 模式**：`recommend_candidate` 是 AI 提议（系统执行，尚无人类决策）；Trial 是**人类批准之后的最后一道交接**，且不可逆地把 candidate 推向 `TRIALING`。用最严档。
- 若 W4 的自动化流水线需要非 owner 创建，属**放宽权限**，须另起 PR 论证（收窄安全、放宽须论证）。

### Q6 — 审计：**单条 `trial.created`，同一 SAVEPOINT**

- 与 `recommend_candidate` 先例严格一致：状态写入 + 审计 + candidate 转换在**一个 `session.begin_nested()`** 内（INV-5），不存在"有 Trial 无审计"的窗口。
- `before` = 该 Recommendation 的决策快照；`after` = Trial 全列快照 + candidate 新状态。
- 不额外写 `candidate.transition` 审计行 —— W3-C 的 `recommend_candidate` / `_sync_candidate_back` 都不写，保持一致（candidate 变更体现在推荐/试用审计的 `after` 里）。

### Q7 — 索引：**0 个显式索引**

- `UNIQUE(recommendation_id)` 自带隐式索引，已覆盖按批准查 Trial 的唯一查询模式。
- `status` 在 V1 只有一个取值，**选择性为零**，建索引无收益。
- W4 出现新查询模式时**另起 additive revision** 补建（继承 R7 2026-09-02 对 §4.1 vs §12.1 冲突的裁决思路：索引数取最小值，契约逐字可审计）。

---

## 4. 数据模型

```python
# src/aios/models.py —— W3-D APPEND-ONLY 段（不触碰其上任何定义）

class TrialStatus(StrEnum):
    """Lifecycle state of one Trial (W3-D).

    W3-D writes exactly one member: ``PROPOSED``. There is deliberately no
    reserved vocabulary for W4's states (ACTIVE / COMPLETED / CANCELLED /
    FAILED): the column is a plain ``sa.String()``, so W4 can add members with
    zero migration, and dead enum members would only obscure the fact that
    this stage has a single state.

    Consequently V1 defines NO ``TRIAL_ALLOWED`` edge table and no
    ``_transition_trial_status``: with zero transitions they would be dead code.
    The invariant is structural instead -- ``trial.status`` has exactly one
    writer, the constructor default in ``create_trial_from_approval``. W4 MUST
    introduce ``TRIAL_ALLOWED`` + a single status writer together with its
    first transition.
    """

    PROPOSED = "proposed"


class Trial(SQLModel, table=True):
    """The hand-off record from an APPROVED Recommendation into a trial (W3-D).

    Deliberately thin: in W3-D a Trial is the *evidence that a human-approved
    hire entered the trial stage*, not yet the substance of that trial. The
    plan, dates and outcome belong to W4 (see Spec §2.2).

    All THREE parent FKs are RESTRICT, mirroring W3-C's DR-1: a trial is live
    hiring evidence and must survive a Job / Candidate / Recommendation delete.
    RESTRICT is itself the fail-closed direction -- refusing the delete is
    safe; an unlock is only a convenience. W3-D ships no unlock path (Spec
    §3-Q2): in V1 it could not unblock anything anyway, because the upstream
    APPROVED recommendation is already un-purgeable. W4 owns the unlock,
    together with the Trial cancellation semantics it depends on.
    """

    __tablename__ = "trial"

    __table_args__ = (
        UniqueConstraint(
            "recommendation_id",
            name="uq_trial_recommendation",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("trial"), primary_key=True)
    candidate_id: str = Field(foreign_key="candidate.id", ondelete="RESTRICT")
    job_version_id: str = Field(
        foreign_key="job_version.id", ondelete="RESTRICT"
    )
    recommendation_id: str = Field(
        foreign_key="recommendation.id", ondelete="RESTRICT"
    )
    status: TrialStatus = Field(default=TrialStatus.PROPOSED)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
```

### 4.1 不变量

| # | 不变量 |
|---|---|
| INV-T1 | 一个 `recommendation_id` 最多一行 `trial`（`uq_trial_recommendation`） |
| INV-T2 | `trial.status` 在 W3-D 恒为 `PROPOSED`；唯一写入者是 `create_trial_from_approval` 的构造默认值 |
| INV-T3 | 每行 `trial` 的上游 `recommendation` 在被创建那一刻满足 `status == APPROVED and decided_by` 非空 |
| INV-T4 | 三个父 FK 全 RESTRICT；任何会孤立/销毁 Trial 的删除被**拒绝**（fail-closed）。本轮**无**解锁路径，解锁归 W4（§12 C-2） |
| INV-T5 | 状态写入 + 审计 + candidate 转换在同一 SAVEPOINT（不存在"有 Trial 无审计"窗口） |

---

## 5. 状态机

### 5.1 `CandidateLifecycle` 受控加边（**唯一的 `workforce.py` 触碰点**）

```python
# workforce.py:479 —— 仅扩展，不删改任何既有边
CandidateStatus.EVALUATED: {REJECTED, RECOMMENDED},          # 不变
CandidateStatus.RECOMMENDED: {EVALUATED, TRIALING},          # ← 新增 TRIALING
CandidateStatus.TRIALING: set(),                             # ← 新增节点，出边归 W4
```

`CandidateStatus` 新增：

```python
# models.py:1542 段内（sa.String() 列，零 migration）
TRIALING = "trialing"
```

> **为什么 `TRIALING` 出边为空集**：早期草稿已明裁「`TRIALING` 的后续边（`TRIALING→EMPLOYED` 等）归 W4」。
> W3-D 只加**入边**，出边留给 W4 定义（与 W3-A 把 `RECOMMENDED` 映射为空边集、待 W3-C 接线的模式同构）。
> ⚠️ 后果见 §12 C-5：candidate 进入 `TRIALING` 后，F-R8 撤回**不会**把它拉回（F-6 静默 no-op）。

### 5.2 `trial` 状态机

**V1 无转移。** `PROPOSED` 是唯一节点，无出边、无入边（只有"创建"）。

### 5.3 全链路状态对照

| 阶段 | Recommendation.status | Candidate.status | Trial |
|---|---|---|---|
| 提议 | `PROPOSED` | `RECOMMENDED` | — |
| 人类批准 | `APPROVED`（`decided_by` 非空） | `RECOMMENDED` | — |
| **创建试用**（W3-D） | `APPROVED` | **`TRIALING`** | **`PROPOSED`** |
| 漂移撤回（惰性，任一点被查询时） | `WITHDRAWN` | **`TRIALING`（不回退，C-5）** | `PROPOSED`（不变） |
| 激活 / 完成 | — | — | W4 |

---

## 6. Gate / fail-closed 规则（F-T1 … F-T8）

| # | 规则 | 违约行为 |
|---|---|---|
| **F-T1** | 前置必须是 `assert_trial_eligible(session, recommendation_id) is True` | 返回 `False` → **409** `recommendation is not eligible for trial` |
| **F-T2** | `assert_trial_eligible` 内部已做惰性 F-R8 撤回；**W3-D 不得自行判断资格、不得绕过** | 直接调该函数（唯一入口） |
| **F-T3** | W3-D 只做"创建"；**激活前复检 `assert_trial_eligible` 是 W4 的义务** —— 推论 A 保证失效证据不会因"创建"这一动作产生新后果 | 写入 Spec §12 C-3 交棒条款 |
| **F-T4** | `actor` 必须 owner（F-17）；keyword-only 无默认 | 非 owner → 403；漏传 → `TypeError`（不静默提权） |
| **F-T5** | Recommendation 不存在 → 404（与 `decide_recommendation` 文案同构） | `recommendation not found: <id>` |
| **F-T6** | 三父 FK 全 RESTRICT；任何会孤立/销毁 Trial 的删除被**拒绝**，且**不得**为 `trial` 引入任何 CASCADE | DB 层拒绝；本轮无解锁路径 |
| **F-T7** | 本轮**不**提供 Trial 删除/purge 路径（§3-Q2） | 任何 `session.delete(trial)` 都是越界；解锁归 W4 |
| **F-T8** | 不得修改 `workforce_recommendation.py` 的任何定义 | 本轮纯新增调用 |

### 6.1 `create_trial_from_approval` 参考实现骨架

```python
def create_trial_from_approval(
    session: Session,
    recommendation_id: str,
    *,
    actor: ActorContext,
) -> Trial:
    """Hand an APPROVED recommendation into a Trial (F-T1 / F-T4 / INV-T5).

    ``assert_trial_eligible`` is the single gate: it is False for a missing,
    PROPOSED, REJECTED or WITHDRAWN row, and it runs the lazy F-R8 reconcile
    first, so drifted evidence is withdrawn (and audited) here rather than
    trusting the caller to remember a separate reconcile step.
    """
    _assert_owner_actor(actor)  # F-T4: keyword-only actor, no default

    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        raise ServiceError(404, f"recommendation not found: {recommendation_id}")

    if not assert_trial_eligible(session, recommendation_id):  # F-T1 / F-T2
        raise ServiceError(409, "recommendation is not eligible for trial")

    # Idempotent replay (§7): the UNIQUE slot is taken -> return the winner.
    existing = session.exec(
        select(Trial).where(Trial.recommendation_id == recommendation_id)
    ).first()
    if existing is not None:
        return existing

    before = {
        "status": rec.status.value,
        "decided_by": rec.decided_by,
        "decided_at": _iso(rec.decided_at),
        "decision_rationale": rec.decision_rationale,
        "match_attempt": rec.match_attempt,
    }
    cand = session.get(Candidate, rec.candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {rec.candidate_id}")

    created_at = now_utc()
    trial = Trial(  # INV-T2: status comes from the constructor default
        candidate_id=rec.candidate_id,
        job_version_id=rec.job_version_id,
        recommendation_id=rec.id,
        created_at=created_at,
        updated_at=created_at,
    )

    try:
        with session.begin_nested():  # INV-T5
            session.add(trial)
            session.flush()
            CandidateLifecycle.require_transition(
                cand.status, CandidateStatus.TRIALING
            )
            cand.status = CandidateStatus.TRIALING
            session.add(cand)
            session.flush()
            append_audit(
                session,
                actor=actor.owner_id,
                action="trial.created",
                resource_type="trial",
                resource_id=trial.id,
                project_id=None,
                task_id=None,
                before=before,
                after={
                    "candidate_id": trial.candidate_id,
                    "job_version_id": trial.job_version_id,
                    "recommendation_id": trial.recommendation_id,
                    "status": trial.status.value,
                    "candidate_status": CandidateStatus.TRIALING.value,
                    "match_attempt": rec.match_attempt,
                },
                idempotency_key=f"trial:{rec.id}",
            )
            session.flush()
    except IntegrityError:
        # Concurrent first-create: another writer won the UNIQUE slot.
        session.expire_all()
        winner = session.exec(
            select(Trial).where(
                Trial.recommendation_id == recommendation_id
            )
        ).first()
        if winner is not None:
            return winner
        raise

    return trial
```

### 6.2 幂等键与唯一性（F-14）

`audit_log.idempotency_key` 是 **unique 且 NOT NULL**（`audit.py:71`）。

`trial.created` 的键 `trial:{recommendation_id}` 与 `UNIQUE(recommendation_id)` 对齐：
同一批准的第二条 `trial.created` 审计行会在 DB 层被拒，与"重放返回既有行（不写审计）"共同构成**双保险**。

本轮**不产生** `trial.deleted` 审计（无删除路径，F-T7）。

---

## 7. 幂等规则

| 场景 | 行为 |
|---|---|
| 同一 APPROVED rec 重复调用 `create_trial_from_approval` | **重放**：命中 `UNIQUE(recommendation_id)`（或并发 `IntegrityError`）→ 返回既有 Trial；**不改状态、不写审计、不动 candidate** |
| 并发首次创建 | `IntegrityError` → `expire_all()` 后回读胜者返回（镜像 `recommend_candidate` §8） |
| rec 被撤回后重建（`_rebuild_recommendation` 原地 UPDATE，id 不变）再批准 | 既有 Trial 行仍在 → 重放返回（**不产生第二个 Trial**） |
| 删除后重建 | **V1 不存在该场景**：无删除路径（F-T7）；解锁归 W4，届时须同步定义重放语义 |

> ⚠️ 注意 `_rebuild_recommendation` 是**原地 UPDATE**（F-11），`recommendation.id` 不变，
> 因此"撤回 → 重建 → 再批准"之后 `UNIQUE(recommendation_id)` 依然命中**同一个** Trial 行。这是期望行为：
> 一次批准的后果不应因证据刷新而分裂成两个试用记录。

---

## 8. Audit / Evidence

| action | resource_type | 时机 | before | after | idempotency_key |
|---|---|---|---|---|---|
| `trial.created` | `trial` | 创建成功（SAVEPOINT 内） | Recommendation 决策快照（status / decided_by / decided_at / decision_rationale / match_attempt） | Trial 全列 + `candidate_status` + `match_attempt` | `trial:{recommendation_id}` |

**V1 只有一条 Trial 审计动作。** 无 `trial.deleted`（无删除路径，F-T7），无状态转移审计（无转移，§5.2）。

- `project_id` / `task_id` 恒 `None`（Workforce 当前无 Project 关联，与 W3-C 一致）。
- 脱敏由 `append_audit` 内部 `redact_secrets` 负责；⚠️ 按 F-16，`decision_rationale` 属自由文本，**值模式匹配对 `sk-live-xxx` 型 token 漏网** —— 与本仓既有审计行为一致，不在本轮扩大脱敏范围（若需加强属独立议题）。

---

## 9. 与 W3-C 的接口契约

W3-D 对 `workforce_recommendation.py` 的**全部**依赖（只读，不改一行）：

| 依赖 | 用途 |
|---|---|
| `assert_trial_eligible(session, recommendation_id)` | F-T1/F-T2 唯一前置闸 |
| `CandidateLifecycle.require_transition` | candidate 加边的边界检查 |
| `ActorContext` / `_assert_owner_actor` | F-T4 人类闸 |
| `append_audit` | §8 审计 |
| `Recommendation` / `Candidate` / `CandidateStatus` 模型 | 读写 |
| `_iso`（私有，跨模块复用） | 审计快照的 datetime 渲染 |

> `_iso` 是 `workforce_recommendation.py:444` 的**私有**函数。W3-D 复用它（同模块族内，W3-C 自己也这么用），或在本模块内重定义一个 2 行副本以避免跨模块私有依赖。**建议复用**，与 W3-C 保持单一实现。

---

## 10. Migration

| 项 | 值 |
|---|---|
| 文件 | `alembic/versions/2026XXXX_0001_workforce_trial.py` |
| `revision` | `2026XXXX_0001_workforce_trial`（**实现首日钉死**） |
| `down_revision` | `20260902_0001_workforce_recommendation`（保持**单 head**） |
| `upgrade` | `op.create_table("trial", …)`；**0 个显式索引** |
| `downgrade` | `op.drop_table("trial")` |
| 性质 | **additive**（只建新表）、**可逆**、**单 head** ✅ |

`upgrade()` 列序与类型（严格对齐 §4，避免模型 ↔ 迁移漂移）：

```python
op.create_table(
    "trial",
    sa.Column("id", sa.String(), nullable=False),
    sa.Column("candidate_id", sa.String(), nullable=False),
    sa.Column("job_version_id", sa.String(), nullable=False),
    sa.Column("recommendation_id", sa.String(), nullable=False),
    sa.Column("status", sa.String(), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(
        ["candidate_id"], ["candidate.id"],
        ondelete="RESTRICT", name="fk_trial_candidate_id",
    ),
    sa.ForeignKeyConstraint(
        ["job_version_id"], ["job_version.id"],
        ondelete="RESTRICT", name="fk_trial_job_version_id",
    ),
    sa.ForeignKeyConstraint(
        ["recommendation_id"], ["recommendation.id"],
        ondelete="RESTRICT", name="fk_trial_recommendation_id",
    ),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint(
        "recommendation_id", name="uq_trial_recommendation"
    ),
)
```

### 10.1 各对象是否需要 migration

| 对象 | 需要？ | 理由 |
|---|---|---|
| `trial` 表 | ✅ | 新实体 |
| `TrialStatus` | ❌ | 新枚举仅 Python 层，落库为字符串列 |
| `CandidateStatus.TRIALING` | ❌ | F-12：`candidate.status` 是 `sa.String()` |
| `CandidateLifecycle` 加边 | ❌ | 纯 Python 字典 |
| `recommendation` / `candidate` / `job_version` / `match` / `approval` | ❌ | 全部冻结，本轮零 ALTER |

---

## 11. 契约测试清单（仅清单，本轮不写测试代码）

新增 `tests/test_workforce_trial_w3d.py`（镜像 `test_workforce_recommendation_w3c.py` 的组织方式）。

### T-TRIAL-GATE — fail-closed（7）

1. `PROPOSED`（未决策）→ `create_trial_from_approval` 抛 **409**
2. `REJECTED` → **409**
3. `WITHDRAWN` → **409**
4. `APPROVED` 但 `decided_by` 为空（手工破坏不变量）→ **409**
5. `recommendation_id` 不存在 → **404**
6. 非 owner actor（agent / system）→ **403**；漏传 `actor` → **`TypeError`**
7. 漂移证据（match 缺失 / attempt 变更）→ `assert_trial_eligible` 触发惰性撤回 → 创建 **409**，且 rec 落 `WITHDRAWN` + 有 `recommendation.withdrawn` 审计

### T-TRIAL-STATE — 受控状态转换（4）

8. 成功路径：candidate `RECOMMENDED → TRIALING`
9. `POOLED → TRIALING` 直连 → **409**（捷径仍非法）
10. `EVALUATED → TRIALING` 直连 → **409**
11. `TRIALING → *`（任意出边）→ **409**（V1 出边为空集）

### T-TRIAL-IDEM — 幂等与并发（4）

12. 同一 APPROVED rec 连续调用两次 → 第二次返回**同一行**，Trial 表仍 1 行，审计仍 1 条
13. 并发首次创建 → 不产生 2 行、不产生 500
14. 撤回 → 重建（原地 UPDATE）→ 再批准 → 创建：仍只有 1 个 Trial
15. `audit_log.idempotency_key` 唯一性未被破坏

### T-TRIAL-FK — RESTRICT 生效（3）

16. 有 Trial 时删 `recommendation` → **被拒**
17. 有 Trial 时删 `candidate` / `job_version` → **被拒**（两个父各一例）
18. 迁移文件里 `trial` 的三个 `ForeignKeyConstraint` 的 `ondelete` **均为 `RESTRICT`**（静态断言，防 CASCADE 回流）

### T-TRIAL-AUDIT — 审计与证据（3）

19. `trial.created` 审计行的 `before` 含 `decided_by`（决策者身份进入证据链）
20. 状态写入与审计**同一 SAVEPOINT**：构造审计失败时回滚，不留下"有 Trial 无审计"
21. 重放（幂等）**不产生第二条** `trial.created` 审计行

### T-TRIAL-BOUNDARY — 不越界（5）

22. 全仓 grep：无 `Employee` 表/列新增
23. 无 `check_budget` / `create_task` / `execute_task` 调用
24. `workforce_recommendation.py` 的**定义**零改动（仅新增对其的调用）
25. `workforce.py` 的唯一改动是 `CandidateLifecycle.ALLOWED` 加边 + docstring
26. 不存在任何 Trial 删除路径（无 `purge_trial` / `session.delete(trial)`）—— F-T7

### T-TRIAL-MIG — 迁移（3）

27. alembic **单 head**，且 head == 新 revision
28. `upgrade` 后 `trial` 表存在、列/类型/约束与模型一致（显式索引数 == 0）
29. `downgrade` 后回到 `20260902_0001_workforce_recommendation`，`trial` 表消失、无残留

### T-TRIAL-CPL — 与既有套件共存（1）

30. W3/W3-A/W3-B/W3-C 既有测试全绿；`CandidateLifecycle` 加边未破坏任何非法边断言

---

## 12. 待 R7 确认的条件（CONDITIONS）

| # | 条件 | 说明 / 影响 |
|---|---|---|
| **C-1** | 确认幂等锚从 `approval_id` 改为 **`UNIQUE(recommendation_id)`** | 早期草稿的 `UNIQUE(approval_id)` 在 Q1=B 落地后是**假约束**（恒 NULL）。不改则幂等形同虚设 |
| **C-2** | ⚠️ 确认 `trial` **三父 FK 全 RESTRICT**，且**本轮不交付 `purge_trial`**（完整论证见 §3-Q2） | 这是本轮最需要 R7 拍板的一条：它偏离了 W3-C 的 D-R1「RESTRICT 必须配显式 purge」先例。理由是 V1 内 purge 无法真正解锁（推论 C），且会制造 orphan-`TRIALING` 不一致。若 R7 认为必须交付 purge，则须同时裁决 `TRIALING` 的出边与释放目标（即 C-5 的内容） |
| **C-3** | 确认 `TrialStatus` **只建 `PROPOSED` 单成员**，不预留 W4 词汇 | 死词汇 vs 结构清晰。W4 加成员零 migration，风险低 |
| **C-4** | 确认 **V1 不建 `TRIAL_ALLOWED` 边表**，把"引入第一个转移时同步引入边表"作为 W4 的交棒义务 | 否则 V1 多一段死代码 |
| **C-5** | ⚠️ **已知行为缺口**：candidate 进入 `TRIALING` 后，F-R8 撤回**不会**把它拉回 `EVALUATED`（`_sync_candidate_back` 对非 RECOMMENDED 静默 no-op）。确认**不在 W3-D 修**，移交 W4 | 危险后果已被 F-T3 阻断（Trial 永不可激活），但池中会留下"TRIALING 但批准已失效"的 candidate。修它需要改 W3-C 冻结代码 + 定义取消语义 |
| **C-6** | 确认 `create_trial_from_approval` **owner-only**（最严档），而非 `recommend_candidate` 的 `recommender: str` 系统执行档 | 若 W4 要自动化流水线，须另起 PR 论证放宽 |
| **C-7** | 确认 **0 个显式索引** | 继承 R7 2026-09-02 的最小索引裁决思路 |
| **C-8** | 确认 W3-D **不修改 `workforce_recommendation.py` 任何定义**（含复用其私有 `_iso`） | 若要避免跨模块私有依赖，改成本模块内 2 行副本 |

---

## 13. 结论

W3-D（Trial）在 V1 交付的是**一张交接凭证**：把一条**经人类 L4 批准**的 Recommendation 落成 `trial` 行，并把 candidate 推进 `TRIALING`。它**不**做试用的实质（计划、起止、结果），**不**碰 Employee / Budget / Scheduler / Execution，也**不改** W3-C 的任何定义。

设计上全部沿用 W3-C 已验证的模式：单一状态 SoT、fail-closed 闸门、owner-only 人类闸、SAVEPOINT 内状态+审计同写、additive 单 head 迁移、零 migration 的枚举扩展。

**刻意偏离的一条**：三父 FK 用 RESTRICT 但**不**配 `purge_trial`（C-2）。这不是疏漏 —— §3-Q2 论证了在 V1 交付 purge 要么不可达、要么制造 orphan-`TRIALING`，两者都劣于一条记录在案的 W4 义务。RESTRICT 本身即 fail-closed 方向，解锁只是便利性。

**待 R7 对 §12 的 C-1…C-8 逐条确认后，进入实现 + DSH 路径③独立审计。**
