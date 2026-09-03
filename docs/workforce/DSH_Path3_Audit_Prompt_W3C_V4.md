# DSH 路径③ 自包含独立审计 Prompt — AIOS W3-C Recommendation/Approval (V4)

> **用途**：供 DeepSeek harness（oxalpha / DSH）在**无任何会话上下文**的情况下，对 AIOS W3-C（Recommendation + Approval）交付物执行**独立 PR gate 审计**。
> **铁律**：DSH 审计 ≠ merge 授权。DSH 只审计 + 出 verdict；exact-head 授权必须由 owner(R7) 显式给出。
> **当前阶段**：本环境处于「V4 设计审计」阶段（V4 是纯设计文档，尚未产生实现 PR）。

---

## 0. 审计目标（每次运行**先填**此处）

- 阶段：☐ **V4 设计审计**（纯 spec 复核，无代码）  /  ☐ **实现 PR gate**（代码+测试核验）
- 设计审计目标文件：`docs/Workforce_W3C_Recommendation_Approval_Spec_V4.md`
- 实现 PR gate（填 PR 后启用）：
  - PR 号 = `<PR#>`
  - exact-head SHA = `<SHA>`
  - CI 状态 = `<SUCCESS | FAIL>`
  - 落地方式 = `<Squash Merge | Merge Commit>`（协议要求优先 Squash；R7 手动 merge 追认授权不违规但须留痕偏差）

> 注：当前**尚无实现 PR**，本 prompt 默认先执行 V4 设计审计；待 R7 授权实现并开 PR 后，再填 PR号/exact-head 切到「实现 PR gate」核验。

---

## 1. 背景（自包含，DSH 无需外部信息）

- **AIOS**：AI 员工工作力编排系统。W3 分层 —— W3-A=Evaluation（已冻结）；W3-B=Match/Ranking+Benchmark（已合入 `main`，tree `c837d78a…`，HEAD `6c08d04`）；**W3-C=Recommendation+Approval（本次审计对象）**；W3-D=Trial；W4=Employee/Training。
- **协作铁律**：GitHub=事实源；AI 绝不自动 merge；owner(R7) 须对 **exact-head SHA** 显式授权（通常为 Squash Merge，授权严格绑定 SHA，head 不一致则 STOP）。
- **W3-C 演进史（避免重复辩论）**：
  - V1 草案：错把 Trial 本体纳入 W3-C（越界）→ **作废**。
  - V2：仅 Recommendation/Approval；DSH 审出 4 项 P1（C1–C4）→ R7 裁决。
  - V3：修复 C1–C4；DSH 第二轮又审出 P1-1/P1-2/P1-3 + P2×7 + P3×5 → R7 最终裁决催生 V4。
  - **V4**：纯设计文档，按 R7 最终裁决修订（C5–C8 + P2-3/P2-5/P2-6/P2-7/P3-1 全部闭环）。**本轮不写代码 / migration / 测试，不 commit / push / PR。**

---

## 2. 七项独立审计契约（A–G，来自 V4 §14，可字面断言）

| 契约 | 内容 | 可字面断言的验收判据 |
|---|---|---|
| **A** | 单 head / additive / 可逆 | `alembic heads` 唯一 = `20260902_0001_workforce_recommendation`；upgrade 只 `create_table` + 2 index（**零 alter 既有表**）；downgrade 完全对称；**12 个测试文件 head 常量已同步**（C3②） |
| **B** | 状态机边界 | W3-A/W3-B 既有边**零改动**；`ALLOWED[EVALUATED]` 只增 `RECOMMENDED`；`ALLOWED[RECOMMENDED] == {EVALUATED}`（**不含 TRIALING**）；`RECOMMENDATION_ALLOWED` 与 §5.2 逐边一致；C3③ 只删 1 个 tuple；C3④ 改 1 个全等断言 `ALLOWED[EVALUATED]=={REJECTED}` → `=={REJECTED,RECOMMENDED}` |
| **C** | downgrade 完整性 | downgrade 后 `recommendation` 表与 2 个索引消失；W3-A/W3-B 表与数据**不受影响**；不修改 `candidate` 数据（D-R3） |
| **D** | SSoT 零新能力词 | 无新 Capability 行；无第二套能力词汇；未重造 Scheduler / Execution / Budget / Audit / Approval 判定逻辑（全部 import 复用，§6.3） |
| **E** | 契约测试 | T-REC **53 项**（V3 46 + 新增 7：47/48 F-R3b、49/50 C8 CAS、51/52 C7+P2-3、53 P2-6）+ T-REG 全绿 ≥246 + `ruff` PASS + **C3 四类调整已完成** |
| **F** | fail-closed 语义 | F-R1a/b/c、F-R2…F-R10 逐条有对应测试；**不存在**绕过 Approval 达致「可 Trial」的路径（含 P2-1：`actor` 无默认值、无 `resolve_owner_actor` 调用） |
| **G** | 可解释评分 + 审批留痕 | `breakdown` / `evidence_refs` / `excluded_fields` / `unknown_dimensions` 强制非空；`decided_by` 必为 owner（INV-3/INV-6）；**5 个审计 action 齐备且均可触发**（`proposed` / `decided` / `withdrawn` / `reproposed` / `deleted`） |

---

## 3. 阶段性核验方法

### 3.1 V4 设计审计（当前阶段，只读 spec）
DSH 只读 `docs/Workforce_W3C_Recommendation_Approval_Spec_V4.md`，确认：
1. **§14 七契约**逐条列出且为「可字面断言」形式（见上表）。
2. **§13.9** 含 **53 项**契约测试清单，数到 53，新增 7 项编号连续 **47–53**（47/48 F-R3b、49/50 C8 CAS、51/52 C7+P2-3、53 P2-6）。
3. **§13.8** C3 四类齐全：C3①/②/③/④，且显式边界「四类不得越界」（不动其他 W3-A 测试/行为）。C3④ = `tests/test_workforce_evaluation_w3a.py:746-748` 全等断言改为 `{REJECTED,RECOMMENDED}`。
4. **§15.2** 共 **21 项**禁止项（V2 原 15 + V3 新增 4 + V4 新增 2）；V4 新增 **#20**（禁止为触发漂移改 W3-B 状态机/`compute_match`）、**#21**（禁止把「证据链完整性」扩大为「unbound 必须存在 `br:`」）。
5. **§16.7 / §16.8** F-R8 = 纵深防御定位（生产不可达，死锁链证明）；CAS 撤销最终契约绑定 `match_attempt = observed_attempt`；rowcount==1 才写 `withdrawn` 审计，rowcount==0 重新读取。
   ⚠️ **第二轮审计新增核验点（P1-NEW-1）**：CAS 的 `WHERE match_attempt = :observed_attempt` 中 `observed_attempt` 来自 **Match.evidence_refs**（当前 Match 的新 attempt，如 4），而 Recommendation 行上 stored 值是旧 attempt（如 3）。§16.2 t3 示例中 rec#1.match_attempt=3、observed=4，`WHERE match_attempt = 4` 在 stored=3 的行上 **rowcount=0**，但 spec 声称 rowcount==1 — 内部矛盾。正确做法：CAS `WHERE match_attempt = :stored_match_attempt`（reconcile 开始时从 Recommendation 行读取的值作为乐观锁 token）。DSH 复核时须验证此点。
6. **§10.3** unbound waive 最终契约（UW-1…UW-4 + 反模式警示）；`br:` 缺失不构成 F-R3，但 `evidence_refs` 须可解析 `match_attempt`（不可解析→F-R3b 422）。
7. **§9.2** 脱敏注记为 `audit.py` 实际**双层**行为（key 名层 + 值级 `_SECRET_VALUE_RE`），语义不变。
8. **文档本身**未含实现代码 / migration / 测试代码（纯设计）。
9. **闭环台账**（§4）除 **P1-NEW-1**（CAS WHERE 子句）与 **P3-NEW-1**（§18 悬空引用）外**全部闭环，无遗留 P0**。详见 §4-NEW。

### 3.2 实现 PR gate（未来阶段，填 PR号 + exact-head 后执行）
DSH 实际执行：
1. `git fetch` + 核 **exact-head SHA** 与 PR head 一致；CI = SUCCESS。
2. `alembic heads` 唯一 = `20260902_0001_workforce_recommendation`；`ruff` PASS。
3. `pytest -q` 全绿，且 **T-REC = 53**、**T-REG ≥ 246**；C3 四类调整已落地（4 个精确行号）。
4. 逐条比对 A–G 与代码实际：状态机边（B）、表结构零 alter（A/C）、import 复用无重造（D）、actor 必填无默认值（F/P2-1）、审计 action 5 个均可触发（G）、fail-closed 无绕过（F）。
5. 仅当**全部通过** → GO；任一条硬失败 → NO-GO + 具体缺口；非阻塞小瑕疵 → GO WITH CONDITIONS。

---

## 4. 闭环台账（DSH 须确认每项已闭环，不再重新辩论）

- **P0**：**无**（0 个）。
- **V2 四项 P1（C1–C4）**：C1 方案A / C2 删 `approval_status` 列 / C3 追认三类 / C4 保留 RESTRICT → 已于 V3 闭环。
- **V3 第二轮 P1**：
  - P1-1（C3④ 受控测试缺 `w3a:746` 全等断言）→ **V4 C5** 扩为四类闭环。
  - P1-2（attempt 不可解析静默 fail-open）→ **V4 C6** 新增 F-R3b（422 `match evidence is not resolvable`）闭环。
  - P1-3（F-R8 触发不可达，死锁链证明：`compute_match` 仅收 EVALUATED→422；活推荐必 RECOMMENDED；回 EVALUATING 需 `RECOMMENDED→EVALUATING` 但该边不存在→409）→ **V4 C7** 明裁 F-R8=纵深防御（危害已被冻结 compute_match 的 422 守卫堵死，F-R8 是第二道锁）；演示改 data-layer 注入 → 闭环。
- **P2×7**：P2-1（actor 必填，禁 `resolve_owner_actor`）→ §6.2/§7 F-R11 + §13.9(53) 闭环；P2-2（`recomputed` 缺 `before.decided_by`）→ 改用 `reproposed` + §9 留痕闭环；P2-3（unbound waive 矛盾）→ V4 保留 unbound waive + evidence 可解析约束 + §15.2 #21 闭环；P2-4（`rank_candidates` 过滤 `status==EVALUATED` 致 RECOMMENDED 消失）→ §9/§12 留痕 + W4 不依赖闭环；P2-5（D-R3 后果不完整）→ §12.5 + §17.2 闭环；P2-6（purge 无 actor）→ §5.2/§6.2/§7 F-R11 闭环；P2-7（INV 编号矛盾）→ §4.5 统一 INV-1…INV-6 闭环。
- **P3×5**：P3-1（§9 脱敏注记错误，实测 `audit.py` 含值级 `_SECRET_VALUE_RE`）→ 按实际双层行为更正，语义不变，闭环；P3-2（RESTRICT 6 处先例）确认；P3-3（`rank` 快照未建表）明示 W3-C 不建快照；P3-4（测试小缺口）补入 53 项覆盖；P3-5（W3-B 冻结缺陷）标冻结不修。
- **D-R1/D-R2/D-R3**：已于 V3 裁决闭环（purge 前置 `status∈{WITHDRAWN,REJECTED}`；`reproposed` 统一；D-R3 默认不重置）。
- **残留待裁（不阻塞）**：§17.2 D-R1（引入 purge 解锁入口）/ D-R2（`reproposed` 命名）/ D-R3（不重置）已由默认建议给出，需 R7 授权实现时一并确认。

### §4-NEW 第二轮审计新发现（**未闭环**，DSH 复核时必须验证）

**P1-NEW-1（阻塞）— §16.8 CAS WHERE 子句乐观锁 token 用错值**

- **现象**：`UPDATE recommendation SET status='withdrawn' WHERE id=:rec_id AND status IN ('proposed','approved') AND match_attempt = :observed_attempt`，其中 `observed_attempt` 定义为「当前 Match.evidence_refs 解析出的 attempt」（§16.8 step 1）。
- **反例（spec 自相矛盾）**：§16.2 t3 中 rec#1 创建时 `match_attempt=3`；Match 被 data-layer 注入 `attempt=4`；`observed_attempt=4`。CAS `WHERE match_attempt = 4` 落在 stored=3 的行上 → **rowcount=0**，但 spec 声称 rowcount==1。
- **后果 1（状态/审计 fail-open）**：stale APPROVED 推荐（stored=3, observed=4）CAS 永不命中 → 永不转 WITHDRAWN、永不写 `recommendation.withdrawn` 审计。trial 闸仍 return False（gate fail-closed），但 F-R8 的状态迁移+审计链实际失效。
- **后果 2（错误撤销）**：若他人已重建（stored=4, observed=4）→ CAS 命中 → **错误撤销**基于新证据的 re-proposed 推荐。
- **根因**：C8 裁决把「Match 的当前 attempt」误当作 CAS 乐观锁 token；正确的 CAS token 是 **reconcile 开始时从 Recommendation 行读取的 stored match_attempt**（标准 optimistic concurrency）。
- **修复（一行，零迁移/零语义变化）**：`WHERE ... AND match_attempt = :stored_match_attempt`。rationale（§16.8 表「已被他人基于新 attempt 重建，CAS 不匹配」）本就描述 stored 语义，与代码矛盾。
- **连带**：测试 49/50（T-REC-CAS）随修复同步调整（49 当前断言 rowcount==1 在错误 CAS 下必失败）。

**P3-NEW-1（非阻塞）— §0 交叉引用「见 §18」悬空**

- §0「本轮未裁决」列（P2-2 F-R6 / P2-4 / P3-2…5）标注「见 §18」，但 V4 无 §18 章节（§0–§17 止）。Dangling cross-reference，仅文档级。

---

## 5. Verdict 协议（DSH 必须按此格式输出）

```
VERDICT: GO | NO-GO | GO WITH CONDITIONS

[逐项]
A: PASS/FAIL — <证据>
B: PASS/FAIL — <证据>
C: PASS/FAIL — <证据>
D: PASS/FAIL — <证据>
E: PASS/FAIL — <证据>（设计审计阶段校验「53 项清单 + C3 四类」；PR gate 阶段核验实际 T-REC=53/T-REG≥246/ruff）
F: PASS/FAIL — <证据>
G: PASS/FAIL — <证据>

[闭环台账] 所有 P0/P1/P2/P3 已闭环：YES/NO
[结论] <一句话>
```

- **GO（设计审计）** → 结论「**V4 已达到最后一次定向 DSH 审计条件**」，路由 R7 对 exact-head SHA 显式授权后进入 TDD。
- **GO WITH CONDITIONS（设计审计，本轮实际状态，因 P1-NEW-1）** → 结论「**V4 设计成熟、C1–C8/P2/P3/D-R1-3 全闭环，但 CAS WHERE 子句乐观锁 token 用错值（P1-NEW-1）须先修正 §16.8 一行 + 测试 49/50 + §16.2 t3 注释，方可达到 R7 授权条件**」。修复后重跑本 packet 进行第三次定向审计，或由 R7 按「修复属于 spec 文档修订、不改变设计语义」直接采信并授权 TDD。
- **GO（实现 PR gate）** → 结论「实现满足七契约，建议 R7 对 exact-head SHA 授权 merge」。
- **NO-GO** → 列出具体未闭环项（P? / 契约 ?），**不授权**。
- **GO WITH CONDITIONS** → 列出非阻塞小瑕疵 + 条件。
- DSH **不得**实现任何代码 / migration / 测试，不得 commit / push / PR，不得替代 R7 授权。仅审计 + 出 verdict。

---

## 6. 硬约束（不可违反）

1. **纯审计**：不改代码、不写 migration、不写测试、不 commit、不 push、不 PR、不授权实现。
2. **exact-head 授权**必须由 R7 显式给出；DSH 审计 ≠ 授权。
3. 任何阶段输出必须可追溯到 **§14 七契约**与**§4 闭环台账**。
4. 若发现 V4 设计本身新缺陷（非台账已闭环项），须作为新 finding 上报，**不得自行修补**。
