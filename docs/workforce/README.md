# Workforce W3 文档索引

Workforce（AI 员工）W3 阶段的设计、实现与审计文档。W3 闭环：

```
W3-A Evaluation ─▶ W3-B Match/Benchmark ─▶ W3-C Recommendation/Approval ─▶ W3-D Trial ─▶ W4 Employee
```

> W3 止于 Trial；`Employee` / `Training` / `Performance` 归 W4。

## 当前有效基线

| 阶段 | 设计基线 | 审计结论 | 代码落点 |
|---|---|---|---|
| W3 总纲 | `Workforce_W3_Evaluation_Matching_Spec_V1.md` | — | — |
| W3-A Evaluation | `Workforce_W3A_Evaluation_Implementation_Design.md` | — | `src/aios/workforce.py`（PR #3） |
| W3-B Match/Benchmark | `Workforce_W3B_Match_Benchmark_Spec_V1.md` | `Workforce_W3B_DSH_Audit_Report.md` | `src/aios/workforce.py`（PR #4） |
| W3-C Recommendation/Approval | **`Workforce_W3C_Recommendation_Approval_Spec_V4.md`** | **`Workforce_W3C_DSH_Audit_Report_V4_Impl.md`（GO）** | `src/aios/workforce_recommendation.py`（PR #5） |
| W3-D Trial | 待立（见 `Workforce_W3C_Recommendation_Approval_Spec_V4.md` §11） | — | 未实现 |

## 文件清单

### 设计规格（Spec）

| 文件 | 内容 | 状态 |
|---|---|---|
| `Workforce_W3_Evaluation_Matching_Spec_V1.md` | W3 总纲：Evaluation → Benchmark → Match → Recommendation → Trial 全闭环设计 | 基线（部分被后续分阶段 Spec 细化） |
| `Workforce_W3A_Evaluation_Implementation_Design.md` | W3-A Evaluation 单环实现设计 | 已落地 |
| `Workforce_W3B_Match_Benchmark_Spec_V1.md` | W3-B Match / Benchmark / Ranking | 已落地 |
| `Workforce_W3C_Recommendation_Approval_Spec_V2.md` | W3-C 早期稿（Q1 裁决前） | 历史 |
| `Workforce_W3C_Recommendation_Approval_Spec_V3.md` | W3-C 定稿（C1/C2/C4 裁决后） | 历史 |
| **`Workforce_W3C_Recommendation_Approval_Spec_V4.md`** | **W3-C 最终实现基线**（含 TDD 期 R7 裁决留痕：严格 2 索引等） | **现行** |
| `Workforce_W3C_Recommendation_Approval_Trial_Spec_V1.md` | 早期 W3-C+Trial 合并稿（标题为 "W3-C Spec V1 — Recommendation / Approval / **Trial**"） | 历史；Trial 部分被 V4 §11 边界表取代 |

### 独立审计（DSH / DeepSeek harness，路径③）

| 文件 | 内容 | 结论 |
|---|---|---|
| `Workforce_W3B_DSH_Audit_Report.md` | W3-B 审计 | — |
| `Workforce_W3C_DSH_Audit_Report_V2.md` | 针对 Spec V2 | 有 finding → 催生 V3 |
| `Workforce_W3C_DSH_Audit_Report_V3.md` | 针对 Spec V3 | 有 finding → 催生 V4 |
| **`Workforce_W3C_DSH_Audit_Report_V4_Impl.md`** | **针对 Spec V4 的实现期审计** | **GO**（PR #5 据此合并） |
| `DSH_Path3_Audit_Prompt_W3C_V4.md` | 本次审计使用的自包含 prompt（路径③：PR 号 / exact-head / 契约 A–G） | 审计可复现凭证 |

## 版本演进要点

- **V2 → V3**：C1（惰性 reconcile，`assert_trial_eligible` 由纯读改为可写 WITHDRAWN）、C2（单一状态 SoT，删 `approval_status` 列）、C4（`recommendation.match_id` 保留 RESTRICT）。
- **V3 → V4**：收紧证据链（P2-3）、更正脱敏行为描述（P3-1，以 `audit.py` 实际行为为准）、锁定 migration revision 名。
- **V4 实现期 R7 裁决**：迁移只建 2 个索引（`ix_recommendation_status` / `ix_recommendation_decided_by`），`models.py` 同步保持一致。
- **W3-C → W3-D 交接**：V4 §11 边界表明确 Trial 实体与 `create_trial` 归 W3-D；`assert_trial_eligible`（C1 惰性 reconcile 版）是 W3-D 唯一前置守卫；Employee / Budget / Scheduler / Execution 归 W4。

## 审计约定

- DSH 审计 ≠ merge 授权：DSH 出 GO 后，仍须 owner 对 exact-head SHA 显式授权才能合并。
- 每次审计使用的 prompt 随报告一并归档，保证审计可复现。
