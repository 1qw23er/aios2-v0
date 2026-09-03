# Workforce W3-C — Recommendation / Approval Spec V4

> **阶段性质：纯设计（Design-Only）**
> 本轮**不写实现代码、不写 migration、不写测试代码、不 commit / push / PR**。
> 本文件**取代** `docs/Workforce_W3C_Recommendation_Approval_Spec_V3.md`（V3 经 DSH 第二轮独立审计判为 **NO-GO**，V4 按 C5–C8 + P2-3 裁决修订）；V3 **不删除**，保留作审计追溯。
> V1 / V2 / V3 **不删除**，保留作审计追溯。

| 项 | 值 |
|---|---|
| 基线分支 | `w3b-match-benchmark` |
| W3-B exact-head | `6c08d044d2049f7fb48467e2e89c0b30baecba75` |
| W3-B exact tree | `c837d78a6ce28474adb045b4e0cd1907c3cf70b0` |
| W3-B landed commit（main） | `0a72c45597f0ff817bdf5e6df7bb8f22fb456d1e` |
| 当前 alembic 单 head | `20260901_0001_workforce_match_benchmark` |
| 本轮范围 | **Recommendation + Approval(L4 人类闸)** |
| 本轮明确排除 | Trial 本体、Employee / Training / Performance、ai-arena、ruleset、Project |
| 审计基线 | `..._V2.md`（0 P0/4 P1/7 P2/4 P3）→ `..._V3.md`（0 P0/**3 P1**/7 P2/5 P3） |
| 本轮裁决 | **C5 / C6 / C7 / C8 已裁**；**P2-3 已明裁**；顺带处理 P2-5 / P2-6 / P2-7 / P3-1 |
| 契约测试 | **53 项**（V3 46 + 本轮新增 7，见 §13.9） |
| 状态 | **V4 已按 C5–C8 + P2-3 裁决修订完成；待最后一次定向 DSH 审计，未获实现授权** |

---

## 0. V3 → V4 修订摘要（**本轮**裁决闭环总表）

| 裁决 | 对应审计项 | V4 修订位置 | 闭环 |
|---|---|---|---|
| **C5** 追认测试调整由三类扩为**四类**（新增 C3④ `w3a:746-748`） | **P1-1** | §0（C3 行同步）/ §5.1 / §13.8（C3④ 行）/ §14 B·E / §15.1 / §15.2 #18 / §17.1 | ✅ |
| **C6** 新增 **F-R3b**（attempt 不可解析 → 422 `match evidence is not resolvable`） | **P1-2** | §4.1（列非空加固说明）/ §7（F-R3b）/ §10.3 / §13.9（测试 47、48）/ §14 F | ✅ |
| **C7** 知情追认 **F-R8 = 纵深防御**（生产不可达；t2/t3 须 fixture 注入；禁改 W3-B） | **P1-3** | **§16.7**（定位专章）/ §16.2（时序改为数据层注入）/ §13.9（测试 51-52 措辞）/ §15.2 #20 | ✅ |
| **C8** 所有 F-R8 CAS 撤销绑定 `status IN (...) AND match_attempt = :stored_match_attempt`（reconcile 读到的旧值作为乐观锁 token） | P2-1（升级为裁决） | §16.1 / §16.2（t3）/ §16.3（并发重写）/ §16.4 #2 / **§16.8 最终契约** / §13.9（测试 49、50） | ✅ |
| **P2-3** 明裁：保留 W3-B unbound waive 语义（`br:` 缺失不构成 F-R3） | P2-3 | **§10.3（unbound waive 承接契约）** / §9（evidence 链二分：必需环 vs 条件环）/ §7 F-R3 注 / §13.9（测试 51、52）/ §15.2 #21 | ✅ |
| **P2-5** D-R3「downgrade 不重置」= 明示设计选择 + 后果三段式 + W4 责任 | P2-5 | §12.5 | ✅ |
| **P2-6** `purge_recommendation(..., *, actor: ActorContext)` 禁止隐式 actor | P2-6 | §5.2 / §6.2 / §7（**F-R11**）/ §8 / §9 / §15.1 / §13.9（测试 53） | ✅ |
| **P2-7** 统一 INV 编号为 INV-1…INV-6 | P2-7 | §0（C2 行）/ §4.5 / §17.1 | ✅ |
| **P3-1** 修正 §9 脱敏注记，以 `audit.py` 实际行为为准（语义不变） | P3-1 | §9 | ✅ |

**本轮未裁决（R7 未列入本轮范围，V4 不擅自改动语义，留痕递延）**：P2-2（F-R6 未校验 Candidate 状态）／P2-4（`rank_candidates` 过滤 EVALUATED 的可观察影响）／P3-2（RESTRICT 表述）／P3-3（`rank_candidates` docstring 的 ranking 快照归属）／P3-4（4 处测试覆盖小缺口）／P3-5（W3-B `match.recomputed` before/after 恒等）。见 **§17.2**。

---

## 0-bis. V2 → V3 修订摘要（**V3 已闭环，V4 继承，本轮无改动**）

| 裁决 | 对应审计项 | V4 修订位置 | 闭环 |
|---|---|---|---|
| **C1** F-R8 执行主体 = 方案 A（惰性 reconcile） | P1-1（原 FAIL→CONDITIONAL） | §5.2 / §5.4 / §8 / §11 / **§16** | ✅ |
| **C2** 删除 `approval_status`，`status` 唯一 SoT | P1-2 | §4.1 / §4.4 / §4.5（不变量 **INV-1…INV-6**）/ §5.2 / §7 / §9 | ✅ |
| **C3** 追认**三类**仅测试调整（V4 的 C5 将其扩为**四类**，见 §0 与 §13.8 C3④） | P1-3（原 FAIL→CONDITIONAL） | §13.8（精确到行号） | ✅ |
| **C4** 保留 `match_id` RESTRICT + 域规则 + 级联测试 | P1-4 | §4.1 / §7（F-R10）/ §12.4 / §13.6 | ✅ |
| **P2-1** `decide_recommendation(actor)` actor 必填 | P2-1 | §6.2 / §6.3 / §13.7 / §15.2 | ✅ |

**D-R1 / D-R2 / D-R3 已由 R7 裁决 YES 并在 V3 落地**（`purge_recommendation` 引入 / action 命名 `recommendation.reproposed` / downgrade 不重置）。V4 §12.5 按 P2-5 补全其后果与责任归属。

**V3 顺带闭环的审计项（来自 V2 审计报告；文档级修订，无需新增授权）**

> ⚠️ **编号消歧**：本节 P2-* / P3-* 为 **V2 审计报告**的编号；本轮裁决的 P2-3 属 **V3 审计报告**（§9 evidence 链矛盾），二者同名不同义，勿混。

| V2 审计项 | V4 处理 | 位置 |
|---|---|---|
| P2-2 审计 before 缺 `decided_by` | 已补齐 `decided_by` / `decided_at` / `decision_rationale` | §9 |
| P2-3 attempt 解析失败静默 fail-open | 解析失败 / Match 行缺失 **视同漂移**（fail-closed）；**V4 追加**：创建路径另由 F-R3b 前置拦截（422） | §7 F-R3b / F-R8 / §16.1 |
| P2-4 `scored` 措辞矛盾 | 澄清为 W3-C **派生守卫标志**，非虚构数据 | §4.3 |
| P2-5 downgrade 是否重置 `candidate.status` | **默认不重置**（保持零数据修改）→ 已由 **D-R3=YES** 裁决 | §12.5 |
| P2-6 `workforce.py` 改动范围未约束 | §15.2 增列 | §15.2 |
| P2-7 L4 闸不在 `owner_inbox`（知情偏离） | 书面留痕 | §6.4 |
| P3-1 revision 含占位符 `2026090X` | **钉死** `20260902_0001_workforce_recommendation` | §12.1 |
| P3-2 契约 E「28 项」与 §13「30 项」不一致 | V3 统一为 46 项；**V4 增补至 53 项**（§13.9） | §13 / §14 |
| P3-3 文件名（R7 指令带 `Trial`，实际无） | 以实际文件为准 | 文件头 |
| P3-4 §5.1「若存在」/ 措辞不准 | 改为「**确实存在**」，精确到行号 | §5.1 / §13.8 |

**D-R1 / D-R2 / D-R3 状态**：R7 已于第二轮审计前裁决 **YES**，V4 中已作为既定设计继承（不再列为待裁）。其中 D-R3 的后果与责任归属按 **P2-5** 补全于 §12.5。

---

## 1. 代码考古结论（只读事实，全部有行号出处）

### 1.1 W3-A / W3-B 已冻结契约

| 事实 | 出处 | 对 W3-C 的含义 |
|---|---|---|
| `CandidateStatus.RECOMMENDED` 已是真实枚举成员 | `models.py:1570-1571` | **无需新增枚举值**，只需放开状态机边 |
| 其 docstring 写 *"deliberately left UNREACHABLE … until the W3-C/D Match gate is implemented"* | `models.py:1571` | V3 正是该 gate；受控解冻须更新此 docstring 表述 |
| `Candidate.status` 是 `sa.String()`，非 DB ENUM | `models.py:1560-1562` | 放开通往 RECOMMENDED 的边 **零 schema 改动** |
| `CandidateLifecycle.ALLOWED[RECOMMENDED] = set()` | `workforce.py:479` | RECOMMENDED 当前零入零出边；W3-C 受控加边 |
| `CandidateLifecycle.ALLOWED[EVALUATED] = {REJECTED}` | `workforce.py:476` | W3-C 只加 `RECOMMENDED` 一个目标 |
| `Match` UNIQUE(candidate_id, job_version_id) | `models.py:1769-1775` | Recommendation 幂等键同构 |
| `Match.breakdown` 含 `excluded: [reliability, historical, cost]` | `workforce.py:1535-1539` | W3-C 必须原样继承，不得改写 |
| `Match.status = BLOCKED` + `match_blocked_reason="capability_gap"` | `workforce.py:1502-1504` | W3-C 的**硬拒绝信号** |
| `Match.evidence_refs[0] = "cand:{id}:attempt:{n}"` | `workforce.py:1543` | F-R8 漂移判据的数据源 |
| `evaluation_context.recommendation_blocked_reason` | `workforce.py:877-880` | W3-A 已预留的前向契约，W3-C 应二次校验 |
| `evaluation_context.{reliability,historical}_evidence.status = "future_capability"` | `workforce.py:867-874` | 必须保持 unknown，禁止转数值 |
| `evaluation_context.cost_evidence.status = "unknown"` | `workforce.py:862-866` | cost 仅 advisory |
| `_attempt_from_evidence_refs(refs)` 已存在 | `workforce.py:1398-1412` | W3-C **复用**解析 attempt，不重写；解析失败返回 `None` |
| `compute_match` 幂等 = SAVEPOINT + IntegrityError 吸收 | `workforce.py:1617-1637` | W3-C 复用同一模式 |
| ⚠️ `compute_match` 新 attempt 时 **in-place UPDATE Match 且不通知 Recommendation** | `workforce.py:1560-1585`（**已冻结**） | **F-R8 必须由 W3-C 侧惰性 reconcile 承担**，这是 C1 的根因 |
| `rank_candidates` 是纯读、无审计 | `workforce.py:1641+` | 既有先例；**V3 有意偏离**（守卫需可写，见 §11） |
| `new_id` = `f"{prefix}_{uuid4().hex[:12]}"`，**不含 `:`** | `models.py:29-30` | attempt 解析安全，不会因 id 含冒号而误判 |
| `SQLite PRAGMA foreign_keys=ON` 真实生效 | `db.py:43-45`（实测 `(1,)`） | **C4 的 RESTRICT 会真实阻断级联删除**，非纸面约束 |
| 尚无 `recommendation` / `trial` / `employee` 表 | 全仓 grep 无 `__tablename__` 命中 | W3-C 建 `recommendation`，其余仍不建 |

### 1.2 既有 Approval / owner_inbox 机制（关键考古）

| 事实 | 出处 | 影响 |
|---|---|---|
| `Approval.project_id` 是 **NOT NULL** FK → `project.id` | `models.py:499` | ⚠️ 无 Project 则无法建 Approval 行 |
| `services.create_approval` 强制 `session.get(Project, ...)` 存在，否则 404 | `services.py:232-233` | ⚠️ 直接复用会 404 |
| `owner_inbox.INBOX_KINDS = {content, cs, feedback, knowledge}` | `owner_inbox.py:140` | ⚠️ 无 workforce inbox |
| `OwnerInboxService.decide` 强制 `_load_live_project(...)` | `owner_inbox.py:2111` | ⚠️ owner_inbox 是 **project-scoped** 通道 |
| Workforce 链（BusinessGoal→RequiredWork→Job→JobVersion→Candidate）**无任何 project_id** | `models.py:1396-1620` | ⚠️ 根因：Workforce 域与 Project 域**不相交** |
| `workforce.py` 全部审计调用传 `project_id=None` | `workforce.py:940/999/1022/1579/1612` | 既有先例，审计支持 `project_id=None` |
| `append_audit(project_id: str \| None, task_id: str \| None)` | `audit.py:110-118` | ✅ 可复用，无需改 |
| `actor._assert_owner_actor()` | `actor.py:79` | ✅ 人类身份校验复用（V3 **只复用这一个**） |
| `actor.resolve_owner_actor()` | `actor.py:53` | ❌ **V3 禁止 W3-C 调用**（P2-1：消除隐式自动审批通道） |
| `ApprovalStatus` = `PENDING / APPROVED / REJECTED / **EXPIRED**` | `models.py:190-196` | 仅复用为**决策输入词汇**；`EXPIRED` 明确排除（§4.4） |
| `RiskLevel` = `L0..L4` | `models.py:198-203` | 复用 `L4` |
| `db.py` 既有链路 `Job→JobVersion→Candidate→{Match,BenchmarkResult}` **全 CASCADE** | `models.py` 各 FK | C4 的 RESTRICT 是这条下行链的**第一个非级联依赖** |

---

### 1.3 V4 新增考古（第二轮审计 N-1…N-8 中，与本轮 C5/C6/C7/P2-3 直接相关者）

| 事实 | 出处 | 对 W3-C 的含义 |
|---|---|---|
| `compute_match` 强制 `cand.status == EVALUATED`，否则 **422** | `workforce.py:1447-1452` | **C7 根因**：Candidate=RECOMMENDED 时无法重算 ⇒ attempt 漂移在生产不可达 |
| `attempt = int(ctx.get("attempt", 0))` 取自 `evaluation_context` | `workforce.py:1465` | attempt 由 W3-A 评估轮次决定，非 Match 自增 |
| `evidence_refs[0] = f"cand:{candidate_id}:attempt:{attempt}"` **无条件**写入 | `workforce.py:1541` | **P2-3 关键**：`cand:` 环恒存在 ⇒ 可安全设为「必需环 + 必须可解析」 |
| `br:{benchmark_result_id}` **仅当** `trusted_result is not None` 才 append | `workforce.py:1542-1543` | **P2-3 关键**：`br:` 是**条件环**，unbound/untrusted 下必然缺失 |
| `benchmark_counted = bound_bv_id is not None and benchmark_score is not None` | `workforce.py:1491` | unbound JobVersion ⇒ `benchmark_counted=False` |
| unbound ⇒ `score = capability_fit`（单分量归一化，不补 0） | `workforce.py:1497-1499` | W3-B **waive 语义**，W3-C 必须原样承接，不得改写为「缺失即拒绝」 |
| `breakdown.benchmark_score = {value: None, status: "waived", reason: "JobVersion unbound"}` | `workforce.py:1521-1533` | waive 是**一等状态**，不是异常 ⇒ 推荐可基于 waived 评分成立 |
| `evaluated_fields` 亦为条件性：unbound 时只含 `capability_fit` | `workforce.py:1544-1546` | F-R3 只判**非空**，不判「必须含 benchmark_score」 |
| `_attempt_from_evidence_refs()` 返回 `None` 的五种情形 | `workforce.py:1398-1411` | **C6**：refs 为空 / 非 4 段 / `parts[0] != "cand"` / `parts[2] != "attempt"` / `int(parts[3])` ValueError |
| `tests/test_workforce_evaluation_w3a.py:746-748` 对 `ALLOWED[EVALUATED]` 做**全等**断言 `== {REJECTED}`（函数起于 :720） | 实测 | **C5**：W3-C 加边后必红，须纳入受控调整（C3④） |
| `rank_candidates` 过滤 `Candidate.status == EVALUATED` | `workforce.py:1655` | 提升为 RECOMMENDED 后退出排序（属 **P2-4**，本轮未裁，见 §17.2） |

---

## 2. 与历史 proposal 的偏差说明

历史 proposal 主要来自 `docs/Workforce_W3_Evaluation_Matching_Spec_V1.md` §6（Recommendation）、§7（Trial）与 §2 复用清单（第 24-30 行）。

### 2.1 P0 偏差（阻塞性，已由 Q1=B 裁决）

| # | 历史 proposal 原文 | 考古实测 | 偏差性质 |
|---|---|---|---|
| **D-1** | §2 表第 29 行：「`Approval`（`models.py:495`）… W3 Recommendation 复用为**唯一人类闸（不新造审批机制）**」；§6.3：「创建 `Approval`（action_type=`workforce.recommend`）」 | `Approval.project_id` **NOT NULL**，`create_approval` 强制 Project 存在；而 Workforce 链**完全没有 Project** 字段 | **proposal 不可直接执行**。物理复用须先改既有核心表（方案 A′）→ **Q1=B 裁决：不物理复用** |
| **D-2** | §6.3：「Owner 在 `owner_inbox` 批准/驳回」 | `owner_inbox` 是 **project-scoped sealed-token** 通道（无 workforce kind；`decide` 强制 live Project） | **proposal 不可直接执行**。接入需新增 inbox kind + purpose + handler = **改 engine**（本轮硬约束禁止）→ **Q1=B 裁决：域内闸门，留痕偏离（§6.4）** |

### 2.2 P1 偏差（范围变更）

| # | 历史 proposal | 本轮约束 | 处理 |
|---|---|---|---|
| **D-3** | §7 Trial：W3 实现 `create_trial`、绑定真实 Task、`check_budget`、`execute_task` | 硬约束 8：**不实现 Trial 本体** | Trial 降级为**接口契约 + 守卫函数**（`assert_trial_eligible`），实体留 W3-D / W4 |
| **D-4** | §6.1：`Recommendation.status: PROPOSED`（单一状态） | 硬约束 7：Approval 是唯一人类闸 → 决策结果必须落库 | 扩展为 `PROPOSED / APPROVED / REJECTED / WITHDRAWN`（§4.4） |
| **D-5** | §6.1：`Recommendation.target_employee_id`（V1 恒 null） | 硬约束 8：不实现 Employee | **不建该列** —— 恒 null 的死列是伪字段，改用 §11 的 W4 握手契约 |
| **D-8**（V3 新增） | §6.3 隐含「Owner 批准即生效、直到 Trial」 | C1 裁决：批准绑定**证据**而非候选 | `APPROVED` 可因 attempt 漂移被系统撤销（F-R8） |

### 2.3 P2 偏差（接口微调）

| # | 历史 proposal | 本轮设计 | 理由 |
|---|---|---|---|
| **D-6** | `recommend_candidate(session, candidate_id)` | 保持同签名，`job_version_id` 从 `Candidate.job_version_id` **派生** | `Candidate` 已唯一绑定 job_version；显式传参会引入不一致误用面 |
| **D-7** | §6.2 未定义 attempt 失效语义 | 新增 `match_attempt` 列 + F-R8 惰性 reconcile | W3-B `compute_match` in-place UPDATE 不通知；不感知则出现「批准了已被新证据推翻的旧推荐」 |

### 2.4 偏差根因一句话

> **历史 proposal 假设 Workforce 域与 Project 域相交；实际冻结代码中二者完全不相交。**
> ⇒ Q1=B（域内闸门）是给定约束下的唯一可行解；其代价是 L4 闸不在 `owner_inbox` UI 内（§6.4 书面留痕）。

---

## 3. W3-C 范围与闭环

### 3.1 本轮闭环（V3：含惰性 reconcile）

```
Match/Ranking (W3-B, 冻结) ── in-place UPDATE（新 attempt，不通知 W3-C）
        │  只读：Match.{score, breakdown, evidence_refs, status, match_blocked_reason}
        ▼
Recommendation (W3-C, 本轮新增)
   status: PROPOSED ──（人类 decide）──> APPROVED / REJECTED
        │                                     │
        │  ⚠️ _reconcile_drift（惰性）         │
        │  检测 attempt 漂移 ⇒ WITHDRAWN       │
        │  + recommendation.withdrawn 审计     │
        ▼                                     ▼
   WITHDRAWN ──（recommend_candidate 重建）──> PROPOSED
        │
        │  唯一人类闸：Approval(L4)，复用 RiskLevel.L4 + _assert_owner_actor + append_audit
        ▼
   assert_trial_eligible()  ← 惰性 reconcile 守卫，**本轮不创建 Trial**
        │
        ▼
   [ W3-D / W4 接管：Trial → Employee ]
```

### 3.2 硬约束映射矩阵

| # | 硬约束 | 落地位置 |
|---|---|---|
| 1 | W3-A/W3-B 全部冻结，禁止修改既有语义 | §5.1 只加 2 条边 + docstring 表述更新；§10 只读契约；§15.2 |
| 2 | Recommendation 建立在可解释 Match 之上 | F-R2 / F-R3 |
| 3 | score/breakdown/evidence_refs 可解释、可审计 | §4.2 强制非空；F-R3；§9 |
| 4 | reliability / historical 保持 unknown，禁止虚构 | F-R4 + §4.3 `unknown_dimensions` |
| 5 | cost_policy 仅 advisory，不制造伪精确成本评分 | F-R5 + `cost_advisory: str \| None` |
| 6 | Recommendation fail-closed | F-R1a/b、F-R2…F-R5、F-R8、F-R10 |
| 7 | Approval(L4) 是 Recommendation → Trial 的唯一人类闸门 | §6（Q1=B）；F-R6 / F-R7 / F-R9 |
| 8 | 不实现 Trial / Employee | §11；D-3 / D-5 |
| 9 | 不新增 Capability 词表、不重造 Scheduler/Execution/Budget/Audit | §14 契约 D；本轮**零调用** Budget/Scheduler/Execution |
| 10 | migration 单 head、可逆、additive | §12 |
| 11 | 复用现有 Scheduler / Execution / Budget / Audit / Approval | §6.3 复用清单（V3 收紧：去除 `resolve_owner_actor`） |

---

## 4. 数据模型（新增 1 张表，全 additive）

### 4.1 `recommendation` 表（V3 定稿）

| 列 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `id` | str PK | `new_id("rec")` | |
| `candidate_id` | FK `candidate.id` | **CASCADE**, index | |
| `job_version_id` | FK `job_version.id` | **CASCADE**, index | 从 Candidate 派生 |
| `match_id` | FK `match.id` | **RESTRICT**, index | 强绑定（**C4 裁决保留**）；Match 被删 = 拒绝，防悬空推荐 |
| `match_attempt` | int | **NOT NULL**（C6：不改为可空） | 复用 `_attempt_from_evidence_refs(Match.evidence_refs)`；F-R8 漂移判据。**写入前置**：F-R3b 保证解析返回 `None` 时在**写行之前**即 422，故 `None` 永不进入本列（§7 F-R3b / §10.3） |
| `status` | `RecommendationStatus` | index | **唯一状态 SoT（C2）**，见 §5.2 |
| `proposed_action` | str | default `"hire"` | V1 仅 `"hire"`；其余值 422 |
| `score` | float | | **Match.score 的原样快照，禁止重算** |
| `weights_version` | str | | 快照 `Match.weights_version` |
| `breakdown` | JSON dict | **NOT NULL 语义** | 原样快照 `Match.breakdown`（深拷贝） |
| `evaluated_fields` | JSON list | | 快照 |
| `evidence_refs` | JSON list | | `Match.evidence_refs + ["match:{match_id}"]` |
| `excluded_fields` | JSON list | | 快照 `Match.breakdown["excluded"]`，F-R4 判据 |
| `unknown_dimensions` | JSON dict | | 只读快照 evaluation_context 三维度状态（§4.3） |
| `cost_advisory` | str \| None | | **纯文本** advisory，禁止数值 |
| `rationale` | str | | 模板生成的可解释文本（非 LLM 自由生成） |
| ~~`approval_status`~~ | — | — | ❌ **C2 裁决：删除此列**（V2 §4.1 曾定义） |
| `risk_level` | `RiskLevel` | default **L4** | 复用既有枚举 |
| `approval_id` | str \| None | index | **前向列**：W4 引入 Project 后回填真实 `Approval` 行；V1 恒 None |
| `decided_by` | str \| None | | = `actor.owner_id or "owner"`，**仅人类决策可写**（INV-3） |
| `decided_at` | datetime \| None | | |
| `decision_rationale` | str \| None | | |
| `recommender` | str | default `"workforce_recommendation"` | |
| `created_at` / `updated_at` | datetime | | |

**唯一约束**：`UNIQUE(candidate_id, job_version_id)` —— 与 `Match` 同构，幂等的物理基础。
**索引**：`ix_recommendation_status`（**替代 V2 的 `ix_recommendation_approval_status`**）、`ix_recommendation_decided_by`。

> ✅ **R7 实现期裁决（2026-09-02）**：上表中 `candidate_id` / `job_version_id` / `match_id` / `approval_id` 的 `index` 标注**暂不落物理索引** —— 以 §12.1「create_table + 2 index」与 §14 契约 A 为准（完整裁决与理由见 §12.1 留痕）。模型侧同步只标 `status` / `decided_by` 两个 `index=True`。

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

**fail-closed 校验**：若上游任一维度的 `status` 不是字符串状态量（例如被写成数字）→ **422「unknown dimension would be fabricated」**。

> **P2-4 澄清**：实测 `evaluation_context` 三段**只有 `status` + `reason`，无 `scored` 键**（`workforce.py:862-874`）。
> `scored: false` 是 W3-C 的**派生守卫标志**，不是对上游缺口的"补全"。
> 二者必须区分：**禁止补全的是「数值」，不禁止派生布尔守卫**。

### 4.4 状态词汇（C2：单一 SoT）

```python
class RecommendationStatus(StrEnum):
    PROPOSED  = "proposed"    # AI 产出，等待人类决策
    APPROVED  = "approved"    # 仅人类决策可达（PROPOSED → APPROVED 唯一入边）
    REJECTED  = "rejected"    # 仅人类决策可达；**终态**
    WITHDRAWN = "withdrawn"   # 仅系统失效可达（F-R8）
```

**关于 `ApprovalStatus.EXPIRED`（C2 明确要求）**

| 结论 | 内容 |
|---|---|
| ❌ | `ApprovalStatus.EXPIRED` **不属于** W3-C Recommendation 语义 |
| ❌ | `recommendation.status` **永不为** `expired` |
| ❌ | 不存在 `WITHDRAWN ⇔ EXPIRED` 的任何映射（二者语义不同：WITHDRAWN = 证据失效，EXPIRED = 审批超时） |
| ✅ | `ApprovalStatus` 仅作为 `decide_recommendation(decision=...)` 的**输入词汇**复用，且**白名单 = {APPROVED, REJECTED}** |
| ✅ | 传入 `PENDING` / `EXPIRED` → **422「invalid decision」**（契约测试 T-REC-SOT-39） |

**不新增**：`TrialStatus`、`EmployeeStatus`、`CandidateStatus.TRIALING`（本轮不实现 Trial/Employee，新增即"为不存在的状态预留物理词汇"）。

### 4.5 不变量（C2：禁止双状态矛盾）

| ID | 层次 | 不变量 | 强制方式 |
|---|---|---|---|
| **INV-1** | DB / schema | `recommendation` 表**不存在** `approval_status` 列 | 物理删除 ⇒ 双状态矛盾**不可能存在**（测试 37：`inspect(engine).get_columns` 断言） |
| **INV-2** | 类型 | `status` 是唯一状态 SoT；取值域 `RecommendationStatus`（4 值，无 `expired`） | Python 枚举 + 列类型 |
| **INV-3** | 服务 | `status == APPROVED` **⇔** `decided_by` 非空；`status == REJECTED` **⇔** `decided_by` 非空；其余状态 `decided_by` **必须为 None** | 仅 `decide_recommendation` 写 `decided_by`；重建路径（WITHDRAWN→PROPOSED）**必须清空** `decided_by/decided_at/decision_rationale`（测试 38） |
| **INV-4** | 状态机 | 所有状态写入**必须**经 `_transition_status(rec, new_status)` 单一私有 helper，校验 `RECOMMENDATION_ALLOWED` 边表；越界 → 409 | 禁止业务函数直接 `rec.status = ...`（静态 + 运行期断言，测试 46） |
| **INV-5** | 审计 | 任何状态变更必须**同事务**产生对应审计行；不存在「状态变了但无审计」的路径 | SAVEPOINT 包裹状态 + 审计（§16） |
| **INV-6** | 决策 | `PROPOSED → APPROVED/REJECTED` 的唯一执行者是 `decide_recommendation`，其第一行是 `_assert_owner_actor(actor)` | 测试 20/21/45 |

---

## 5. 状态机（V3：统一语义，无冲突定义）

### 5.1 `CandidateLifecycle` 受控加边（唯一的 W3-A 触碰点）

| 源 | 目标 | 变更 | 守卫 |
|---|---|---|---|
| `EVALUATED` | `REJECTED` | **既有，不动** | — |
| `EVALUATED` | `RECOMMENDED` | **新增（受控）** | 仅 `recommend_candidate` 创建 / 重建成功路径；须 Match COMPUTED 且 F-R1…F-R5 全过 |
| `RECOMMENDED` | `EVALUATED` | **新增（受控）** | ① 人类 REJECT（`decide_recommendation`）；② 系统 WITHDRAWN（`_sync_candidate_back`）。**幂等守卫**：若已是 EVALUATED 则跳过，不调用 `require_transition` |
| `RECOMMENDED` | 其它任何状态 | **禁止** | 尤其禁止 `RECOMMENDED → TRIALING`（Trial 属 W3-D/W4） |

**净变更 = `ALLOWED[EVALUATED]` 加 1 个成员 + `ALLOWED[RECOMMENDED]` 从 `set()` 变为 `{EVALUATED}`。**
既有边零改动，W3-A 语义不被削弱。

> **P3-4 更正（事实确认）**：V2 写「**若**存在『RECOMMENDED 不可达』的守卫测试」——**确实存在**，且精确落点为
> `tests/test_workforce_models.py:611` 的 illegal tuple `(CandidateStatus.EVALUATED, CandidateStatus.RECOMMENDED)`。
> 另需同步 :610 的行内注释（原文 "RECOMMENDED remains unreachable until the W3-C/D Match gate lands" 在 W3-C 后失效）。
> 同组 :612 `(POOLED, RECOMMENDED)`、:613 `(RECOMMENDED, POOLED)`、:625 自环 `(RECOMMENDED, RECOMMENDED)` 在 W3-C 下**仍然非法，全部保留**。

### 5.2 `Recommendation` 状态机（表级，V3 定稿）

**边表（唯一权威定义）**

```python
RECOMMENDATION_ALLOWED: dict[RecommendationStatus, set[RecommendationStatus]] = {
    RecommendationStatus.PROPOSED:  {APPROVED, REJECTED, WITHDRAWN},
    RecommendationStatus.APPROVED:  {WITHDRAWN},
    RecommendationStatus.REJECTED:  set(),        # 终态（fail-closed：新证据不自动复活人类拒绝）
    RecommendationStatus.WITHDRAWN: {PROPOSED},
}
```

**转移表（含唯一执行主体 —— 修复 V2 P1-1「无执行主体」）**

| 源 → 目标 | 触发者 | **唯一执行位置** | 前置条件 | 审计 action |
|---|---|---|---|---|
| (none) → `PROPOSED` | 系统（AI） | `recommend_candidate` **创建** | F-R1a…F-R5 全过 | `recommendation.proposed` |
| `PROPOSED` → `APPROVED` | **仅人类** | `decide_recommendation` | `_assert_owner_actor`；无漂移 | `recommendation.decided` |
| `PROPOSED` → `REJECTED` | **仅人类** | `decide_recommendation` | 同上 | `recommendation.decided` |
| `PROPOSED` → `WITHDRAWN` | 仅系统 | `_reconcile_drift`（§16） | 漂移 / 不可解析 / Match 缺失 | `recommendation.withdrawn` |
| `APPROVED` → `WITHDRAWN` | 仅系统 | `_reconcile_drift`（§16） | 同上；**含已批准推荐** | `recommendation.withdrawn` |
| `WITHDRAWN` → `PROPOSED` | 系统（AI） | `recommend_candidate` **重建** | F-R1b…F-R5 全过（基于新证据） | `recommendation.reproposed` |
| `WITHDRAWN` / `REJECTED` → *(行删除)* | 人类（owner） | `purge_recommendation`（**D-R1**；**P2-6** 加 owner actor） | status ∈ {WITHDRAWN, REJECTED} **且** `_assert_owner_actor(actor)` | `recommendation.deleted` |
| `APPROVED` / `REJECTED` | 终态 | — | 不可再决策（重复决策 → 409） | — |
| `REJECTED` → `PROPOSED` | — | **不存在** | 人类拒绝是终态，新 attempt 不自动复活 | — |

### 5.3 V2 → V3 转移表差异（新增 / 删除）

| 边 | V2 | V3 | 说明 |
|---|---|---|---|
| `(none) → PROPOSED` | ✅ | ✅ 保留 | 审计 action 不变 |
| `PROPOSED → APPROVED` | ✅ | ✅ 保留 | 前置由 `approval_status==PENDING` 改为 **`status==PROPOSED`**（C2） |
| `PROPOSED → REJECTED` | ✅ | ✅ 保留 | 同上 |
| `PROPOSED → WITHDRAWN` | ✅ | ✅ 保留 | V2 无执行主体 → **V3 明确为 `_reconcile_drift`** |
| `APPROVED → WITHDRAWN` | ✅ | ✅ 保留 | V2 定义但**无执行主体**（P1-1）→ **V3 由 `_reconcile_drift` 闭环** |
| `WITHDRAWN → PROPOSED` | ✅ | ✅ 保留 | V2 审计 action 冲突（§5.2 写 `proposed`、§8 写 `recomputed`）→ **V3 统一为 `reproposed`** |
| **新 attempt → `PROPOSED`（§8 直接改状态）** | ✅ | ❌ **删除** | 与 §5.2 的 `→WITHDRAWN` 同源冲突、无仲裁 → V3 统一为「**先 withdrawn，再 reproposed**」 |
| **审计 `recommendation.recomputed`** | ✅ | ❌ **删除** | 被 `withdrawn` + `reproposed` 取代（单一语义） |
| **`* → (行删除)`** | — | ✅ **新增** | `purge_recommendation`（C4 解锁路径，D-R1） |
| **`REJECTED → PROPOSED`** | — | ❌ 明确不存在 | V3 显式裁为终态（fail-closed） |
| `REJECTED / WITHDRAWN` 遇漂移 | V2 未定义 | ✅ **不改状态、不写审计** | 无可撤销的活状态 ⇒ 幂等（§16） |

### 5.4 关键 fail-closed 点

> `APPROVED` **不是**不可逆承诺 —— 一旦底层 Match 被新证据推翻，系统必须把 `APPROVED` 拉回 `WITHDRAWN`（F-R8）。
> **人类批准的是「当时那份证据」，不是候选本身。**
> 反向同理（fail-closed）：人类**拒绝**也是终态，新证据**不会**自动复活 `REJECTED`。

---

## 6. Approval(L4) 唯一人类闸（Q1=B 定稿）

### 6.1 方案定稿

| 方案 | 结论 |
|---|---|
| **A′ 物理复用 `approval` 表** | 备选（需改核心表 `project_id`→可空，downgrade 带数据删除） |
| **B 域内闸门** | ✅ **Q1 裁决采纳** |
| **C 引入 Project** | ❌ 排除（违反冻结） |

**方案 B 要点**：新建 `recommendation` 表承载 `status` / `risk_level=L4` / `decided_by`；决策函数复用 `actor._assert_owner_actor` + `ApprovalStatus`（决策输入词汇）+ `RiskLevel` + `append_audit` + SAVEPOINT 幂等；保留 `approval_id` 前向列供 W4 升级为 A′。

### 6.2 人类闸定义（**P2-1：actor 必填**）

```python
# 纯设计示意，本轮不实现
def decide_recommendation(
    session,
    recommendation_id: str,
    decision: ApprovalStatus,      # 复用既有枚举；白名单 = {APPROVED, REJECTED}
    rationale: str | None = None,
    *,
    actor: ActorContext,           # ★ 必填，无默认值（P2-1）
) -> Recommendation:
    _assert_owner_actor(actor)     # 复用 actor.py:79；非 owner → 403
    ...
```

> ⚠️ **P2-1 变更点**：V2 为 `actor: ActorContext | None = None`，并在 `actor is None` 时回落 `resolve_owner_actor()`。
> 该写法与 `services.decide_approval:331-333` 逐行同构（属既有先例），但**字面违反** F-R7「禁止任何自动绕过 Approval 的路径」——省略 `actor` 即得 owner 权限，构成隐式自动审批通道。
> **V3 收紧**：`actor` 必填（缺参 → `TypeError`）；**W3-C 全模块禁止调用 `resolve_owner_actor`**。

**P2-6 扩及 `purge_recommendation`（V4 新增）**

```python
# 纯设计示意，本轮不实现
def purge_recommendation(
    session,
    recommendation_id: str,
    *,
    actor: ActorContext,           # ★ 必填，无默认值（P2-6 / F-R11）
) -> None:
    _assert_owner_actor(actor)     # 复用 actor.py:79；非 owner → 403
    if rec.status not in (WITHDRAWN, REJECTED):
        raise ServiceError(409, "...")   # F-R10 / DR-3
    ...
```

> ⚠️ **P2-6 变更点**：V3 的 `purge_recommendation` **无 actor 参数**，可清除人类终态 `REJECTED` 且**无身份留痕**。
> V4 收紧为「owner actor 必填」，与 `decide_recommendation` 同构；审计 `recommendation.deleted` 的 `actor` 落 `owner_id`。
> **理由**：清除 `REJECTED`（人类终态）等价于撤销一次人类决策的**证据载体**，其权限不得低于作出该决策所需的权限。

### 6.3 复用清单（V3 收紧版）

| 能力 | 复用来源 | 出处 | V3 状态 |
|---|---|---|---|
| owner 身份断言 | `actor._assert_owner_actor` | `actor.py:79` | ✅ 复用 |
| ~~owner 身份解析~~ | ~~`actor.resolve_owner_actor`~~ | `actor.py:53` | ❌ **移除**（P2-1） |
| 决策状态词（输入） | `ApprovalStatus`（白名单 APPROVED/REJECTED） | `models.py:190-196` | ✅ 复用 |
| 风险分级 | `RiskLevel.L4` | `models.py:198-203` | ✅ 复用 |
| 审计写入 | `append_audit` | `audit.py:110-118` | ✅ 复用 |
| 重复决策 409 语义 | 对齐 `decide_approval`「该审批已被处理」 | `services.py:~338` | ✅ 对齐 |
| 并发幂等 | SAVEPOINT + CAS（P2-1 模式） | `workforce.py:1617-1637` | ✅ 复用 |
| attempt 解析 | `workforce._attempt_from_evidence_refs` | `workforce.py:1398` | ✅ 复用 |

**不复用（也不重写）**：`services.create_approval`（需 Project）、`owner_inbox` 通道（project-scoped）、`append_event`（需 project_id）、`resolve_owner_actor`（P2-1）。
⇒ 本轮**不产生** `approval.requested` 事件；审计靠 `append_audit(project_id=None, task_id=None)`。

### 6.4 P2-7 偏离书面留痕

> **留痕**：硬约束 7 原文为「Approval(**owner_inbox** / L4)」。因 D-2（owner_inbox 是 project-scoped 通道，接入须改 engine，本轮禁止），
> **W3-C 的 L4 闸为「域内闸门」** = owner actor 断言（`_assert_owner_actor`）+ `RiskLevel.L4` + `append_audit` + SAVEPOINT。
> R7 已于 Q1=B 知情接受。**owner 在统一收件箱中的可见性由 W4 引入 Project 后补齐**（届时经 `approval_id` 前向列升级为 A′）。

---

## 7. Gate / fail-closed 规则（V3）

| ID | 规则 | 违反行为 |
|---|---|---|
| **F-R1a** | **创建路径**（无既有 Recommendation 行）：`Candidate.status` 必须 `EVALUATED` | 409 `illegal candidate state transition`；**不回退状态**、不写推荐 |
| **F-R1b** | **幂等 replay 路径**（有行、无漂移）：**不校验** Candidate 状态，零写入返回既有行 | —（保证幂等，避免「PROPOSED 时 Candidate 已 RECOMMENDED 导致自检失败」） |
| **F-R1c** | **重建路径**（有行、漂移、status=WITHDRAWN）：Candidate 必须 `EVALUATED` | 409；保持 WITHDRAWN |
| **F-R2** | 必须存在 `Match` 且 `Match.status == COMPUTED`；`BLOCKED` / `match_blocked_reason` 非空 → 拒绝 | 409 `recommendation blocked: {reason}`；保留 EVALUATED，不进 RECOMMENDED |
| **F-R3** | `Match.breakdown` / `evaluated_fields` / `evidence_refs` / `excluded_fields` 任一为空 → 拒绝 | 422 `match is not explainable`；禁止生成默认 breakdown |
| **F-R3** 补充（P2-3） | F-R3 只判 §10.3 的**必需证据环**（`cand:` 可解析 + `match:`），**不判** `br:` 条件环 | unbound / bound-but-untrusted 下 `br:` 缺失 **不构成** F-R3 拒绝（见 §10.3 明裁） |
| **F-R3b** 🆕**（C6）** | `evidence_refs` 非空但 `_attempt_from_evidence_refs(Match.evidence_refs)` 返回 **`None`** → 拒绝 | **422 `match evidence is not resolvable`**（全域统一错误语义：创建路径、重建路径、以及任何需要解析 attempt 的位置，**同一字符串**）；**禁止**让 `None` 落入 NOT NULL 的 `match_attempt` 列 —— 否则 `IntegrityError` 逃逸为 500，**字面违反 fail-closed** |
| **F-R4** | reliability / historical / cost 三段必须落为 `unknown` / `future_capability` 状态量，禁止任何数值 | 422 `unknown dimension would be fabricated` |
| **F-R5** | `cost_policy` 仅生成**文本** advisory；禁止产生 cost 数值、禁止进入 `score`、禁止产生「伪精确成本分」 | 设计约束（`cost_advisory: str \| None`）；测试断言 score 不含 cost 分量 |
| **F-R6** | `assert_trial_eligible()` 仅当 `status == APPROVED` **且** `decided_by` 非空时返回 True（INV-3） | 否则返回 False |
| **F-R7** | **禁止任何自动绕过 Approval 的路径**：① 无函数在 `status != APPROVED` 下产出「可进 Trial」信号；② `decide_recommendation` 的 `actor` 无默认值、W3-C 不调用 `resolve_owner_actor`（P2-1） | 静态可审计（§13.7）；§14 契约 F |
| **F-R8** | attempt 漂移（含**解析失败** / **Match 行缺失**，见 §16）→ 对活状态 `{PROPOSED, APPROVED}` 执行 `→ WITHDRAWN` + `recommendation.withdrawn` 审计 | fail-closed：不可验证 ⇒ 不可信 ⇒ 撤销 |
| **F-R9** | 非 owner actor 调用决策 → 拒绝 | 403（`_assert_owner_actor` 抛出，不重写） |
| **F-R10** | **（C4）** 存在 `recommendation` 行（任意状态）的 Candidate **不得**随 Job / JobVersion 级联删除 → DB `IntegrityError`；解锁须先 `status ∈ {WITHDRAWN, REJECTED}` 再 `purge_recommendation` | `purge` 在 `PROPOSED` / `APPROVED` 下 → 409 |
| **F-R11** 🆕**（P2-6）** | `purge_recommendation(..., *, actor: ActorContext)`：`actor` **必填无默认值**；首行 `_assert_owner_actor(actor)`；审计 `actor` 落 owner_id | 缺参 → `TypeError`；非 owner（agent / system）→ **403**。**禁止**隐式 actor |

> **F-R8 + F-R7 是本轮最关键的两条**：前者堵住「批准旧证据 → 新证据推翻 → 仍可 Trial」；后者堵住「省略 actor 即得 owner 权限」。

---

## 8. 幂等规则（V3：统一，无冲突）

| 场景 | 行为 | 审计 |
|---|---|---|
| 无既有行 | 创建 `PROPOSED`（F-R1a…F-R5 全过）；Candidate `EVALUATED → RECOMMENDED` | `recommendation.proposed` |
| 有行 + **无漂移**（同 `match_attempt`） | **幂等 replay**：返回既有行，不改状态、不写审计、不动 Candidate | 无（与 `compute_match` replay 语义一致） |
| 有行 + **漂移** → status ∈ {PROPOSED, APPROVED} | `_reconcile_drift` 撤销 → `WITHDRAWN` + 审计；Candidate `RECOMMENDED → EVALUATED`（幂等守卫） | `recommendation.withdrawn`（含 `before.decided_by`） |
| 有行 + 漂移 → status = WITHDRAWN | 尝试重建：过 F-R1c…F-R5 → `PROPOSED`（更新 `match_attempt`、清空 `decided_by/decided_at/decision_rationale`）；不过 → **保持 WITHDRAWN**（fail-closed） | 成功：`recommendation.reproposed`；失败：无（409/422） |
| 有行 + 漂移 → status = REJECTED | 409「rejected by owner; cannot re-propose」（人类终态） | 无 |
| 并发首次创建 | SAVEPOINT + `IntegrityError` 吸收 → 从 fresh session 读权威行返回 | 无重复 |
| 并发 reconcile | CAS `UPDATE … WHERE status IN ('proposed','approved')` + rowcount 校验 → **恰好 1 行**审计 | 见 §16 |
| 重复决策（已 APPROVED/REJECTED 再决策） | 409「该推荐已被决策，不能重复决策」 | 无 |
| `purge_recommendation` | 前置 status ∈ {WITHDRAWN, REJECTED}；删行 + 审计全量快照 | `recommendation.deleted` |
| 幂等键 | `recommend:{candidate_id}:{job_version_id}` / `rec:{id}:decision:{decision}` | — |

**并发安全边界**：与 W3-B 完全一致（SAVEPOINT + CAS，非分布式锁）；本轮**不引入**新并发机制。

---

## 9. Audit / Evidence 要求（V3）

| action | resource_type | 时机 | payload 要求 |
|---|---|---|---|
| `recommendation.proposed` | `recommendation` | 创建 | `after` 必含 `match_id`, `match_attempt`, `score`, `weights_version`, `evaluated_fields`, `evidence_refs`, `excluded_fields`, `unknown_dimensions`, `proposed_action`, `status` |
| `recommendation.decided` | `recommendation` | 人类决策 | `decision`, `decided_by`, `rationale`, `before.status`, `after.status`；`actor` = owner_id |
| `recommendation.withdrawn` | `recommendation` | F-R8 失效（§16） | `reason`（`match_attempt_drift` / `attempt_unresolvable` / `match_missing`）, `before.{status, decided_by, decided_at, decision_rationale, match_attempt}`, `after.{status, detected_attempt}` |
| `recommendation.reproposed` | `recommendation` | WITHDRAWN → PROPOSED 重建 | `before.{status, match_attempt}`, `after.{status, match_attempt, score, breakdown, evidence_refs}` |
| `recommendation.deleted` | `recommendation` | `purge_recommendation`（**F-R11**） | **全量快照**（所有列）—— 保证删行后证据不丢失；`actor` = owner_id（**P2-6**：无身份留痕的删除被禁止） |

**统一参数**：`project_id=None, task_id=None`（与 W3-A/W3-B 全部既有调用一致；Workforce 域无 Project）。

> **P2-2 修复**：`withdrawn` / `reproposed` 的 `before` **必须**含 `decided_by` / `decided_at` / `decision_rationale` —— 否则人类批准证据在被覆盖/撤销后不可追溯，审计链断裂。

### 9.1 evidence 链完整性（**V4 明裁：P2-3**）

`recommendation.evidence_refs` = `Match.evidence_refs + ["match:{match_id}"]`（§4.1）。回溯链拆分为**必需环**与**条件环**：

| 环 | ref 形态 | 必需性 | 违反判据 |
|---|---|---|---|
| **W3-A 评估环** | `cand:{candidate_id}:attempt:{n}` | **必需**，且**必须能解析出 attempt**（`workforce.py:1541` 无条件写入） | 缺失 / 为空 → **F-R3** `422 match is not explainable`；非空但不可解析 → **F-R3b** `422 match evidence is not resolvable` |
| **W3-B match 环** | `match:{match_id}` | **必需** | 由 W3-C 自身 append，构造上恒存在 |
| **W3-B benchmark 环** | `br:{benchmark_result_id}` | 🟡 **条件性** —— 仅当 `JobVersion.benchmark_version_id is not None` **且**存在 `status=RECORDED` 的 `BenchmarkResult` 时才 append（`workforce.py:1542-1543`） | **缺失不构成拒绝** |

> 🚫 **明裁（P2-3）**：**禁止**把「证据链完整性」错误扩大为「unbound 必须存在 `br:`」。
> unbound JobVersion、以及 bound-but-untrusted（无 recorded result）的 Match，在 W3-B 中是**一等合法状态**：`benchmark_score` 按既有规则 **waive**，`score = capability_fit`（`workforce.py:1491/1497-1499`），`breakdown.benchmark_score.status = "waived"`。
> 若按 V3 §9 字面实现「任一环缺失 → F-R3 拒绝」，将**拒绝所有 unbound 匹配**，与 W3-B 冻结语义直接冲突 —— 这是本轮最易造成返工的一条。
> **完整承接契约见 §10.3。**

### 9.2 脱敏行为（**P3-1 更正：以 `audit.py` 实际行为为准；设计语义不变**）

V3 注记称「`redact_secrets` 只按 key 名脱敏，不对字符串值做模式匹配」——**该表述不准确**。`redact_secrets` 实为**双层**：

1. **key 名层**：命中 `SECRET_KEYS` → 整值替换为 `[REDACTED]`；
2. **值级层**：`_SECRET_VALUE_RE`（`audit.py:37-49`，在 `:101` 应用）对字符串值做**模式匹配**，含 `sk-[A-Za-z0-9]{8,}`。

实测（第二轮审计记录）：

```
{'api_key':   'sk-live-abc123'}                        -> '[REDACTED]'   # key 名命中
{'rationale': 'cost advisory: ... sk-abcdef123456'}    -> '[REDACTED]'   # 值级模式命中
{'message':   'boom sk-live-abc123'}                   -> 未脱敏          # 连字符打断 [A-Za-z0-9]{8,}
{'note':      'plain text no secret here'}             -> 未脱敏
```

⇒ **设计语义不变**：`rationale` / `cost_advisory` / `decision_rationale` 中**禁止拼接任何密钥原文**（值级模式对含连字符的 token 会逃逸，**不可依赖**）。
⇒ **测试指引更正**：验证脱敏时注入**匹配 `SECRET_KEYS` 的 key** 仍是最可靠路径；但**不得**据此断言「值级脱敏不存在」——它存在，只是覆盖范围有限。**本轮不因此改变任何 W3-C 设计语义。**

---

## 10. 与 W3-B Match / Ranking 的接口契约

### 10.1 `recommend_candidate(session, candidate_id)` 的读集（白名单）

**允许读**：
- `Candidate.{id, agent_id, job_version_id, status, evaluation_context}` ← `evaluation_context` **只读**
- `Match.{id, score, weights_version, breakdown, evaluated_fields, evidence_refs, status, match_blocked_reason, benchmark_version_id}`
- `Agent.{id, cost_policy}` ← 仅用于 `cost_advisory` 文本

**禁止调用 / 禁止写**：
- ❌ 调用 `compute_match` / `run_benchmark` / `evaluate_candidate`（Recommendation 不触发任何重算）
- ❌ 写 `Candidate.evaluation_context`
- ❌ 写 `Match.*`
- ❌ 写 `Candidate.status`（**唯一例外**：经 `CandidateLifecycle.require_transition` 的受控边 `EVALUATED ⇄ RECOMMENDED`）
- ❌ 调用 `delegation.check_budget` / `scheduler` / `execution`（无 Project，且 Trial 不属本轮）

### 10.2 返回值契约

返回 `Recommendation`，且保证：
1. `score == Match.score`（字节级相等，非重算）
2. `breakdown == Match.breakdown`（深拷贝，非引用共享，防后续篡改）
3. `evidence_refs[-1] == f"match:{match_id}"`
4. `excluded_fields` 覆盖 reliability / historical / cost 三项

---

### 10.3 W3-B unbound waive 承接契约（**P2-3 最终裁决**）

> **裁决落地**：保留 W3-B 的 unbound waive 语义 ——
> ① unbound JobVersion / Match 没有 benchmark binding 时，`br:` 缺失**不构成 F-R3**；
> ② `benchmark_score` 在该场景继续按 W3-B **既有规则 waive**；
> ③ 但**实际存在的** `evidence_refs` 必须能够解析出 `match_attempt`；
> ④ 无法解析 → **F-R3b 422 `match evidence is not resolvable`**。
> ⑤ **明确禁止**把「证据链完整性」错误扩大成「unbound 必须存在 `br:`」。

**四场景判定矩阵**（全部基于冻结代码 `workforce.py:1470-1546` 实测）

| 场景 | `JobVersion.benchmark_version_id` | `benchmark_counted` | `score` | `breakdown.benchmark_score` | `evidence_refs` 构成 | W3-C 行为 |
|---|---|---|---|---|---|---|
| **A · unbound** | `None` | `False` | `capability_fit` | `{value: None, status: "waived", reason: "JobVersion unbound"}` | `[cand:…:attempt:N, match:…]`（**无 `br:`**） | ✅ **可推荐**（waive 合法） |
| **B · bound + trusted** | 非 None，存在 `RECORDED` result | `True` | `0.6*cf + 0.4*bs` | `{value: bs, status: "computed", reason: None}` | `[cand:…:attempt:N, br:…, match:…]` | ✅ 可推荐 |
| **C · bound + untrusted** | 非 None，无 `RECORDED` result | `False` | `capability_fit` | `{value: None, status: "waived", reason: "no recorded/trusted benchmark_result"}` | `[cand:…:attempt:N, match:…]`（**无 `br:`**） | ✅ **可推荐**（waive 合法） |
| **D · 不可解析** | 任意 | 任意 | 任意 | 任意 | `cand:` 环存在但格式异常（解析返回 `None`） | ❌ **422 `match evidence is not resolvable`**（F-R3b） |
| **E · `evidence_refs` 为空** | 任意 | 任意 | 任意 | 任意 | `[]` | ❌ **422 `match is not explainable`**（F-R3） |

**四条硬边界**

| # | 规则 |
|---|---|
| **UW-1** | `br:` 是**条件性证据环**，其缺失**永远不触发** F-R3。只有「必需环（`cand:` / `match:`）缺失或不可解析」才触发 F-R3 / F-R3b |
| **UW-2** | W3-C **不得**因 `breakdown.benchmark_score.value is None` 判定「证据不足」——`None` 是 waive 的**正常表示**，不是缺口 |
| **UW-3** | `cand:` 环由 `workforce.py:1541` **无条件**写入 ⇒ 「非空且可解析」是常态。故 F-R3b 在**正常 W3-B 生产路径下不会触发**，它防的是数据层异常与未来演进 —— 与 F-R8 同为**纵深防御**（§16.7） |
| **UW-4** | W3-C 原样快照 `breakdown` / `evaluated_fields` / `evidence_refs`（§10.2），**不得**为「补齐 `br:`」而改写任何快照字段 |

> **反模式警示（实现者必读）**
> ❌ `if not any(r.startswith("br:") for r in evidence_refs): raise ServiceError(422, ...)` —— **会拒绝所有 unbound 匹配**（场景 A / C）。
> ✅ `if _attempt_from_evidence_refs(evidence_refs) is None: raise ServiceError(422, "match evidence is not resolvable")`。

---

## 11. W4 / Trial 接口边界（**V3 更新：`assert_trial_eligible` 不再纯读**）

| 边界 | 本轮（V3） | W3-D / W4 接手 |
|---|---|---|
| Trial 实体 | **不建表、不建函数** | `create_trial` 在 W3-D |
| Trial 守卫 | `assert_trial_eligible(session, recommendation_id) -> bool`<br>⚠️ **（C1）升级为惰性 reconcile：可能写 `status=WITHDRAWN` + 审计**，不再是纯读 | W3-D 调用它作为前置；**无需调用方自律即自动 fail-closed** |
| Employee | **不建表、不建列**（D-5） | W4 建 `Employee` 表 |
| Training / Performance | **不建表、不建列** | W4+ |
| `status == APPROVED` 的 Recommendation | 是 **W4 创建 Employee 的唯一合法输入** | W4 读取 |
| `approval_id` 列 | V1 恒 `None` | W4 引入 Project 后回填真实 `Approval` 行（B → A′ 升级路径） |
| Budget / Scheduler / Execution | **本轮零调用**（无 Project，Q5 递延） | W4 在 Trial 创建时复用 `check_budget` / `create_task` / `execute_task`，不重写 |
| Candidate 状态 | **止于 `RECOMMENDED`**；`TRIALING` 不新增 | W4 接手 `RECOMMENDED → TRIALING → EMPLOYED` |

> **V2 → V3 偏离说明**：V2 §11 规定 `assert_trial_eligible` 「纯读、不写审计」，对齐 `rank_candidates` 先例。
> 该规定使 F-R8 **无执行主体**（P1-1）。**C1 裁决放弃纯读**，改为惰性 reconcile。
> 这是**有意偏离** `rank_candidates` 先例，理由是：风控守卫不能依赖调用方自觉调用单独的 reconcile 函数（否则 W4 遗忘即 fail-open）。

---

## 12. Migration 必要性分析（V3 定稿）

### 12.1 规划（Q1=B 方案）

| 项 | 值 |
|---|---|
| **文件名** | `alembic/versions/20260902_0001_workforce_recommendation.py` |
| **revision** | `20260902_0001_workforce_recommendation`（**P3-1：已钉死，无占位符**） |
| **down_revision** | `20260901_0001_workforce_match_benchmark`（保持**单 head**） |
| **upgrade** | `op.create_table("recommendation", …)` + 索引 `ix_recommendation_status`、`ix_recommendation_decided_by` |
| **downgrade** | `op.drop_index(...)` ×2 + `op.drop_table("recommendation")` |
| **性质** | **additive**（只建新表）、**可逆**（drop 完全对称）、**单 head** ✅ |

> ⚠️ 若实现首日 ≠ 2026-09-02，revision 的日期前缀须随之调整，且 §13 测试 29（单 head 断言）与契约 A 必须同步。
> **建议：实现首日钉死后不再变更。**

> ✅ **R7 裁决（2026-09-02，TDD 实现期）— §4.1 与本节「2 index」的冲突**
>
> §4.1 把 `candidate_id` / `job_version_id` / `match_id` / `approval_id` 也标注了 `index`，与本节「create_table + **2 index**」及 §14 契约 A 的逐字表述冲突，二者不可同时满足。
>
> **裁决 = 严格 2 索引**：迁移只建 `ix_recommendation_status` / `ix_recommendation_decided_by`；`models.py` 的 `Recommendation` 相应只在这两列标 `index=True`（其余 FK / `approval_id` 不标），**保持模型 ↔ 迁移一致**（仓库无 autogenerate 漂移测试，故须人工维持）。
>
> 理由：① 契约 A 是 DSH **逐字**审计判据，多建索引会被记为偏差；② `candidate_id` 已是 `UNIQUE(candidate_id, job_version_id)` 的最左列，SQLite 以隐式索引覆盖；③ `approval_id` 在 V1 恒为 `None`，无查询收益。
>
> 后续若确需 FK 索引，**另起一个 additive revision** 补建，不回溯修改本 revision。

### 12.2 各对象是否需要 migration

| 对象 | 是否需 migration | 理由 |
|---|---|---|
| `recommendation` 表 | ✅ **需要** | 新实体 |
| `CandidateStatus.RECOMMENDED` | ❌ 不需要 | 枚举成员已存在（`models.py:1570`）；`candidate.status` 是 `sa.String()` |
| `RecommendationStatus` | ❌ 不需要 | 新枚举仅 Python 层；落库为字符串列 |
| `CandidateLifecycle` 加边 | ❌ 不需要 | 纯 Python 字典 |
| `RECOMMENDATION_ALLOWED` 边表 | ❌ 不需要 | 纯 Python 字典 |
| `approval` 表 | ❌ 不需要 | **Q1=B，不物理复用**（A′ 才需要，且 downgrade 带数据删除） |
| `candidate` / `match` / `benchmark*` | ❌ 不需要 | W3-B 已建，冻结 |

### 12.3 外键策略（**C4 裁决：保留 RESTRICT**）

| 列 | 策略 | 理由 |
|---|---|---|
| `recommendation.candidate_id` | **CASCADE** | 与既有下行链一致 |
| `recommendation.job_version_id` | **CASCADE** | 同上 |
| `recommendation.match_id` | **RESTRICT** | C4 裁决保留：Match 被删 = 悬空推荐，必须阻断 |
| `recommendation.approval_id` | 无 FK（纯前向列，V1 恒 None） | W4 升级时再建约束 |

### 12.4 C4 域规则（新增 §7 F-R10 的物理后果）

| ID | 规则 |
|---|---|
| **DR-1** | 存在 `recommendation` 行（**任意状态**）的 Candidate **不得**随 Job / JobVersion 删除。删除尝试 → DB `IntegrityError`（由 `db.py:43-45` 的 `PRAGMA foreign_keys=ON` 真实强制，非应用层模拟） |
| **DR-2** | **解锁路径**：把 status 变为 `WITHDRAWN`（系统漂移撤销）或 `REJECTED`（人类拒绝）后，经 `purge_recommendation` 显式删行（审计 `recommendation.deleted` 保存全量快照）→ RESTRICT 解除 |
| **DR-3** | `PROPOSED` / `APPROVED` **禁止** purge（F-R10，fail-closed） |
| **DR-4** | **既有 CASCADE 链零改动**：Job→JobVersion→Candidate→{Match, BenchmarkResult} 全部保持 CASCADE；RESTRICT 只在 `recommendation.match_id` 一处引入 |
| **DR-5** | 无 `recommendation` 行的 Candidate 删除行为**与 W3-C 之前完全一致**（既有回归测试必须仍绿） |

### 12.5 downgrade 数据处置（**D-R3，默认：不重置**）

> **默认决策**：`downgrade()` **仅** drop 表与索引，**不执行** `UPDATE candidate SET status='evaluated' WHERE status='recommended'`。
> 理由：保持 migration **严格 additive + 零数据修改**，符合硬约束 10 与契约 C。
> **残留风险（已在 V2 P2-5 记录）**：downgrade 后可能残留 `status='recommended'` 的 Candidate 行，在无 W3-C 代码的旧版本下"不可达且不可解释"。
> **缓解**：W3-C 代码与 migration 必须**同进同出**（同一次部署）；若确需回滚且已存在 recommended 行，须由 R7 **单独授权**一条数据修正语句（届时作为显式数据变更单独留痕）。

---

## 13. 契约测试清单（仅清单，本轮不写测试代码）

V3 共 **46 项**（V2 基础 30 项 + 新增 16 项）。

### T-REC-STATE — 受控状态转换（4）
1. `EVALUATED + COMPUTED Match` → `recommend_candidate` 成功，Candidate 变 `RECOMMENDED`
2. `EVALUATED → RECOMMENDED → EVALUATED`（人类 REJECT 后回退）
3. `POOLED` / `REJECTED` / `EVALUATING` 调用 → 409，状态**不变**
4. `RECOMMENDED → TRIALING` 不存在（无 TRIALING 枚举；`ALLOWED[RECOMMENDED] == {EVALUATED}`）

### T-REC-GATE — fail-closed（8）
5. `Match.status == BLOCKED`（capability_gap）→ 409，Candidate 仍 `EVALUATED`，无 Recommendation 行
6. 无 Match 行 → 422
7. `Match.breakdown == {}` → 422（F-R3）
8. `evidence_refs == []` → 422（F-R3）
9. `evaluation_context.reliability_evidence.status` 被篡改为数值 → 422（F-R4）
10. `cost_policy` 含数字 → `cost_advisory` 为文本，`score` **不含** cost 分量，`excluded_fields` 含 cost（F-R5）
11. `assert_trial_eligible` 在 `PROPOSED` / `REJECTED` / `WITHDRAWN` 下均 False（F-R6）
12. Match 重算后 `assert_trial_eligible` 对**已 APPROVED** 推荐返回 False（F-R8；与 31 同场景，此项只断言返回值）

### T-REC-EXPL — 可解释与审计（4）
13. `recommendation.score == match.score` 且 `breakdown` 深拷贝（改 Match 不影响 Recommendation）
14. `evidence_refs` 末尾 = `match:{match_id}`，且含 `cand:...:attempt:N`
15. `recommendation.proposed` 审计行存在，`after` 含全部必需字段
16. `recommendation.decided` 审计行 `actor` = owner_id

### T-REC-IDEM — 幂等与并发（3）
17. 同 attempt 重复推荐 → 返回同一行，`created_at` 不变，**审计行数不增**
18. 新 attempt → 撤销（`withdrawn` 审计，含 `before.decided_by`）+ 重建（`reproposed` 审计），`decided_by` 已清空
19. 并发首次创建 → 无重复行、无 IntegrityError 逃逸

### T-REC-APPROVAL — L4 人类闸（5）
20. owner actor 决策 → `APPROVED` / `REJECTED` 成功
21. 非 owner actor（agent / system）→ 403（F-R9）
22. 重复决策 → 409
23. **无 `decided_by` 的 `APPROVED` 不可达**（不存在使 `status=APPROVED` 而 `decided_by` 为空的代码路径）（INV-3）
24. `REJECTED` 后 Candidate 回 `EVALUATED`，`assert_trial_eligible` False

### T-REC-BOUNDARY — 不越界（4）
25. W3-C 全程**零写** `Candidate.evaluation_context`
26. 未创建任何 `trial` / `employee` / `training` / `performance` / `candidate_evaluation` 表（deferred-table 断言；⚠️ **`recommendation` 不再属于 deferred 集合** —— 见 §13.8 C3①）
27. 未调用 `compute_match` / `run_benchmark` / `check_budget` / `execute_task`（monkeypatch 计数断言）
28. 未新增 Capability 词表行（`select(Capability)` 计数不变）

### T-REC-MIG — 迁移（2）
29. alembic 单 head == `20260902_0001_workforce_recommendation`
30. `downgrade()` 后表/索引完全消失，`candidate` / `match` / `benchmark*` 表与数据不受影响

### 🆕 T-REC-RECONCILE — F-R8 惰性 reconcile（6）**（C1）**
31. `APPROVED` + attempt 漂移 → `assert_trial_eligible` 返回 False；`status == WITHDRAWN`；`recommendation.withdrawn` 审计存在，且 `before.status == APPROVED`、`before.decided_by` 非空（P2-2）
32. **幂等**：连续调用 `assert_trial_eligible` 2 次 → 审计行数不增、`status` 不变、Candidate 状态不变
33. **并发**：双 session 并发 reconcile → **恰好 1 行** `withdrawn` 审计（CAS rowcount 保证），无 IntegrityError 逃逸
34. **不可解析 / Match 缺失**：`evidence_refs` 格式异常（解析返回 None）或 Match 行不存在 → 视同漂移 → `WITHDRAWN`，审计 `reason ∈ {attempt_unresolvable, match_missing}`（P2-3，fail-closed）
35. 漂移后 `recommend_candidate` 重建成功 → `PROPOSED` + `recommendation.reproposed` 审计（`before.match_attempt` ≠ `after.match_attempt`），Candidate 回 `RECOMMENDED`
36. 漂移后重建**失败**（新 Match BLOCKED）→ 保持 `WITHDRAWN`，Candidate 保持 `EVALUATED`，**不产生** PROPOSED（fail-closed）

### 🆕 T-REC-SOT — 单一状态 SoT（3）**（C2）**
37. `recommendation` 表**不含** `approval_status` 列（`inspect(engine).get_columns` 断言）（INV-1）
38. 不存在任何代码路径在 `status ∉ {APPROVED, REJECTED}` 时写 `decided_by`；重建路径清空 `decided_by/decided_at/decision_rationale`（INV-3）
39. `ApprovalStatus.EXPIRED` / `PENDING` 作为 decision 传入 → **422**；且无任何路径把 recommendation 状态映射为 `expired`（§4.4）

### 🆕 T-REC-CASCADE — RESTRICT 与解锁（4）**（C4）**
40. `PROPOSED` recommendation 存在 → `session.delete(job)` **抛 IntegrityError**；candidate / match / recommendation 三行均仍存在（DR-1）
41. `purge_recommendation` 在 `PROPOSED` / `APPROVED` → **409**（DR-3）
42. `WITHDRAWN` → `purge_recommendation` → 行删除 + `recommendation.deleted` 审计含**全量快照** → 随后 `session.delete(job)` 成功，candidate / match 随之 CASCADE（DR-2）
43. **无** recommendation 的 candidate → `session.delete(job)` 仍 CASCADE 成功（既有回归，DR-5）

### 🆕 T-REC-ACTOR — actor 必填（3）**（P2-1）**
44. `decide_recommendation` 省略 `actor` → **`TypeError`**（签名强制，无默认值）
45. 静态断言：W3-C 实现文件中**无** `resolve_owner_actor` 调用（grep）
46. 静态断言：状态写入**全部**经由 `_transition_status`（无裸 `rec.status = ` 赋值）（INV-4）

### 13.8 受控测试调整清单（**C3 追认，仅测试，精确到行号**）

| # | 文件 : 行 | 现状 | W3-C 落地后必须调整 | 是否改行为 |
|---|---|---|---|---|
| **C3①** | `tests/test_workforce_evaluation_w3a.py:694-698`（函数 `test_w3a_is_zero_migration` 起于 :675） | `for deferred in ("recommendation", "trial", "candidate_evaluation"): assert deferred not in tables` | **移除 `"recommendation"`**，保留 `"trial"` / `"candidate_evaluation"` | ❌ 不改行为（W3-C 确实要建 `recommendation` 表） |
| **C3②** | **12 个测试文件**的 alembic 单 head 常量 `20260901_0001_workforce_match_benchmark` → `20260902_0001_workforce_recommendation`<br>`test_content_draft.py:51`、`test_cs_migration.py:29`、`test_feedback_zero_migration.py:33`、`test_knowledge_models.py:175`、`test_review_binding_migration.py:38`、`test_series_id_metadata_precheck.py:29`、`test_series_id_migration.py:40`、`test_v4_agent_platform.py:1193`、`test_work_log.py:54`、`test_workforce_benchmark_match_w3b.py:678 / :699`、`test_workforce_evaluation_w3a.py:682`、`test_workforce_models.py:369` | 全部为**单 head 断言**（已逐文件核验，无一用作 downgrade 目标） | 常量同步为新 head；同一断言组的行内注释可同步更正（`test_workforce_evaluation_w3a.py:680-681`、`test_workforce_models.py:367-368`、`test_cs_migration.py:4`、`test_series_id_migration.py:4`） | ❌ 不改行为（仅断言值随 head 推进） |
| **C3③** | `tests/test_workforce_models.py:611`（函数 `test_candidate_illegal_transition_rejected_409` 起于 :555） | illegal_edges 含 `(CandidateStatus.EVALUATED, CandidateStatus.RECOMMENDED)` | **删除该 1 个 tuple**；同步更正 :610 行内注释 | ⚠️ 唯一语义变更点（W3-C 正是要让这条边合法） |
| **C3④** | `tests/test_workforce_evaluation_w3a.py:746-748`（函数 `test_w2_discovery_and_lifecycle_semantics_unchanged` 起于 :720） | 全等断言 `ALLOWED[EVALUATED] == {REJECTED}` | 调整为 `ALLOWED[EVALUATED] == {REJECTED, RECOMMENDED}` | ⚠️ **第二处**语义变更点（与 C3③ 同性质；W3-C 正是要让 `EVALUATED → RECOMMENDED` 合法） |


**C3 的显式边界（不得越界）**

| 保留项 | 内容 |
|---|---|
| ✅ 保留 | `test_workforce_models.py:612` `(POOLED, RECOMMENDED)`（W3-C 下仍非法） |
| ✅ 保留 | `test_workforce_models.py:613` `(RECOMMENDED, POOLED)`（仍非法） |
| ✅ 保留 | `test_workforce_models.py:625` 自环 `(RECOMMENDED, RECOMMENDED)`（仍非法） |
| ✅ 保留 | 上述 illegal_edges 中的**其余全部** tuple |
| ✅ 保留 | `test_workforce_benchmark_match_w3b.py:704 / :721` 的 downgrade 目标 `20260827_0002_workforce_candidate`（**不得改动**） |
| ✅ 保留 | `test_candidate_cascade_on_job_delete`（`test_workforce_models.py:632`）既有断言（无 recommendation 行，不受影响；由测试 43 覆盖回归） |
| ❌ 禁止 | **除上述四类外**，修改 W3-A / W3-B 的任何既有测试或行为 |

### T-REG — 回归（必须全绿）
- `tests/test_workforce_evaluation_w3a.py` 16 项（**含 C3① 调整**）
- `tests/test_workforce_benchmark_match_w3b.py` 12 项（**含 C3② 调整**）
- `tests/test_workforce_models.py` 26 项（**含 C3② + C3③ 调整**）
- 其余 9 个携带 head 常量的测试文件（含 C3② 调整）
- 全量回归 **≥ 246 项** + `ruff` PASS

---


### 🆕 §13.9 V4 新增契约测试（C6/C7/C8/P2-3/P2-6，7 项，总 **53 项**）

> 在 V3 的 46 项基础上新增 7 项（总契约测试 = **53 项**）。仅清单，本轮不写测试代码。

#### 🆕 T-REC-EVID — F-R3b attempt 不可解析（2）**（C6）**
47. **创建路径**：`evidence_refs` 非空但 `_attempt_from_evidence_refs()` 返回 `None`（如 `cand:` 段格式异常）→ `recommend_candidate` **422 `match evidence is not resolvable`**；**绝不**让 `None` 落入 NOT NULL `match_attempt`（否则 `IntegrityError` → 500，违反 fail-closed）（F-R3b）
48. **重建路径**：`status=WITHDRAWN` 上 `recommend_candidate` 重建，同样遇不可解析 `evidence_refs` → 422（F-R3b）；与创建路径**同一错误字符串**

#### 🆕 T-REC-CAS — F-R8 CAS 撤销绑定（2）**（C8）**
49. **CAS 条件撤销**：`_reconcile_drift` 执行 `UPDATE … WHERE id=? AND status IN ('proposed','approved') AND match_attempt = :stored_match_attempt`（reconcile 开始时从 Recommendation 行读取的值）；`rowcount == 1` 才写 `recommendation.withdrawn` 审计；`stored_match_attempt` 须等于 reconcile 开始时从 Recommendation 行读取的 `match_attempt` 值（非 Match 当前值）
50. **rowcount=0 重新判定**：并发下他人已撤销（`rowcount == 0`）→ **不写审计、不写状态**，回滚 SAVEPOINT，`session.get` 重新读取并按实际状态（`WITHDRAWN` / `REJECTED`）返回 False；保证并发下**最多一次** withdrawn 审计

#### 🆕 T-REC-DRIFT — F-R8 纵深防御 / 生产不可达（2）**（C7 + P2-3）**
51. **生产不可达证明**：用 **data-layer 注入**（直接 `UPDATE match SET evidence_refs=…` 改 attempt=4）模拟 attempt 漂移；**禁止**为触发漂移而调用 `compute_match` 或改 W3-B 状态机。演示 `_reconcile_drift` 在注入态下对 `APPROVED` 推荐正确撤销（F-R8 纵深防御有效）
52. **unbound waive 不受误伤**：场景 A/C（`benchmark_version_id is None` 或无 recorded result，`evidence_refs` 无 `br:`）仍可成功推荐（F-R3 不拒）；且 `cand:` 环可解析出 `match_attempt`（F-R3b 不拒）；证明「证据链完整性」未被扩大成「unbound 必须存在 `br:`」

#### 🆕 T-REC-PURGE-ACTOR — purge 必填 actor（1）**（P2-6）**
53. `purge_recommendation` 省略 `actor` 参数 → **`TypeError`**；传入非 owner actor（agent / system）→ **403**；审计 `recommendation.deleted` 的 `actor` 落 `owner_id`（F-R11）

## 14. DSH 七项独立审计契约（W3-C V4 版）

| 契约 | 内容 | V3 判据（可字面断言） |
|---|---|---|
| **A** | 单 head / additive / 可逆 | `alembic heads` 唯一 = `20260902_0001_workforce_recommendation`；upgrade 只 create_table + 2 index（**零 alter 既有表**）；downgrade 完全对称；**12 个 head 常量已同步**（C3②） |
| **B** | 状态机边界 | W3-A/W3-B 既有边**零改动**；`ALLOWED[EVALUATED]` 只增 `RECOMMENDED`；`ALLOWED[RECOMMENDED] == {EVALUATED}`（**不含 TRIALING**）；`RECOMMENDATION_ALLOWED` 与 §5.2 逐边一致；C3③ 只删 1 个 tuple；C3④ 改 1 个全等断言 `ALLOWED[EVALUATED]=={REJECTED}` → `=={REJECTED,RECOMMENDED}` |
| **C** | downgrade 完整性 | downgrade 后 `recommendation` 表与 2 个索引消失；W3-A/W3-B 表与数据**不受影响**；不修改 `candidate` 数据（D-R3） |
| **D** | SSoT 零新能力词 | 无新 Capability 行；无第二套能力词汇；未重造 Scheduler / Execution / Budget / Audit / Approval 判定逻辑（全部 import 复用，§6.3） |
| **E** | 契约测试 | T-REC **53 项** + T-REG 全绿（≥246）+ ruff PASS + **C3 四类调整已完成** |
| **F** | fail-closed 语义 | F-R1a/b/c、F-R2…F-R10 逐条有对应测试；**不存在**绕过 Approval 达致「可 Trial」的路径（含 P2-1：`actor` 无默认值、无 `resolve_owner_actor` 调用） |
| **G** | 可解释评分 + 审批留痕 | `breakdown` / `evidence_refs` / `excluded_fields` / `unknown_dimensions` 强制非空；`decided_by` 必为 owner（INV-3/INV-6）；**5 个审计 action 齐备且均可触发**（`proposed` / `decided` / `withdrawn` / `reproposed` / `deleted`） |

> **V2 → V3 → V4 契约判据变化**：
> - **E**（V2→V3）：28/30 → **46 项**；新增「C3 三类调整已完成」作为硬判据。
> - **E**（V3→V4）：46 项 → **53 项**；「C3 三类」扩充为「**C3 四类调整已完成**」（C5 第四类 `tests/test_workforce_evaluation_w3a.py:746`）。
> - **G**：`recommendation.withdrawn` 从「不可触发」→ **可由 `_reconcile_drift` 真实触发**（P1-1 修复）；V4 进一步约束 CAS 撤销须绑定 `match_attempt = :stored_match_attempt`（C8：reconcile 读到的旧值作为乐观锁 token）。

---

## 15. 实施边界与明确禁止事项

### 15.1 允许（W3-C 实现阶段）

- 新建 `recommendation` 表 + **1 个** alembic revision（单 head、additive、可逆）
- 新增 `RecommendationStatus` 枚举 + `RECOMMENDATION_ALLOWED` 边表（纯 Python，零 schema 改动）
- `CandidateLifecycle.ALLOWED` 加 2 条受控边（`EVALUATED ⇄ RECOMMENDED`）
- 新增函数（**4 公开 + 3 私有**）：
  - 公开：`recommend_candidate` / `decide_recommendation` / `assert_trial_eligible` / `purge_recommendation`（D-R1）
  - 私有：`_reconcile_drift` / `_transition_status` / `_sync_candidate_back`
- 复用 import：`actor._assert_owner_actor`、`models.ApprovalStatus`、`models.RiskLevel`、`audit.append_audit`、`workforce._attempt_from_evidence_refs`、`workforce.CandidateLifecycle`
- **仅测试**调整：§13.8 的 C3 四类（**不得超出**）

### 15.2 明确禁止（保留 V2 全部 15 项 + V3 新增 4 项 + V4 新增 2 项 = 21 项）

**V2 原有 15 项（全部保留）**

| # | 禁止项 | V3 一致性检查 |
|---|---|---|
| 1 | ❌ 修改 `engine` / `ruleset` / `owner_inbox.py` / `services.py` / `models.py` 既有定义（只允许在 `models.py` **末尾追加** W3-C 区段） | ✅ 一致：Q1=B 只追加，不碰既有定义 |
| 2 | ❌ 修改 W3-A（`evaluate_candidate` / `_build_evaluation_context` / `_collect_capability_evidence`）与 W3-B（`compute_match` / `rank_candidates` / `run_benchmark`）任何语义 | ✅ 一致：函数级零改动；唯一触碰是 `CandidateLifecycle.ALLOWED` 加边（受控） |
| 3 | ❌ 写 `Candidate.evaluation_context` | ✅ 一致：§10.1 白名单只读 |
| 4 | ❌ 除受控边外写 `Candidate.status` | ✅ 一致：仅 `EVALUATED ⇄ RECOMMENDED`，且仅 3 个受控位置 |
| 5 | ❌ 调用 `compute_match` / `run_benchmark` / `evaluate_candidate` | ✅ 一致：§10.1 |
| 6 | ❌ 调用 `check_budget` / Scheduler / `execute_task` | ✅ 一致：Q5 递延 W4；测试 27 断言 |
| 7 | ❌ 创建 / 预实现 Trial、Employee、Training、Performance 任何表或字段 | ✅ 一致：D-3 / D-5；测试 26 |
| 8 | ❌ 新增 `CandidateStatus.TRIALING` 或其它未来状态枚举 | ✅ 一致：§4.4 明确不新增 |
| 9 | ❌ 接入 ai-arena、修改 ruleset | ✅ 一致：本轮无 adapter |
| 10 | ❌ 新增 Capability 词汇 / 第二套能力 SSoT | ✅ 一致：测试 28 |
| 11 | ❌ 虚构 reliability / historical / cost 数值评分 | ✅ 一致：F-R4 / F-R5；`cost_advisory: str \| None` |
| 12 | ❌ 任何自动绕过 Approval 的路径 | ✅ 一致（**V3 加强**）：F-R7 + P2-1（actor 必填） |
| 13 | ❌ `commit` / `push` / 创建 PR / 合并（需 R7 显式授权 exact-head SHA） | ✅ 一致 |
| 14 | ❌ 引入 Project 到 Workforce 域 | ✅ 一致：方案 C 已排除 |
| 15 | ❌ 物理修改既有 `approval` 表 | ✅ 一致：Q1=B |

**V3 新增 4 项**

| # | 禁止项 | 依据 |
|---|---|---|
| 16 | ❌ 修改 `workforce.py` 中除 `CandidateLifecycle.ALLOWED` 字典外的任何定义（含 `compute_match` / `rank_candidates` / `_attempt_from_evidence_refs` / 各 W3-A 函数） | P2-6 |
| 17 | ❌ 为 `decide_recommendation` 的 `actor` 参数提供默认值；W3-C 任何文件调用 `resolve_owner_actor` | P2-1（测试 44/45） |
| 18 | ❌ 修改 W3-A / W3-B 既有测试与行为（**§13.8 的 C3 四类除外**） | C3 边界 |

**V4 新增 2 项（C7 / P2-3）**

| # | 禁止项 | 依据 |
|---|---|---|
| 20 | ❌ 为制造 F-R8 的生产触发路径而修改 W3-B 状态机（`compute_match` / `CandidateLifecycle.ALLOWED` / `rank_candidates`）或让 `RECOMMENDED → EVALUATING` 可达；drift 演示只能经由 fixture / data-layer 注入 | C7（纵深防御知情追认） |
| 21 | ❌ 把「证据链完整性」错误扩大为「unbound 必须存在 `br:`」；禁止因 `breakdown.benchmark_score.value is None`（waive 正常表示）判定证据不足；F-R3 只判必需环（`cand:` / `match:`），F-R3b 只判 attempt 可解析 | P2-3（保留 W3-B unbound waive 语义） |
| 19 | ❌ 引入 `ApprovalStatus.EXPIRED` 语义到 recommendation 状态域 | C2 / §4.4（测试 39） |

### 15.3 §15.2 越界复检（针对 V3 全部设计）

| 设计点 | 是否触碰 15.2 | 说明 |
|---|---|---|
| 惰性 reconcile 写 `status` + 审计 | ✅ 不越界 | 写的是**新建的** `recommendation` 表，非 W3-A/B 对象 |
| `_sync_candidate_back` 写 `Candidate.status` | ✅ 不越界 | 属第 4 项「受控边」例外（`RECOMMENDED → EVALUATED`） |
| `purge_recommendation` 删行 | ✅ 不越界 | 删的是新建表的行；审计保存全量快照（不破坏证据链） |
| `match_id` RESTRICT | ⚠️ **已声明** | C4 裁决保留；DR-1…DR-5 + 测试 40-43 显式声明并覆盖；**既有 CASCADE 链零改动**（DR-4） |
| 复用 `ApprovalStatus` | ✅ 不越界 | 仅作输入词汇；不建 `approval_status` 列（INV-1） |
| 不动 `owner_inbox` | ✅ 不越界 | D-2 / §6.4 留痕 |

---

## 16. F-R8 最终时序与幂等语义（**C1 专项**）

### 16.1 漂移判据

```python
drifted, detected = _reconcile_drift(session, rec)
# 判据（fail-closed：不可验证 ⇒ 不可信 ⇒ 撤销）
#   match 行不存在                      → drifted=True, reason="match_missing"
#   _attempt_from_evidence_refs() 返回 None → drifted=True, reason="attempt_unresolvable"
#   detected != rec.match_attempt       → drifted=True, reason="match_attempt_drift"
#   否则                                 → drifted=False
```

### 16.2 主时序（APPROVED 推荐 + W3-B 重算 → 惰性撤销）

```
t0  recommend_candidate(c1)
    → rec#1 {status=PROPOSED, match_attempt=3}；Candidate EVALUATED → RECOMMENDED
    audit: recommendation.proposed (after.match_attempt=3)

t1  decide_recommendation(rec#1, APPROVED, actor=owner)
    → _assert_owner_actor(actor) ✓
    → rec#1 {status=APPROVED, decided_by=owner, decided_at=t1}
    audit: recommendation.decided (before.status=PROPOSED, decision=approved, actor=owner)

t2  ⚠️ **生产不可达（C7 关键事实）**：`compute_match` 仅接受 `cand.status == EVALUATED`（`workforce.py:1447-1452`），
    而 `rec#1` 处于 `APPROVED` 时 Candidate=RECOMMENDED，且 `RECOMMENDED → EVALUATING` 不存在（§5.1）
    ⇒ 正常业务链路**无法**重算该 Match，attempt 漂移在生产中**不可直接产生**。
    ★ 为演示 F-R8 纵深防御，本时序用 **data-layer 注入**模拟漂移（直接 UPDATE Match.evidence_refs 改写 attempt=4），
      **禁止**为制造触发路径而修改 W3-B 状态机或调用 `compute_match`（C7）。
    ⇒ rec#1 进入「陈旧批准」：status=APPROVED，但 match_attempt=3 ≠ 注入的 4

t3  assert_trial_eligible(rec#1)    ← 惰性 reconcile 触发点
    ├─ _reconcile_drift: detected=4 ≠ 3 → drifted=True, reason=match_attempt_drift
    ├─ CAS: UPDATE recommendation SET status='withdrawn'
    │        WHERE id=rec#1 AND status IN ('proposed','approved')
    │                          AND match_attempt = 3   ← stored_match_attempt（C8：reconcile 读到的旧值，CAS 乐观锁 token）
    │   ├─ rowcount == 1 → 提交 SAVEPOINT（继续）
    │   └─ rowcount == 0 → 回滚 SAVEPOINT，零写入（他人已撤销），跳到返回
    ├─ append_audit(action='recommendation.withdrawn',           ← 同事务
    │       before={status:'approved', decided_by:owner, decided_at:t1,
    │               decision_rationale:..., match_attempt:3},
    │       after ={status:'withdrawn', detected_attempt:4,
    │               reason:'match_attempt_drift'})
    ├─ _sync_candidate_back: Candidate RECOMMENDED → EVALUATED
    │   （幂等守卫：若已是 EVALUATED 则跳过，不调用 require_transition）
    └─ return False                 ← fail-closed，不再返回 True

t4  assert_trial_eligible(rec#1)    ← 再次调用
    ├─ _reconcile_drift: detected=4 ≠ 3 → drifted=True
    ├─ status 已是 WITHDRAWN → 不在活状态集合 → **零写入、零审计、不动 Candidate**
    └─ return False                 ← 与 t3 结果相同，副作用为零 ⇒ 幂等

t5  recommend_candidate(c1)         ← 重建路径
    ├─ _reconcile_drift → drifted=True（status 已 WITHDRAWN，零写入）
    ├─ status == WITHDRAWN → 允许重建
    ├─ F-R1c…F-R5 复检（基于 attempt=4 的新 evidence）
    │   ├─ 通过 → rec#1 {status=PROPOSED, match_attempt=4,
    │   │                decided_by=None, decided_at=None, decision_rationale=None}
    │   │         Candidate EVALUATED → RECOMMENDED
    │   │         audit: recommendation.reproposed (before.match_attempt=3, after.match_attempt=4)
    │   └─ 不通过（如新 Match BLOCKED）→ 保持 WITHDRAWN，409/422
    │             Candidate 保持 EVALUATED，不产生 PROPOSED
    └─ return rec#1
```

### 16.3 并发语义

```
P1: _reconcile_drift(rec#1) ─┐
P2: _reconcile_drift(rec#1) ─┤ 并发（两个 session，均读到 status=APPROVED）

两者均执行 CAS: UPDATE … WHERE id=rec#1 AND status IN ('proposed','approved') AND match_attempt = :stored_match_attempt
   → 先提交者：rowcount = 1 → 写 WITHDRAWN + 1 行 withdrawn 审计
   → 后提交者：rowcount = 0 → SAVEPOINT 回滚 → 零写入、零审计

⇒ 恰好 1 行 withdrawn 审计
⇒ 两者均返回 False（fail-closed 一致性）
⇒ 无 IntegrityError 逃逸、无重复审计、无状态撕裂
```

### 16.4 幂等三重保证

| # | 机制 | 保证 |
|---|---|---|
| 1 | **单向状态迁移** | 撤销只对活状态 `{PROPOSED, APPROVED}` 生效；一旦 `WITHDRAWN` / `REJECTED`，后续 reconcile **零写入** |
| 2 | **CAS 条件更新** | `UPDATE … WHERE status IN ('proposed','approved') AND match_attempt = :stored_match_attempt` + `rowcount` 校验 → 并发下**恰好一次**撤销 |
| 3 | **状态与审计同事务** | SAVEPOINT 包裹「状态迁移 + 审计写入 + Candidate 回退」→ 要么全成、要么全不成，**不出现「撤销了但无审计」的留痕断裂**（契约 G / INV-5） |

### 16.5 事务边界

- `_reconcile_drift` 在**调用方事务内的 SAVEPOINT**（`session.begin_nested()`）中执行，与调用方后续动作（创建 / 重建 / 判定）同属一个外层事务
- reconcile **不调用** `session.commit()`（提交权归调用方，与 W3-B `compute_match` 一致）
- 单次 reconcile 最多产生：**1 次**状态迁移 + **1 行**审计 + **最多 1 次** Candidate 状态回退
- CAS `rowcount == 0`（已被他人撤销）→ 回滚 SAVEPOINT，**不报错**（幂等而非冲突）

### 16.6 V2 冲突定义的统一结果

| V2 冲突点 | V3 统一结果 |
|---|---|
| §5.2「漂移 → WITHDRAWN」 vs §8「新 attempt → PROPOSED」 | ✅ **统一为「先 withdrawn，再 reproposed」两段式**（单一语义，无仲裁歧义） |
| §5.2 `WITHDRAWN→PROPOSED` 审计 `proposed` vs §8 审计 `recomputed` | ✅ **统一为 `reproposed`**；`recomputed` action **删除** |
| §11「纯读守卫」 vs §5.2「系统写 WITHDRAWN」 | ✅ **放弃纯读**，`assert_trial_eligible` = 惰性 reconcile（C1） |
| §15.1 只允 3 个函数 vs 需要执行主体 | ✅ **私有 helper `_reconcile_drift` 明确列入**；公开函数增至 4（+`purge_recommendation`，D-R1） |

### 16.7 F-R8 = 纵深防御机制（**C7 知情追认**）

> **性质裁定**：F-R8 是**纵深防御（defense-in-depth）**机制，不是主链路常态。

| 项 | 内容 |
|---|---|
| **生产不可达** | 当前冻结 W3-B 生产路径下，Match attempt 漂移**不可直接由正常业务链路产生**。原因链：① `compute_match` 仅接受 `cand.status == EVALUATED`（`workforce.py:1447-1452`）；② `recommend_candidate` 成功时 Candidate 已升至 `RECOMMENDED`；③ `RECOMMENDED → EVALUATING` **不存在**（§5.1 受控边只含 `RECOMMENDED ⇄ EVALUATED`）；⇒ 已 APPROVED 的推荐其 Candidate 永远无法回到 EVALUATED 触发重算 |
| **演示须注入** | 因此 §16.2 的 t2/t3 **必须**使用 **fixture / data-layer 注入**（直接改 `Match.evidence_refs` 的 attempt）模拟漂移；**禁止**为制造生产触发路径而修改 W3-B 状态机、`compute_match` 或新增 `RECOMMENDED → EVALUATING` 边（§15.2 #20） |
| **保留价值** | 作为**未来演进 / W4 接入**时的纵深防御：若 W4 引入「RECOMMENDED 候选可被重新评估」或 Trial 阶段触发重算，F-R8 已在位，无需补建 |
| **不降级** | 虽生产不可达，F-R8 **仍必须保留**（fail-closed 兜底），不得因「当前触发不到」而从实现中删除 |

### 16.8 F-R8 CAS 撤销最终契约（**C8 采纳**）

所有 `_reconcile_drift` 对活状态 `{PROPOSED, APPROVED}` 的撤销，**必须**满足：

```python
# 1) 检测：
#    observed_attempt = 当前 Match.evidence_refs 解析出的 attempt（fail-closed：None → 视为漂移）
#    stored_match_attempt = reconcile 开始时从 Recommendation 行读取的 match_attempt 值（CAS 乐观锁 token）
# 2) 撤销：
UPDATE recommendation
   SET status = 'withdrawn'
 WHERE id = :rec_id
   AND status IN ('proposed', 'approved')
   AND match_attempt = :stored_match_attempt     # ★ C8：CAS 乐观锁 token（reconcile 读到的旧值；若他人已重建则不匹配→rowcount=0→不重复撤销）
# 3) 判定：
if cursor.rowcount == 1:
    session.flush()
    append_audit(action='recommendation.withdrawn', ...)   # 同事务
elif cursor.rowcount == 0:
    session.rollback_to_savepoint()                        # 他人已撤销
    rec = session.get(Recommendation, rec_id)              # 重新读取
    # 按当前实际状态判定：WITHDRAWN/REJECTED → return False（零写入）
```

| 要求 | 保证 |
|---|---|
| `match_attempt = :stored_match_attempt` 绑定 | CAS 乐观锁 token = reconcile 开始时从 Recommendation 行读取的 match_attempt；若该推荐已被他人基于新 attempt 重建（stored≠新值），CAS 不匹配 → rowcount=0 → 不重复撤销 |
| `rowcount == 1` 才写审计 | 并发下**恰好一次** `withdrawn` 审计（契约 G / INV-5） |
| `rowcount == 0` 重新读取判定 | 不报错、不写审计；返回与首次调用一致的结果（幂等） |
| 状态 + 审计同事务 | SAVEPOINT 包裹，要么全成要么全不成，无「撤销了但无审计」留痕断裂 |

---

## 17. 结论与残留待裁项

### 17.1 C1–C4 + P2-1 闭环状态

| 裁决 | 闭环 | 证据位置 |
|---|---|---|
| **C1** F-R8 执行主体 = 方案 A（惰性 reconcile） | ✅ **闭环** | §16（时序 / 并发 / 幂等 / 事务边界）；§5.2 明确唯一执行主体 `_reconcile_drift`；§5.3 删除冲突边；§16.6 逐条列出 V2 冲突的统一结果 |
| **C2** 删除 `approval_status`，`status` 唯一 SoT | ✅ **闭环** | §4.1 删除该列 + 索引改名；§4.4 明确 `EXPIRED` 不属于 W3-C 语义；§4.5 六条不变量（INV-1 物理层 / INV-2 类型 / INV-3 决策 / INV-4 边表 / INV-5 审计 / INV-6 执行者）；§5.2 / §7 / §9 全量同步 |
| **C3** 追认**四类**仅测试调整 | ✅ **闭环** | §13.8 精确到行号（C3① :694-698；C3② 12 文件常量 + 4 处注释；C3③ :611 tuple + :610 注释；**C3④ :746-748 全等断言**）；附「不得越界」保留清单（:612/:613/:625、w3b:704/721、cascade 测试） |
| **C4** 保留 RESTRICT + 域规则 + 级联测试 | ✅ **闭环** | §4.1 / §12.3 保留 RESTRICT；§7 F-R10；§12.4 域规则 DR-1…DR-5（含「既有 CASCADE 链零改动」）；§13.6 测试 40–43 |
| **P2-1** `actor` 必填 | ✅ **闭环** | §6.2 签名改为 `actor: ActorContext`（无默认值）；§6.3 从复用清单**移除** `resolve_owner_actor`；§7 F-R7 增列；§15.2 新增第 17 项禁止；§13.7 测试 44–46 |


### 17.1.5 V4 新增裁决闭环（C5–C8 + P2-3 / P2-5 / P2-6 / P2-7 / P3-1）

| 裁决 | 闭环 | 证据位置 |
|---|---|---|
| **C5** 受控测试调整扩为**四类**（新增 C3④ `w3a:746-748`） | ✅ **闭环** | §13.8 C3④（全等断言 `ALLOWED[EVALUATED]=={REJECTED}` → `=={REJECTED,RECOMMENDED}`）；§14 B（C3④ 改 1 个全等断言）；§17.1 C3 行；边界「四类」不得越界（不动其他 W3-A 测试） |
| **C6** 新增 F-R3b（attempt 不可解析 → 422 `match evidence is not resolvable`） | ✅ **闭环** | §4.1（`match_attempt` NOT NULL 写入前置说明）；§7 F-R3b；§10.3 UW-3；§13.9 测试 47、48；§14 F |
| **C7** F-R8 = 纵深防御（生产不可达；t2/t3 须 fixture 注入；禁改 W3-B） | ✅ **闭环** | §16.7（专章裁定）；§16.2 t2 改写为 data-layer 注入；§13.9 测试 51；§15.2 #20（禁止为触发漂移改 W3-B） |
| **C8** CAS 撤销绑定 `status IN (...) AND match_attempt = :stored_match_attempt`（reconcile 读到的旧值作为乐观锁 token） | ✅ **闭环** | §16.1 / §16.2 t3 / §16.3 / §16.4 #2 / **§16.8 最终契约**；§13.9 测试 49、50 |
| **P2-3** 保留 W3-B unbound waive 语义 | ✅ **闭环** | §9.1（证据链二分：必需环 vs 条件环）；§10.3（四场景矩阵 + UW-1…UW-4 + 反模式警示）；§13.9 测试 52；§15.2 #21；§7 F-R3 补充 |
| **P2-5** D-R3「downgrade 不重置」= 明示设计选择 + 后果 + W4 责任 | ✅ **闭环** | §12.5（默认不重置 + 残留风险 + W4 接手责任）；§17.2 D-R3 |
| **P2-6** `purge_recommendation(..., *, actor: ActorContext)` 禁止隐式 actor | ✅ **闭环** | §5.2 转移表 #7；§6.2 签名（actor 必填）；§7 F-R11；§13.9 测试 53；§9 审计 `recommendation.deleted` 落 owner_id |
| **P2-7** 统一 INV 编号为 INV-1…INV-6 | ✅ **闭环** | §4.5（六条不变量统一编号）；§0-bis 继承 |
| **P3-1** 修正 §9 脱敏注记，以 `audit.py` 实际双层行为为准 | ✅ **闭环（语义不变）** | §9.2（key 名层 + 值级层 `_SECRET_VALUE_RE`，实测 4 例）；明确「本轮不改变任何 W3-C 设计语义」 |

### 17.2 残留待裁项（**不阻塞审计，需 R7 在授权实现时一并确认**）

| # | 待裁项 | 默认建议 | 不确认的后果 |
|---|---|---|---|
| **D-R1** | 是否引入 `purge_recommendation` 作为 RESTRICT 的**解锁入口**（C4 衍生：无删除入口则已推荐候选人**永久**不可随 Job 删除，DR-1 无解锁路径） | ✅ 引入（含审计全量快照 `recommendation.deleted`；前置 status ∈ {WITHDRAWN, REJECTED}） | 若不引入：C4 只能测试「删除被拒」（测试 40/41/43），测试 42（解锁后删除成功）**不可实现**，契约 A/C 的「可逆」叙事不完整 |
| **D-R2** | 审计 action 命名 `recommendation.reproposed`（替代 V2 冲突的 `proposed` / `recomputed`） | ✅ 采用 `reproposed` | 仅命名；不影响语义。若偏好沿用 `recomputed` 亦可，但须同步改 §9 / §13 / §14-G |
| **D-R3** | downgrade 是否重置 `candidate.status='recommended'` 的行 | ✅ **不重置**（保持零数据修改、严格 additive/可逆）；残留风险由「代码与迁移同进同出」承担 | 若需重置，downgrade 将带数据修改，须单独授权并留痕 |

### 17.3 最终结论（V4）

> ## ✅ **V4 已达到「最后一次定向 DSH 审计」条件**
>
> - **V2 的 4 项 P1 全部闭环**（C1–C4），**V3 的 3 项 P1（P1-1/P1-2/P1-3）经 C5–C8 全部闭环**（§17.1 + §17.1.5）
> - **P2-1…P2-7 全部闭环**；P3-1 已按 `audit.py` 实际双层脱敏行为更正（语义不变）；P2-3 已明裁保留 unbound waive
> - **C5（四类测试调整）/ C6（F-R3b）/ C7（F-R8 纵深防御）/ C8（CAS 绑定）/ P2-3（unbound waive）/ P2-5（D-R3 后果）/ P2-6（purge actor）/ P2-7（INV 编号）全部闭环**
> - **0 个 P0 未决**；§14 七契约判据均已升级为**可字面断言**形式（revision 钉死、测试数统一为 **53**、审计 action 均可真实触发、C3 四类调整可核验）
> - §15.2 从 15 项扩为 **21 项**，并完成 V4 全量越界复检（§15.3），**无自相矛盾**
>
> **⚠️ 但 V4 本身不构成实现授权。** 按既定协议，本轮为纯设计交付：
> **未写实现代码 / migration / 测试代码，未 commit / push / PR。**
> 下一轮须由独立审计方（DSH 路径③）按 §14 七契约（现含 §13.9 的 53 项测试判据）对 V4 复检，再由 R7 针对 exact-head SHA 显式授权，方可进入 TDD 实现。

---

> **本阶段到此为止**：未写实现代码、未写 migration、未写测试代码、未 commit / push / PR。
> 当前工作树除设计/审计 `.md` 外无任何改动，W3-B exact tree `c837d78a…` 保持纯洁。
