# Workforce W3-A — Evaluation 实现设计（Implementation Design）

> **上游契约**：`docs/Workforce_W3_Evaluation_Matching_Spec_V1.md`（**已 R7 批准，本文件不得修改其内容**）。
> **授权**：R7 = `GRANTED`（6 项条件全部接受），授权范围 = **W3-A Evaluation implementation design → implementation → independent audit → Gate**。
> **本轮范围**：**只做 Evaluation 单环**。`match` / `benchmark` / `recommendation` / `trial` 的**表与实现全部留给 W3-B/C/D**。
> **事实源**：`aios_gap2_recover` @ `fdb72d4`（分支 `w2-clean`，工作树干净）。

---

## 0. 本设计的定位与边界

| 项 | W3-A（本轮） | 延后 |
|----|-------------|------|
| `CandidateStatus` | 提升 `EVALUATING`/`EVALUATED`/`RECOMMENDED` 为真实枚举 | — |
| `CandidateLifecycle` | 加 `POOLED→EVALUATING`、`EVALUATING→{EVALUATED, POOLED}`、`EVALUATED→REJECTED` | `EVALUATED→RECOMMENDED`（需 Match 闸门，W3-C/D） |
| `evaluate_candidate` | ✅ 实现 | — |
| capability evidence + `capability_fit` | ✅ 实现（Spec §5.1） | — |
| 四类 evidence 全量写入 `evaluation_context` | ✅ 实现（benchmark/cost/reliability/historical 恒 `unknown`/`future_capability`，**不虚构**） | — |
| fail-closed F1–F4 | ✅ 实现 | — |
| `compute_match` / `Match` 表 / `MATCH_WEIGHTS_V1` 聚合 | ❌ | W3-C |
| `benchmark*` 三表 / `JobVersion.benchmark_version_id` / BenchmarkAdapter | ❌ | W3-B |
| `recommendation` 表 / `recommend_candidate` / Approval 闸门 | ❌ | W3-D |
| `trial` 表 / `create_trial` / `execute_task` 接线 | ❌ | W3-D |
| Alembic migration | **无（零迁移）** | W3-B 起 |

**零迁移的可行性证明（已考古核实）**：
`alembic/versions/20260827_0002_workforce_candidate.py:63` → `sa.Column("status", sa.String(), nullable=False)`。
`candidate.status` 在 DB 层是 **String 而非 DB Enum**，且 `evaluation_context` 本就是 `Column(JSON)`。
⇒ 提升枚举成员、往 `evaluation_context` 写值，**两者均不需要任何 migration**。W3-A 的风险面因此收敛到「纯 Python 层」。

---

## 1. 变更清单（Change Inventory）

| # | 文件 | 变更类型 | 内容 |
|---|------|---------|------|
| C1 | `src/aios/models.py` | 枚举解冻 | `CandidateStatus` 增加 `EVALUATING`/`EVALUATED`/`RECOMMENDED` 三个真实成员；重写类 docstring |
| C2 | `src/aios/workforce.py` | 状态机扩展 | `CandidateLifecycle.ALLOWED` 增边；类 docstring 更新 |
| C3 | `src/aios/workforce.py` | 新常量 | `EVALUATION_CONTEXT_SCHEMA_V1`、`PREFERRED_BONUS_WEIGHT`、`PREFERRED_BONUS_CAP` |
| C4 | `src/aios/workforce.py` | 新内部函数 | `_capability_fit_value()`、`_collect_capability_evidence()`、`_build_evaluation_context()` |
| C5 | `src/aios/workforce.py` | 新公开服务 | `evaluate_candidate()` |
| C6 | `src/aios/workforce.py` | 导入 | 新增 `AgentCapability`（models）；`IntegrityError` 已导入 |
| C7 | `src/aios/workforce.py` | 模块 docstring | 更新（原 docstring 称 W3 态「不可进入」，W3-A 后不再准确） |
| C8 | `tests/test_workforce_models.py` | 受控测试更新 | 仅 `test_candidate_illegal_transition_rejected_409`：保留原 2 条断言，新增 W3-A 边断言 |
| C9 | `tests/test_workforce_evaluation_w3a.py` | 新增测试文件 | T-EVAL-1…8 / T-AUDIT-1 / T-ZEROMIG-1 / T-REG-1 |

**明确不改**：W1 全部服务与语义；`discover_candidates`（含 P2-1 SAVEPOINT 并发幂等）；`reject_candidate` / `repool_candidate` 语义；`CapabilityRequirement` / `Capability` / `AgentCapability` 模型与 SSoT；任何 alembic 文件；任何 Core 域模块。

---

## 2. 数据模型（`evaluation_context` JSON Schema V1）

W3-A **不新建表**（Spec §9.2 明确 V1 不建 `CandidateEvaluation`）。评估证据落 `Candidate.evaluation_context`（`models.py:1589`，W2 已预留的 JSON 包）。

```jsonc
{
  "schema_version": "w3a.evaluation.v1",   // 常量 EVALUATION_CONTEXT_SCHEMA_V1
  "attempt": 1,                            // 第 N 次评估（失败重试递增；审计键用）
  "evaluated_at": "2026-08-28T02:40:33+00:00",   // ISO-8601 UTC
  "evaluator": "workforce_evaluation",

  // Spec §2.1「实际算出的组件列表，用于可解释性」—— W3-A 恒为 ["capability_fit"]
  "evaluated_fields": ["capability_fit"],

  "capability_evidence": {
    "status": "computed",                  // computed | unknown
    "requirements": [
      {
        "capability_id": "cap_xxx",        // Alpha-1 SSoT id（唯一权威链接）
        "capability_name": "writing",      // 展示快照（非权威）
        "required": true,
        "min_proficiency": 60,
        "agent_priority": 75,              // AgentCapability.priority；未声明/enabled=False → 0
        "declared": true,                  // AgentCapability 行是否存在
        "capability_enabled": true,        // AgentCapability.enabled
        "meets_threshold": true,           // agent_priority >= min_proficiency
        "fit": 0.375                       // clamp((p-min)/(100-min), 0, 1)
      }
    ],
    "capability_fit": 0.375,               // 见 §4 公式
    "threshold_passed": true,              // 所有 required 均 meets_threshold
    "blocked_requirements": []             // 未达标的 required requirement_id 列表
  },

  // ↓↓ 以下四类 W3-A 恒为「不可算」，绝不写数字（Spec §2.5 F3 / §3.4 / §3.6）↓↓
  "benchmark_evidence":  {"status": "unknown", "waived": true,
                          "reason": "JobVersion.benchmark_version_id 不存在（W3-B 引入）"},
  "cost_evidence":       {"status": "unknown",
                          "reason": "Agent.cost_policy schema 未定义（归 W5 Budget 域）"},
  "reliability_evidence":{"status": "future_capability",
                          "reason": "Alpha-1 Agent 模型无成功率/可用性时序"},
  "historical_evidence": {"status": "future_capability",
                          "reason": "Employee/Performance 数据属 W4+"},

  "recommendation_blocked_reason": null,   // 或 "capability_gap"（F1）
  "evaluation_error": null                 // 失败回退时写入 {type, message}
}
```

**不变量（供 DSH 审计与后续阶段依赖）**：
- INV-1：`evaluated_fields` 只含 `"capability_fit"`；W3-C 引入 benchmark 后才会追加。
- INV-2：`benchmark_evidence.status / cost_evidence.status` 恒为 `"unknown"`；`reliability_evidence.status / historical_evidence.status` 恒为 `"future_capability"`。**任何分支都不得写入数值 score。**
- INV-3：`evaluation_context` **不复制** Agent Registry 数据（不存 cost_policy 原文、不存 capabilities 镜像）—— 只存 id 与派生分，避免快照漂移与潜在敏感字段外泄。
- INV-4：`capability_evidence.requirements[].capability_id` 必须等于 `CapabilityRequirement.capability_id`（SSoT 引用），`capability_name` 仅为展示快照。

---

## 3. 状态机（W3-A 后）

```python
ALLOWED: dict[CandidateStatus, set[CandidateStatus]] = {
    CandidateStatus.POOLED:    {CandidateStatus.REJECTED, CandidateStatus.EVALUATING},
    CandidateStatus.REJECTED:  {CandidateStatus.POOLED},
    CandidateStatus.EVALUATING:{CandidateStatus.EVALUATED, CandidateStatus.POOLED},
    CandidateStatus.EVALUATED: {CandidateStatus.REJECTED},
    # EVALUATED -> RECOMMENDED 在 W3-C/D 与 Match 闸门一同引入；W3-A 内 RECOMMENDED 不可达。
    CandidateStatus.RECOMMENDED: set(),
}
```

图示：

```
        ┌──── reject ────┐
        ▼                │
    POOLED ──evaluate──▶ EVALUATING ──done──▶ EVALUATED
        ▲                    │                    │
        └──── error/fail ────┘                    │
        ▲                                         │ reject
        └──────────── repool ──── REJECTED ◀──────┘

    EVALUATED ⇢ RECOMMENDED      （虚线 = W3-A 内不存在，409）
```

**关键规则**：
- R1：`REJECTED → EVALUATING` **非法**（409），必须先 `repool` 回 `POOLED`（Spec §2.4）。
- R2：`POOLED → EVALUATED` **非法**（409），不能跳过 `EVALUATING`。
- R3：`EVALUATED → POOLED` **非法**（409），必须经 `REJECTED → POOLED`。评估是**不可变快照**；重评路径 = `reject → repool → evaluate`（全程留审计）。
- R4：`EVALUATING → EVALUATED` 与 `EVALUATING → POOLED` 都合法，后者是失败/崩溃恢复路径。
- R5：`RECOMMENDED` 在 W3-A 是真实枚举成员但**零入边零出边**，任何进出迁移均 409（由测试断言，防未来误接线）。

---

## 4. `capability_fit` 公式（Spec §5.1 的确定性落地）

```
对 JobVersion 的每个 CapabilityRequirement r：
    ac = AgentCapability(agent_id, r.capability_id)
    p  = ac.priority   当 ac 存在且 ac.enabled == True
       = 0             当 ac 不存在 或 ac.enabled == False     # Spec §2.2 fail-closed
    meets = (p >= r.min_proficiency)

    # 除零守卫（min_proficiency 上界为 100，models.py:1526 ge=1 le=100）
    if r.min_proficiency >= 100:
        fit_i = 1.0 if p >= 100 else 0.0
    else:
        fit_i = clamp((p - r.min_proficiency) / (100 - r.min_proficiency), 0.0, 1.0)

    required_fits    = [fit_i for r in reqs if r.required]
    preferred_fits   = [fit_i for r in reqs if not r.required]

    if not required_fits:
        raise ServiceError(422, "job version has no required capability requirements to evaluate against")
        # 与 discover_candidates 的「无门槛不过滤」fail-closed 一致；绝不返回默认 0.5

    base = mean(required_fits)
    if preferred_fits:
        bonus = PREFERRED_BONUS_WEIGHT * mean(preferred_fits)   # 0.05 * [0,1] ≤ 0.05
        capability_fit = min(1.0, base + bonus)                 # 「微量加成，不喧宾夺主」Spec §5.1
    else:
        capability_fit = base

    threshold_passed = all(meets for r in reqs if r.required)
```

**常量**：
```python
EVALUATION_CONTEXT_SCHEMA_V1 = "w3a.evaluation.v1"
PREFERRED_BONUS_WEIGHT = 0.05   # Spec §5.1「≤5%」
PREFERRED_BONUS_CAP = 1.0
```

**F1 落地**：`threshold_passed == False` ⇒
`evaluation_context["recommendation_blocked_reason"] = "capability_gap"`，候选**停留** `EVALUATED`。
W3-A 内 `EVALUATED→RECOMMENDED` 本就无边，故此标记是给 **W3-C/D `recommend_candidate`** 的前向契约：它必须读此字段并拒绝推荐。

> 注：`MATCH_WEIGHTS_V1`（Spec §5.2，`capability_fit=0.6` / `benchmark_score=0.4`）在 **W3-C `compute_match` 首次使用时定义**。W3-A 不预先定义未使用常量（避免死常量与"猜测未来权重"）。

---

## 5. `evaluate_candidate` 服务契约

```python
def evaluate_candidate(
    session: Session,
    candidate_id: str,
    *,
    evaluator: str = "workforce_evaluation",
) -> Candidate:
```

### 5.1 执行流程

| 步 | 动作 | 失败语义 |
|---|------|---------|
| 1 | `cand = session.get(Candidate, candidate_id)` | `None` → `ServiceError(404, f"candidate not found: {candidate_id}")` |
| 2 | 若 `cand.status == EVALUATED` → **幂等 no-op**，直接 `return cand`（不写审计、不改状态、不改 context） | — |
| 3 | `resuming = (cand.status == EVALUATING)`（崩溃恢复续跑）；否则 `CandidateLifecycle.require_transition(cand.status, EVALUATING)` → 非法态 409 | 409 |
| 4 | `attempt = int(cand.evaluation_context.get("attempt", 0)) + 1` | — |
| 5 | `before = {"status": <原状态>, "attempt": attempt}`；`cand.status = EVALUATING`；`updated_at`；`flush()` | — |
| 6 | SAVEPOINT 内写审计 `candidate.evaluate.start`，`idempotency_key = f"evaluate:start:{candidate_id}:{attempt}"` | `IntegrityError` → 吸收，见 §5.3 |
| 7 | `get_agent(session, cand.agent_id)` 存在性校验（软引用 fail-closed） | 404 |
| 8 | `reqs = list_capability_requirements(session, cand.job_version_id)` | — |
| 9 | `_build_evaluation_context(...)` 计算 capability evidence + 四类 unknown evidence | 422（无 required 门槛）/ 其他异常 → §5.2 回退 |
| 10 | `cand.evaluation_context = ctx`；`cand.status = EVALUATED`；`updated_at`；`flush()` | — |
| 11 | 写审计 `candidate.evaluate`，`idempotency_key = f"evaluate:{candidate_id}:{attempt}"`，`after` 含 `status` + `evaluated_fields` + `capability_fit` + `recommendation_blocked_reason` | — |
| 12 | `return cand` | — |

### 5.2 失败回退（Spec §2.4 / F2 / T-EVAL-3）

步骤 7–9 抛**任何**异常时：
```python
cand.status = CandidateStatus.POOLED
cand.evaluation_context = {
    **cand.evaluation_context,
    "attempt": attempt,
    "evaluation_error": {"type": type(exc).__name__, "message": str(exc)[:500]},
}
cand.updated_at = now_utc()
session.flush()
append_audit(..., action="candidate.evaluate.error",
             idempotency_key=f"evaluate:error:{candidate_id}:{attempt}", ...)
raise            # 原样重抛：fail-closed，绝不吞异常
```
- **绝不停留半状态**：出口只有 `EVALUATED`（成功）或 `POOLED`（失败 + `evaluation_error`）。
- **不吞异常**：调用方必须知道评估失败；回退的状态与审计在同一未提交事务内，调用方若整体 rollback，则候选干净停留在 `POOLED`（无副作用）——两条路径都安全。
- 因为 W3-A 无 benchmark，异常只能来自 capability evidence 收集（如 SSoT 解析缺失）。测试用 monkeypatch 注入，见 §7 T-EVAL-3。

### 5.3 并发幂等（P0-3）

沿用 W2 的 P2-1 SAVEPOINT 吸收范式（`workforce.py:508`）：步骤 6 的 start 审计若撞 UNIQUE（`AuditLog.idempotency_key` `unique=True`，`audit.py:70`），说明另一并发评估已抢先开始：
```python
except IntegrityError:
    # SAVEPOINT 已回滚；外层事务完整。从 fresh connection 读回权威视图：
    with Session(session.get_bind()) as fresh:
        authoritative = fresh.get(Candidate, candidate_id)
    if authoritative is not None and authoritative.status == CandidateStatus.EVALUATED:
        return authoritative          # 对方已完成 → 幂等返回同一结果
    raise ServiceError(409, f"candidate evaluation already in progress: {candidate_id}")
```
**关键**：不静默返回 `POOLED`/`EVALUATING` 的候选冒充成功——那才是真正的 fail-open。

### 5.4 审计契约（F4）

| 阶段 | action | before | after | idempotency_key |
|------|--------|--------|-------|-----------------|
| 开始 | `candidate.evaluate.start` | `{status, attempt}` | `{status: "evaluating"}` | `evaluate:start:{cid}:{attempt}` |
| 成功 | `candidate.evaluate` | `{status, attempt}` | `{status: "evaluated", evaluated_fields, capability_fit, recommendation_blocked_reason, attempt}` | `evaluate:{cid}:{attempt}` |
| 失败 | `candidate.evaluate.error` | `{status, attempt}` | `{status: "pooled", error, attempt}` | `evaluate:error:{cid}:{attempt}` |

- 全部经 `append_audit`（`audit.py:110`），自动 `redact_secrets`。
- `before/after` 至少含 `status` 与 `evaluated_fields`（F4）。
- 审计与状态写入**同一 flush 序列**，调用方一次 commit 落库。

### 5.5 边界遵守（Spec §8）

- 只读消费 `get_agent` / `list_agents`；**不写** Agent Registry、**不改** `AgentCapability.priority` 语义。
- 只引用 `Capability` SSoT（`capability_id`），**不新建能力词表**。
- **不读** `Agent.capabilities` JSON 镜像（Spec §2.2：唯一来源是 `AgentCapability`）。
- **不碰** Scheduler / Execution / Budget / Knowledge / Context。
- **不新建**审批机制；W3-A 无 Approval 交互。
- 反向依赖：Core 域任何模块**不得** import `workforce`（现状保持）。

---

## 6. 偏差与需 R7 知悉的决策（Deviations & Decisions）

| # | 议题 | 本设计决策 | 理由 / 风险 |
|---|------|-----------|------------|
| D1 | `EVALUATING` 是否持久化 | **是**（flush 后写 start 审计），失败显式回退 `POOLED` | Spec §2.4 明确其为「进行中态，失败回退」；T-EVAL-3 要求可观测回退 |
| D2 | `EVALUATED → EVALUATING`（原地重评） | **不开此边**。重评路径 = `reject → repool → evaluate` | 评估是不可变快照（对齐 W1 JobVersion 不可变历史原则）；全程留审计。**副作用**：Agent capability 变更后需 2 步才能重评 → 列 Open Question Q-A |
| D3 | `RECOMMENDED` 枚举是否提升 | **提升**，但 `ALLOWED` 中显式 `set()`（零入边零出边） | 忠实执行 R7 条件 1（提升三个注释态）；同时用空集 + 测试断言防「提前接线」 |
| D4 | `EVALUATED → RECOMMENDED` 边 | **W3-A 不加** | 需 Match 闸门（Spec §6.1「需先有 Match」），归 W3-C/D |
| D5 | `min_proficiency == 100` 除零 | 显式分支：`p >= 100 → 1.0`，否则 `0.0` | `models.py:1526` 允许 `le=100`，分母可为 0；必须守卫 |
| D6 | 无 required 门槛的 JobVersion | `ServiceError(422)` fail-closed，**不返回默认分** | 与 `discover_candidates` 的「无门槛不过滤 → 422」一致；默认 0.5 即虚构评分（违反条件 2） |
| D7 | preferred 加成公式 | `min(1.0, base + 0.05 * mean(preferred_fits))` | Spec §5.1 只说「≤5% 微量加成」，本设计给出**确定性**定义，消除歧义 |
| D8 | 并发吸收后返回 | 已完成 → 返回权威行；仍在进行 → **409** | 不静默冒充成功（fail-closed > 静默） |
| D9 | `MATCH_WEIGHTS_V1` 是否预置 | **不预置**，W3-C 首次使用时定义 | 避免死常量 / 猜测未来权重 |
| D10 | W2 测试改动范围 | 只改 `test_candidate_illegal_transition_rejected_409` 一个函数，且**保留其原有 2 条断言** | R7 条件 6：「仅更新 Candidate 状态机相关测试」 |
| D11 | 失败是否吞异常 | **重抛** | fail-closed；状态回退已落（未提交事务内），调用方 rollback 亦安全 |

---

## 7. 测试计划

### 7.1 更新：`tests/test_workforce_models.py::test_candidate_illegal_transition_rejected_409`

**保留**（W2 回归守卫）：
1. `reject_candidate` 二次调用 → 409（`REJECTED → REJECTED` 非法）
2. `require_transition(POOLED, POOLED)` → 409（无自环）

**新增**（W3-A 边契约）：
3. `require_transition(POOLED, EVALUATING)` → 合法
4. `require_transition(EVALUATING, EVALUATED)` → 合法
5. `require_transition(EVALUATING, POOLED)` → 合法（失败回退通道）
6. `require_transition(EVALUATED, REJECTED)` → 合法
7. `require_transition(REJECTED, POOLED)` → 合法（W2 既有）
8. `require_transition(REJECTED, EVALUATING)` → 409（须先 repool，R1）
9. `require_transition(POOLED, EVALUATED)` → 409（不得跳过 EVALUATING，R2）
10. `require_transition(EVALUATED, POOLED)` → 409（须经 REJECTED，R3）
11. `require_transition(EVALUATED, RECOMMENDED)` → 409（W3-C 闸门未装，R5）
12. `require_transition(POOLED, RECOMMENDED)` → 409
13. `require_transition(RECOMMENDED, POOLED)` → 409（零出边，R5）

### 7.2 新增：`tests/test_workforce_evaluation_w3a.py`

复用 `test_workforce_models.py` 的 `_db` / `_seed_capability` / `_seed_agent` / `_build_chain` 风格（本文件内重建同名 helper，避免跨文件耦合）。

| ID | 测试 | 断言要点 |
|----|------|---------|
| T-EVAL-1 | 基本评估闭环 | `POOLED → EVALUATED`；`evaluation_context` 含 `capability_evidence` + `evaluated_at` + `evaluator`；`evaluated_fields == ["capability_fit"]`；`attempt == 1` |
| T-EVAL-1b | **不虚构评分**（条件 2） | `benchmark/cost_evidence.status == "unknown"`；`reliability/historical_evidence.status == "future_capability"`；四者**均不含** `score`/`value` 数值键；`capability_evidence.status == "computed"` |
| T-EVAL-1c | preferred 加成 | 加一个 `required=False` 需求后，`capability_fit` 增量 `> 0` 且 `<= 0.05`；且与手工按 §4 公式复算一致（可解释） |
| T-EVAL-1d | 除零守卫 | `min_proficiency=100` + `priority=100` → `fit == 1.0` 且不抛 `ZeroDivisionError`；`priority=99` → `fit == 0.0` |
| T-EVAL-2 | **F1 fail-closed** | required capability `priority < min_proficiency` → 状态仍 `EVALUATED`、`recommendation_blocked_reason == "capability_gap"`、`threshold_passed is False`、`blocked_requirements` 含该 requirement_id、该行 `fit == 0.0` |
| T-EVAL-2b | 未声明 capability | `AgentCapability` 行不存在 → `agent_priority == 0`、`declared is False`、`capability_gap` |
| T-EVAL-2c | `AgentCapability.enabled=False` | → `agent_priority == 0`、`capability_enabled is False`、`capability_gap`（Spec §2.2） |
| T-EVAL-3 | 失败回退（无半状态） | monkeypatch `_build_evaluation_context` 抛异常 → `evaluate_candidate` 抛出；候选中途掉落 `POOLED` + `evaluation_error`；审计含 `candidate.evaluate.error`；**无** `EVALUATING` 残留 |
| T-EVAL-4 | 幂等重放 | 对已 `EVALUATED` 候选再调 `evaluate_candidate` → 返回同一对象、`evaluation_context` 不变、`attempt` 不递增、审计**不新增** `candidate.evaluate` 行 |
| T-EVAL-5 | 并发/冲突 fail-closed | 预插 `AuditLog(idempotency_key="evaluate:start:{cid}:1")` 模拟并发抢跑 → 候选非 `EVALUATED` 时抛 **409**（不静默成功） |
| T-EVAL-6 | 404 / 409 前置 | 不存在的 candidate → 404；`REJECTED` 候选直接 evaluate → 409 |
| T-EVAL-7 | SSoT fail-closed | monkeypatch `get_agent` 抛 404 → 评估失败并回退 `POOLED`（软引用策略） |
| T-EVAL-8 | 无 required 门槛 → 422 | 仅 `required=False` 需求的 JobVersion → `ServiceError(422)`，**不产生** `capability_fit`（不得默认 0.5） |
| T-AUDIT-1 | 审计契约（F4） | 成功路径产生 `candidate.evaluate.start` + `candidate.evaluate` 两条审计；`before` 含 `status`；`after` 含 `status` + `evaluated_fields`；幂等键形如 `evaluate:{cid}:{attempt}`；`redact_secrets` 生效（注入假 `api_key` 后落库为 `[REDACTED]`） |
| T-ZEROMIG-1 | **零迁移**（契约 A） | alembic 仍是**单 head** 且 head 值不变；`inspect(engine).get_table_names()` 与 W2 基线集合**完全相同**（无新表）；`candidate` 表列集合不变 |
| T-REG-1 | W2 回归 | `discover_candidates` 仍可用、新建候选 `status == POOLED` 且 `evaluation_context == {}`；`reject` / `repool` 语义不变 |

---

## 8. W3-A 风险（承接 Spec §11.3）

| 级别 | 风险 | 缓解 |
|------|------|------|
| P0-2 | 虚构评分（诱填占位值） | INV-2 + T-EVAL-1b + T-EVAL-8（无门槛 422，不返回 0.5） |
| P0-3 | 幂等 / 并发重放副作用 | §5.3 SAVEPOINT 吸收 + 审计 UNIQUE 键 + T-EVAL-4 / T-EVAL-5 |
| P1 | 半状态残留 | §5.2 显式回退 + T-EVAL-3 |
| P2 | 除零 / 边界数值 | §4 显式分支 + T-EVAL-1d |
| P2 | 重评需 2 步（D2） | 记录为 Open Question Q-A，W3-B 前由 R7 定夺 |
| P2 | W2 测试改写引入回归 | D10（只改 1 个函数，保留原断言）+ T-REG-1 + 全量 pytest |

---

## 9. Open Questions（W3-A 新产生，需 R7 定夺；不阻断本轮实现）

- **Q-A（由 D2 产生）**：`EVALUATED → EVALUATING` 是否应在 W3-B 开边（允许原地重评，审计留痕 attempt+1），还是维持"评估不可变 + reject/repool 重评"？当前选择后者。
- **Q-B**：`capability_fit` 是否应纳入 `Agent.enabled` / `Agent.status`（非 `AgentCapability.enabled`）？W3-A 只做 `get_agent` 存在性校验，不把 Agent 停用当作评分为 0 的门。若 agent 在 pool 后被停用，W3-A 仍会评估它——是否可接受？
- **Q-C**：`evaluation_error.message` 截断 500 字符是否足够？（审计脱敏后存储，过长会撑大 JSON）

---

## 10. W3-A Gate（实现完成后）

按 Spec §10.2 执行：**DSH 独立审计**（路径③自包含 prompt：exact-head SHA + 7 契约 A–G）+ **R7 对 exact-head SHA 显式授权** + **AI 绝不自动 merge**。

W3-A 专属追加校验项（供审计员直接核对）：
1. `git diff --stat` 仅触及 `models.py`（枚举）、`workforce.py`（状态机 + 服务）、2 个测试文件；**无 alembic 变更**。
2. `CandidateStatus` 三态已提升，`RECOMMENDED` 入边/出边均为空集。
3. `evaluation_context` 中四类 unknown evidence **无数值 score**（grep 级可验证）。
4. `discover_candidates` 的 P2-1 SAVEPOINT 逻辑**逐字未改**（diff 级可验证）。
5. alembic `heads` 输出与 W2 基线一致（单 head）。
6. 全量 `pytest` 串行绿 + `ruff check` clean。

---

*本文件为 W3-A 实现设计；未修改任何生产代码 / migration / 测试，未 commit / push / PR。*
