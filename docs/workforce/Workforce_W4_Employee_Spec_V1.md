# Workforce W4 — Employee 任命（试用运行与转正）Spec V1

> **状态**：草稿 — 待 R7 拍板 §12 的 D-1 ~ D-6 后方可实现。
> **交接基线**：`origin/main` = `95330c0`（W3-D 归档后），alembic 单 head = `20260903_0001_workforce_trial`。
> **上游契约**：
> - `Workforce_W3C_Recommendation_Approval_Trial_Spec_V1.md` §8（W4 接口边界）
> - `Workforce_W3D_Trial_Spec_V1.md` §2.2（明确不做清单）+ §12（C-1 ~ C-8 待确认条件）
>
> **本 Spec 的编号约定**：契约 `F-E*`（Employee / trial Execution），测试 `T-EMP*`。

---

## 1. 背景与交接

W3 把"发现一个 Agent 能干这活"推进到"人类批准进入试用"：

```
Discovery(W2) → Evaluation(W3-A) → Match/Benchmark(W3-B)
              → Recommendation & Approval(W3-C) → Trial(W3-D) → Employee(W4)
```

**交接标记（唯一握手态）**：`Candidate.status = TRIALING`。
**W4 入口**：`SELECT * FROM trial WHERE status = 'proposed'`。

W3-D 交付的 Trial 在语义上只是**一张交接凭证**——"这条人类批准已进入试用流程"（W3-D §3-Q4）。W4 负责填充其实质内容：跑完试用、判定结果、成功则正式雇佣。

| W3-C §8 划给 W4 的职责 | 本 Spec 对应章节 |
|---|---|
| Trial `PROPOSED → ACTIVE` | §6.1 `activate_trial` |
| 运行试用 | §5.5 状态机（运行期 = `ACTIVE`，本轮不交付执行引擎） |
| 成功则 `Candidate.TRIALING → EMPLOYED` 并建 `Employee` | §6.4 `promote_to_employee` + §5.3 |
| 失败则回退 / 标记 | §6.2 `complete_trial` / §6.3 `cancel_trial` / §6.5 `release_candidate` |
| W3-C 禁做：不建 `Employee`、不写 ACTIVE+ 流转、不碰 Agent 调度 | §3.2 持续遵守 |

---

## 2. 代码考古：W4 起点事实清单

以下每一条都可在 `95330c0` 上直接核验，是本 Spec 全部设计的前提。

| # | 事实 | 证据位置 | 对 W4 的含义 |
|---|---|---|---|
| 1 | **`Employee` 零代码** | `grep -n "Employee" src/aios/*.py` 仅命中注释 | 全新表，需 additive 迁移 |
| 2 | `trial` 表 7 列，三父 FK **全 RESTRICT** | `models.py` `class Trial`；`test_migration_foreign_keys_are_restrict` | 沿用 DR-1 模式；unlock 语义见 D-4 |
| 3 | `TrialStatus` **仅 `PROPOSED`** | `models.py` `class TrialStatus` | W4 加 4 成员 → **零迁移**（列是 `sa.String()`） |
| 4 | `CandidateLifecycle.ALLOWED[TRIALING] = set()` | `workforce.py` ~L480 | W4 是唯一解冻点；出边集合待 D-2 裁决 |
| 5 | **`CandidateStatus` / `TrialStatus` 列均为 plain `sa.String()`** | 迁移 `20260827_0002_workforce_candidate`、`20260903_0001_workforce_trial` | 加枚举成员零 schema 变更；**加列才需迁移** |
| 6 | ⚠️ **Workforce 链完全不接 `Project` 表** | `Job.required_work_id → required_work → business_goal`（`owner` 锚点在 BusinessGoal）；链上无 `project_id` | `delegation.check_budget(session, project, ...)` **无法直接复用** → 见 D-1 |
| 7 | `workforce.py` 评估袋注释：`cost_evidence → "W5 Budget domain"` | `workforce.py` `_build_evaluation_context` ~L870 | Budget 在既有设计里被标为 **W5** → 与 W3-D §2.2「Budget 归 W4」冲突，见 D-1 |
| 8 | `historical_evidence` 注释 `"Employee / Performance data is W4+"` | 同上 | W4 只还"`Employee` 是否存在"这笔债；`Performance` 仍 W5+ |
| 9 | `JobStatus.FILLED` 已存在（注释 "a candidate has been appointed to this job (W2+)"）但**全仓从未写过** | `models.py` `JobStatus`；`grep FILLED` | 转正是否置 `FILLED` 待裁决，见 D-3 子项 |
| 10 | `evaluation_context` 是 JSON 袋，`trial` 无对应 JSON 袋 | `Candidate.evaluation_context` | Trial 扩展列只能走具名列或新 JSON 列，见 D-5 |
| 11 | 既有 deferred 清单 = `employee` / `training` / `performance` / `candidate_evaluation` | `test_workforce_recommendation_w3c.py::test_no_deferred_tables_were_created` | W4 只把 `employee` 移出（沿用 W3-D 对 `trial` 的同样操作） |
| 12 | **22 处** alembic 单 head 断言，分布 **14 个测试文件** | `grep -rn "20260903_0001_workforce_trial" tests/` | W4 加迁移后须全部前移（W3-D 前移了 13 处） |
| 13 | `_assert_owner_actor(actor)` 是 W3-C/D 的既有闸；`actor` keyword-only 无默认 | `aios/actor.py`；`workforce_trial.py` L69 | W4 一律沿用最严档，见 Q7 |
| 14 | `append_audit(..., idempotency_key=...)` + `session.begin_nested()` SAVEPOINT 同原子 | `workforce_trial.py` L109-137 | W4 每个写操作沿用同构模式 |

---

## 3. 范围

### 3.1 本轮闭环（W4）

1. `employee` 表（additive，1 张）
2. `EmployeeStatus` 枚举
3. Trial 表扩展列（§5.2）+ 1 个 additive 迁移
4. `TrialStatus` 扩展 4 成员 + `TRIAL_ALLOWED` 边表 + 单一状态写入器（**W3-D §12 C-4 明确交棒给 W4**）
5. `CandidateStatus.EMPLOYED` + 解冻 `CandidateLifecycle.ALLOWED[TRIALING]`
6. 服务函数：`activate_trial` / `complete_trial` / `cancel_trial` / `promote_to_employee` / `release_candidate`
7. additive alembic 迁移 1 个（单 head）
8. 契约测试（§11）
9. 22 处 head 断言前移 + deferred 清单更新（事实 11 / 12）

### 3.2 明确不做（W5+）

| 不做 | 归谁 | 理由 |
|---|---|---|
| Budget（`check_budget`）闸门 | **W5**（建议，见 D-1） | 事实 6/7：链上无 `Project`，且 `cost_evidence` 已标 W5 |
| Scheduler / Execution 引擎（真跑试用任务） | W5+ | W3-C §8 明裁；W4 只提供 `ACTIVE` 状态位与时间戳，不解释"怎么跑" |
| `training` / `performance` / `candidate_evaluation` 表 | W5+ | 事实 11：继续留在 deferred 清单 |
| `Employee` 离职 / 调岗 / 复雇（`OFFBOARDED` 转移） | W5+ | `EmployeeStatus` 单成员 → 零出边，见 Q5 |
| `purge_employee()` | W5+ | 同 W3-D §3-Q2 论证：W4 内不可达（Trial 仍 RESTRICT 锁着），交付即死代码 |
| 写 `Job.status = FILLED` | 待 D-3 | 语义待定（一岗多席位？），本 Spec 建议不做 |
| 修改 `workforce_recommendation.py` / `workforce_trial.py` 的定义 | — | W3-C/D 已冻结；W4 **纯新增**新模块 + 对 `workforce.py` 的唯一受控触碰 |

---

## 4. 设计裁决（Q1–Q9）

### Q1 — Employee 身份锚：`(candidate_id, trial_id)` 双父，非 `agent_id` 单锚

Employee 是**一次雇佣关系**，不是 Agent 的属性。一个 Agent 可以在不同 Job 上被雇佣多次（`Candidate` 本身就是 `Agent × Job × JobVersion` 的叉积，见 `Candidate` docstring）。

因此：

- **主锚** = `candidate_id`（雇佣来自哪个候选）→ FK RESTRICT（镜像 DR-1）
- **依据锚** = `trial_id`（雇佣依据哪次试用）→ FK RESTRICT + **UNIQUE**（幂等锚，见 Q8）
- `agent_id` / `job_id` / `job_version_id` 作为**冗余快照列**保留（查询便利），从 `Candidate` 复制而非重新解析

**为什么不只存 `agent_id`**：那会让"同一 Agent 在同一 Job 上被二次雇佣"无法表达，且丢失"这次雇佣是依据哪次试用"的证据链。

### Q2 — Trial 扩展列：4 列最小集

W3-D §2.2 把 `trial_plan_ref` / `started_at` / `ended_at` 明确划给 W4（"W3-D 恒空 = 死列"）。本轮填实它们，并补一个 `outcome`：

| 列 | 类型 | 写入者 | 说明 |
|---|---|---|---|
| `trial_plan_ref` | `str \| None` | `activate_trial` 入参 | **不透明引用**，AIOS 从不解释其内容（同 `Agent.config_ref` 哲学） |
| `started_at` | `datetime \| None` | `activate_trial` | `PROPOSED → ACTIVE` 时刻 |
| `ended_at` | `datetime \| None` | `complete_trial` / `cancel_trial` | 进入终态时刻 |
| `outcome` | `TrialOutcome \| None` | `complete_trial` | `pass` / `fail`；`CANCELLED` 恒为 `None` |

**不建**：`outcome_notes`（审计行的 `after` 已承载理由）、`ended_by`（审计行 `actor` 已有）、`trial_context` JSON 袋（弱类型，与 fail-closed 风格相悖）。

> ⚠️ **加列 = 需要 additive 迁移**（不像加枚举成员零迁移）。这是 D-5 的核心权衡。

### Q3 — `TrialStatus` 扩展 + `TRIAL_ALLOWED` + 单一写入器

W3-D §12 **C-4** 明确：*"W4 引入第一个转移时，必须同步引入 `TRIAL_ALLOWED` + 单一状态写入器"*。本轮兑现：

```python
class TrialStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

```python
TRIAL_ALLOWED: dict[TrialStatus, set[TrialStatus]] = {
    TrialStatus.PROPOSED: {TrialStatus.ACTIVE, TrialStatus.CANCELLED},
    TrialStatus.ACTIVE: {
        TrialStatus.COMPLETED,
        TrialStatus.FAILED,
        TrialStatus.CANCELLED,
    },
    TrialStatus.COMPLETED: set(),   # 终态
    TrialStatus.FAILED: set(),      # 终态
    TrialStatus.CANCELLED: set(),   # 终态
}
```

- `COMPLETED`（合格）与 `FAILED`（不合格）分开，而非一个 `COMPLETED` + `outcome` 布尔 —— 让"结果"出现在状态里，查询 `WHERE status='completed'` 无需二次判断。
- `CANCELLED` 可从 `PROPOSED`（还没开始就取消）与 `ACTIVE`（中途取消）双向进入；它**不携带 `outcome`**（取消 ≠ 评定）。
- **单一写入器**：`_transition_trial_status(trial, new_status)` 私有函数，内部 `TRIAL_ALLOWED` 校验 + 写 `updated_at`。**所有状态变更必须经它**，无第二处直接赋值。

**放置位置**：新模块 `src/aios/workforce_employee.py`（`TrialLifecycle` 与 `CandidateLifecycle` 分处不同模块，但风格完全同构）。

### Q4 — `CandidateStatus.EMPLOYED` 与 `TRIALING` 出边

```python
class CandidateStatus(StrEnum):
    ...
    TRIALING = "trialing"
    EMPLOYED = "employed"   # W4：终态，零出边
```

`CandidateLifecycle.ALLOWED` 变更（**W4 对 `workforce.py` 的唯一触碰点**）：

```python
CandidateStatus.RECOMMENDED: {CandidateStatus.EVALUATED, CandidateStatus.TRIALING},
CandidateStatus.TRIALING: {CandidateStatus.EMPLOYED, CandidateStatus.POOLED},  # 解冻
CandidateStatus.EMPLOYED: set(),
```

- `TRIALING → EMPLOYED`：成功转正，**唯一成功路径**（`promote_to_employee`）。
- `TRIALING → POOLED`：失败/取消后**释放回池**（`release_candidate`）。这条边是 **D-2** 的标的——它决定是否解决 W3-D §12 **C-5** 的 known-gap。
- `EMPLOYED` 零出边：雇佣是终态，离职属于 W5+。

**零迁移**：列是 plain `sa.String()`（事实 5）。

### Q5 — `EmployeeStatus`：**单成员 `ACTIVE`**

镜像 W3-D §3-Q3 的哲学（"让'本轮只有一个状态'成为结构事实而非口头约定"）：

```python
class EmployeeStatus(StrEnum):
    ACTIVE = "active"
```

不预留 `OFFBOARDED` / `ON_LEAVE` 死词汇——W5 加成员时**零迁移**（列同样用 `sa.String()`）。

### Q6 — 外键策略：**全 RESTRICT，`agent_id` 例外用 NO ACTION**

| 列 | 策略 | 理由 |
|---|---|---|
| `employee.candidate_id` | **RESTRICT** | 镜像 W3-D §3-Q2：雇佣记录是在用的证据，不得随上游删除而消失 |
| `employee.trial_id` | **RESTRICT** | 同上；且它是幂等锚 |
| `employee.agent_id` | **NO ACTION**（软引用） | 镜像 `Candidate.agent_id` 的既有约定：只存 id、不拷注册表数据；Agent 退役不抹除雇佣史 |
| `employee.job_id` / `job_version_id` | **RESTRICT**（建议） | ⚠️ `Candidate` 用的是 CASCADE，但雇佣史不是 Job 的附属品——沿用 CASCADE 会在删 Job 时静默抹除雇佣记录。**这是对 `Candidate` 模式的刻意偏离，需要 R7 确认（并入 D-3）** |

### Q7 — actor：**owner-only，keyword-only，无默认**

沿用 W3-D §12 **C-6** 的最严档裁决：所有 5 个服务函数的 `actor` 均为 keyword-only 且无默认值，非 owner 一律 403。

**理由**：
1. 与 `create_trial_from_approval` / `purge_recommendation` 完全一致（一致性本身即价值）；
2. W4 是**雇佣决策**域，比 W3 的推荐/试用更靠近不可逆边界；
3. 若未来要自动化流水线，须另起 PR 论证放宽（W3-D C-6 已立此先例）。

### Q8 — 幂等锚：**`UNIQUE(employee.trial_id)`**

镜像 W3-D §3-Q1 的 `UNIQUE(trial.recommendation_id)` 思路（"不改则幂等形同虚设"）：

- `promote_to_employee` 重复调用 → 返回既有 `Employee` 行，**不写第二行、不写第二条审计、不二次移动 candidate**；
- 并发首次创建 → `IntegrityError` 吸收，返回 winner（同 `create_trial_from_approval` 的 `session.expire_all()` 模式）；
- 审计 `idempotency_key = f"employee:{trial_id}"`。

**注意（W3-D 已踩）**：`select` 必须来自 **`sqlmodel`** 而非 `sqlalchemy`——对 `table=True` 类，Core `select` 返回不可变 `Row`，`.trial_id` 会 `AttributeError`。

### Q9 — 迁移：**1 个 additive 迁移，单 head**

`2026MMDD_000N_workforce_employee`（按落地日期定：`20260903_0002_…` 若当天，否则 `20260904_0001_…`），`down_revision = "20260903_0001_workforce_trial"`。

迁移内容两张表合一：
1. `employee` 新表（含 UNIQUE + FK）
2. `trial` 加 4 列

`downgrade()` 完整可逆（删表 + 删列）。

**为什么不拆两个迁移**：W4 的两部分内容在语义上同属一次"雇佣能力"交付，拆开只会让 head 前移两次、22 处断言改两遍。

---

## 5. 数据模型

### 5.1 枚举

```python
class TrialStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"        # W4
    COMPLETED = "completed"  # W4：合格
    FAILED = "failed"        # W4：不合格
    CANCELLED = "cancelled"  # W4


class TrialOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class EmployeeStatus(StrEnum):
    ACTIVE = "active"
```

### 5.2 `trial` 表扩展（4 列，加在既有 7 列之后）

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `trial_plan_ref` | `str \| None` | `None` | 不透明引用 |
| `started_at` | `datetime \| None` | `None` | `PROPOSED→ACTIVE` 写入 |
| `ended_at` | `datetime \| None` | `None` | 进入终态写入 |
| `outcome` | `TrialOutcome \| None` | `None` | 仅 `COMPLETED`/`FAILED` 有值 |

**0 个显式索引**（沿用 W3-D §12 C-7 的最小索引裁决）。

### 5.3 `employee` 表（新建，10 列）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `str` PK | `new_id("emp")` | `emp_<12hex>` |
| `candidate_id` | `str` | FK `candidate.id` **RESTRICT** | 雇佣来源（主锚） |
| `trial_id` | `str` | FK `trial.id` **RESTRICT**, **UNIQUE** | 雇佣依据 + 幂等锚 |
| `agent_id` | `str` | FK `agent.id` **NO ACTION** | 软引用快照（自 `Candidate` 复制） |
| `job_id` | `str` | FK `job.id` **RESTRICT**（建议） | 任职岗位快照 |
| `job_version_id` | `str` | FK `job_version.id` **RESTRICT**（建议） | 雇佣时生效的岗位版本 |
| `status` | `EmployeeStatus` | 默认 `ACTIVE` | 单成员，零出边 |
| `hired_at` | `datetime` | `default_factory=now_utc` | 雇佣生效时刻 |
| `created_at` | `datetime` | `default_factory=now_utc` | |
| `updated_at` | `datetime` | `default_factory=now_utc` | 由单一写入器更新 |

`__table_args__ = (UniqueConstraint("trial_id", name="uq_employee_trial"),)`

### 5.4 `TrialLifecycle` 边表（新建于 `workforce_employee.py`）

```python
class TrialLifecycle:
    ALLOWED: dict[TrialStatus, set[TrialStatus]] = {
        TrialStatus.PROPOSED: {TrialStatus.ACTIVE, TrialStatus.CANCELLED},
        TrialStatus.ACTIVE: {
            TrialStatus.COMPLETED,
            TrialStatus.FAILED,
            TrialStatus.CANCELLED,
        },
        TrialStatus.COMPLETED: set(),
        TrialStatus.FAILED: set(),
        TrialStatus.CANCELLED: set(),
    }

    @classmethod
    def require_transition(cls, current: TrialStatus, new: TrialStatus) -> None:
        if new not in cls.ALLOWED[current]:
            raise ServiceError(409, f"illegal trial transition: {current} -> {new}")
```

### 5.5 状态机

**Trial**

```
                    ┌──────────────────► CANCELLED（终态，outcome=None）
                    │
  PROPOSED ──activate──► ACTIVE ──complete(pass)──► COMPLETED（终态）
                          │  └──complete(fail)───► FAILED（终态）
                          └────────cancel────────► CANCELLED（终态）
```

**Candidate（W4 解冻部分）**

```
  RECOMMENDED ──[W3-D]──► TRIALING ──promote───► EMPLOYED（终态）
                              │
                              └──release───► POOLED（回池，可重评）
```

**耦合不变式（INV-E1）**：`Candidate.TRIALING → EMPLOYED` **仅**在 `Trial.status == COMPLETED` 时允许；`Trial` 处于 `ACTIVE`/`FAILED`/`CANCELLED` 时 `promote_to_employee` 一律 409。

---

## 6. 服务层契约

所有函数位于 **`src/aios/workforce_employee.py`**（新模块）。统一约定：

- `actor: ActorContext` —— keyword-only，**无默认**；非 owner → 403（F-E1）
- 每个写操作 = 一个 `session.begin_nested()` SAVEPOINT，内含「状态变更 + 审计」（INV-E2）
- 每个写操作经 `_transition_trial_status` 单一写入器（INV-E3）

### 6.1 `activate_trial`

```python
def activate_trial(
    session: Session,
    trial_id: str,
    *,
    plan_ref: str | None = None,
    actor: ActorContext,
) -> Trial:
```

| 契约 | 规则 |
|---|---|
| F-E2 | `trial` 不存在 → 404 |
| F-E3 | `status != PROPOSED` → 409（含幂等重放语义，见 §7） |
| F-E4 | 写入 `trial_plan_ref` + `started_at = now_utc()`；`status → ACTIVE` |
| F-E5 | 审计 `trial.activated`，`idempotency_key = f"trial:{trial_id}:activate"` |
| INV-E4 | **`activate_trial` 不移动 `Candidate`**（candidate 在 W3-D 已是 `TRIALING`；激活是 Trial 内部事） |

### 6.2 `complete_trial`

```python
def complete_trial(
    session: Session,
    trial_id: str,
    *,
    outcome: TrialOutcome,
    actor: ActorContext,
) -> Trial:
```

| 契约 | 规则 |
|---|---|
| F-E6 | `trial` 不存在 → 404 |
| F-E7 | `status != ACTIVE` → 409（`PROPOSED` 直接结案属非法，须先激活或取消） |
| F-E8 | `outcome` 必须是 `TrialOutcome` 成员；非法值 → 422 |
| F-E9 | `outcome == PASS` → `status = COMPLETED`；`outcome == FAIL` → `status = FAILED`；写 `outcome` + `ended_at` |
| F-E10 | 审计 `trial.completed`，`after` 含 `outcome`；`idempotency_key = f"trial:{trial_id}:complete:{outcome.value}"` |
| INV-E5 | **`complete_trial` 不移动 `Candidate`** —— 结案 ≠ 雇佣。转正须 `promote_to_employee`（D-6 人类闸） |

### 6.3 `cancel_trial`

```python
def cancel_trial(
    session: Session,
    trial_id: str,
    *,
    actor: ActorContext,
) -> Trial:
```

| 契约 | 规则 |
|---|---|
| F-E11 | `trial` 不存在 → 404 |
| F-E12 | `status` 已是终态 → 409（`COMPLETED`/`FAILED`/`CANCELLED` 不可再取消） |
| F-E13 | `status → CANCELLED`，写 `ended_at`；**`outcome` 恒 `None`**（取消 ≠ 评定） |
| F-E14 | 审计 `trial.cancelled`，`idempotency_key = f"trial:{trial_id}:cancel"` |

### 6.4 `promote_to_employee`

```python
def promote_to_employee(
    session: Session,
    trial_id: str,
    *,
    actor: ActorContext,
) -> Employee:
```

| 契约 | 规则 |
|---|---|
| F-E15 | `trial` 不存在 → 404 |
| F-E16 | **`trial.status != COMPLETED` → 409**（INV-E1；未结案/失败/取消均不可转正） |
| F-E17 | `candidate.status` 必须为 `TRIALING`，否则 409（防并发漂移） |
| F-E18 | 原子写入：`Employee` 行 + `CandidateLifecycle.require_transition(TRIALING, EMPLOYED)` + `cand.status = EMPLOYED` |
| F-E19 | 从 `Candidate` 复制 `agent_id` / `job_id` / `job_version_id` 快照（**不重新解析**，防 TOCTOU） |
| F-E20 | 审计 `employee.hired`，`before` = trial 快照（`status`/`outcome`/`started_at`/`ended_at`），`after` = Employee 全字段；`idempotency_key = f"employee:{trial_id}"` |
| F-E21 | 幂等：重复调用返回既有行，不二次写（Q8） |

### 6.5 `release_candidate`

```python
def release_candidate(
    session: Session,
    trial_id: str,
    *,
    actor: ActorContext,
) -> Candidate:
```

| 契约 | 规则 |
|---|---|
| F-E22 | `trial` 不存在 → 404 |
| F-E23 | `trial.status` 必须是 `FAILED` 或 `CANCELLED` → 否则 409（`ACTIVE`/`PROPOSED` 不可释放；`COMPLETED` 须走 promote） |
| F-E24 | `candidate.status` 必须为 `TRIALING` → 否则 409 |
| F-E25 | `TRIALING → POOLED`（**D-2 标的**，见 §12） |
| F-E26 | 审计 `candidate.released`，`idempotency_key = f"trial:{trial_id}:release"` |

> `release_candidate` 是 W3-D §12 **C-5** known-gap（F-R8 撤回不把 `TRIALING` 拉回）的**部分**缓解：它给了一条显式释放路径，但不修改 W3-C 冻结代码（见 D-2）。

---

## 7. 幂等与并发

| 场景 | 行为 |
|---|---|
| `activate_trial` 在已 `ACTIVE` 的 trial 上重放 | **409**（与 `create_trial_from_approval` 的"返回 winner"不同：激活带 `started_at`，重放会改写时间戳 → 拒绝更安全）。若 R7 需要幂等返回，见 D-6 备注 |
| `complete_trial` 同 outcome 重放 | **409**（终态不可再转移） |
| `cancel_trial` 已 `CANCELLED` 重放 | **409** |
| `promote_to_employee` 重放 | **返回既有 Employee**（F-E21，靠 `UNIQUE(trial_id)`） |
| `release_candidate` 重放 | **409**（candidate 已非 `TRIALING`） |
| 并发首次 `promote` | `IntegrityError` 吸收 → `session.expire_all()` → 返回 winner（Q8） |

**为什么只有 promote 幂等返回**：它带 UNIQUE 锚且是终态写；其余四个是"带时间戳/带结果"的转移，拒绝重放比静默返回更符合 fail-closed（与 W3-D 的 `create_trial_from_approval` 区分在于：那个是**创建**，本轮这四个是**转移**）。

---

## 8. 失败语义（fail-closed）

| 失败情形 | 响应 | 依据 |
|---|---|---|
| 非 owner actor | 403 | Q7 / F-E1 |
| 缺 `actor` 关键字 | `TypeError`（不是静默按 owner 执行） | Q7 / W3-D C-6 |
| 非法状态转移 | 409 | INV-E3 |
| 未结案即转正 | 409 | INV-E1 / F-E16 |
| 审计写入失败 | 整个 SAVEPOINT 回滚，无孤儿 Employee / 无漂移 Candidate | INV-E2 |
| 上游 `Recommendation` 被撤回（F-R8 漂移） | **不自动回滚 Trial**；由 owner 显式 `cancel_trial` + `release_candidate` | D-2（见 §12） |

**不变式 INV-E6**：任何时刻不存在「`candidate.status == EMPLOYED` 但无对应 `employee` 行」或反之。二者在同一 SAVEPOINT 内原子写入（F-E18）。

---

## 9. 审计契约

| action | resource_type | resource_id | `idempotency_key` |
|---|---|---|---|
| `trial.activated` | `trial` | `<trial_id>` | `trial:<trial_id>:activate` |
| `trial.completed` | `trial` | `<trial_id>` | `trial:<trial_id>:complete:<outcome>` |
| `trial.cancelled` | `trial` | `<trial_id>` | `trial:<trial_id>:cancel` |
| `employee.hired` | `employee` | `<employee_id>` | `employee:<trial_id>` |
| `candidate.released` | `candidate` | `<candidate_id>` | `trial:<trial_id>:release` |

- `project_id` / `task_id` 一律 `None`（Workforce 链不接 Project —— 事实 6）
- 所有 datetime 经 `_iso()` 渲染（沿用 `workforce_trial.py` 的既有 2 行副本约定，不改 W3-C/D 私有函数）

---

## 10. 迁移

| 项 | 值 |
|---|---|
| revision | `2026MMDD_000N_workforce_employee`（落地日定） |
| down_revision | `20260903_0001_workforce_trial` |
| upgrade | 建 `employee` 表（含 `uq_employee_trial` + 5 FK）；`trial` 加 4 列 |
| downgrade | 删 4 列；删 `employee` 表 |
| 显式索引 | **0 个**（沿用 W3-D C-7） |

**连带修改（必须同 PR 完成）**：
1. **22 处 alembic 单 head 断言**前移至新 revision（14 个测试文件，事实 12）
2. `tests/test_workforce_recommendation_w3c.py::test_no_deferred_tables_were_created` —— 把 `"employee"` 移出 deferred 清单（`training` / `performance` / `candidate_evaluation` 保留），并加 `assert "employee" in tables`

---

## 11. 契约测试计划（`tests/test_workforce_employee_w4.py`）

| 组 | 测试（T-EMP） | 覆盖契约 |
|---|---|---|
| **A. 权限** | `test_non_owner_actor_403` / `test_missing_actor_raises_typeerror` | F-E1 |
| **B. 激活** | `test_activate_proposed_ok_writes_started_at` / `test_activate_active_409` / `test_activate_missing_404` / `test_activate_does_not_move_candidate` | F-E2..F-E5, INV-E4 |
| **C. 结案** | `test_complete_pass_sets_completed` / `test_complete_fail_sets_failed` / `test_complete_from_proposed_409` / `test_complete_bad_outcome_422` / `test_complete_does_not_move_candidate` | F-E6..F-E10, INV-E5 |
| **D. 取消** | `test_cancel_from_proposed_ok` / `test_cancel_from_active_ok` / `test_cancel_terminal_409` / `test_cancel_outcome_is_none` | F-E11..F-E14 |
| **E. 转正** | `test_promote_completed_creates_employee_and_employed` / `test_promote_active_409` / `test_promote_failed_409` / `test_promote_cancelled_409` / `test_promote_idempotent_replay` / `test_promote_concurrent_returns_winner` | F-E15..F-E21, INV-E1, INV-E6 |
| **F. 释放** | `test_release_after_failed_to_pooled` / `test_release_after_cancelled_ok` / `test_release_active_409` / `test_release_completed_409` | F-E22..F-E26, D-2 |
| **G. 生命周期** | `test_trialing_outbound_edges` / `test_employed_is_terminal` / `test_illegal_edges_still_rejected` | Q4 |
| **H. 幂等/审计** | `test_promote_no_second_audit` / `test_audit_failure_rolls_back_no_orphan_employee` | INV-E2, INV-E6 |
| **I. 边界** | `test_no_budget_scheduler_execution_calls` / `test_no_training_performance_tables` / `test_w3c_w3d_definitions_unchanged` | §3.2 |
| **J. 迁移** | `test_single_alembic_head` / `test_employee_table_shape_and_fks` / `test_downgrade_removes_employee_and_columns` | §10 |

预计 ~28 个测试（对齐 W3-D 的 31 个量级）。

---

## 12. 待 R7 确认的决策点（D-1 ~ D-6）

> 每条给出 **建议**（WB 推荐）与 **备选**，以及选错的代价。

### D-1 — Budget 闸门：**建议 W4 不做，维持归 W5**

- **背景冲突**：W3-D §2.2 把「Budget（`check_budget`）」划给 W4；但本 Spec 事实 6/7 显示：① Workforce 链（`BusinessGoal→RequiredWork→Job→JobVersion`）**完全没有 `Project` 关联**，而 `delegation.check_budget(session, project, estimated_cost)` 的签名强制要求 `Project`；② `workforce.py` 评估袋的 `cost_evidence` 注释已明确写着 `"W5 Budget domain"`。
- **建议（A）**：**W4 不引入任何 Budget 检查**，把 Budget 正式重新归类为 W5。理由：硬做要么给 Workforce 链凭空加 `project_id`（污染 W1/W2 冻结模型），要么伪造一个 Project 上下文（说谎）。
- **备选（B）**：W4 内为 `activate_trial` 加一个"预算占位闸"（接受 `estimated_cost: float | None = None`，有值则……无处可查 → 只能是 no-op 或恒拒）。**这等于死代码或假闸门，不建议。**
- **代价**：若 R7 选 B 且要求真检查，需先开一个 W4-pre PR 给 `BusinessGoal` 或 `Job` 加预算语义 —— 那是 W5 的活，会让 W4 膨胀。

### D-2 — Trial 失败/取消后的 Candidate 语义：**建议 `TRIALING → POOLED`（释放回池）**

- **建议（A）**：加 `TRIALING → POOLED` 边 + `release_candidate`（§6.5）。理由：① 解决 W3-D §12 **C-5** known-gap 的一半（给一条显式释放路径，且不改 W3-C 冻结代码）；② 让 W3-D §3-Q2 批评的"orphan-`TRIALING`"不再无解；③ 释放后可重新评估（试错是试用的正常结局）。
- **备选（B）**：`TRIALING → REJECTED`（失败即永久拒绝，不回池）。代价：一次试用失败终身不得再评，与"POOLED 可重评"的现状相悖，且堵死了 D-4 的 unlock 路径。
- **备选（C）**：失败后 **candidate 停在 `TRIALING` 不动**（`TRIALING` 出边仅 `EMPLOYED`）。代价：复现 W3-D §3-Q2 明令反对的孤儿状态，且池中永远堆积死候选。
- ⚠️ **连带**：选 A 才有 `release_candidate` 与 F-E22..F-E26；选 C 则删掉 §6.5 整节与测试组 F。

### D-3 — `Employee` 表范围：**建议最小核心（10 列，含双 job 快照）**

- **建议（A）**：§5.3 的列集（PK + 双锚 + 3 快照 + status + 3 时间戳 = 10 列），**不含** `training` / `performance` / `salary` / `manager_id`。
- **备选（B）**：只存 `id` + `candidate_id` + `trial_id` + `status` + `hired_at`（5 列），其余全靠 join。代价：每次查询都要回溯 Candidate，且 Candidate 的 `job_id`/`job_version_id` 是 CASCADE，删 Job 后雇佣记录会丢岗位信息。
- **子项 D-3b — `job_id` / `job_version_id` 的 FK 策略**：`Candidate` 用 **CASCADE**，本 Spec 建议 Employee 用 **RESTRICT**（Q6）。**理由**：雇佣史不是 Job 的附属品，删 Job 不该静默抹除"谁被雇过"。**代价**：删一个已有 Employee 的 Job 会 409，需要 W5 补 `purge_employee` 或直接接受"有雇佣史的岗位不可删"这一 fail-closed 语义。
- **子项 D-3c — 是否写 `Job.status = FILLED`**：**建议不写**。理由：`FILLED` 的语义是"岗位已填满"，但一个 Job 是否允许多个 Employee 未定；W4 写了就等于默认一岗一人，是未经论证的语义承诺。

### D-4 — RESTRICT unlock：**建议 W4 仍不交付 `purge_employee`，登记为 W5 义务**

- **背景**：W3-D §3-Q2 已论证过同一问题的 Trial 版本，结论是"交付一个记录在案的 W4 义务，而非一个半吊子函数"。
- **建议（A）**：沿用同一论证。W4 内 `purge_employee` **不可达**（`employee.trial_id` RESTRICT → 要先删 Trial；删 Trial 要先过 `purge_trial`，而 W3-D 已证明它在 V1 内必然 409）。交付即得死代码。
- **备选（B）**：W4 一次性交付 `purge_trial` + `purge_employee` 完整解锁链。代价：① 需要同时裁决"解锁后 candidate 去哪"（同 D-2）；② 需要修改/扩展 W3-C 的 `purge_recommendation` 语义（W3-C 已冻结）；③ 会让 W4 从"雇佣"膨胀成"雇佣 + 数据生命周期"。
- **记录**：若选 A，本 Spec §3.2 的"不做"清单即成为 W5 的正式义务登记（与 W3-D §12 C-2 同构）。

### D-5 — Trial 扩展列：**建议加 4 列（+1 迁移）**

- **建议（A）**：`trial_plan_ref` / `started_at` / `ended_at` / `outcome`（§5.2）。**代价**：需要 1 个 additive 迁移 + 22 处 head 断言前移。
- **备选（B）**：加**单列** `trial_context: dict` JSON 袋（1 次迁移，未来可扩展）。代价：弱类型，`outcome` 的可查询性（`WHERE status='completed'`）退化为应用层约定，与 fail-closed 风格相悖。
- **备选（C）**：**一列不加**，`outcome` 只体现在 `status`（`COMPLETED`/`FAILED`）+ 审计行的 `after`。**代价**：丢失"试用起止时间"这一基本事实（无法回答"他干了多久"），且 W3-D §2.2 明确说这 3 列"W3-D 恒空 = 死列"——W4 若仍不加，它们就永远不该被加，等于承认 Trial 是一张无时间戳的凭证。
- ⚠️ 选 C 则 W4 **零迁移**（只有 `employee` 新表），但 head 仍要前移到 employee 迁移 —— 22 处断言照样要改。

### D-6 — 转正人类闸：**建议 `promote_to_employee` 必须 owner 显式调用，且与 `complete_trial` 解耦**

- **建议（A）**：两段式 —— `complete_trial`（记录试用结果）与 `promote_to_employee`（雇佣决策）**分开**，`promote` 是 owner-only 的独立人类闸（§6.4，INV-E5）。**理由**：① 与 W3-D §12 C-6「owner-only 最严档」一致；② `EMPLOYED` 零出边 = 不可逆，fail-closed 要求人类签字；③ 允许现实路径"试用合格但因组织变化不录用"（Trial `COMPLETED` 但永不 promote，candidate 可经 D-2 释放）。
- **备选（B）**：`complete_trial(outcome=PASS)` 一步到位自动建 Employee + 置 `EMPLOYED`。代价：① 雇佣决策无人类签字，与 W3 系列"L4 人类批准闸"的整体设计哲学相悖；② 丢掉"合格但不录用"的表达能力；③ 幂等锚从 `UNIQUE(employee.trial_id)` 退化。
- **备注**：若 R7 希望 `activate_trial` 也幂等返回（而非 409），请在此条一并说明——§7 目前按"转移一律拒绝重放"设计。

---

## 13. 结论

W4 是 Workforce 主链路的**最后一环**：它把 W3-D 那张"交接凭证"式的 Trial 填成一次有起止、有结果、有雇佣产出的完整雇佣过程，并首次解冻 `TRIALING` 的出边。

本 Spec 的全部设计均遵循三条既有原则：

1. **fail-closed 优先于便利**（非法转移 409、非 owner 403、审计失败全回滚、RESTRICT 不配半吊子 purge）
2. **不重写，只复用**（`_assert_owner_actor` / `append_audit` / `CandidateLifecycle` / `_iso` 全部沿用；W3-C/D 定义零修改）
3. **不虚构数据**（`trial_plan_ref` 是不透明引用；`outcome` 只能来自显式入参；无任何"自动推断"的雇佣结果）

**交棒 W5**：Budget（D-1）、`training` / `performance` 表、`Employee` 离职与 `purge` 链（D-4）、Execution 引擎。

---

## 附录 A — 决策点速查

| # | 决策点 | 建议 | 备选 | 影响章节 |
|---|---|---|---|---|
| D-1 | Budget 闸门 | **不做，归 W5** | W4 内假闸门 | §3.2, §12 |
| D-2 | 失败/取消后 Candidate | **`TRIALING → POOLED` + `release_candidate`** | `→ REJECTED` / 停住不动 | §4-Q4, §6.5, §11-F |
| D-3 | Employee 表范围 | **最小核心 10 列** | 5 列纯锚点 | §5.3, §11-J |
| D-3b | `job_id` FK 策略 | **RESTRICT** | CASCADE（同 Candidate） | §4-Q6 |
| D-3c | 写 `Job.status = FILLED` | **不写** | 转正即置 FILLED | §2 事实 9, §3.2 |
| D-4 | RESTRICT unlock | **不交付 purge，登记 W5** | 一次交付完整解锁链 | §3.2, §12 |
| D-5 | Trial 扩展列 | **加 4 列（+1 迁移）** | JSON 袋 / 零列 | §5.2, §10, §11-J |
| D-6 | 转正人类闸 | **两段式 owner-only** | 一步自动雇佣 | §6.4, §7, INV-E5 |
