# W3-C Spec V3 — DSH 第二轮独立审计报告

> **审计性质**：独立复审（**不继承上一轮任何结论**，全部判据重新对冻结代码逐行取证）
> **审计对象**：`docs/Workforce_W3C_Recommendation_Approval_Spec_V3.md`（870 行）
> **审计人**：DSH 路径③ 等价独立审计
> **日期**：2026-09-02

| 项 | 值 |
|---|---|
| 基线分支 | `w3b-match-benchmark` |
| HEAD | `6c08d044d2049f7fb48467e2e89c0b30baecba75` |
| tree | `c837d78a6ce28474adb045b4e0cd1907c3cf70b0`（= W3-B exact tree） |
| `git diff -- src alembic tests` | **空** ✅ |
| 本轮裁决 | D-R1=YES（引入 `purge_recommendation`）／D-R2=YES（`recommendation.reproposed`）／D-R3=YES（downgrade 不重置） |
| 本轮结论 | **NO-GO** — 差 2 项必裁 + 1 项知情 + 1 项建议 |

---

## 0. 审计结论速览

| 契约 | 判定 | 主因 |
|---|---|---|
| **A** 单 head / additive / 可逆 | **CONDITIONAL PASS** | 设计正确；待 C3② 执行 + revision 日期钉死 |
| **B** 状态机边界 | **CONDITIONAL PASS** ⚠️ | **P1-1**：C3 测试调整范围缺第 4 类（硬阻塞） |
| **C** downgrade 完整性 | **PASS** | D-R3=YES 与 §12.5 默认一致 |
| **D** SSoT 零新能力词 | **PASS** | 复用清单逐条核实，无重写 |
| **E** 契约测试 | **FAIL → CONDITIONAL** | **P1-1 / P1-2 / P1-3** 三项未闭环 |
| **F** fail-closed 语义 | **CONDITIONAL PASS** ⚠️ | **P1-2** 类型冲突破口；P2-1/P2-2 纵深缺口 |
| **G** 可解释评分 + 审批留痕 | **CONDITIONAL PASS** | 5 个 action 均可触发 ✅；P2-3 内部矛盾 |

**问题计数：0 × P0 ／ 3 × P1 ／ 7 × P2 ／ 5 × P3**

> **0 个 P0**：无安全漏洞、无设计根本错误、无虚构评分路径、无绕过人类闸的通路。
> V3 相对 V2 是**实质性进步**——V2 的 4 项 P1 中，C1/C2/C4 已真正闭环，C3 闭环但**范围不全**（本轮新发现 P1-1）。

---

## 1. 独立代码复核：V3 前置假设逐条验证

**方法**：不采信 Spec 自述，全部重新对源码取行号证据。

| # | V3 假设 | 复核证据 | 判定 |
|---|---|---|---|
| 1 | `CandidateStatus.RECOMMENDED` 已是真实枚举 | `models.py:1570` | ✅ |
| 2 | docstring 自承"deliberately left UNREACHABLE" | `models.py:1556-1559` | ✅ |
| 3 | `candidate.status` 是 `sa.String()`，非 DB ENUM | `models.py:1560-1562` 注释 | ✅ |
| 4 | `ALLOWED[RECOMMENDED] = set()` | `workforce.py:479` | ✅ |
| 5 | `ALLOWED[EVALUATED] = {REJECTED}` | `workforce.py:476` | ✅ |
| 6 | `Match` UNIQUE(candidate_id, job_version_id) | `models.py:1764-1770` | ✅ |
| 7 | `Match.breakdown["excluded"]` 三元素 | `workforce.py:1535-1539` | ✅ |
| 8 | `Match.evidence_refs[0] = "cand:{id}:attempt:{n}"` | `workforce.py:1543` | ✅ |
| 9 | 有 `br:{id}` 仅当 trusted_result 存在 | `workforce.py:1545` | ✅ **（引出 P2-3）** |
| 10 | `evaluation_context.recommendation_blocked_reason` | `workforce.py:877-880` | ✅ |
| 11 | 三维度 `status` = unknown/future_capability，且**只有 status+reason** | `workforce.py:862-874` | ✅ |
| 12 | `_attempt_from_evidence_refs()` 存在且可返回 `None` | `workforce.py:1398-1412` | ✅ **（引出 P1-2）** |
| 13 | `compute_match` 新 attempt 时 in-place UPDATE 且不通知 | `workforce.py:1555-1585` | ✅ |
| 14 | `new_id` 不含 `:` | `models.py:29-30` 实测 | ✅ |
| 15 | `PRAGMA foreign_keys=ON` 真实生效 | `db.py:43-48`；`timeout=30` | ✅ |
| 16 | 无 `recommendation` / `trial` / `employee` 表 | 全仓 grep 仅命中 `recommendation_blocked_reason`（`workforce.py:875/878/1030`） | ✅ |
| 17 | `ApprovalStatus` = PENDING/APPROVED/REJECTED/**EXPIRED** | `models.py:190-196` | ✅ |
| 18 | `RiskLevel` L0..L4 | `models.py:198-203` | ✅ |
| 19 | `_assert_owner_actor` 校验 `kind=="owner" and owner_id` | `actor.py:79-95` | ✅ |
| 20 | `append_audit(project_id, task_id)` 可复用 | `audit.py:110-118` | ✅ |
| 21 | 12 个测试文件携带 head 常量，全为单 head 断言 | 实测 12 文件／16 处，无一用作 downgrade 目标 | ✅ |
| 22 | downgrade 目标 `20260827_0002…` 仅 w3b:704/:721 | 实测确认 | ✅ |
| 23 | C3① 落点 `w3a:694-698`（函数起于 :675） | 实测 `deferred` tuple 在 :694-698，函数名 :675 | ✅ |
| 24 | C3③ 落点 `models:611`（函数起于 :555） | 实测 :555 函数、:610 注释、:611/:612/:613 tuple、:625 自环成员、:632 cascade 测试 | ✅ **精确命中** |
| 25 | W3-C 拟新增符号零冲突 | `RecommendationStatus`/`purge_recommendation`/`assert_trial_eligible`/`decide_recommendation`/`_reconcile_drift`/`_transition_status`/`_sync_candidate_back` 全为 0 命中 | ✅ |

**新增发现（V3 未记载）**

| # | 新事实 | 证据 | 影响 |
|---|---|---|---|
| N-1 | `test_workforce_evaluation_w3a.py:746` 对 `ALLOWED[EVALUATED]` 做**全等断言** `== {REJECTED}`，位于 `test_w2_discovery_and_lifecycle_semantics_unchanged`（起于 :720） | 实测 | **P1-1** |
| N-2 | `compute_match` 强制 `cand.status == EVALUATED`，否则 **422** | `workforce.py:1448-1452` | **P1-3** |
| N-3 | `rank_candidates` 过滤 `Candidate.status == EVALUATED` | `workforce.py:1655` | **P2-4** |
| N-4 | RESTRICT 在 `models.py` 已有 **6 处**先例（:1148/1211/1319/1327/1532/1727） | 实测 | P3-2 |
| N-5 | `audit.py:101` 存在**值级**脱敏 `_SECRET_VALUE_RE`（含 `sk-[A-Za-z0-9]{8,}`） | `audit.py:37-49` + 实测 | P3-1 |
| N-6 | `append_audit` **不强制** `idempotency_key` 唯一（仅落库） | `audit.py:129-130` | 佐证 P2-1 |
| N-7 | `rank_candidates` docstring 明写"materialize a ranking snapshot; that is W3-C's job" | `workforce.py:1649` | P3-3 |
| N-8 | W3-B 既有缺陷：in-place UPDATE 先改 `existing.status` 再写审计 ⇒ `match.recomputed` 的 `before.status == after.status` 恒等 | `workforce.py:1568` vs `:1579` | P3-5 |

**实测记录（脱敏行为，推翻 V3 §9 注记）**

```
{'message': 'boom sk-live-abc123'}                  -> 未脱敏（连字符打断 sk-[A-Za-z0-9]{8,}）
{'api_key': 'sk-live-abc123'}                       -> '[REDACTED]'（key 名命中）
{'rationale': 'cost advisory: ... sk-abcdef123456'} -> '[REDACTED]'（值级模式命中）
{'note': 'plain text no secret here'}               -> 未脱敏
```

---

## 2. 七契约逐条判定（A–G）

### A · 单 head / additive / 可逆 — **CONDITIONAL PASS**

| 判据 | 结果 |
|---|---|
| revision 已钉死 `20260902_0001_workforce_recommendation` | ✅（P3-1 已修） |
| down_revision = `20260901_0001_workforce_match_benchmark`，单 head | ✅ |
| upgrade 仅 create_table + 2 index，零 alter 既有表 | ✅ |
| downgrade 完全对称（drop_index ×2 + drop_table） | ✅ |
| C3② 12 文件 head 常量同步 | ⏳ 设计已列全（实测 12 文件 16 处，无一误用为 downgrade 目标），**待执行** |

**条件**：实现首日 ≠ 2026-09-02 时须同步改 revision（V3 §12.1 已自注 ⚠️）；建议在实现首日钉死后不再变更。

### B · 状态机边界 — **CONDITIONAL PASS**（挂 P1-1，硬阻塞）

| 判据 | 结果 |
|---|---|
| W3-A/W3-B 既有边零改动 | ✅ `ALLOWED` 仅 `EVALUATED` +1、`RECOMMENDED` set()→{EVALUATED} |
| `ALLOWED[RECOMMENDED]` 不含 TRIALING | ✅ |
| `RECOMMENDATION_ALLOWED` 与 §5.2 转移表逐边一致 | ✅ 7 条边 + 2 条终态，无冲突残留 |
| V2 冲突定义已统一 | ✅ `recomputed` 已删、§8「直接改 PROPOSED」已删 |
| C3③ 只删 1 个 tuple | ✅ 实测 `RECOMMENDED` 全仓仅 `test_workforce_models.py` 一处，无遗漏 |
| **C3 是否覆盖全部受冲击测试** | ❌ **缺第 4 类 → P1-1** |

**P1-1 证据链**：
```python
# tests/test_workforce_evaluation_w3a.py:746-748
# 函数：test_w2_discovery_and_lifecycle_semantics_unchanged（起于 :720）
assert CandidateLifecycle.ALLOWED[CandidateStatus.EVALUATED] == {
    CandidateStatus.REJECTED
}
```
- W3-C 令 `ALLOWED[EVALUATED] = {REJECTED, RECOMMENDED}` ⇒ **全等断言必然失败**。
- V3 §13.8 三类（:694-698 / 12 文件 head / :611）**均不包含此行**；全仓 grep `746` 在 Spec 内 **0 命中**。
- 该函数名含 `semantics_unchanged`，属 W2 语义守卫，修正它**超出 Q2/C3 现有授权字面范围**。
- 后果：实现者将被迫在「违反 §15.2 #18（禁改 W3-A 测试）」与「停滞」之间二选一。

### C · downgrade 完整性 — **PASS**

| 判据 | 结果 |
|---|---|
| downgrade 后 `recommendation` 表 + 2 索引消失 | ✅ 对称 |
| W3-A/W3-B 表与数据不受影响 | ✅ 无 alter 既有表 |
| 不修改 `candidate` 数据 | ✅ 与 D-R3=YES 一致 |
| 残留风险已留痕 | ✅ §12.5 明示 + 「代码与迁移同进同出」缓解 |

**D-R3 闭环判定：✅ 闭环**（裁决 YES 与 §12.5 默认"不重置"完全一致）。
唯一改进项：**P2-5** —— 残留后果描述不完整（见 §3）。

### D · SSoT 零新能力词 — **PASS**

| 判据 | 结果 |
|---|---|
| 无新 Capability 行 | ✅ 测试 28 覆盖 |
| 无第二套能力词汇 | ✅ 只读 `capability_evidence`，不重算 |
| 未重造 Scheduler / Execution / Budget / Audit | ✅ §6.3 全部 import 复用 |
| 未重造 Approval 判定 | ✅ 复用 `ApprovalStatus`/`RiskLevel`/`_assert_owner_actor`；**未借用 `create_approval` / `owner_inbox`**（正确，二者需 Project） |
| 未新增 Capability 枚举/表 | ✅ |

### E · 契约测试 — **FAIL → CONDITIONAL**

46 项清单（30 基础 + RECONCILE6 / SOT3 / CASCADE4 / ACTOR3）**结构完整、分组清晰、可字面断言**，较 V2 显著改进。但存在三项未闭环：

| 问题 | 影响 |
|---|---|
| **P1-1** | C3 缺第 4 类 ⇒ 测试 1（受控转换）落地即红 |
| **P1-2** | `match_attempt: int` 非空 vs 解析器可返回 `None` ⇒ 无对应测试，且缺 F-R3b 规则 |
| **P1-3** | 测试 31/34/35/36 依赖的漂移场景在生产不可达，Spec 未说明须数据层注入 |

**覆盖缺口（P3-4，非阻塞）**：
- F-R1c（重建路径要求 `Candidate.status == EVALUATED`）无独立测试
- §4.2 四字段非空门槛中，`evaluated_fields` / `excluded_fields` 空值无 422 测试（仅 breakdown/evidence_refs 有）
- §16.5「reconcile 不调用 `commit`」无测试
- §9「审计统一 `project_id=None, task_id=None`」无断言

### F · fail-closed 语义 — **CONDITIONAL PASS**

**已确认闭环（重新验证，非继承）**

| 项 | 结果 |
|---|---|
| 三种漂移判据是否全部撤销 | ✅ `detected != stored` / 解析返回 `None` / Match 行缺失 —— §16.1 三条明确，全部 `drifted=True` |
| 活状态集合 | ✅ 仅 `{PROPOSED, APPROVED}`；`WITHDRAWN`/`REJECTED` 遇漂移零写入 ⇒ 幂等 |
| `actor` 必填 | ✅ §6.2 无默认值；§6.3 移除 `resolve_owner_actor`；§15.2 #17 禁止；测试 44/45 |
| 无自动绕过 Approval | ✅ `PROPOSED→APPROVED` 唯一执行者 `decide_recommendation`，首行 `_assert_owner_actor`（INV-6） |
| `REJECTED` 终态 | ✅ `REJECTED → {}`；新证据不自动复活 |
| F-R10 / DR-1…DR-5 | ✅ RESTRICT 真实强制（`db.py:43-48` `PRAGMA foreign_keys=ON` + `timeout=30`） |
| purge 不绕过审批语义 | ✅ 前置 `status ∈ {WITHDRAWN, REJECTED}`，`PROPOSED`/`APPROVED` → 409（DR-3） |

**未闭环**

| 问题 | 性质 |
|---|---|
| **P1-2** | `match_attempt: int`（`§4.1` 非空）与 `_attempt_from_evidence_refs() -> int | None` 类型冲突 ⇒ `NOT NULL` 违反 → 未捕获 `IntegrityError`（500），**而非 fail-closed 422** |
| **P2-1** | CAS 未绑定观测值 `match_attempt` ⇒ 陈旧读可误撤新重建的**无漂移**推荐 |
| **P2-2** | F-R6 未校验 `Candidate.status == RECOMMENDED` |

### G · 可解释评分 + 审批留痕 — **CONDITIONAL PASS**

| 判据 | 结果 |
|---|---|
| `breakdown`/`evidence_refs`/`excluded_fields`/`unknown_dimensions` 强制非空 | ✅ §4.2 + F-R3 |
| `decided_by` 必为 owner | ✅ INV-3 / INV-6 / 测试 23 |
| 5 个审计 action 齐备**且均可真实触发** | ✅ **`withdrawn` 已由 `_reconcile_drift` 承担执行主体**（V2 P1-1 已修复） |
| `before` 含 `decided_by/decided_at/decision_rationale` | ✅ P2-2 已修 |
| 与既有 action 无命名冲突 | ✅ W3-B 为 `match.recomputed`/`match.computed`（resource_type=`match`），与 `recommendation.*` 无碰撞 |

**未闭环**：**P2-3**（§9 evidence 链「任一环缺失 → F-R3 拒绝」与 unbound JobVersion 无 `br:` ref 矛盾）。

---

## 3. 问题清单（P0 / P1 / P2 / P3）

### P0 — 无

### P1（必须修复，阻塞授权）

#### **P1-1 · C3 测试调整范围缺第 4 类**
- **位置**：`tests/test_workforce_evaluation_w3a.py:746`（函数 `test_w2_discovery_and_lifecycle_semantics_unchanged`，起于 :720）
- **事实**：`assert CandidateLifecycle.ALLOWED[CandidateStatus.EVALUATED] == {CandidateStatus.REJECTED}` —— 全等断言。W3-C 加 `RECOMMENDED` 后必红。
- **性质**：**授权范围缺陷**（非设计缺陷）。V3 §13.8 三类未含此行；Spec 内 grep `746` 零命中。
- **修复建议**：C3 扩为四类，新增
  > **C3④** `tests/test_workforce_evaluation_w3a.py:746-748`：断言改为 `== {REJECTED, RECOMMENDED}`；同步更正 :745 上下文注释。
- **为何必须**：否则实现者要么违反 §15.2 #18，要么停滞。

#### **P1-2 · `match_attempt` 类型冲突 —— fail-closed 破口**
- **事实**：
  - `§4.1` 定义 `match_attempt: int`（**非空**）
  - `_attempt_from_evidence_refs()` 返回 `int | None`（`workforce.py:1398-1412`）
  - F-R3 只校验 `evidence_refs` **非空**，未校验**可解析**
- **后果**：`evidence_refs` 非空但格式异常（如 `["weird-ref"]`）→ 解析返回 `None` → 写入非空 `int` 列 → **`IntegrityError` 未捕获 → 500**，而非契约要求的 422。这是**字面违反 F-R3 fail-closed**。
- **修复建议**（二选一）：
  - **方案 1（推荐）**：新增 **F-R3b** —— `_attempt_from_evidence_refs(Match.evidence_refs) is None` → `422 "match evidence is not resolvable"`。保持列非空。
  - **方案 2**：列改 `int | None`，把 `None` 定义为"恒漂移"（每次 reconcile 都撤销）。**不推荐** —— 会污染 `match_attempt` 语义。
- **配套**：增补测试「evidence_refs 格式异常 → 422，且不产生 Recommendation 行」。

#### **P1-3 · F-R8 主时序在冻结代码下不可达（结构性事实，需知情）**
- **证明链**（全部实测）：
  1. `compute_match` 强制 `cand.status == EVALUATED`，否则 **422**（`workforce.py:1448-1452`）
  2. 活推荐（`PROPOSED`/`APPROVED`）⇒ `Candidate.status == RECOMMENDED`（W3-C 是唯一提升点，`A37` 全仓确认无其他写入）
  3. bump `attempt` 需 `evaluate_candidate` 走 `EVALUATING → EVALUATED`（`workforce.py:1012`）
  4. 进入 `EVALUATING` 需 `require_transition(status, EVALUATING)`（`workforce.py:917-918`）；`RECOMMENDED → EVALUATING ∉ ALLOWED[RECOMMENDED]={EVALUATED}` ⇒ **409**
  5. ⇒ **死锁**：漂移需重算 → 重算需回到 EVALUATED → 回到 EVALUATED 需撤销 → 撤销需漂移
- **后果**：
  - V3 **§16.2 的 t2 步骤字面不可执行**（"W3-B `compute_match(c1)`" 在 Candidate=RECOMMENDED 时 422）
  - 测试 31/34/35/36 **只能由 fixture 在数据层注入漂移**（直接改写 `Match.evidence_refs` / 删除 Match 行）
  - **F-R8 在 W3-C V1 为纯纵深防御**，真实触发概率 ≈ 0（价值在 W3-D/W4 引入变更路径后）
- **风险**：实现者若照 §16.2 字面写测试会撞 422 → 调试返工；更糟的是可能去放宽 `compute_match`（**违反冻结**）。
- **修复建议**：
  - 更正 §16.2：t2 改为「**由测试 fixture 在数据层注入**新 attempt（或删除 Match 行）」，并加注冻结代码为何不可达
  - 明示 F-R8 在 W3-C V1 的定位 = 纵深防御（不为 V1 生产路径）
- **注意**：这**不是**安全缺陷 —— 冻结 `compute_match` 的 422 守卫本身已经堵死了「陈旧批准」危害，F-R8 是第二道锁。

### P2（应修复，建议与 P1 一并裁决）

| ID | 问题 | 证据 | 建议 |
|---|---|---|---|
| **P2-1** | **CAS 未绑定观测值**。CAS 为 `WHERE id=? AND status IN ('proposed','approved')`，不含 `match_attempt`。并发下 P2 持陈旧读（detected=3）可在 P1 撤销并重建（attempt=4）后命中新的 `PROPOSED` 行，撤销一个**实际无漂移**的推荐，并写误导性 `reason=match_attempt_drift` 审计 | §16.2 :763 / §16.3 :799；`append_audit` 不强制 idempotency 唯一（`audit.py:129`） | CAS 改为 `WHERE id=? AND status IN (...) AND match_attempt = <observed>`，构成严格 compare-and-swap |
| **P2-2** | **F-R6 未校验 Candidate 状态**。`assert_trial_eligible` 仅判 `status==APPROVED && decided_by`，不校验 `Candidate.status == RECOMMENDED`。带外数据不一致时仍返回 True | §7 F-R6 / §13 测试 11 | 增列 `AND candidate.status == RECOMMENDED`（纵深防御；W3-C 自身路径已保持一致，非活跃 fail-open） |
| **P2-3** | **§9 evidence 链自相矛盾**。"必须能回溯 `cand:…` → `br:…`（若有）→ `match:…`。**任一环缺失 → F-R3 拒绝**"。但 `br:` 仅当 trusted benchmark 存在时 append（`workforce.py:1545`）⇒ **unbound JobVersion 下无 `br:`** ⇒ 按字面会拒绝所有 unbound 匹配，与 W3-B 的 waive 语义直接冲突 | §9 :445-446 | 改为：`cand:` + `match:` 为**必需**，`br:` 为**条件性**（bound 且有 trusted result 时必需）；F-R3 只判必需环 |
| **P2-4** | **`rank_candidates` 过滤 `status == EVALUATED`**（`workforce.py:1655`）。W3-C 将候选提升为 `RECOMMENDED` 后，该候选**从 W3-B 排序结果中消失**。这是冻结函数**可观察行为**的改变（非代码改动），V3 全文 4 处提及 `rank_candidates` 但**无一处涉及此过滤** | 实测 | 书面留痕：① 提升后退出排序是预期行为；② **W4 不得依赖 `rank_candidates` 查找已批准候选**，必须读 `Recommendation`；③ 若未来需"含已推荐的完整排序"，属 W3-D/W4 新 API |
| **P2-5** | **D-R3 后果描述不完整**。downgrade 后残留 `status='recommended'` 的 Candidate 不止"不可解释"，而是**永久不可操作**：`compute_match` 422、`evaluate_candidate` 409（RECOMMENDED→EVALUATING 非法）、`rank_candidates` 过滤掉 | §12.5 | 保持"不重置"（与 D-R3=YES 一致），补全后果三段式描述，并给出运维解锁语句（`UPDATE candidate SET status='evaluated' WHERE status='recommended'`）作为**需 R7 单独授权**的数据变更 |
| **P2-6** | **`purge_recommendation` 无 actor 要求**。可清除 `REJECTED`（人类终态），使"REJECTED 终态"不变量可被运维绕过。人类闸本身未破（重建仍需审批），但删除动作无身份留痕 | §5.2 / §8 / §15.1 | 建议 `purge_recommendation(..., *, actor: ActorContext)` 并要求 owner；审计 `actor` 落 owner_id |
| **P2-7** | **不变量编号自相矛盾**：§0 表写"INV-1…INV-5"，§4.5 与 §17.1 实为 **INV-1…INV-6** | §0 :27 vs §4.5 :252-261 | 统一为 INV-1…INV-6 |

### P3（提示，不阻塞）

| ID | 问题 | 证据 |
|---|---|---|
| **P3-1** | **§9 脱敏注记与实测不符**。V3 称"只按 key 名脱敏，不对字符串值做模式匹配"——**错误**。`audit.py:101` 存在值级 `_SECRET_VALUE_RE`（`audit.py:37-49`，含 `sk-[A-Za-z0-9]{8,}`）。实测：`{'rationale':'… sk-abcdef123456'}` **被值级脱敏**；而 `{'message':'boom sk-live-abc123'}` **未脱敏**（连字符打断 `[A-Za-z0-9]{8,}`）。⇒ 结论（禁止在自由文本中拼接凭证）正确，但**理由错误**，且"必须注入匹配 SECRET_KEYS 的 key"的指引过严，会误导测试设计 | 实测 + `audit.py:37-49/101` |
| **P3-2** | RESTRICT 并非新颖策略：`models.py` 已有 6 处（:1148/1211/1319/1327/1532/1727）。V3 §1.2 称 RESTRICT 是"这条下行链的第一个非级联依赖"——就该链而言正确，但易被误读为全局首创 | 实测 |
| **P3-3** | `rank_candidates` docstring 明写 "materialize a ranking snapshot; **that is W3-C's job**"（`workforce.py:1649`），V3 未建 ranking 快照表（每候选快照落在 `recommendation` 行内）。需在 Spec 中显式裁定为「已由 `recommendation` 行快照满足」或「递延 W3-D」，避免后续审计判为丢契约 | `workforce.py:1649` |
| **P3-4** | 测试覆盖小缺口：F-R1c 重建前置、`evaluated_fields`/`excluded_fields` 空值 422、reconcile 不调 `commit`、审计 `project_id is None` 断言 | §13 |
| **P3-5** | W3-B 既有缺陷（**冻结，非 W3-C 引入**）：in-place UPDATE 先 `existing.status = status`（:1568）再写审计（:1576-1582），导致 `match.recomputed` 的 `before.status` 与 `after.status` 恒等。W3-C 只读 `Match.status` 不受影响，但审计消费方需知情 | `workforce.py:1568/1579` |

---

## 4. D-R1 / D-R2 / D-R3 闭环判定

| 裁决 | 判定 | 核验证据 | 附注 |
|---|---|---|---|
| **D-R1 = YES** 引入 `purge_recommendation` | ✅ **闭环** | §5.2 转移表含 `* → (行删除)`；§7 F-R10 / §12.4 DR-2/DR-3；§13.6 测试 40–43；§15.1 列入 4 公开函数 | **未绕过 RESTRICT/审批语义**：前置 `status ∈ {WITHDRAWN, REJECTED}`，`PROPOSED`/`APPROVED` → 409；审计 `recommendation.deleted` 全量快照保证据链不断。仅 **P2-6**（无 actor 要求）待补 |
| **D-R2 = YES** 审计 action = `recommendation.reproposed` | ✅ **闭环** | §9 表统一为 5 个 action；§13 测试 18/35；§14-G 判据；`recomputed` 仅在"已删除"上下文出现（§0:43 / §5.3:322 / §16.6:828） | 与 W3-B `match.recomputed` **无冲突**（`resource_type` 分别为 `recommendation` / `match`） |
| **D-R3 = YES** downgrade 不重置 Candidate status | ✅ **闭环** | §12.5 默认"仅 drop 表与索引，不执行 UPDATE"，与裁决字面一致；残留风险已留痕 | 仅 **P2-5**（后果描述不完整）待补 |

---

## 5. 重点审计项逐条回答（对应用户 14 项）

| # | 审计问题 | 结论 |
|---|---|---|
| 1 | C1–C4、P2-1 是否真正闭环 | **C1 ✅ / C2 ✅ / C4 ✅ / P2-1 ✅ 真正闭环；C3 ❌ 范围不全（P1-1）** |
| 2 | F-R8 lazy reconcile 是否真正 fail-closed | ✅ **逻辑上真正 fail-closed**（三种判据全部撤销、活状态单向迁移、CAS+SAVEPOINT）；⚠️ 但**触发条件在生产不可达**（P1-3） |
| 3 | `detected=None / Match 缺失 / attempt 漂移` 是否全部撤销 | ✅ **全部撤销**，§16.1 三条明确，reason 分别为 `attempt_unresolvable` / `match_missing` / `match_attempt_drift` |
| 4 | CAS + SAVEPOINT + 审计能否保证并发下最多一次 `withdrawn` | ⚠️ **基本保证，但有缺口**：CAS 未绑定 `match_attempt`（P2-1）→ 恰好一次"撤销动作"成立，但可能撤销**错误的对象**并写误导审计。修复后完全成立 |
| 5 | Recommendation `status` 是否成为唯一 SoT | ✅ **是**。`approval_status` 已删（INV-1 物理层）；`ApprovalStatus` 降级为输入词汇；六条不变量覆盖 DB/类型/服务/状态机/审计/执行者六层 |
| 6 | `ApprovalStatus` 是否仅作决策输入，且无隐式自动审批 | ✅ **是**。白名单 `{APPROVED, REJECTED}`，`PENDING`/`EXPIRED` → 422（测试 39）；`actor` 无默认值 + 禁 `resolve_owner_actor`（测试 44/45）；`EXPIRED` 明确排除 |
| 7 | `purge_recommendation` 是否会绕过 RESTRICT / 审批语义 | ✅ **不会**。前置 `status ∈ {WITHDRAWN, REJECTED}` ⇒ 无法清除活推荐；审计全量快照保留证据。仅 P2-6（建议加 owner actor） |
| 8 | Candidate ⇄ Recommendation 状态机是否隐含 fail-open | ⚠️ **无活跃 fail-open**，但 **P2-2**：F-R6 未校验 `Candidate.status == RECOMMENDED`，带外不一致时仍返回 True（纵深防御缺口） |
| 9 | `match_id RESTRICT` 与既有 CASCADE 链是否一致 | ✅ **一致**。DR-4 明示"既有 CASCADE 链零改动"；RESTRICT 仅 `recommendation.match_id` 一处；`db.py:43-48` + `timeout=30` 保证真实强制；测试 40–43 覆盖四种情形。RESTRICT 在库内已有 6 处先例（P3-2 表述问题） |
| 10 | W3/W4 边界是否仍然严格 | ✅ **严格**。止于 `RECOMMENDED`；无 `TRIALING`；无 Trial/Employee/Training/Performance 表或列；Budget/Scheduler/Execution 零调用；`approval_id` 仅为前向列 |
| 11 | 46 项测试是否覆盖所有可字面断言 | ⚠️ **结构完整但有 3 项缺口**：P1-1（C3 缺第 4 类）、P1-2（`match_attempt=None` 无测试）、P3-4（4 处小缺口） |
| 12 | 19 条禁止事项是否全部无越界 | ✅ **全部无越界**。§15.3 复检表逐项核对；V3 新增的 reconcile 写 status / `_sync_candidate_back` 写 Candidate.status / purge 删行 / RESTRICT 均落在新建表与受控边例外内，与 19 条无冲突 |
| 13 | 是否仍有"Spec 写了但冻结代码没有执行主体" | ✅ **V2 的 P1-1 已修复**（`_reconcile_drift` 是 `withdrawn` 的真实执行主体）；❌ 但**发现反向问题 P1-3** —— 执行主体有了，但**触发条件（attempt 漂移）在冻结代码下没有生产者** |
| 14 | 是否存在新的 P0/P1/P2/P3 | **0 P0 / 3 P1（P1-1、P1-2、P1-3）/ 7 P2 / 5 P3** |

---

## 6. 是否达到 R7 exact-head 授权条件

> # ❌ **NO-GO —— 不具备授权条件**
>
> 差 **2 项必裁**、**1 项知情追认**、**1 项建议**。

| # | 待裁项 | 性质 | 建议 |
|---|---|---|---|
| **C5** | **追认测试调整范围由三类扩为四类**：新增 **C3④** `tests/test_workforce_evaluation_w3a.py:746-748`（ALLOWED 全等断言改 `== {REJECTED, RECOMMENDED}` + :745 注释） | 🔴 **必裁**（授权范围扩展，不得类推） | GRANT |
| **C6** | **`match_attempt` 空值处理**：新增 **F-R3b** —— `_attempt_from_evidence_refs()` 返回 `None` → `422 "match evidence is not resolvable"`（保持列非空），并增补 1 项测试 | 🔴 **必裁**（fail-closed 语义破口） | 采用方案 1 |
| **C7** | **知情追认 F-R8 定位**：确认其在 W3-C V1 为**纵深防御**（生产不可达）；§16.2 时序须更正为"测试 fixture 数据层注入漂移"；测试 31/34/35/36 据此实现 | 🟡 **知情项**（不改设计，影响实现与测试写法） | 知情确认即可 |
| **C8** | **CAS 绑定观测值**：`WHERE id=? AND status IN (...) AND match_attempt = <observed>`（P2-1） | 🟢 **建议**（可与实现授权合并） | 建议一并裁 |

**不阻塞、可在实现期顺带处理**：P2-2 / P2-3 / P2-4 / P2-5 / P2-6 / P2-7 + 全部 P3。
（P2-3 例外：**若实现者按 §9 字面实现 evidence 链校验，将拒绝所有 unbound 匹配** —— 建议至少在实现前口头确认，否则易造成返工。）

**裁决后无需重做设计** —— V3 的架构、状态机、数据模型、迁移策略均已扎实，上述 4 项均为**局部增补**，可直接进入 TDD 实现。

---

## 7. 审计约束遵守声明

本轮**未修改 Spec、未改代码、未写 migration、未写测试、未 commit / push / PR**。

- 新增文件仅本审计报告（`.md`），为**审计产物**，不纳入 W3-C 交付内容
- 工作树 `src/` `alembic/` `tests/` 相对 HEAD **零改动**
- 基线复核：`HEAD=6c08d04`、`tree=c837d78a…`，未漂移

> 唯一副作用：为验证 `new_id` 格式与脱敏行为执行了两段只读 Python 片段（无文件写入、无数据库落盘）。
