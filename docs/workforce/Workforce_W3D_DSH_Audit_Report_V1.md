VERDICT: GO WITH CONDITIONS

[A–G 逐项]

A: PASS — §10 表明确 `down_revision = 20260902_0001_workforce_recommendation`（保持**单 head**）；§10 表 `upgrade = op.create_table("trial", …)` 且 **0 个显式索引**，`downgrade = op.drop_table("trial")`；§10.1 逐对象列出 `recommendation / candidate / job_version / match / approval` 全部 **❌ 全部冻结，本轮零 ALTER**；§10 代码块列序/类型与 §4 模型段一致：`id, candidate_id, job_version_id, recommendation_id, status, created_at, updated_at` + PK + 3 FK + `UniqueConstraint("recommendation_id", name="uq_trial_recommendation")`。

B: PASS — §5.1 原文：`CandidateStatus.EVALUATED: {REJECTED, RECOMMENDED},          # 不变`；`CandidateStatus.RECOMMENDED: {EVALUATED, TRIALING},          # ← 新增 TRIALING`；`CandidateStatus.TRIALING: set(),                             # ← 新增节点，出边归 W4`；`TRIALING = "trialing"` 且说明 `列是 sa.String() 零 migration`（F-12）；§5.2 明写 `V1 无转移`；§3-Q3 明写 `不建 TRIAL_ALLOWED 边表、不建 _transition_trial_status`。

C: PASS — §10 表：`downgrade | op.drop_table("trial")`；§10.1：`recommendation / candidate / job_version / match / approval` 全部 **否**，零 ALTER；附件 A 全文未出现修改 `candidate` / `recommendation` 既有表结构或数据的 migration 动作。

D: PASS — §9 接口契约表全部依赖为 import 复用：`assert_trial_eligible`、`CandidateLifecycle.require_transition`、`_assert_owner_actor`、`append_audit`；§6 F-T2 明写 `W3-D 不得自行判断资格、不得绕过`；§2.2 明确 `Budget（check_budget）/ Scheduler / Execution` 归 W4，`不重写` / `只复用`。

E: PASS — §11 清单逐项编号 1–30，分组标题逐字为：`T-TRIAL-GATE（7）`、`T-TRIAL-STATE（4）`、`T-TRIAL-IDEM（4）`、`T-TRIAL-FK（3）`、`T-TRIAL-AUDIT（3）`、`T-TRIAL-BOUNDARY（5）`、`T-TRIAL-MIG（3）`、`T-TRIAL-CPL（1）`；加总 7+4+4+3+3+5+3+1=30。§11 明写 `仅清单，本轮不写测试代码`。

F: PASS — §6 含 `F-T1` 至 `F-T8` 共 8 条，测试映射如下：F-T1→GATE 1/2/3/4/7；F-T2→GATE 7；F-T3→STATE 11；F-T4→GATE 6；F-T5→GATE 5；F-T6→FK 16/17/18；F-T7→BOUNDARY 26；F-T8→BOUNDARY 24。§6.1 骨架中 `assert_trial_eligible(...)` 是唯一资格闸；§2.1 规定 `create_trial_from_approval` 是唯一把 `candidate.status` 写成 `TRIALING` 的地方；§3-Q2 明写三父 FK 全 RESTRICT 且 `不得为 trial 引入任何 CASCADE`。

G: PASS — §8 表：`trial.created | trial | 创建成功（SAVEPOINT 内） | Recommendation 决策快照（status / decided_by / decided_at / decision_rationale / match_attempt） | Trial 全列 + candidate_status + match_attempt | trial:{recommendation_id}`；`V1 只有一条 Trial 审计动作。`；§6.2 幂等键 `trial:{rec.id}` 与 `UNIQUE(recommendation_id)` 对齐；明写 `本轮不产生 trial.deleted 审计`。

[计数核验] F-T=8 / INV-T=5 / Q=7 / C=8 / 测试=30（分组加总=30）：YES

[闭环台账 L-1…L-7] 全部确认闭环：YES  
（含 L-6：§3-Q2 给出一组可辩护的偏离论证，并升格为 §12 C-2；DSH 独立复核认为该论证成立，但仍属 R7 拍板项，不等于授权。）

[薄弱点 W-1…W-8] 逐条独立判定：

W-1: PASS — §6.1 先 `assert_trial_eligible` 后查 existing，是 fail-closed 顺序；§7 幂等表第 1 行明确限定 `同一 APPROVED rec`，未涵盖“Trial 已存在但 rec 已转 WITHDRAWN/REJECTED”场景。该场景行为应为 409，但 §7 未显式写出。**非阻塞条件**：建议在 §7 增加一行显式说明“Trial 已存在但 rec 当前非 APPROVED → 409，不重放”。

W-2: FAIL（条件） — §6.1 使用 `session.exec(select(Trial).where(...)).first()`，但附件 A 未钉死 `select` 的 import 来源。对 `table=True` 类，`from sqlalchemy import select` 可能返回不可变 Row，造成实现期陷阱。**必须修订/条件**：在 spec 或实现说明中显式要求使用 `from sqlmodel import select`。

W-3: PASS — F-15 已给出 `append_audit` 全关键字签名；§6.1 调用采用关键字传参，满足签名与约束。

W-4: PASS — F-12 明确 `candidate.status` 为 `sa.String()`；§5.1 使用 `CandidateStatus` 作为 `CandidateLifecycle.ALLOWED` 的 key。Python `StrEnum` 与 `str` 哈希相等，查找成立。虽然 spec 未逐字声明，但属于实现常识；不构成缺陷。

W-5: PASS — §6.1 的 `except IntegrityError` 位于 `with session.begin_nested():` 之外，捕获后 `session.expire_all()` 再回读 winner；这与 SAVEPOINT 回滚后读胜者的模式自洽。附件 A 声称“镜像 `recommend_candidate` §8”，因附件 A 未嵌入该节，无法逐字比对，但未见内部矛盾。

W-6: FAIL（条件） — §11 测试 13 仅写 `并发首次创建 → 不产生 2 行、不产生 500`，未给出在既有 SQLite 测试基建下可确定性复现并发 `IntegrityError` 的方法。**必须修订/条件**：应明确测试 13 采用确定性注入，例如先用 data-layer 直接插入抢占 `UNIQUE(recommendation_id)` 槽位，再调用 `create_trial_from_approval` 断言回放/`IntegrityError`，或定义明确并发测试装置。

W-7: FAIL（条件） — §6.2 的 `UNIQUE(recommendation_id)` 与 `audit_log.idempotency_key` **对齐**，但表述为“双保险”过强。正常顺序重放会在写审计前返回；并发首次创建会先触发 trial UNIQUE，审计唯一约束实际不会作为独立第二层生效。**必须修订/条件**：改为“一致性/对齐约束”等更准确表述。

W-8: PASS — 边界测试 22/23/26 的静态 grep 属于廉价 fail-closed 边界断言，W3-C 已有同类先例；作为契约测试清单项可接受。

[新发现] 无（W-1、W-2、W-6、W-7 已列入条件；未发现附件 A 中上述之外的新内部缺陷）

[结论] 设计语义成熟，七项契约可逐字核验通过；非阻塞条件为文档级修订（明确 SQLModel `select` import、确定性并发测试、修正审计“双保险”表述、补充已存在 Trial 但 rec 非 APPROVED 的幂等表场景），R7 仍须逐条确认 §12 C-1…C-8 后进入实现。