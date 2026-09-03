# Workforce W3-C Spec V2 — DSH 独立审计报告

> **审计性质**：设计阶段（pre-implementation）独立审计，只读代码考古 + Spec 逐条契约核对。
> **本轮硬约束遵守**：未修改任何代码 / migration / 测试 / Spec；未 commit / push / PR；未做任何实现性 workaround。

---

## 0. 审计基线（只读核验，全部实测）

| 项 | 实测值 | 结论 |
|---|---|---|
| 分支 | `w3b-match-benchmark` | ✅ |
| HEAD | `6c08d044d2049f7fb48467e2e89c0b30baecba75` | ✅ 与声明一致 |
| HEAD tree | `c837d78a6ce28474adb045b4e0cd1907c3cf70b0` | ✅ = W3-B exact tree，零漂移 |
| `git diff HEAD -- src alembic tests` | **空** | ✅ 代码零改动 |
| 工作树 | 仅 3 个未跟踪 `.md`（W3B 审计报告 / W3C Spec V2 / W3C Spec V1） | ✅ 纯洁 |
| 当前 alembic 单 head | `20260901_0001_workforce_match_benchmark` | ✅ 单 head |

**审计对象文件**：`docs/Workforce_W3C_Recommendation_Approval_Spec_V2.md`
> ⚠️ 命名更正：R7 指令中写作 `..._Trial_Spec_V2.md`，实际落盘文件**不含** `Trial`（V2 已把 Trial 移出范围）。V1（含 Trial 字样）已被 V2 取代作废。

---

## 1. Spec 前置假设的独立代码复核

审计原则：**不信任 Spec 自述，只认代码行号**。

| # | Spec V2 假设 | 代码实测 | 判定 |
|---|---|---|---|
| 1 | `CandidateStatus.RECOMMENDED` 已是真实枚举成员 | `models.py:1571` ✅；其 docstring 明确写 *"deliberately left UNREACHABLE … **until the W3-C/D Match gate is implemented**"* | ✅ PASS |
| 2 | `candidate.status` 是 `sa.String()`，加边零 schema 改动 | `models.py:1560-1562` + 实测列类型 | ✅ PASS |
| 3 | `ALLOWED[RECOMMENDED] = set()` | `workforce.py:479` ✅ | ✅ PASS |
| 4 | `ALLOWED[EVALUATED] = {REJECTED}` | `workforce.py:476` ✅ | ✅ PASS |
| 5 | `Match` UNIQUE(candidate_id, job_version_id) | `models.py:1769-1775` ✅ | ✅ PASS |
| 6 | `Match.breakdown["excluded"]` 恒含 reliability/historical/cost | `workforce.py:1535-1539` ✅ | ✅ PASS |
| 7 | `Match.evidence_refs[0] = "cand:{id}:attempt:{n}"` | `workforce.py:1543` ✅ | ✅ PASS |
| 8 | `_attempt_from_evidence_refs` 可复用 | `workforce.py:1398-1412`；只解析 `refs[0]`，格式 `cand:x:attempt:N` | ✅ PASS（有约束，见 P2-3） |
| 9 | `compute_match` SAVEPOINT + IntegrityError 吸收 | `workforce.py:1617-1637` ✅ | ✅ PASS |
| 10 | `rank_candidates` 纯读无审计（先例） | `workforce.py:1641+` ✅ | ✅ PASS |
| 11 | `Approval.project_id` NOT NULL → 物理复用不可行 | `models.py:499` + `services.py:232` ✅ | ✅ PASS（D-1 属实） |
| 12 | `INBOX_KINDS` 无 workforce；`decide` 强制 live project | `owner_inbox.py:140` / `:2111` ✅ | ✅ PASS（D-2 属实） |
| 13 | `append_audit(project_id=None)` 可复用 | `audit.py:110-118` 签名支持 `str \| None` ✅；W3-B 全部调用传 `None` | ✅ PASS |
| 14 | `ApprovalStatus` / `RiskLevel` 可复用 | `models.py:190-203`：`PENDING/APPROVED/REJECTED/**EXPIRED**`、`L0..L4` ✅ | ✅ PASS（EXPIRED 未定义语义 → P1-2） |
| 15 | `resolve_owner_actor()` / `_assert_owner_actor()` 可复用 | `actor.py:53` / `:79` ✅；`services.decide_approval:331-333` 与 Spec §6.2 伪代码**逐行同构** | ✅ PASS（继承风险 → P2-1） |
| 16 | evaluation_context 三维度 status 为字符串状态量 | `workforce.py:862-874`：`unknown` / `future_capability` ✅ | ✅ PASS（无 `scored` 键 → P2-4） |
| 17 | `recommendation_blocked_reason` 前向契约 | `workforce.py:877-880` ✅ | ✅ PASS |
| 18 | `new_id` 产出不含 `:`（attempt 解析安全） | `models.py:29-30` = `f"{prefix}_{uuid4().hex[:12]}"`；实测 `cand_15aa63a459c5` | ✅ PASS |
| 19 | 尚无 `recommendation` / `trial` / `employee` 表 | grep 全仓无 `__tablename__` 命中 ✅ | ✅ PASS |
| 20 | Workforce 链无 `project_id` | `models.py:1396-1620` 无 project 字段 ✅ | ✅ PASS |

**结论：Spec V2 的 20 项前置假设全部属实，代码考古扎实，无一处虚构。** 这是 Spec 的高质量之处。

---

## 2. DSH 七契约逐条判定

| 契约 | 内容 | 判定 | 阻塞项 |
|---|---|---|---|
| **A** | 单 head 不可变 / additive / 可逆 | **CONDITIONAL PASS** | P3-1（revision 含占位符 `X`）、P1-3②（12 文件 head 常量） |
| **B** | 状态机边界（既有边零改动） | **CONDITIONAL PASS** | P1-3①（1 个 illegal tuple）、P2-6（workforce.py 改动范围未约束） |
| **C** | downgrade 完整性 | **PASS**（附 P2-5） | downgrade 不重置 `status='recommended'` 行 |
| **D** | SSoT 零新能力词 | **PASS** | — （双状态列属 P1-2，不违反 D 字面） |
| **E** | 契约测试 | **FAIL → CONDITIONAL** | P1-3（T-REG 清单严重不全） |
| **F** | fail-closed 语义 | **CONDITIONAL PASS** | P2-1（actor 默认 owner）、P2-3（attempt=None 静默失效） |
| **G** | 可解释评分 + 审批留痕 | **FAIL → CONDITIONAL** | P1-1（`recommendation.withdrawn` 无执行主体）、P2-2（decided_by 未入审计快照） |

**汇总：0 P0 / 4 P1 / 7 P2 / 4 P3。无 P0（无安全漏洞、无设计根本错误）。**

---

## 3. 问题清单

### 🔴 P1 — 必须修复，否则实现必然返工或契约不可验证

#### P1-1 · F-R8 缺少执行主体，且 §5.2 / §8 / §11 三处互相冲突

**证据链**：
- `compute_match`（W3-B，**已冻结**）在新 attempt 时做 in-place UPDATE 并写 `match.recomputed` 审计（`workforce.py:1560-1585`）——它**不会**通知 Recommendation，W3-C 也不许改它。
- §5.2 规定 `APPROVED → WITHDRAWN`（仅系统，写 `recommendation.withdrawn` 审计）。
- §11 规定 `assert_trial_eligible` 是**纯读、不写审计**。
- §15.1 允许实现的函数只有 `recommend_candidate` / `decide_recommendation` / `assert_trial_eligible`（+ 私有 helper）。
- §8 又规定「同键 + 新 attempt」→ 状态回 `PROPOSED` + 审计 `recommendation.recomputed`。

**冲突**：
1. **没有**任何被允许的函数能承担「系统写 WITHDRAWN」——`assert_trial_eligible` 被 §11 禁止写入。
2. 同一触发条件（新 attempt）在 §5.2 走向 `WITHDRAWN`、在 §8 走向 `PROPOSED`，**无仲裁规则**。
3. §5.2 的 `WITHDRAWN → PROPOSED`（审计 `recommendation.proposed`）与 §8 的新 attempt → `PROPOSED`（审计 `recommendation.recomputed`）：同源同目标，**审计 action 不同**。

**后果**：§9 审计清单中的 `recommendation.withdrawn` **永远不会产生** → 契约 G「审计 action 齐备」**不可验证**；测试 12 只断言返回 False，无法断言审计行。

**修复建议（二选一，需 R7 裁决）**：
- **方案 A（推荐）**：把 `assert_trial_eligible` 升级为**惰性 reconcile**——检测到 attempt 漂移时执行 `APPROVED→WITHDRAWN` 并写 `recommendation.withdrawn` 审计，然后返回 False。放弃 §11「纯读」。优点：**无需调用方自律即自动 fail-closed**；测试 12 增补断言 `status == WITHDRAWN` 且审计行存在。
- **方案 B**：保留纯读，§15.1 增列 `reconcile_recommendation(session, recommendation_id)`，由 W4/owner 显式调用。缺点：**不"自动"**，W4 若忘记调用则 fail-open。

---

#### P1-2 · `status` 与 `approval_status` 双状态列无不变量

**证据**：§4.1 同时定义 `status: RecommendationStatus`（proposed/approved/rejected/withdrawn）与 `approval_status: ApprovalStatus`（pending/approved/rejected/**expired**）。
- §5.2 状态机操作 `status`；F-R6 / 测试 23 判定 `approval_status`。
- `ApprovalStatus.EXPIRED` 在 Spec 全文中**从未出现**，语义未定义。
- Spec 未给出两列的映射不变量（如 `status=APPROVED ⇔ approval_status=APPROVED`）。

**后果**：DB 可存 `status=APPROVED, approval_status=REJECTED` 的矛盾行；实现者会各自选择主列 → 判定逻辑分叉。也构成对硬约束 11「不重复造轮子」的内部违背。

**修复建议（推荐）**：**删除 `approval_status` 列**，`status: RecommendationStatus` 作为唯一 SoT；保留 `risk_level=L4` / `decided_by` / `decided_at` / `decision_rationale` / `approval_id`（前向列）。「复用既有 Approval 语义」由 `RiskLevel` + owner 断言 + `append_audit` + SAVEPOINT 承担，不依赖该列。若不删，则必须在 §4.1 显式写出双列不变量与 EXPIRED 语义。

---

#### P1-3 · 受控测试调整清单严重不完整（且部分超出 Q2 授权字面范围）

Spec §13 T-REG 只列了 `test_workforce_models.py` 的 alembic head 断言。实测**必要调整远超此范围**：

| # | 位置 | 现状 | W3-C 建表后 | 是否被 §13 覆盖 |
|---|---|---|---|---|
| ① | `tests/test_workforce_evaluation_w3a.py:694-699`（`test_w3a_is_zero_migration`，函数起于 :675） | `for deferred in ("recommendation","trial","candidate_evaluation"): assert deferred not in tables` | **`recommendation` 被建 → 断言失败（红）** | ❌ **未覆盖** |
| ② | **12 个测试文件**携带 head 常量 `20260901_0001_workforce_match_benchmark` | test_content_draft / test_cs_migration / test_feedback_zero_migration / test_knowledge_models / test_review_binding_migration / test_series_id_metadata_precheck / test_series_id_migration / test_v4_agent_platform / test_work_log / test_workforce_benchmark_match_w3b / test_workforce_evaluation_w3a / test_workforce_models | 全部需同步为新 head（W3-C 自身测试保留旧 head 作 downgrade 目标，同 W3-B 模式） | ❌ **仅提 1 个文件** |
| ③ | `tests/test_workforce_models.py:611`（`test_candidate_illegal_transition_rejected_409`，函数起于 :555） | illegal_edges 含 `(EVALUATED, RECOMMENDED)` | 需删除**该 1 个** tuple | ⚠️ §5.1 提及但用"若"字，实际**确实存在** |

**Q2 授权范围缺口**：R7 的 Q2=YES 措辞为「对 W3-A『RECOMMENDED 不可达』守卫进行受控解冻」——**字面只覆盖 ③**。①（deferred-table）与 ②（12 文件 head 常量）属不同性质的必要调整，**需 R7 追加追认**。

**好消息**：③ 的规模极小且边界干净——`(POOLED, RECOMMENDED)`（:612）、`(RECOMMENDED, POOLED)`（:613）、自环 `(RECOMMENDED, RECOMMENDED)`（:625）在 W3-C 下**仍然非法**，全部可保留，只需移除 :611 一行。

---

#### P1-4 · `match_id` RESTRICT 改变既有级联删除语义，Spec 未声明、无测试、无解锁路径

**证据**：
- `src/aios/db.py:43-45` 注册 `PRAGMA foreign_keys=ON` → **SQLite 外键真实强制**（实测 `PRAGMA foreign_keys = (1,)`）。
- 既有链路：`Job → JobVersion → Candidate → {Match, BenchmarkResult}`，全部 `ondelete="CASCADE"`。
- §4.1 新增 `recommendation.match_id → match.id` `ondelete="RESTRICT"` → 这是 Candidate 下行链中**第一个非 CASCADE 依赖**。

**后果**：一旦某 Candidate 有 Recommendation，
- 删 Job → JobVersion CASCADE → Candidate CASCADE → Match CASCADE ← **被 recommendation.match_id RESTRICT 阻断 → IntegrityError**
- 现有 `test_candidate_cascade_on_job_delete`（`test_workforce_models.py:632`）当前无 recommendation 行故不受影响，但**该级联能力在 W3-C 后对被推荐候选人失效**。

**性质**：这是对 W3-A/W3-B 聚合行为的**语义变更**（虽未改其代码），且与「W3 冻结、禁止修改既有语义」存在张力；Spec 全文未声明。

**修复建议（需 R7 裁决）**：
- **方案 a（推荐，契合 Spec 意图）**：保留 RESTRICT，但必须①在 Spec 显式声明「已推荐候选人不可删除，须先 WITHDRAWN / REJECT」；②§13 增补级联测试（断言删除被拒且错误信息明确）；③提供解锁路径。
- **方案 b**：改 `ondelete="CASCADE"` → 保住级联 UX，但**已批准推荐的审计证据被静默销毁**，与 §9 / 契约 G 冲突，不推荐。
- **方案 c**：`SET NULL` + 可空 → evidence 链断裂（F-R3），不推荐。

---

### 🟡 P2 — 应修复（不阻塞，但影响健壮性/可审计性）

| ID | 问题 | 证据 / 影响 | 建议 |
|---|---|---|---|
| **P2-1** | `decide_recommendation(actor=None)` 默认 `resolve_owner_actor()` → **潜在自动批准通道** | 与 `services.decide_approval:331-333` **逐行同构**（属既有先例，非 Spec 自创）；但硬约束 7 与 F-R7 要求「禁止任何自动绕过 Approval 的路径」——省略 actor 即得 owner 权限，字面冲突 | `actor` 设为**必填**（无 None 默认值）。当前无路由层调用，零迁移成本，严格强于先例 |
| **P2-2** | `recommendation.recomputed` 审计缺 `before.decided_by` / `before.decided_at` | §9 只列 `before.{score,status,approval_status}`；而 §8 明确「清空 decided_by/decided_at」→ **人类批准证据被覆盖后不可追溯**，审计链断裂 | 审计 before 增补 `decided_by` / `decided_at` / `decision_rationale` |
| **P2-3** | attempt 解析失败 → F-R8 静默**失效**（fail-open） | `_attempt_from_evidence_refs` 解析失败返回 `None`；`match_attempt` 声明为 `int`（非 Optional）。若写入 None，则 `None == None` 判定为「无漂移」→ 漂移检测静默失效 | 解析失败即 422（并入 F-R3），或显式规则「无法验证 attempt ⇒ 视为漂移 ⇒ WITHDRAWN」 |
| **P2-4** | `unknown_dimensions.scored` 措辞矛盾 | 实测 `evaluation_context` 的 cost/reliability/historical 三段**只有 `status` + `reason`，无 `scored` 键**（`workforce.py:862-874`）。`scored: false` 是 W3-C 派生断言，而 §4.3 写「禁止补全」 | 语义无害（是加固而非虚构），但需澄清：「禁止补全」指**数值**，不指该守卫标志位 |
| **P2-5** | downgrade 不重置 `Candidate.status='recommended'` 的行 | 迁移 downgrade 仅删表 → 残留 status 为 recommended 的 Candidate，在无 W3-C 代码的旧版本下**不可达且不可解释** | 决策：downgrade 是否需 `UPDATE candidate SET status='evaluated' WHERE status='recommended'`（会产生数据修改，需 R7 明示） |
| **P2-6** | §15.2 未约束 `workforce.py` 的改动范围 | W3-C **必须**改 `workforce.py` 的 `CandidateLifecycle.ALLOWED`（W3-A/W3-B 冻结函数同在此文件），但 §15.2 禁止清单只列 engine/ruleset/owner_inbox/services/models，**对 workforce.py 沉默** | §15.2 增列：「`workforce.py` 仅允许修改 `CandidateLifecycle.ALLOWED` 字典，其余函数零改动」 |
| **P2-7** | Q1=B 后 L4 闸不在 `owner_inbox`，与硬约束 7 字面偏离 | 硬约束 7 原文「Approval(**owner_inbox** / L4)」；Q1=B 明确放弃 owner_inbox（结构性不可行，D-2 已论证）。R7 知情后选 B，属**知情接受** | 需**书面留痕**该偏离：「W3-C 的 L4 闸为域内闸门（owner actor 断言 + append_audit），owner 可见性由 W4 引入 Project 后补齐」 |

### ⚪ P3 — 文档细节

| ID | 问题 | 建议 |
|---|---|---|
| P3-1 | §12 revision 字符串含占位符 `2026090X_0001_workforce_recommendation` | 需钉死为具体日期（如 `20260902_0001_workforce_recommendation`）；否则契约 A 与测试 29 无法字面断言 |
| P3-2 | §14 契约 E 写「T-REC-\* **28** 项」，§13 实列 **30** 项（4+8+4+3+5+4+2） | 统一为 30 |
| P3-3 | 文件名：R7 指令写 `..._Trial_Spec_V2.md`，实际为 `Workforce_W3C_Recommendation_Approval_Spec_V2.md`（无 Trial） | 以实际文件为准；V1（含 Trial）已作废 |
| P3-4 | §5.1 注「既有测试…仍针对 POOLED→REJECTED 之类路径」措辞不准；§16 Q2 用「**若**存在」 | 事实已确认：**确实存在**，且恰为 1 个 tuple（:611）。措辞改「确实存在」 |

---

## 4. §15.2 禁止事项越界检查

逐条核对 15 项禁止项与 Spec 全部设计（§4–§12）的相容性：

| 禁止项 | 设计是否越界 | 说明 |
|---|---|---|
| ❌ 改 engine / ruleset / owner_inbox / services / models 既有定义 | ✅ 无越界 | Q1=B 只追加 models.py 尾部区段，不碰既有定义 |
| ❌ 改 W3-A / W3-B 任何语义 | ⚠️ **边界风险** | 函数级无越界；但 **P1-4**（RESTRICT 级联）构成聚合行为变更，需显式声明 |
| ❌ 写 `evaluation_context` | ✅ 无越界 | W3-C 只读快照 |
| ❌ 除受控边外写 `Candidate.status` | ✅ 无越界 | 仅 EVALUATED⇄RECOMMENDED |
| ❌ 调用 compute_match / run_benchmark / evaluate_candidate | ✅ 无越界 | §10.1 明确禁止 |
| ❌ 调用 check_budget / Scheduler / execute_task | ✅ 无越界 | Q5 递延 |
| ❌ 创建 Trial/Employee/Training/Performance 表或字段 | ✅ 无越界 | D-3 / D-5 |
| ❌ 新增 `CandidateStatus.TRIALING` 等未来状态枚举 | ✅ 无越界 | §4.4 明确不新增 |
| ❌ 接入 ai-arena / 改 ruleset | ✅ 无越界 | — |
| ❌ 新增 Capability 词汇 / 第二套 SSoT | ✅ 无越界 | 契约 D PASS |
| ❌ 虚构 reliability/historical/cost 数值 | ✅ 无越界 | F-R4 / F-R5；`cost_advisory` 为 `str` |
| ❌ 任何自动绕过 Approval 的路径 | ⚠️ **潜在风险** | **P2-1**（actor 默认 owner）字面构成「省略即 owner」通道，建议收紧 |
| ❌ commit / push / PR / merge 未经授权 | ✅ 无越界 | §15.2 已声明 |

**15.2 完整性缺口**：未覆盖
1. `workforce.py` 的改动范围（→ P2-6）；
2. 测试文件的受控调整范围（→ P1-3）；
3. `recommendation` 表外键策略对既有级联的影响（→ P1-4）。

---

## 5. 审计结论

### **GO WITH CONDITIONS**

**Spec V2 的质量评价**：20 项代码前置假设**全部属实，零虚构**；D-1/D-2 对历史 proposal 的证伪（Workforce 域 vs Project 域不相交）是本轮最有价值的考古发现，直接避免了实现期踩坑；Q1=B 在给定约束下是正确选择（零改既有表、严格 additive、保留升级路径）。

**但存在 4 项 P1，其中 2 项会导致契约不可验证或实现返工，1 项超出 R7 已授权的调整范围。**这些都不是设计根本错误，全部可通过 Spec 增补/澄清解决，**无需回到设计阶段重做**。

### 进入实现前必须裁决的 4 项

| # | 待裁项 | 建议 |
|---|---|---|
| **C1**（P1-1） | F-R8 执行主体 | **方案 A**：`assert_trial_eligible` 升级为惰性 reconcile（放弃纯读），自动 `APPROVED→WITHDRAWN` + 写审计 |
| **C2**（P1-2） | 双状态列 | **删除 `approval_status`**，`status: RecommendationStatus` 为唯一 SoT |
| **C3**（P1-3） | 受控测试调整范围追认 | 追认① deferred-table 断言移除 `"recommendation"`；② 12 文件 head 常量同步；③ 删除 1 个 illegal tuple（:611） |
| **C4**（P1-4） | 外键策略 / 级联语义 | **方案 a**：保留 RESTRICT + 显式声明「已推荐候选人须先撤回方可删除」+ 增补级联测试 |

> C1/C2/C4 属设计取舍（R7 可在授权实现时一并确认）；**C3 属授权范围扩展，必须单独明示**——Q2=YES 的字面范围不足以覆盖 ①②。

### 是否具备 R7 授权条件

> ## ❌ **当前不具备，差 4 项裁决**
>
> R7 就 C1–C4 明确表态后（可与「授权进入 W3-C 实现」合并为一条指令），即具备授权条件，**无需再走一轮完整 Spec 设计**。

### 裁决后流程（沿用 W3-B 既有链路）

1. R7 裁决 C1–C4 + 授权实现
2. TDD 实现（先测试后实现），30 项 T-REC + 受控测试调整（P1-3）+ 回归 ≥246 + ruff
3. DSH 路径③七契约独立审计（本报告 §2 为判据基线，届时所有 CONDITIONAL 须转为硬 PASS）
4. R7 针对 **exact-head SHA** 显式授权
5. 仅 **Squash Merge** 合入 `main`（⚠️ 勿用 Create a merge commit —— PR#4 已因此产生「结构偏差」留痕）

---

## 6. 本轮硬约束遵守声明

| 约束 | 状态 |
|---|---|
| 不修改任何代码 | ✅ `git diff HEAD -- src alembic tests` 为空 |
| 不修改 migration | ✅ |
| 不新增测试 | ✅ |
| 不 commit / push / PR | ✅ HEAD 仍 `6c08d04`，工作树仅 3 个未跟踪 `.md` |
| 不改变 Spec | ✅ Spec V2 未被编辑 |
| 不进行实现性 workaround | ✅ 全程只读命令（grep / sed / awk / git rev-parse / python 只读探测） |

> 唯一副作用：为满足 P1-3② 的实测需要，在临时目录创建过 `fk_probe.db`（SQLite 探测库），位于系统 temp，不入仓。
