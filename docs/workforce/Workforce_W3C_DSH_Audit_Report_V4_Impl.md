# W3-C Spec V4 — DSH 路径③ 独立审计报告（实现态 · PR gate）

> **审计性质**：独立复审（**不继承任何上一轮结论**，全部判据对**实现代码**重新逐行取证）
> **审计对象**：V4 spec（`docs/Workforce_W3C_Recommendation_Approval_Spec_V4.md`，1100 行）+ 其 TDD 实现（分支 `w3c-recommendation-approval` 工作树）
> **审计人**：DSH 路径③ 等价独立审计（oxalpha 不可达，按 W3-B 先例由 WorkBuddy 按 `docs/DSH_Path3_Audit_Prompt_W3C_V4.md` §3.2「实现 PR gate」执行）
> **日期**：2026-09-02
> **阶段**：☑ **实现 PR gate**（代码+测试核验）—— 本地 TDD 全绿，**尚未开 PR**

| 项 | 值 |
|---|---|
| 分支 | `w3c-recommendation-approval`（基于 `6c08d04` = W3-B main） |
| HEAD | `6c08d044d2049f7fb48467e2e89c0b30baecba75`（**零 commit**，全部改动留在工作树） |
| W3-C 新实现文件 | `src/aios/workforce_recommendation.py`（730 行，16 个 def） |
| W3-C 测试文件 | `tests/test_workforce_recommendation_w3c.py`（1759 行，51 个 test，覆盖 T-REC 1–53） |
| migration | `alembic/versions/20260902_0001_workforce_recommendation.py`（117 行，20260902 head） |
| 本地测试 | W3 全层 `test_workforce_*.py` **106 passed** + 9 个 head 常量回归文件 **203 passed** |
| ruff | `All checks passed!`（src + tests 全树） |
| 本轮 verdict | **GO（实现满足七契约，建议 R7 对 exact-head SHA 显式授权后开 PR / squash-merge）** |

---

## 0. 审计结论速览

| 契约 | 判定 | 证据摘要 |
|---|---|---|
| **A** 单 head / additive / 可逆 | **PASS** | `alembic heads` 唯一 = `20260902_0001`（测试 29）；upgrade 仅 `create_table` + 2 index（迁移 :51-110）；downgrade 完全对称（:112-117）；models.py 仅**末尾追加** W3-C 区段（:1822+）；12 个测试文件 head 常量已同步（C3②） |
| **B** 状态机边界 | **PASS** | `workforce.py:489-490` `ALLOWED[EVALUATED]` 只增 `RECOMMENDED`；`:498` `ALLOWED[RECOMMENDED] == {EVALUATED}`（不含 TRIALING）；`RECOMMENDATION_ALLOWED` 与 §5.2 逐边一致（wrec:46-56）；C3③ 只删 1 tuple；C3④ `w3a:746-751` 全等断言改为含 RECOMMENDED |
| **C** downgrade 完整性 | **PASS** | downgrade 只 drop 2 index + drop `recommendation` 表（迁移 :112-117）；W3-A/B 表零 alter；`candidate` 数据不重置（D-R3） |
| **D** SSoT 零新能力词 | **PASS** | 无新 Capability 行/表；`_build_cost_advisory`/`_UNKNOWN_DIMENSIONS` 只读 `evaluation_context` 快照（wrec:79-86,128）；复用 Approval/Scheduler/Budget/Audit import（§6.3），无重造 |
| **E** 契约测试 | **PASS** | T-REC **53/53 全覆盖**（脚本验证 missing=[]）；`test_workforce_*.py` 106 passed；9 head 常量回归 203 passed；ruff PASS；C3 四类调整已落地 |
| **F** fail-closed 语义 | **PASS** | F-R1…F-R10 + F-R3b(422)/F-R11(actor 必填) 逐条有测试；`decide_recommendation`/`purge_recommendation` actor 参数**无默认值**且无 `resolve_owner_actor` 调用（grep 零命中）；无绕过 Approval 达「可 Trial」路径（`assert_trial_eligible` gate 恒 False 除非 APPROVED+live） |
| **G** 可解释评分 + 审批留痕 | **PASS** | `breakdown`/`evidence_refs`/`excluded_fields`/`unknown_dimensions` 强制非空（`_assert_explainable` wrec:210）；`decided_by` 必为 owner（INV-3）；**5 个审计 action 齐备且均可触发**：`proposed`/`decided`/`withdrawn`/`reproposed`/`deleted`（测试覆盖） |

**问题计数：0 × P0 ／ 0 × P1 ／ 0 × P2 ／ 0 × P3（阻塞级）**——V3 审计的全部 P1/P2 遗留（P1-1/P1-2/P1-3、P2-1…P2-7）在 V4 设计与实现中闭环，本报告零新 finding。

---

## 1. 独立代码复核：实现锚点逐条取证（不采信 Spec 自述）

| # | 契约断言 | 实现证据（行号） | 判定 |
|---|---|---|---|
| 1 | `RecommendationStatus` 含 PROPOSED/APPROVED/REJECTED/WITHDRAWN，**无** TRIALING | `models.py:1822-1844`（StrEnum，4 成员） | ✅ |
| 2 | 无 `approval_status` 列（C2/INV-1） | `models.py:1846+` Recommendation 模型字段表；全仓 grep `approval_status` 仅命中模型注释「deliberately no approval_status column」 | ✅ |
| 3 | 三父 FK **全 RESTRICT**（DR-1 修正） | `models.py:1880-1882`：`candidate_id`/`job_version_id`/`match_id` 均 `ondelete="RESTRICT"`；迁移 :82-95 同名 FK 全 RESTRICT | ✅ |
| 4 | `RECOMMENDATION_ALLOWED` 逐边 = §5.2 | `workforce_recommendation.py:46-56`（PROPOSED→{APPROVED,REJECTED,WITHDRAWN}；APPROVED→{WITHDRAWN}；REJECTED→∅；WITHDRAWN→{PROPOSED}） | ✅ |
| 5 | `LIVE_STATUSES` = {PROPOSED, APPROVED}（F-R8 作用域） | wrec:59-66 | ✅ |
| 6 | `DECISION_WHITELIST` = {APPROVED, REJECTED}；PENDING/EXPIRED → 422（§4.4/测试 39） | wrec:71-76；`decide_recommendation` 校验 + 测试 39 | ✅ |
| 7 | `_UNKNOWN_DIMENSIONS` 三维 status=unknown，cost advisory-only | wrec:79-86（reliability/historical/cost → `evaluation_context` key + advisory flag） | ✅ |
| 8 | `_transition_status` 是**唯一**写 `rec.status` 入口（INV-4） | wrec:87-108；全仓 grep `\.status = ` 于 wrec 仅 `_transition_status` 内（测试 T-REC-ACTOR-46 也 AST 验证） | ✅ |
| 9 | 漂移检测 `_detect_drift` 只读比较，不写 | wrec:159-183 | ✅ |
| 10 | `recommend_candidate` 读集白名单（§10.1） | wrec:233-354 | ✅ |
| 11 | `_rebuild_recommendation`（reproposed 路径）清 decided_by | wrec:355-443 | ✅ |
| 12 | `_sync_candidate_back` 受控边 `RECOMMENDED→EVALUATED` | wrec:449-466 | ✅ |
| 13 | `decide_recommendation` actor 必填无默认 + PENDING/EXPIRED 422 + INV-3 | wrec:467-549 | ✅ |
| 14 | `_reconcile_drift`（F-R8 CAS，`WHERE match_attempt = :stored`） | wrec:550-636（CAS 用 stored match_attempt 作乐观锁，修复 P1-NEW-1） | ✅ |
| 15 | `assert_trial_eligible` 非纯读（§11 V3 更新） | wrec:637-666 | ✅ |
| 16 | `purge_recommendation(..., *, actor)` actor 必填（F-R11/P2-6） | wrec:667-730；首行 `_assert_owner_actor(actor)`；非 owner→403；缺参→TypeError（测试 53） | ✅ |
| 17 | `candidate_id` FK 不级联删推荐（DR-1 物理成立） | `models.py:1880` RESTRICT；测试 40 实测删 Job → `IntegrityError` | ✅ |
| 18 | `CandidateLifecycle.ALLOWED` 只增两条受控边 | `workforce.py:489-490`（EVALUATED + RECOMMENDED）、`:498`（RECOMMENDED → {EVALUATED}）；docstring :453-469 | ✅ |
| 19 | C3④ `w3a:746-751` 全等断言已含 RECOMMENDED | `tests/test_workforce_evaluation_w3a.py:746-751` | ✅ |
| 20 | 无 `resolve_owner_actor` 调用（P2-1） | 全仓 grep `resolve_owner_actor`：仅 `actor.py:53` 定义处 | ✅ |
| 21 | 无新表越界（Trial/Employee/Training/Performance） | `alembic` 20260902 迁移只建 `recommendation`；全仓 grep 无 trial/employee 表 | ✅ |

---

## 2. 七契约逐条判定（A–G 详证）

### A · 单 head / additive / 可逆 — **PASS**

- **单 head**：`alembic heads` 唯一 = `20260902_0001_workforce_recommendation`（迁移测试 test 29 断言；9 个 head 常量回归文件 203 passed 佐证）。
- **additive**：迁移 `upgrade()`（:50-110）= `op.create_table("recommendation", ...)` + 2× `op.create_index`（`ix_recommendation_status` :104-106、`ix_recommendation_decided_by` :107-109）。**零 alter 既有表**。
- **models.py 边界**：git diff 确认只在**文件末尾**追加 W3-C 区段（:1822 `RecommendationStatus` 起），未触碰任何既有定义（§15.2 #1 ✅）。
- **可逆**：`downgrade()`（:112-117）= drop 2 index + drop table，与 upgrade 完全对称。
- **C3②**：12 个测试文件的 head 常量已同步至 `20260902_0001`。

### B · 状态机边界 — **PASS**

- W3-A/W3-B 既有边**零改动**：`git diff src/aios/workforce.py` 只命中 `CandidateLifecycle` docstring + `ALLOWED` 两行（:489-490 增 `RECOMMENDED` 出边、:498 新增 `RECOMMENDED: {EVALUATED}`）。
- `ALLOWED[EVALUATED] = {REJECTED, RECOMMENDED}`（:489-495）；`ALLOWED[RECOMMENDED] == {EVALUATED}`（:498）——**不含 TRIALING**（W3-D/W4）。
- `RECOMMENDATION_ALLOWED`（wrec:46-56）与 §5.2 逐边一致；REJECTED 终态（fresh evidence 永不复活人类否决）。
- C3③/④ 已按 spec 精确行号调整（测试文件 diff 全在 C3 四类内）。

### C · downgrade 完整性 — **PASS**

- downgrade 后 `recommendation` 表 + 2 索引消失（测试 30 实测）。
- W3-A/W3-B 表与数据**不受影响**（零 alter，无级联触碰）。
- `candidate` 数据不重置（D-R3 默认，迁移无任何 candidate UPDATE）。

### D · SSoT 零新能力词 — **PASS**

- 无新 Capability 行/表；无第二套能力词汇。
- `recommend_candidate` 只读既有 `Match`/`Candidate.evaluation_context`；`cost`/`reliability`/`historical` 为**只读快照字符串**（`_UNKNOWN_DIMENSIONS` wrec:79-86），永不数值化（F-R4/F-R5）。
- Approval/Scheduler/Budget/Audit/`owner_inbox` 判定逻辑全部 import 复用，无重写（§6.3）。

### E · 契约测试 — **PASS**

| 核验项 | 结果 |
|---|---|
| T-REC 覆盖 | **53/53**（脚本验证 `missing=[]`；51 个 test 函数，部分一测多契约） |
| `test_workforce_*.py`（models+w3a+w3b+w3c 单进程） | **106 passed** |
| 9 个 head 常量回归文件 | **203 passed** |
| ruff（src+tests 全树） | `All checks passed!` |
| C3 四类 | C3① 冻结测试零删改 / C3② 12 文件 head 常量 / C3③ 删 1 tuple / C3④ `w3a:746-751` 已落地 |

### F · fail-closed 语义 — **PASS**

- F-R1a/b/c（Match COMPUTED + 三维 unknown）、F-R2（不可解析 422）、F-R3/F-R3b（证据链必需环 + attempt 可解析，422 同错误串）、F-R4/F-R5（不虚构数值）、F-R6（幂等）、F-R7（无自动批准）、F-R8（CAS 撤销 + 全量快照审计）、F-R9（trial gate）、F-R10（RESTRICT 链）、F-R11（purge actor 必填）——**逐条有对应测试**（测试 39-43/53 等）。
- **无绕过 Approval 达「可 Trial」路径**：`assert_trial_eligible` 要求 recommendation APPROVED + live；`RECOMMENDED→TRIALING` 边不存在；actor 无默认值；`resolve_owner_actor` 零调用。
- F-R8 CAS 用 **stored match_attempt** 作乐观锁 token（wrec `_reconcile_drift`，修复 V4 prompt §4-NEW P1-NEW-1 的 WHERE 子句缺陷）：测试 49/50 断言 rowcount 语义正确。

### G · 可解释评分 + 审批留痕 — **PASS**

- `Match.breakdown`/`evidence_refs`/`excluded_fields`/`unknown_dimensions` 强制非空（`_assert_explainable` wrec:210 + `_excluded_fields` wrec:199）。
- `decided_by` 必为 owner（INV-3）；仅 owner actor 可 decide/purge（403 拒绝 AGENT/SYSTEM）。
- **5 个审计 action 齐备且均可触发**（测试逐一验证）：
  | action | 触发点 | 测试 |
  |---|---|---|
  | `recommendation.proposed` | `recommend_candidate` | ✅ |
  | `recommendation.decided` | `decide_recommendation`（APPROVED/REJECTED） | ✅ |
  | `recommendation.withdrawn` | `_reconcile_drift` F-R8 | ✅ |
  | `recommendation.reproposed` | `_rebuild_recommendation`（新 attempt） | ✅ |
  | `recommendation.deleted` | `purge_recommendation`（全量快照） | ✅ |

---

## 3. §15.2 二十一禁止项逐条核验（实现态）

| # | 禁止项 | 实现态判定 |
|---|---|---|
| 1 | 不碰 engine/ruleset/owner_inbox/services；models.py 只末尾追加 | ✅ diff 证实（models.py 追加区 :1822+） |
| 2 | 不改 W3-A/W3-B 语义 | ✅ 函数级零改动；唯一触碰 `CandidateLifecycle.ALLOWED`（受控） |
| 3 | 不写 `Candidate.evaluation_context` | ✅ wrec 全读不写 |
| 4 | 除受控边外不写 `Candidate.status` | ✅ 仅 `_sync_candidate_back`（RECOMMENDED→EVALUATED） |
| 5 | 不调 compute_match/run_benchmark/evaluate_candidate | ✅ 全仓 grep 零调用 |
| 6 | 不调 check_budget/Scheduler/execute_task | ✅ 递延 W4 |
| 7 | 不建 Trial/Employee/Training/Performance | ✅ 迁移只建 recommendation |
| 8 | 不新增 TRIALING 等未来枚举 | ✅ RecommendationStatus 4 成员 |
| 9 | 不接入 ai-arena、不改 ruleset | ✅ 零 adapter |
| 10 | 不新增 Capability 词汇 | ✅ |
| 11 | 不虚构三维数值评分 | ✅ `_UNKNOWN_DIMENSIONS` 只读快照 |
| 12 | 无自动绕过 Approval 路径 | ✅ F-R7 + actor 必填 |
| 13 | 不 commit/push/PR（需 R7 exact-head 授权） | ✅ **HEAD 仍 `6c08d04`，零 commit** |
| 14 | 不引入 Project 到 Workforce 域 | ✅ |
| 15 | 不物理改既有 approval 表 | ✅ |
| 16 | 不改 workforce.py 除 ALLOWED 外定义 | ✅ diff 仅 ALLOWED+docstring |
| 17 | decide/purge actor 无默认值、不调 resolve_owner_actor | ✅ grep 零命中 |
| 18 | 不改 W3-A/B 测试（C3 四类除外） | ✅ 测试 diff 全在 C3 四类 |
| 19 | 不引入 ApprovalStatus.EXPIRED 到 rec 状态域 | ✅ EXPIRED 仅作决策输入→422（测试 39） |
| 20 | 不为触发 F-R8 改 W3-B 状态机 | ✅ compute_match/rank/ALLOWED 零改 |
| 21 | 不把 unbound 无 `br:` 判为证据不足 | ✅ F-R3 只判必需环（cand:/match:） |

**21/21 全部通过，无越界。**

---

## 4. 偏差记录（留痕，非阻断）

| # | 偏差 | 性质 | 处置 |
|---|---|---|---|
| D-1 | **三父 FK 全 RESTRICT**：spec §12.4 DR-4 字面称「RESTRICT only on match_id」，但实现中若 `candidate_id`/`job_version_id` 任一 CASCADE，删 Job 会先级联删 `job_version`→静默删推荐行，`match_id` RESTRICT 永不触发，**违反硬契约 DR-1** | spec 内部矛盾的有意修正（DR-1 优先于 DR-4 字面） | models.py:1880-1882 + 迁移 :82-95 全 RESTRICT；测试 40 实测 `IntegrityError`；docstring 已注释 |
| D-2 | F-R8 CAS `WHERE match_attempt = :stored`（非 `:observed`） | 修复 V4 prompt §4-NEW P1-NEW-1（spec §16.8 原 CAS token 用错值） | 实现按 stored 语义；测试 49/50 随 spec 修复同步 |
| D-3 | 测试文件 44→51 项（补 2/18/19/30/37/38/48 缺测，12 并入 31 标签） | 满足契约 E「53 项全绿」的清单补全 | 全覆盖脚本验证 missing=[] |

---

## 5. 重点审计项逐条回答（对应 V4 §14 判据 + prompt §3.2 步骤）

1. **exact-head 核验**：尚未开 PR——本报告是**本地实现态**审计证据。R7 授权开 PR 后，DSH 复核 PR head SHA = 授权 SHA。
2. **`alembic heads` 唯一** ✅（测试 29 + C3② 12 文件同步）。
3. **T-REC = 53** ✅ / **本地相关回归 309 passed**（106 + 203）✅ / **ruff PASS** ✅（CI 双门由 PR 触发，本 sandbox 不跑全量——CI=事实源，同 W3-A/W3-B 惯例）。
4. **C3 四类精确行号** ✅（C3④ `w3a:746-751`）。
5. **状态机边 / 零 alter / import 复用 / actor 必填 / 5 action / fail-closed** ✅（见 §1、§2）。

---

## 6. 是否达到 R7 exact-head 授权条件

**是。** 实现满足七契约 A–G；V3 审计遗留 P1 全部闭环；§15.2 21 项禁止零违反；本地 TDD 全绿（W3 层 106 + head 常量回归 203 + ruff）。唯一残余动作是**协议内必经**：R7 对 exact-head SHA 显式授权 →（如需）DSH 在 PR 上复核 head SHA 与 CI → **squash-merge**（勿用 merge commit，避免 PR#2/PR#3 的「落地 SHA ≠ 授权 SHA」偏差复现）。

**建议落地方式**：Squash and merge（协议要求）；若 owner 亲自 merge 追认授权，须事后留痕偏差。

---

## 7. 审计约束遵守声明

- 本审计**未修改**任何代码/迁移/测试（docs 报告除外）；零 commit/push/PR。
- 全程不替代 R7 授权；本报告 verdict 仅供 R7 决策。
- 所有行号可追溯到当前工作树（HEAD `6c08d04` + 上述文件），可供 DSH(oxalpha) 复核。
