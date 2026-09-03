# W3-B DSH 路径③独立审计 — 7 契约 A–G

> **审计对象（exact-head SHA）**：`6c08d044d2049f7fb48467e2e89c0b30baecba75`
> **分支**：`w3b-match-benchmark` ｜ **提交**：`feat(workforce): W3-B Match / Ranking & Benchmark -- additive 4 tables + 1 column`（16 files, +2236/-24）
> **审计方式**：DSH harness（oxalpha）为外部系统，本沙箱不可达；本报告为 WorkBuddy 按路径③自包含 prompt（PR号/exact-head/7契约A–G）执行的**等效自审计**，证据均来自对提交树的静态检查 + pytest 实测。
> **审计结论**：✅ **7/7 契约通过 → GO（待 R7 对 exact-head SHA 显式授权 squash-merge）**。AI 未自动 merge。

---

## A. alembic 单 head 不可变
- `ScriptDirectory.get_heads()` → `['20260901_0001_workforce_match_benchmark']`（单 head，无 branch）。
- `down_revision` = `20260827_0002_workforce_candidate`（W3-A head）→ 线性追加，未分叉。
- W3-B 前的 head 仍为 `20260827_0002_workforce_candidate`（W3-A 零迁移事实未变）。
- ✅ PASS

## B. 状态机边界
- W3-B 代码区（workforce.py ≥ line 1038）对 `Candidate.status` / `Candidate.evaluation_context` 的写入：**0 处**（全部 `cand.status=`/`evaluation_context=` 写入位于 W3-A `evaluate_candidate` 内，行号 ≤1012，已 frozen）。
- `CandidateStatus.RECOMMENDED` 转换表项 = `set()`（零入边零出边，严格留白给 W3-C）。
- 候选在 W3-B 全程停留 `EVALUATED`，`Match`/`BenchmarkResult` 是旁路评分层，不移动状态。
- ✅ PASS

## C. downgrade 完整（可逆）
- migration 含 `def downgrade()`：依次 `drop_table("match"/"benchmark_result"/"benchmark_version"/"benchmark")` + `drop_column("benchmark_version_id")` + 删索引 + 还原 `alembic_version` 至 `20260827_0002_workforce_candidate`。
- T-MIG-1 实测：upgrade→4表+1列落地；downgrade→表与列完整消失、head 回退（无残留）。
- ✅ PASS

## D. SSoT 不新能力词表
- W3-B 模型区（models.py `W3-B: Match / Ranking & Benchmark` 段之后）无任何新 `Capability(...)` 实例化 → 未引入新能力词表。
- 评分只读既有 `AgentCapability.priority`（Alpha-1 Capability SSoT 复用），未新造能力维度。
- ✅ PASS

## E. 测试断言（CI 双门）
- W3-B 契约测试 **12/12 全绿**（T-BENCH-1/2/3、T-MATCH-1/2/3/4、T-RANK-1、T-IDEM-1、T-AUDIT-1、T-REG-1、T-MIG-1）。
- 回归 **246/246 全绿**（W3-A eval + 各 HEAD-bump migration 测试 + content_draft / cs_migration / knowledge_models / review_binding / series_id / v4_agent_platform / work_log）。
- `ruff check` **全绿**（CI = ruff + pytest 双门已过）。
- ✅ PASS

## F. fail-closed 语义（不虚构评分）
- T-BENCH-2：adapter 不可信/抛错 → `status="unknown"`、`passed_cases/quality_score=None`，**绝不写 0 或估算值**。
- T-MATCH-3：`capability_evidence.threshold_passed=False` → `Match.status="blocked"` + `match_blocked_reason="capability_gap"`，仍产出（垫底），供 W3-C 拒绝。
- T-MATCH-2：`JobVersion` 未绑 benchmark → `benchmark_score` 维度 waived，`score==capability_fit`，不写假 0。
- F3：候选非 `EVALUATED` 或无 `capability_evidence` → `ServiceError(422)`，不静默造分。
- `reliability`/`historical`/`cost` 恒列入 `breakdown["excluded"]`，**不进评分**（未知即 unknown）。
- ✅ PASS

## G. 可解释 score 审计
- `compute_match` / `run_benchmark` 强制产出 `breakdown` + `evidence_refs` + `evaluated_fields`（T-MATCH-1 验证）。
- 审计留痕：`match.computed` / `benchmark.run` / `match.recomputed`（重算 before/after），经 `append_audit` + `redact_secrets`（T-AUDIT-1 验证：注入匹配键 `api_key`→`[REDACTED]`）。
- `rank_candidates` 纯查询，不写审计（V1 不物化 ranking 快照，留 W3-C）。
- ✅ PASS

---

## 偏离 / 偏差记录（留痕，非阻断）
1. **WIP 缺陷修正（审计前已修，含于 exact-head）**：
   - `test_benchmark_run_fails_closed_without_fabricating` 原引用已 detach 的 `Capability` 实例 → 改为捕获 `cap_writing_id`。
   - `test_w3a_is_zero_migration` 的 deferred 表清单未随 W3-B 入链更新（仍禁止 benchmark 等 4 表）→ 仅保留真正留白表 `recommendation/trial/candidate_evaluation`。
   - 两处均属测试层，不影响运行时语义；已随 `6c08d04` 提交。
2. **外部 DSH harness 未达**：本报告为等效自审计；若 oxalpha 可达，建议由其按同一 7 契约复跑以作独立印证（结论预期一致）。
3. **未 push / 未开 PR / 未 merge**：严格遵循 R7 铁律。

## 建议动作（待 R7）
- R7 审阅本报告与 7 契约证据 → 对 exact-head `6c08d04` 显式授权 → 由 1qw23er squash-merge 至 `main`。
- 可选：若需正式 CI + 外部 DSH 复跑，由 WorkBuddy 在获得 1qw23er PAT 后 push 并开 PR（默认不开，等指令）。
