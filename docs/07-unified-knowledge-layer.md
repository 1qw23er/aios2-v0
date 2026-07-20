# AIOS 统一知识层（Unified Knowledge Layer）架构盘点与分阶段规划

> 范围：仅规划，不实现、不开 PR。基于 `src/aios/` 与 `alembic/versions/` 源码逐文件核实（Alembic 头 `20260719_0004`）。
> 目标：确认现有 KnowledgeCandidate / KnowledgeReviewDecision / KnowledgeFact / Policy / Decision / Artifact / ContextService 是否足以支撑多个异构 Agent 共享统一知识，并规划最小的 Unified Knowledge Layer。

---

## 摘要 / TL;DR

- **结论**：结构化知识底座**已基本具备**——候选→审核→版本化事实→取代 的完整生命周期、scope（company/project）过滤、不可变 TaskContext 溯源都已存在。
- **关键缺口（必须补）**：①scope 只有两档且无枚举；②ContextService 内部**不做 capability/task 级投影**（最小权限只发生在委派边界）；③KnowledgeFact **无写入侧信任守卫**；④**无任何矛盾检测**（冲突会静默版本化而非进入人工）；⑤Policy/Decision **代码内无写入器**；⑥知识审批人身份硬编码 "owner"，无 agent 级溯源。
- **向量库**：**现在不需要**。当前是结构化、低体量、按 scope+status 精确过滤的知识，embedding/语义检索只在 Phase B（数据量确实增长后）引入，且检索结果仍须过 scope+approval+provenance 三道过滤。
- **最小扩展**：Phase A 补 scope 枚举、capability 投影、写入守卫、矛盾人工审核、检索审计、tags；Phase B 才上 embeddings。

---

## 1. 当前能力盘点

### 1.1 已存在的知识类型

| 知识类型 | 表 / 模型 | 关键标识 | 版本化 | 取代机制 |
|---|---|---|---|---|
| approved facts | `KnowledgeFact` (`models.py:547`) | `series_id`+`version` | ✅ | `supersedes_fact_id` 自引用；旧头置 `SUPERSEDED` |
| policies | `Policy` (`models.py:591`) | `series_id`+`version`, `enabled` | ✅ | 仅取每 series 最新 `enabled` 版 |
| decisions | `Decision` (`models.py:576`) | `series_id`+`version` | ✅ | 取每 series 最新 `APPROVED` 版 |
| approved artifacts | `Artifact.review_status == APPROVED` (`models.py:326`) | `revision_count`/`revision_of` | ✅(修订血缘) | 修订循环封顶→owner 升级 |
| campaign knowledge | `KnowledgeCandidate`/`KnowledgeFact` 的 `project_id = <源 campaign>` | `source_project_id` | — | 同 fact 机制 |
| company knowledge | 上述实体的 `project_id IS NULL` | `scope` 字符串推导 `"company"` | — | 同 fact 机制 |

> 注：代码内 "campaign" 与 "project" **等同**（无独立 `CAMPAIGN` scope 值）。

### 1.2 各类知识如何：产生 / 审核 / 版本化 / supersede / 注入 / 来源

**KnowledgeCandidate**（`models.py:518`，状态 `DRAFT/APPROVED/REJECTED`）
- 产生：`KnowledgeService.submit_candidate`（`knowledge_service.py:40`）+ 触发器 `knowledge_candidate_validate_insert` 强制「源 artifact 须 `APPROVED` 且同 campaign」+「`source_project_id` 非空」。
- 审核：`review_candidate`（`knowledge_service.py:146`）→ `APPROVE`(建 fact) / `REJECT`。
- 版本化：候选本身**无 version**（轻量记录，身份不可变，由 `knowledge_candidate_validate_update` 锁）。
- 注入：不直接注入；仅作为 fact 的来源溯源出现在上下文。

**KnowledgeFact**（`models.py:547`，状态 `APPROVED/SUPERSEDED/INACTIVE`）
- 产生：仅 `_approve`（`knowledge_service.py:279`）在 `decision=APPROVE` 且提供 `series_id`+`version` 时创建。
- 版本化：`version` 必须 `head+1`；首条须 v1（`knowledge_service.py:297-310`）。
- supersede：新头批准后旧头置 `SUPERSEDED`（`knowledge_service.py:327`）；`deactivate_fact` 置 `INACTIVE`。
- 注入：经 `ContextService._knowledge_facts`（`context_service.py:302`）按 `status=APPROVED AND (project_id IS NULL OR = project)` 注入。
- 来源：`source_candidate_id` / `source_artifact_id` / `review_decision_id` / `source_project_id` 全部落行（`models.py:563-571`），并回传 `source_references`。

**KnowledgeReviewDecision**（`models.py:535`，枚举 `APPROVE/REJECT`）—— 决策**不可变**（触发器 `knowledge_review_reject_update` 永远 `RAISE`）。`KnowledgeReviewResult` 仅是 `knowledge_service.py:23` 的 dataclass（非表）。

**Policy / Decision**：有读取、有版本、`ContextService` 消费（`context_service.py:381/407`），但**代码内无任何 service/API 写入器**（grep `Policy(`/`Decision(` 仅命中模型定义与读取）。

**Artifact（approved）**：`review_status=APPROVED` 经 `ContextService._dependency_content`（`context_service.py:227`）注入依赖产物 + `ReviewedFact`；Phase 1 评审协议已加 `ReviewResult`(含 `reviewer_agent_id`/`user_id`)。

### 1.3 ContextService 注入与投影现状

`ContextService.build_context`（`context_service.py:103`）：
- 注入表：`KnowledgeFact`(APPROVED) / `Decision`(APPROVED) / `Policy`(enabled) / 依赖 `Artifact`(APPROVED) + `ReviewedFact`(APPROVED)。
- 按审批态过滤：✅（均过滤 APPROVED/enabled）。
- 按 scope 过滤：✅（company 跨 campaign 复用；project 仅限归属）。
- 来源保留：✅（`source_references` 含 resource_type/id/version/inclusion_reason；fact 行回传全链路 id）。
- **capability/task 级投影：❌（仅委派边界做）**。`ContextService` 对任何内部 agent 注入「全部已批准+scope 匹配」知识；最小权限投影发生在 `delegation.py`（`_project_context` / `project_external_context` / `_EXTERNAL_CONTEXT_ALLOWLIST`）：**外部 agent 被剥离全部知识键**，仅得任务级白名单上下文。

### 1.4 多 Agent 共享的当前保证（对照需求）

| 需求 | 现状 | 缺口 |
|---|---|---|
| 仅 approved 知识进正式上下文 | ✅ | — |
| project 知识不泄漏到其他 campaign | ✅ | scope 仅靠 `project_id` 可空，无枚举易错 |
| company 知识跨 campaign 复用 | ✅ | — |
| 每 Agent 按 capability+task 最小投影 | ⚠️ | 仅外部 agent 被剥离；内部 agent 全量；无 capability/task 维度 |
| 外部 Agent 不得直接写 KnowledgeFact | ⚠️ | **无代码层信任守卫**，仅"仅 owner 路由可达"结构性约束 |
| 知识冲突不静默覆盖，须人工审核 | ❌ | **无任何矛盾检测** |
| 引用可追溯到 Artifact/Agent/Campaign | ✅(Artifact/Campaign) | 审批人身份硬编码 "owner"，无 agent 级溯源（与 Artifact `ReviewResult` 不一致） |

---

## 2. 差距清单（Gap List）

| ID | 差距 | 严重度 | 说明 |
|---|---|---|---|
| G1 | 无显式 `KnowledgeScope` 枚举 | 中 | scope 靠 `project_id` 可空 + 字符串推导，易错、不可扩展（未来多 campaign 公司级定位脆弱） |
| G2 | ContextService 无 capability/task 投影 | 高 | 内部 agent 拿到全部知识；无法表达「Research 看 X，Content 看 Y」 |
| G3 | KnowledgeFact 无写入侧信任守卫 | 高 | 任何拿到 `Session` 的调用方可写 fact；缺 `trust_level` 断言 |
| G4 | 无矛盾检测 | 高 | 两条相矛盾事实会各自版本化，互不告警；违反「不静默覆盖」 |
| G5 | Policy/Decision 无写入器 | 中 | 系统无法由 agent/流程生产策略与决策，只能种子数据 |
| G6 | 知识审批人身份硬编码 "owner" | 中 | 与 Artifact `ReviewResult`(reviewer_agent_id/user_id) 不一致，缺 agent 级溯源 |
| G7 | 无语义检索 / embeddings | 低(现在) | 当前精确过滤足够；规模化后才需要 |
| G8 | 无知识过期 / 新鲜度 | 低 | 事实永不过期直到被取代，可能长期陈旧 |
| G9 | 无检索审计 | 中 | `TaskContext.source_references` 存在，但无显式「哪个 agent/task 注入了哪条知识」审计事件 |
| G10 | 无 tags/categories | 中 | 检索过滤只能靠 scope+status，无法按主题/能力细分 |
| G11 | 历史任务保留旧快照 | ✅已满足 | `TaskContext` 不可变、构建即存；新任务读当前 APPROVED 头（需显式验证场景确认） |

---

## 3. 最小数据模型变更

### Phase A（不引入向量）

1. **`KnowledgeScope` 枚举**（`PROJECT`/`COMPANY`）+ 在 `KnowledgeFact`/`Policy`/`Decision`/`KnowledgeCandidate` 增加显式 `scope` 列（由 `project_id` 推导后落库，加索引）。消除 G1。
2. **`KnowledgeConflict` 表**：`id, fact_a_id, fact_b_id, detected_at, status(PENDING/RESOLVED), resolution, resolver_agent_id`。插入/审核事实时做**结构性矛盾检测**（同主题/series 或显式 flag），命中即建 Conflict → 进入人工审核，**不自动 supersede**。消除 G4。
3. **KnowledgeFact 写入守卫**：`KnowledgeService` 写 fact 前断言 `actor_trust != EXPERIMENTAL` 且调用方为 owner/internal service；新增 `written_by`(agent_id/user_id) + `writer_trust` 列做溯源。消除 G3/G6。
4. **`retrieval_audit` 表**：`id, task_id, agent_id, knowledge_fact_id, version, projection_reason, injected_at`。`build_context` 注入时发射。消除 G9。
5. **知识 tags/categories**：`KnowledgeFact.tags`(JSON) + 可选 `KnowledgeTag` 表；检索按 tag 过滤。消除 G10。
6. **capability 投影配置**：扩展 `Policy` 或新增 `KnowledgeProjectionPolicy`（`capability`/`task_type` → 允许 `tags`/`series` 白名单），供 `ContextService` 内部投影使用。消除 G2。
7. **Policy/Decision 写入器**：补 `KnowledgeService`/`DecisionService` + API（或显式裁定为 owner-only 并在文档固定）。消除 G5。
8. **`expires_at`**（可空）到 `KnowledgeFact`：可选、Phase A 可后置。缓解 G8。

### Phase B（仅数据量增长后；不引入通用向量库 / 大型 RAG 平台）

9. **embeddings**：`KnowledgeEmbedding(fact_id, model, vector)` 表（或 `KnowledgeFact.embedding` 列），**不使用独立向量数据库**；检索服务算相似度后，**结果仍须过 scope+approval+provenance 三道过滤**才注入。
10. **语义矛盾检测**：基于 embedding 聚类识别潜在冲突，仍走 `KnowledgeConflict` 人工路径。

---

## 4. ContextService 改造建议

- **把 capability/task 投影移入 `ContextService`**（而非仅委派边界）：新增 `_project_for_agent(agent, task, facts)`，按 `agent.capabilities` + `task.type` 对照 fact `tags`/`KnowledgeProjectionPolicy` 做最小权限裁剪。
- **保留不可变 `TaskContext`**：历史任务天然保留构建时快照（G11 已满足）；确保新任务读取当前 `APPROVED` 头。
- **外部 agent**：继续在 `delegation.py` 边界剥离知识；并叠加 G3 写入守卫——`EXPERIMENTAL`/`EXTERNAL` trust 禁止写 `KnowledgeFact`（除非显式授权）。
- **检索审计**：`build_context` 注入每条知识时发射 `retrieval_audit` + `append_audit`。
- **矛盾检测钩子**：fact 置 `APPROVED` 前后跑 `detect_conflict`，命中建 `KnowledgeConflict` 并挂起（不静默覆盖）。

---

## 5. 是否现在真的需要向量库

**不需要。** 理由：
- 当前知识是**结构化、低体量、scope 精确过滤**的（company/project + status + tag 即可命中）。
- 异构 agent 共享的统一知识，现阶段靠「scope 过滤 + 审批态 + capability 投影 + 溯源」即可满足，无需语义相似度。
- 引入向量库/大型 RAG 平台的收益（"相关而非全部"）在事实量未超阈值前为负（运维成本、漂移风险）。
- **判定阈值（Phase B 触发条件）**：①事实数增长使精确过滤召回不足；②agent 需要"与任务相关的知识"而非"scope 内全部"；③出现跨 series 语义冲突需聚类。届时再上 embeddings，且**检索结果强制 scope+approval+provenance 过滤**。

---

## 6. 分阶段实施 Issue（建议拆法）

> 先建一个规划 Issue 承载本盘点；Phase A 各子项建独立实施 Issue（评审通过后）；Phase B 待数据量达标再开。

### Phase A（补全统一知识目录 / 搜索 / 作用域 / 版本 / 冲突 / 来源）
- **A1 — KnowledgeScope 枚举 + 显式 `scope` 列 + 迁移**（G1）
- **A2 — ContextService capability/task 投影**（G2，核心）
- **A3 — KnowledgeFact 写入信任守卫 + 写入人溯源**（G3/G6）
- **A4 — KnowledgeConflict 检测 + 人工审核路径**（G4，核心）
- **A5 — retrieval_audit 表 + build_context 发射**（G9）
- **A6 — KnowledgeFact tags/categories + 过滤**（G10）
- **A7 — Policy/Decision 写入器 + API**（G5）
- **A8 —（可选）expires_at 新鲜度**（G8）

### Phase B（数据量增长后，不开 PR 不实现直到触发）
- **B1 — embeddings 列 + 语义检索服务（检索结果过三道过滤）**
- **B2 — 语义矛盾检测（仍走 KnowledgeConflict 人工）**

---

## 7. 第一条验证场景（验收标准）

**Campaign A 沉淀一条公司级个人 IP 定位知识**
- `KnowledgeCandidate`(scope=COMPANY, source_project_id=CampaignA) → 审核 APPROVE → `KnowledgeFact`(series=`ip_positioning`, v1, project_id=NULL)。

**Campaign B 的三类 Agent 获得适配投影**
- Research Agent（`capabilities=[fact_research]`）：经 A2 投影，仅得 `ip_positioning` 等 research 相关 tag 的知识。
- Strategy Agent（`capabilities=[brand_strategy]`）：得 brand/strategy tag 的知识（含该 IP 定位的 strategy 视角）。
- Content Agent（`capabilities=[copywriting]`）：得 copywriting tag 的知识。
- 三者**共享同一官方版本** `series=ip_positioning, v1`（来源一致，可追溯）。

**外部 Hermes Agent 仅得最小上下文**
- 经 `delegation.py` 边界 + G3 守卫：Hermes（VERIFIED_EXTERNAL）**不得写入** `KnowledgeFact`；其接收上下文仅含任务级白名单，**不含内部知识库**。

**supersede 后**
- Campaign A 更新 IP 定位 → `ip_positioning` v2 取代 v1（v1 置 SUPERSEDED）。
- **新任务**读取 v2；**历史已构建的 TaskContext** 保留 v1 快照（不可变）。
- 若 v2 与某现存事实矛盾 → 触发 A4 `KnowledgeConflict` → 人工审核，**不静默覆盖**。

---

## 附：关键文件索引（供实施参考）

- 模型：`src/aios/models.py:518-627`（KnowledgeCandidate/Fact/ReviewDecision/Decision/Policy/TaskContext）
- 知识服务：`src/aios/knowledge_service.py`（submit/review/next_version/deactivate）
- 上下文服务：`src/aios/context_service.py`（build_context / _knowledge_facts / _latest_versions）
- 委派边界投影：`src/aios/delegation.py:352-373, 589-645`
- 触发器：`alembic/versions/20260718_0007_knowledge_scope_provenance.py:92-209`
- Alembic 头：`20260719_0004_review_protocol.py`
