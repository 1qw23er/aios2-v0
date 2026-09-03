# Workforce W3-C — Recommendation / Approval Spec V2

> **阶段性质：纯设计（Design-Only）**
> 本轮**不写实现代码、不写 migration、不写测试代码、不 commit / push / PR**。
> 本文件取代并作废 `docs/Workforce_W3C_Recommendation_Approval_Trial_Spec_V1.md`（V1 错误地把 Trial 本体纳入 W3-C 范围，与本轮硬约束第 8 条冲突）。

| 项 | 值 |
|---|---|
| 基线分支 | `w3b-match-benchmark` |
| W3-B exact-head | `6c08d044d2049f7fb48467e2e89c0b30baecba75` |
| W3-B exact tree | `c837d78a6ce28474adb045b4e0cd1907c3cf70b0` |
| W3-B landed commit（main） | `0a72c45597f0ff817bdf5e6df7bb8f22fb456d1e` |
| 当前 alembic 单 head | `20260901_0001_workforce_match_benchmark` |
| 本轮范围 | **Recommendation + Approval(L4 人类闸)** |
| 本轮明确排除 | Trial 本体、Employee / Training / Performance、ai-arena、ruleset |
| 状态 | 待 R7 审批 |

---

## 1. 代码考古结论（只读事实，全部有行号出处）

### 1.1 W3-A / W3-B 已冻结契约

| 事实 | 出处 | 对 W3-C 的含义 |
|---|---|---|
| `CandidateStatus.RECOMMENDED` 已是真实枚举成员 | `models.py:1570` | **无需新增枚举值**，只需放开状态机边 |
| `Candidate.status` 是 `sa.String()`，非 DB ENUM | `models.py:1560-1562` | 放开通往 RECOMMENDED 的边 **零 schema 改动** |
| `CandidateLifecycle.ALLOWED[RECOMMENDED] = set()` | `workforce.py:479` | RECOMMENDED 当前零入零出边；W3-C 受控加边 |
| `CandidateLifecycle.ALLOWED[EVALUATED] = {REJECTED}` | `workforce.py:476` | W3-C 只加 `RECOMMENDED` 一个目标 |
| `Match` UNIQUE(candidate_id, job_version_id) | `models.py:1769-1775` | Recommendation 幂等键同构 |
| `Match.breakdown` 含 `excluded: [reliability, historical, cost]` | `workforce.py:1535-1539` | W3-C 必须原样继承，不得改写 |
| `Match.status = BLOCKED` + `match_blocked_reason="capability_gap"` | `workforce.py:1502-1504` | W3-C 的**硬拒绝信号** |
| `evaluation_context.recommendation_blocked_reason` | `workforce.py:878-880` | W3-A 已预留的前向契约，W3-C 应二次校验 |
| `evaluation_context.{reliability,historical}_evidence.status = "future_capability"` | `workforce.py:867-874` | 必须保持 unknown，禁止转数值 |
| `evaluation_context.cost_evidence.status = "unknown"` | `workforce.py:863-866` | cost 仅 advisory |
| `_attempt_from_evidence_refs(refs)` 已存在 | `workforce.py:1398` | W3-C **复用**解析 attempt，不重写 |
| `compute_match` 幂等 = SAVEPOINT + IntegrityError 吸收（P2-1 模式） | `workforce.py:1602-1637` | W3-C 复用同一模式 |
| `rank_candidates` 是纯读、无审计 | `workforce.py:1641-1666` | 纯读守卫不写审计（前例） |

### 1.2 既有 Approval / owner_inbox 机制（关键考古）

| 事实 | 出处 | 影响 |
|---|---|---|
| `Approval.project_id` 是 **NOT NULL** FK → `project.id` | `models.py:499` | ⚠️ 无 Project 则无法建 Approval 行 |
| `services.create_approval` 强制 `session.get(Project, ...)` 存在，否则 404 | `services.py:232-233` | ⚠️ 直接复用会 404 |
| `decide_approval` 用 `approval.project_id` 写审计 | `services.py:~360` | 复用需 project_id 有值 |
| `owner_inbox.INBOX_KINDS = {content, cs, feedback, knowledge}` | `owner_inbox.py:140` | ⚠️ 无 workforce inbox |
| `OwnerInboxService.decide` 强制 `_load_live_project(claims["operating_project"])` | `owner_inbox.py:2111` | ⚠️ owner_inbox 是 **project-scoped** 通道 |
| `_dispatch` 按 purpose 前缀分发（content./cs./feedback./knowledge.） | `owner_inbox.py:2119-2134` | 接 workforce 需改 engine |
| Workforce 链（BusinessGoal→RequiredWork→Job→JobVersion→Candidate）**无任何 project_id** | `models.py:1396-1620` | ⚠️ 根因：Workforce 域与 Project 域**不相交** |
| `workforce.py` 全部审计调用传 `project_id=None` | `workforce.py:940/999/1022/1579/1612` | 既有先例，审计支持 `project_id=None` |
| `append_audit(project_id: str \| None, task_id: str \| None)` | `audit.py:117-118` | ✅ 可复用，无需改 |
| `actor.resolve_owner_actor()` / `actor._assert_owner_actor()` | `actor.py:53 / 79` | ✅ 人类身份校验可复用（`services.decide_approval` 同路径） |

---

## 2. 与历史 proposal 的偏差说明 ⚠️

历史 proposal 主要来自 `docs/Workforce_W3_Evaluation_Matching_Spec_V1.md` §6（Recommendation）、§7（Trial）与 §2 复用清单（第 24-30 行）。

### 2.1 P0 偏差（阻塞性，需 R7 决策）

| # | 历史 proposal 原文 | 考古实测 | 偏差性质 |
|---|---|---|---|
| **D-1** | §2 表第 29 行：「`Approval`（`models.py:495`）… W3 Recommendation 复用为**唯一人类闸（不新造审批机制）**」；§6.3：「创建 `Approval`（action_type=`workforce.recommend`），Owner 在 `owner_inbox` 批准/驳回」 | `Approval.project_id` **NOT NULL**，`create_approval` 强制 Project 存在；而 Workforce 链**完全没有 Project** 字段 | **proposal 不可直接执行**。物理复用 `approval` 表必须先改既有核心表（见 §6 方案 A′） |
| **D-2** | §6.3：「Owner 在 `owner_inbox` 批准/驳回」 | `owner_inbox` 是 **project-scoped sealed-token** 通道（`INBOX_KINDS` 只有 4 类，无 workforce；`decide` 强制 live Project） | **proposal 不可直接执行**。接入需新增 inbox kind + purpose + handler = **改 engine**（本轮硬约束禁止） |

### 2.2 P1 偏差（范围变更）

| # | 历史 proposal | 本轮约束 | 处理 |
|---|---|---|---|
| **D-3** | §7 Trial：W3 实现 `create_trial`、绑定真实 Task、`check_budget`、`execute_task` | 本轮第 8 条：**不实现 Trial 本体** | Trial 降级为**接口契约 + 守卫函数**（`assert_trial_eligible`），实体留 W3-D / W4 |
| **D-4** | §6.1：`Recommendation.status: PROPOSED`（单一状态） | 本轮第 7 条：Approval 是唯一人类闸 → 决策结果必须落库 | 扩展为 `PROPOSED / APPROVED / REJECTED / WITHDRAWN`（见 §5.2） |
| **D-5** | §6.1：`Recommendation.target_employee_id`（V1 恒 null，Employee 在 W4） | 本轮第 8 条：不实现 Employee | **不建该列** —— 恒 null 的死列是伪字段，改用 §11 的 W4 握手契约表达 |

### 2.3 P2 偏差（接口微调）

| # | 历史 proposal | 本轮设计 | 理由 |
|---|---|---|---|
| **D-6** | `recommend_candidate(session, candidate_id)` | 保持同签名，`job_version_id` 从 `Candidate.job_version_id` **派生** | `Candidate` 已唯一绑定 job_version；显式传参会引入「candidate 与 job_version 不一致」的误用面 |
| **D-7** | §6.2 未定义 attempt 失效语义 | 新增 `match_attempt` 列 + F-R8 自动失效 | W3-B `compute_match` 会在新 attempt 时 **in-place UPDATE** Match；若 Recommendation 不感知，会出现「批准了已被新证据推翻的旧推荐」 |

### 2.4 偏差根因一句话

> **历史 proposal 假设 Workforce 域与 Project 域相交；实际冻结代码中二者完全不相交。**
> 因此「复用 `approval` 表 + `owner_inbox` UI」这条路径在当前架构下存在结构性缺口，必须在 §6 的 A′/B/C 三方案中选择其一，否则 W3-C 无法落地。

---

## 3. W3-C Spec V1（范围与闭环）

### 3.1 本轮闭环

```
Match/Ranking (W3-B, 冻结)
        │  只读：Match.{score, breakdown, evidence_refs, status, match_blocked_reason}
        ▼
Recommendation (W3-C, 本轮新增)  ── status: PROPOSED
        │
        │  唯一人类闸：Approval(L4)
        │  复用 RiskLevel.L4 + ApprovalStatus + resolve_owner_actor/_assert_owner_actor + append_audit
        ▼
Recommendation ── status: APPROVED / REJECTED
        │
        │  assert_trial_eligible()  ← 守卫，本轮不创建 Trial
        ▼
   [ W3-D / W4 接管：Trial → Employee ]
```

### 3.2 硬约束映射矩阵

| # | 硬约束 | 落地位置 |
|---|---|---|
| 1 | W3-A/W3-B 冻结 | §5.1 只加 1 条边；§10 只读契约 |
| 2 | Recommendation 建立在可解释 Match 之上 | F-R2 / F-R3 |
| 3 | 可解释、可审计 | §4.2 breakdown/evidence_refs 强制；F-R3 |
| 4 | reliability/historical 保持 unknown | F-R4 + `unknown_dimensions` 列 |
| 5 | cost_policy 仅 advisory | F-R5 + `cost_advisory` 文本列 |
| 6 | Recommendation fail-closed | F-R1…F-R8 |
| 7 | Approval(L4) 是唯一人类闸 | §6 方案 B（推荐）；F-R6/F-R7 |
| 8 | 不实现 Trial / Employee | §11；D-3/D-5 |
| 9 | 不新增 Capability 词表、不重造 Scheduler/Execution/Budget | §14 契约 D；本轮**不调用** Budget/Scheduler/Execution |
| 10 | migration 单 head、可逆、additive | §12 |

---

## 4. 数据模型（新增 1 张表，全 additive）

### 4.1 `recommendation` 表

| 列 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `id` | str PK | `new_id("rec")` | |
| `candidate_id` | FK `candidate.id` | CASCADE, index | |
| `job_version_id` | FK `job_version.id` | CASCADE, index | 从 Candidate 派生 |
| `match_id` | FK `match.id` | **RESTRICT**, index | 强绑定；Match 被删 = 拒绝（防悬空推荐） |
| `match_attempt` | int | | 复用 `_attempt_from_evidence_refs(Match.evidence_refs)`；F-R8 失效判据 |
| `status` | `RecommendationStatus` | index | 见 §5.2 |
| `proposed_action` | str | default `"hire"` | V1 仅 `"hire"`；其余值 422 |
| `score` | float | | **Match.score 的原样快照，禁止重算** |
| `weights_version` | str | | 快照 `Match.weights_version` |
| `breakdown` | JSON dict | **NOT NULL 语义** | 原样快照 `Match.breakdown` |
| `evaluated_fields` | JSON list | | 快照 |
| `evidence_refs` | JSON list | | `Match.evidence_refs + ["match:{match_id}"]` |
| `excluded_fields` | JSON list | | 快照 `Match.breakdown["excluded"]`，F-R4 判据 |
| `unknown_dimensions` | JSON dict | | 只读快照 evaluation_context 三维度状态（见 4.3） |
| `cost_advisory` | str \| None | | **纯文本** advisory，禁止数值 |
| `rationale` | str | | 模板生成的可解释文本（非 LLM 自由生成） |
| `approval_status` | `ApprovalStatus` | index, default PENDING | **复用既有枚举**，不新造状态词 |
| `risk_level` | `RiskLevel` | default **L4** | 复用既有枚举 |
| `approval_id` | str \| None | index | **前向列**：W4 引入 Project 后回填真实 `Approval` 行；V1 恒 None |
| `decided_by` | str \| None | | = `actor.owner_id or "owner"`，**仅人类决策可写** |
| `decided_at` | datetime \| None | | |
| `decision_rationale` | str \| None | | |
| `recommender` | str | default `"workforce_recommendation"` | |
| `created_at` / `updated_at` | datetime | | |

**唯一约束**：`UNIQUE(candidate_id, job_version_id)` —— 与 `Match` 同构，幂等的物理基础。
**索引**：`approval_status`（供 W4 查询待批/已批）、`decided_by`。

### 4.2 可解释性硬门槛

`breakdown` / `evaluated_fields` / `evidence_refs` / `excluded_fields` 四者**任一为空 → 422**
（F-R3：宁可拒绝推荐，也不产出无来源的推荐）。

### 4.3 `unknown_dimensions` 契约（约束 4 的物理落点）

只读快照 `Candidate.evaluation_context`，**禁止转换、禁止补全、禁止插值**：

```json
{
  "reliability": {"status": "future_capability", "scored": false},
  "historical":  {"status": "future_capability", "scored": false},
  "cost":        {"status": "unknown", "scored": false, "advisory_only": true}
}
```

**fail-closed 校验**：若上游任一维度的 status 不是字符串状态量（例如被写成数字），或 `scored != false`
→ **422「unknown dimension would be fabricated」**，拒绝推荐。

### 4.4 新增枚举（仅 1 个，纯 Python 层，零 schema 改动）

```python
class RecommendationStatus(StrEnum):
    PROPOSED  = "proposed"    # AI 产出，等待人类决策
    APPROVED  = "approved"    # 仅人类决策可达
    REJECTED  = "rejected"    # 仅人类决策可达
    WITHDRAWN = "withdrawn"   # 仅系统失效可达（F-R8）
```

> **不新增**：`TrialStatus`、`EmployeeStatus`、`CandidateStatus.TRIALING`。
> 理由：本轮不实现 Trial/Employee，新增即「为不存在的状态预留物理词汇」，与 D-5 同一逻辑。

---

## 5. 状态机

### 5.1 `CandidateLifecycle` 受控加边（唯一的 W3-A 触碰点）

| 源 | 目标 | 变更 | 守卫 |
|---|---|---|---|
| `EVALUATED` | `REJECTED` | **既有，不动** | — |
| `EVALUATED` | `RECOMMENDED` | **新增（受控）** | 仅 `recommend_candidate` 成功路径；须 Match COMPUTED 且 F-R1…F-R5 全过 |
| `RECOMMENDED` | `EVALUATED` | **新增（受控）** | 仅人类 REJECT 或系统 WITHDRAWN（撤回推荐，回到不可变快照） |
| `RECOMMENDED` | 其它任何状态 | **禁止** | 尤其禁止 `RECOMMENDED → TRIALING`（Trial 属 W3-D/W4） |

**净变更 = `ALLOWED[EVALUATED]` 加 1 个成员 + `ALLOWED[RECOMMENDED]` 从 `set()` 变为 `{EVALUATED}`。**
既有边零改动，W3-A 语义不被削弱（既有测试 `test_candidate_illegal_transition_rejected_409` 仍针对 POOLED→REJECTED 之类路径）。

> ⚠️ 既有测试若断言「RECOMMENDED 不可达」（W3-A 冻结时的守卫测试），在 W3-C 落地后**必须按受控解冻流程更新**——这是 R7 需确认的先决条件之一（见 §16）。

### 5.2 `Recommendation` 状态机（表级）

| 源 → 目标 | 触发者 | 前置条件 | 审计 action |
|---|---|---|---|
| (none) → `PROPOSED` | 系统（AI） | F-R1…F-R5 全过 | `recommendation.proposed` |
| `PROPOSED` → `APPROVED` | **仅人类** | `approval_status` PENDING；`_assert_owner_actor` | `recommendation.decided` |
| `PROPOSED` → `REJECTED` | **仅人类** | 同上 | `recommendation.decided` |
| `PROPOSED` → `WITHDRAWN` | 仅系统 | F-R8（Match 被重算 / 候选回退） | `recommendation.withdrawn` |
| `APPROVED` → `WITHDRAWN` | 仅系统 | F-R8（**已批准的推荐同样失效**） | `recommendation.withdrawn` |
| `WITHDRAWN` → `PROPOSED` | 系统（AI） | 新 attempt 且重过 F-R1…F-R5 | `recommendation.proposed` |
| `APPROVED` / `REJECTED` | 终态 | 不可再决策（重复决策 → 409） | — |

**关键 fail-closed 点**：`APPROVED` **不是**不可逆承诺——一旦底层 Match 被新证据推翻，系统必须把 `APPROVED` 拉回 `WITHDRAWN`（F-R8）。人类批准的是**当时那份证据**，不是候选本身。

---

## 6. Approval(L4) 唯一人类闸 —— 三方案与推荐

### 6.1 方案对比

| 方案 | 做法 | 优点 | 缺点 / 风险 | 结论 |
|---|---|---|---|---|
| **A′ 物理复用 `approval` 表** | migration 中 `batch_alter_table("approval")`：① `project_id` 改可空 ② 加 `recommendation_id` 可空列。Workforce 内直接 `session.add(Approval(...))` 绕过 `create_approval` | 单一审批表，最贴近历史 proposal「不新造审批机制」 | ⚠️ 改**核心既有表**（非 Workforce 域）；`create_approval` 的幂等/事件机制被绕过（需 W3-C 自行补）；project-scoped owner_inbox **永远看不到**这些行；downgrade 需删 `project_id IS NULL` 的行才能恢复 NOT NULL（**downgrade 带数据删除**） | 备选 |
| **B 域内闸门（推荐）** | 新建 `recommendation` 表承载 `approval_status` / `risk_level=L4` / `decided_by`；决策函数严格复用 `resolve_owner_actor` + `_assert_owner_actor` + `ApprovalStatus` + `RiskLevel` + `append_audit` + SAVEPOINT 幂等；保留 `approval_id` 前向列 | **零改既有表**（严格 additive、可逆）；不触 engine / owner_inbox；语义复用而非重写审批机制；W4 引入 Project 后可无缝升级为 A′（`approval_id` 回填） | 未在 `approval` 表留物理行 → 「复用 Approval」为**语义复用** | ✅ 推荐 |
| **C 给 Workforce 引入 Project** | Job/BusinessGoal 加 `project_id` | 可原生走 owner_inbox | **违反本轮硬约束 1**（W3-A/W3-B 冻结，禁止修改既有语义） | ❌ 排除 |

### 6.2 推荐方案 B 的「人类闸」定义

```python
# 纯设计示意，本轮不实现
def decide_recommendation(
    session,
    recommendation_id: str,
    decision: ApprovalStatus,        # 复用既有枚举
    rationale: str | None = None,
    *,
    actor: ActorContext | None = None,
) -> Recommendation:
    if actor is None:
        actor = resolve_owner_actor()   # 复用 actor.py
    _assert_owner_actor(actor)          # 复用 actor.py（services.decide_approval 同路径）
    ...
```

**与 `services.decide_approval` 同构复用的部分**（全部 import，零重写）：

| 能力 | 复用来源 | 出处 |
|---|---|---|
| owner 身份解析 | `actor.resolve_owner_actor` | `actor.py:53` |
| owner 身份断言 | `actor._assert_owner_actor` | `actor.py:79` |
| 决策状态词 | `ApprovalStatus` | `models.py:191` |
| 风险分级 | `RiskLevel.L4` | `models.py:198` |
| 审计写入 | `append_audit` | `audit.py:110` |
| 重复决策 409 语义 | 对齐 `decide_approval` 的「该审批已被处理」 | `services.py:~338` |
| 并发幂等 | SAVEPOINT + IntegrityError（P2-1 模式） | `workforce.py:1602` |

**不复用（也不重写）**：`services.create_approval`（需 Project）、`owner_inbox` 通道（project-scoped）、`append_event`（需 project_id）。
⇒ 本轮**不产生** `approval.requested` 事件；审计靠 `append_audit(project_id=None)`（与 W3-A/W3-B 全部既有调用一致）。

---

## 7. Gate / fail-closed 规则

| ID | 规则 | 违反行为 |
|---|---|---|
| **F-R1** | `Candidate.status` 必须 `EVALUATED` | 409 `illegal candidate state transition`；**不回退状态**、不写推荐 |
| **F-R2** | 必须存在 `Match` 且 `Match.status == COMPUTED`；`BLOCKED` / `match_blocked_reason` 非空 → 拒绝 | 409 `recommendation blocked: {reason}`；**保留 EVALUATED**，不进 RECOMMENDED |
| **F-R3** | `Match.breakdown` / `evaluated_fields` / `evidence_refs` / `excluded_fields` 任一为空 → 拒绝 | 422 `match is not explainable`；禁止生成默认 breakdown |
| **F-R4** | reliability / historical / cost 三段必须落为 `unknown` / `future_capability` 状态量，`scored=false`；禁止任何数值 | 422 `unknown dimension would be fabricated` |
| **F-R5** | `cost_policy` 仅生成**文本** advisory；禁止产生 cost 数值、禁止进入 `score`、禁止产生「伪精确成本分」 | 设计约束（见 §4.1 `cost_advisory` 为 `str`）；测试断言 score 不含 cost 分量 |
| **F-R6** | 仅 `approval_status == APPROVED` 且 `decided_by` 为 owner actor 时，`assert_trial_eligible()` 返回 True | 否则返回 False / 抛 409 |
| **F-R7** | **禁止任何自动绕过 Approval 的路径**：不存在任何函数能在 `approval_status != APPROVED` 下产出「可进 Trial」信号 | 静态可审计（§13 T-REC-APPROVAL 组；§14 契约 F） |
| **F-R8** | `Match` 被重算（`match_attempt` 漂移）或 Candidate 回退 → Recommendation 自动 `WITHDRAWN`（含已 APPROVED） | 写 `recommendation.withdrawn` 审计；已批准推荐同样失效 |
| **F-R9** | 非 owner actor 调用决策 → 拒绝 | 403（由 `_assert_owner_actor` 抛出，不重写） |

> **F-R8 是本轮最关键的一条**：它堵住「批准旧证据 → 新证据推翻 → 仍然可 Trial」这个最危险的 fail-open 通道。

---

## 8. 幂等规则

| 场景 | 行为 | 审计 |
|---|---|---|
| 同 `(candidate_id, job_version_id)` + **同 `match_attempt`** 重复 `recommend_candidate` | **返回既有 Recommendation**，不改状态、不写审计、不重复转状态 | 无（与 `compute_match` replay 语义一致） |
| 同键 + **新 `match_attempt`** | in-place UPDATE（score/breakdown/evidence_refs/…），状态回 `PROPOSED`，`approval_status` 回 `PENDING`，清空 `decided_by/decided_at` | `recommendation.recomputed`（含 before/after） |
| 并发首次创建 | SAVEPOINT + `IntegrityError` → 从 fresh session 读权威行返回（P2-1 模式） | 无重复 |
| 重复决策（已 APPROVED/REJECTED 再决策） | 409「该推荐已被决策，不能重复决策」 | 无 |
| 幂等键 | `recommend:{candidate_id}:{job_version_id}` / `rec:{id}:decision:{decision}` | — |

**并发安全边界**：与 W3-B 完全一致（SAVEPOINT，非分布式锁）；本轮**不引入**新并发机制。

---

## 9. Audit / Evidence 要求

| action | resource_type | 时机 | `after` 必须包含 |
|---|---|---|---|
| `recommendation.proposed` | `recommendation` | 创建 / 重新激活 | `match_id`, `match_attempt`, `score`, `weights_version`, `evaluated_fields`, `evidence_refs`, `excluded_fields`, `unknown_dimensions`, `proposed_action`, `status` |
| `recommendation.recomputed` | `recommendation` | 新 attempt 覆盖 | `before.{score,status,approval_status}` / `after.{score,status,approval_status,match_attempt}` |
| `recommendation.decided` | `recommendation` | 人类决策 | `decision`, `decided_by`, `rationale`, `before.approval_status` |
| `recommendation.withdrawn` | `recommendation` | F-R8 失效 | `reason`, `before.{status,approval_status,match_attempt}`, `after.match_attempt` |

**统一参数**：`project_id=None, task_id=None`（与 W3-A/W3-B 全部既有调用一致；Workforce 域无 Project）。

**evidence 链完整性要求**：`recommendation.evidence_refs` 必须能回溯到
`cand:{candidate_id}:attempt:{n}`（W3-A 证据）→ `br:{benchmark_result_id}`（W3-B 基准，若有）→ `match:{match_id}`（W3-B 评分）。
任一环缺失 → F-R3 拒绝。

> ⚠️ **脱敏陷阱（既有，务必遵守）**：`append_audit` 的 `redact_secrets` **只按 key 名脱敏**（命中 `SECRET_KEYS` → `[REDACTED]`），**不对字符串值内的 token 做模式匹配**。
> ⇒ `rationale` / `cost_advisory` / `decision_rationale` 中**禁止拼接任何密钥原文**；测试如需验证脱敏，必须注入匹配 `SECRET_KEYS` 的 **key**，不能靠往文本里塞 token。

---

## 10. 与 W3-B Match / Ranking 的接口契约

### 10.1 `recommend_candidate(session, candidate_id)` 的读集（白名单）

**允许读**：
- `Candidate.{id, agent_id, job_version_id, status, evaluation_context}` ← `evaluation_context` **只读**
- `Match.{id, score, weights_version, breakdown, evaluated_fields, evidence_refs, status, match_blocked_reason, benchmark_version_id}`
- `Agent.{id, cost_policy}` ← 仅用于 `cost_advisory` 文本

**禁止调用 / 禁止写**：
- ❌ 调用 `compute_match` / `run_benchmark` / `evaluate_candidate`（Recommendation 不触发任何重算，保证「推荐基于既有证据」）
- ❌ 写 `Candidate.evaluation_context`
- ❌ 写 `Match.*`
- ❌ 写 `Candidate.status`（**唯一例外**：`recommend_candidate` 成功路径的 `EVALUATED → RECOMMENDED`，以及决策/失效路径的 `RECOMMENDED → EVALUATED`，均经 `CandidateLifecycle.require_transition`）
- ❌ 调用 `delegation.check_budget` / `scheduler` / `execution`（无 Project，且 Trial 不属本轮；见 D-3）

### 10.2 返回值契约

返回 `Recommendation`，且保证：
1. `score == Match.score`（字节级相等，非重算）
2. `breakdown == Match.breakdown`（深拷贝，非引用共享，防后续篡改）
3. `evidence_refs[-1] == f"match:{match_id}"`
4. `excluded_fields` 覆盖 reliability / historical / cost 三项

---

## 11. W4 / Trial 接口边界

| 边界 | 本轮 | W3-D / W4 接手 |
|---|---|---|
| Trial 实体 | **不建表、不建函数** | `create_trial` 在 W3-D |
| Trial 守卫 | 提供 `assert_trial_eligible(session, recommendation_id) -> bool`（**纯读，不写审计**，对齐 `rank_candidates` 先例） | W3-D 调用它作为前置 |
| Employee | **不建表、不建列**（D-5） | W4 建 `Employee` 表 |
| `approval_status == APPROVED` 的 Recommendation | 是 **W4 创建 Employee 的唯一合法输入** | W4 读取 |
| `approval_id` 列 | V1 恒 `None` | W4 引入 Project 后回填真实 `Approval` 行（B → A′ 升级路径） |
| Budget / Scheduler / Execution | **本轮零调用**（无 Project） | W4 在 Trial 创建时复用 `check_budget` / `create_task` / `execute_task`，不重写 |

---

## 12. Migration 必要性分析

| 对象 | 是否需 migration | 理由 |
|---|---|---|
| `recommendation` 表 | ✅ **需要** | 新实体，必须建表 |
| `CandidateStatus.RECOMMENDED` | ❌ 不需要 | 枚举成员已存在（`models.py:1570`）；`candidate.status` 是 `sa.String()`，非 DB ENUM |
| `RecommendationStatus` | ❌ 不需要 | 新枚举仅 Python 层；落库为字符串列 |
| `CandidateLifecycle` 加边 | ❌ 不需要 | 纯 Python 字典 |
| `approval` 表 | ❌ 不需要（**方案 B**）<br>✅ 需要（方案 A′，且 downgrade 带数据删除） | 取决于 §6 决策 |
| `candidate` / `match` / `benchmark*` | ❌ 不需要 | W3-B 已建，冻结 |

### 规划（方案 B）

- **文件名**：`alembic/versions/2026090X_0001_workforce_recommendation.py`
- **revision**：`2026090X_0001_workforce_recommendation`
- **down_revision**：`20260901_0001_workforce_match_benchmark`（保持**单 head**）
- **upgrade**：`op.create_table("recommendation", …)` + 2 个 index（`ix_recommendation_approval_status`、`ix_recommendation_decided_by`）
- **downgrade**：`op.drop_index(...)` ×2 + `op.drop_table("recommendation")`
- **性质**：additive（只建新表）、可逆（drop 完全对称）、单 head ✅

### 若采纳方案 A′（额外部分）

- `batch_alter_table("approval")`: `alter_column("project_id", nullable=True)`, `add_column("recommendation_id", nullable FK)`
- **downgrade 需先 `DELETE FROM approval WHERE project_id IS NULL`**，再恢复 NOT NULL —— **downgrade 非无副作用**，需 R7 显式接受并留痕。

---

## 13. 契约测试清单（仅清单，本轮不写测试代码）

### T-REC-STATE — 受控状态转换（4）
1. `EVALUATED + COMPUTED Match` → `recommend_candidate` 成功，Candidate 变 `RECOMMENDED`
2. `EVALUATED → RECOMMENDED → EVALUATED`（人类 REJECT 后回退）
3. `POOLED` / `REJECTED` / `EVALUATING` 调用 → 409，状态**不变**
4. `RECOMMENDED → TRIALING` 不存在（无 TRIALING 枚举；且 `ALLOWED[RECOMMENDED] == {EVALUATED}`）

### T-REC-GATE — fail-closed（8）
5. `Match.status == BLOCKED`（capability_gap）→ 409，Candidate 仍 `EVALUATED`，无 Recommendation 行
6. 无 Match 行 → 422
7. `Match.breakdown == {}` → 422（F-R3）
8. `evidence_refs == []` → 422（F-R3）
9. `evaluation_context.reliability_evidence.status` 被篡改为数值 → 422（F-R4）
10. `cost_policy` 含数字 → `cost_advisory` 为文本，`score` **不含** cost 分量，`excluded_fields` 含 cost（F-R5）
11. `assert_trial_eligible` 在 `PROPOSED` / `REJECTED` / `WITHDRAWN` 下均 False（F-R6）
12. Match 重算后 `assert_trial_eligible` 对**已 APPROVED** 推荐返回 False（F-R8）

### T-REC-EXPL — 可解释与审计（4）
13. `recommendation.score == match.score` 且 `breakdown` 深拷贝（改 Match 不影响 Recommendation）
14. `evidence_refs` 末尾 = `match:{match_id}`，且含 `cand:...:attempt:N`
15. `recommendation.proposed` 审计行存在，`after` 含全部必需字段
16. `recommendation.decided` 审计行 `actor` = owner_id

### T-REC-IDEM — 幂等与并发（3）
17. 同 attempt 重复推荐 → 返回同一行，`created_at` 不变，**审计行数不增**
18. 新 attempt → UPDATE + `recommendation.recomputed` 审计，`approval_status` 回 PENDING、`decided_by` 清空
19. 并发首次创建 → 无重复行、无 IntegrityError 逃逸

### T-REC-APPROVAL — L4 人类闸（5）
20. owner actor 决策 → `APPROVED` / `REJECTED` 成功
21. 非 owner actor（agent / system）→ 403（F-R9）
22. 重复决策 → 409
23. 无 `decided_by` 的 `APPROVED` 不可达（不存在使 approval_status=APPROVED 而 decided_by 为空的代码路径）
24. `REJECTED` 后 Candidate 回 `EVALUATED`，`assert_trial_eligible` False

### T-REC-BOUNDARY — 不越界（4）
25. W3-C 全程**零写** `Candidate.evaluation_context`
26. 未创建任何 `trial` / `employee` / `training` / `performance` 表（deferred-table 断言）
27. 未调用 `compute_match` / `run_benchmark` / `check_budget` / `execute_task`（monkeypatch 计数断言）
28. 未新增 Capability 词表行（`select(Capability)` 计数不变）

### T-REC-MIG — 迁移（2）
29. alembic 单 head == `2026090X_0001_workforce_recommendation`
30. `downgrade()` 后表/索引完全消失，`candidate`/`match` 表不受影响

### T-REG — 回归（必须全绿）
- `test_workforce_evaluation_w3a.py` 16 项
- `test_workforce_benchmark_match_w3b.py` 12 项
- `test_workforce_models.py` 26 项（**含 alembic head 断言需同步更新**）
- 全量回归 ≥ 246 项 + `ruff` PASS

---

## 14. DSH 7 项独立审计契约（W3-C 版）

| 契约 | 内容 | 判据 |
|---|---|---|
| **A** | 单 head 不可变 / migration additive + 可逆 | `alembic heads` 唯一 = `2026090X_0001_...`；upgrade 只 create_table/index；downgrade 完全对称 |
| **B** | 状态机边界 | W3-A/W3-B 既有边 **零改动**；`ALLOWED[EVALUATED]` 只增 `RECOMMENDED`；`ALLOWED[RECOMMENDED] == {EVALUATED}`（**不含 TRIALING**） |
| **C** | downgrade 完整性 | downgrade 后 `recommendation` 表与索引消失，W3-A/W3-B 表与数据不受影响 |
| **D** | SSoT 零新能力词 | 无新 Capability 行 / 无第二套能力词汇；未重造 Scheduler / Execution / Budget / Audit / Approval 判定逻辑（全部 import 复用） |
| **E** | 契约测试 | T-REC-* 28 项 + T-REG 全绿 + ruff PASS |
| **F** | fail-closed 语义 | F-R1…F-R9 逐条有对应测试；**不存在**绕过 Approval 达致「可 Trial」的路径 |
| **G** | 可解释评分 + 审批留痕 | `breakdown`/`evidence_refs`/`excluded_fields`/`unknown_dimensions` 强制非空；`decided_by` 必为 owner；审计 action 齐备 |

---

## 15. 实施边界与明确禁止事项

### 15.1 允许（W3-C 实现阶段）
- 新建 `recommendation` 表 + 1 个 alembic revision（单 head、additive、可逆）
- 新增 `RecommendationStatus` 枚举
- `CandidateLifecycle` 加 2 条受控边（`EVALUATED ⇄ RECOMMENDED` 的进/出）
- 新增函数：`recommend_candidate` / `decide_recommendation` / `assert_trial_eligible`（+ 必要私有 helper）
- 复用 import：`actor.resolve_owner_actor`、`actor._assert_owner_actor`、`models.ApprovalStatus`、`models.RiskLevel`、`audit.append_audit`、`workforce._attempt_from_evidence_refs`

### 15.2 明确禁止
- ❌ 修改 `engine` / `ruleset` / `owner_inbox.py` / `services.py` / `models.py` 既有定义（只允许在 `models.py` **末尾追加** W3-C 区段）
- ❌ 修改 W3-A（`evaluate_candidate` / `_build_evaluation_context` / `_collect_capability_evidence`）与 W3-B（`compute_match` / `rank_candidates` / `run_benchmark`）任何语义
- ❌ 写 `Candidate.evaluation_context`
- ❌ 除受控边外写 `Candidate.status`
- ❌ 调用 `compute_match` / `run_benchmark` / `evaluate_candidate`
- ❌ 调用 `check_budget` / Scheduler / `execute_task`（无 Project，且 Trial 不属本轮）
- ❌ 创建 / 预实现 Trial、Employee、Training、Performance 任何表或字段
- ❌ 新增 `CandidateStatus.TRIALING` 或其它未来状态枚举
- ❌ 接入 ai-arena、修改 ruleset
- ❌ 新增 Capability 词汇 / 第二套能力 SSoT
- ❌ 虚构 reliability / historical / cost 数值评分（unknown 就是 unknown）
- ❌ 任何自动绕过 Approval 的路径
- ❌ `commit` / `push` / 创建 PR / 合并（本阶段与实现阶段均需 R7 显式授权 exact-head SHA）

---

## 16. 结论与待 R7 决策项

### 结论：**GO WITH CONDITIONS**

W3-C 设计本身自洽且可落地，但存在 **1 个必须先裁决的架构缺口**（D-1/D-2）与 **4 个需确认的设计取舍**。

### 🔴 必裁项（1）

| # | 问题 | 选项 |
|---|---|---|
| **Q1** | Approval(L4) 人类闸采用哪个方案？ | **B（推荐）**：域内闸门，零改既有表，语义复用，保留 `approval_id` 升级路径<br>**A′**：物理复用 `approval` 表（需改核心表 `project_id` → 可空，downgrade 带数据删除）<br>**C**：引入 Project（违反冻结，已排除） |

### 🟡 需确认项（4）

| # | 问题 | 本设计默认 | 需确认 |
|---|---|---|---|
| **Q2** | 受控解冻：既有 W3-A 若存在「RECOMMENDED 不可达」的守卫测试，是否允许按受控流程更新？ | 允许，且必须有新测试顶上（T-REC-STATE-4） | ✅/❌ |
| **Q3** | `APPROVED` 可被 F-R8 系统拉回 `WITHDRAWN`？ | 是（人类批准的是**那一份证据**，不是候选本身） | ✅/❌ |
| **Q4** | 不建 `target_employee_id` 死列（D-5）？ | 不建，改用 W4 握手契约 | ✅/❌ |
| **Q5** | 本轮零调用 Budget / Scheduler / Execution（无 Project），递延 W4 Trial 阶段复用？ | 是，递延 | ✅/❌ |

### 裁决后流程（与 W3-B 一致）
1. R7 裁决 Q1–Q5
2. 进入 W3-C 实现（TDD：先测试后实现）
3. DSH 路径③独立审计（§14 七契约）
4. R7 针对 **exact-head SHA** 显式授权
5. 仅 **Squash Merge** 合入 `main`

> **本阶段到此为止**：未写实现代码、未写 migration、未写测试代码、未 commit / push / PR。
> 当前工作树除本设计文档外无任何改动，W3-B exact tree `c837d78a…` 保持纯洁。
