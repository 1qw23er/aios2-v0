# Workforce W7 Design V1 — Workforce Execution Boundary & Lifecycle Hardening

> **STATUS: DESIGN ONLY.**
> 本文档是 W7 的**设计产物**，不是实现。本阶段**未修改** `src/`、`tests/`、`alembic/` 任何一个文件，**未产生任何迁移**，**未 push / 未开 PR / 未 merge**。
> 唯一新增文件：`docs/workforce/Workforce_W7_Design_V1.md`。

| 项 | 值 |
| --- | --- |
| Design branch | `docs/w7-design` |
| Base | `main` tip `d888a930ccee1294f8bff017aca607cbb05141fa`（PR#15 W6 Squash merge） |
| Base tree | `03bb33464c08451df1156a9986fc1e108b8d45f7` |
| Alembic head（base，未变） | `20260904_0001_workforce_cost_evidence` |
| 前序 | W1–W5 已合并（`bf2f5c1`/PR#3/#4/`225de0a`/PR#7/#8/`3f313ad`/`95330c0`/`6a4fd9f`/`642fbb1`/`fedd388`），W6 已合并（`d888a93`） |
| 仍 OPEN（W6 遗留） | P-9、F-1、F-2、HTTP 409 映射、DR-D4-4 |

---

## 1. Executive Summary

W7 回答两个问题：**Workforce 的执行边界在哪里**（Track A），以及 **Employee 的生命周期是否需要加固**（Track B）。

**核心结论（证据支撑，非推断）：**

1. **Workforce 至今没有任何真实执行入口。** Workforce 五个模块对 `delegation` / `project` / `task` / `budget` **零 import**；`project_id` / `task_id` 只以 `None` 字面量出现在 `append_audit` 调用中。唯一"执行类"入口是 `run_benchmark`（`workforce.py:1327`），其 V1 实现 `_DefaultBenchmarkAdapter`（`workforce.py:1281`）返回 `trusted=False` 的占位结果，**没有执行后端**。`BenchmarkResult`（`models.py:1715`）**没有任何 cost / usage 列**。

2. **repo 里唯一能产生成本的执行路径属于 Delegation 域，且是活的。** 完整链条：
   `WorkerResult.usage["cost"]`（`worker_contract.py:138`）→ `worker_delegated.py:106-108`（桥接成 `response["cost"]`）→ `delegation.py:443`（`r.cost = float(info["cost"])`）→ `delegation.py:491`（`project.budget_used += cost`，**全仓唯一写入点**）→ `build_delegated_provenance`（`delegation.py:554`，把 `cost` / `usage` 写进 `Artifact.provenance_json`）。
   这条链锚定 `Task → Project`，**不可被当作 Workforce 可归因的成本源事件**（W5 设计已明令禁止复用 `delegated_run.id` 冒充 Workforce 源事件）。

3. **本地执行零成本计量。** `ExecutionResult`（`execution.py:45`）字段只有 `summary / claims / artifacts / metadata`，**没有 usage / cost**；`LLMExecutionAdapter.run`（`execution.py:571`）写入的 metadata 只有 `{"attempts": …, "max_attempts": …}`（`execution.py:629`）。因此"Workforce 走本地执行然后记账"这条路**今天在数据结构上就不存在**。

4. **预算/成本/证据三权分离，且各自唯一（已证明）：**
   - Budget Authority = **Delegation**（`check_budget` `delegation.py:157`，唯一调用点 `:282`，位于远程提交**之前**）；
   - Authoritative Ledger = **`Project.budget_used`**（`models.py:257`，唯一写入 `delegation.py:491`）+ `DelegatedRun.cost`（`models.py:454`）；
   - Cost Evidence = **`cost_evidence`**（`models.py:2143`），是 **evidence / projection**，**不是 budget authority**，永不写 budget。

5. **P-9 建议：目标方案 = C（execution receipt），V1 维持 = A（caller responsibility）。** 明确**否决 B（source-event registry）与 D（跨域 FK）**。理由见 §10。（`[DECISION REQUIRED] DR-W7-1` = C 的采纳时机）

6. **F-1：W7 不改 FK。** Employee 无 DB 级删除阻挡是**事实**，但"没有写入者"也是**事实**（全仓无 `session.delete(Employee)`）。建议以**零迁移的测试级不变量**钉死"不存在 Employee 删除写入者"，而非提前加 schema 约束。（`[DECISION REQUIRED] DR-W7-2`）

7. **F-2：W7 不改 FK。** 决定性证据：**全仓 11 个指向 `agent` 的 FK 全部 NO ACTION**，且 `agent_registry.py` **没有任何 Agent 删除函数**（只有 `set_agent_enabled` `:202` 软停用 = `status=UNAVAILABLE`；`:296` 的 `session.delete(row)` 只删 `AgentCapability` 行）。`employee.agent_id` 的 NO ACTION 是**与既有约定一致**，不是遗漏。改 RESTRICT 需 **SQLite 表重建迁移**，且会引入 Workforce → Agent Registry 的跨域强依赖。（`[DECISION REQUIRED] DR-W7-3`）

8. **DR-D4-4 不拍板。** W6 批准 D4-4 的前提是 DR-D4-1=(B)，而实际选了 **(A) 不加 TERMINATED**。`EmployeeStatus`（`models.py:1996`）仍只有 `ACTIVE` 单成员，W4 无 terminate writer ⇒ "terminated employee" 这个实体**今天不存在**，D4-4 的子语义**当前不适用**。（`[DECISION REQUIRED] DR-W7-4`）

9. **执行边界目前是"故意未桥接"的。** `Workforce → … → Delegation` 之间**零边**。W7 的结论不是去搭桥，而是**把桥的形状定义清楚，并把"不得伪造桥"写成不变量**。（`[DECISION REQUIRED] DR-W7-5`）

10. **W7 迁移影响 = 零。** 所有建议均为 docs-only 或**测试级**不变量；无 additive 迁移、无表重建、无 schema 变更。

---

## 2. Current Repo Evidence（证据基座）

### 2.1 取证方法

- 全部事实以 `file:line` 标注，来源为 `main` tip `d888a93` 的工作副本。
- **repo 级检索一律使用 Bash `grep -rn` / SQLite `PRAGMA`**（W6 教训：Grep 工具曾对 `check_budget|budget_used` 返回假 "No matches"，不得信任单次空结果）。
- FK 的 `on_delete` 语义通过 SQLite `PRAGMA foreign_key_list(<table>)` 实测取得，**不读** `sqlite_master.sql` 文本（CREATE TABLE 内约束声明顺序非确定性，见长期记忆）。
- 凡无法从 repo 证明的内容，一律标 `[ASSUMPTION]`，不冒充 fact。

### 2.2 执行层（repo 已存在，非 W7 引入）

| 事实 | 位置 |
| --- | --- |
| `ExecutionResult` 数据模型（字段：`summary` / `claims` / `artifacts` / `metadata`） | `src/aios/execution.py:45` |
| **无 usage / cost 字段** | `src/aios/execution.py:45-56` |
| `ExecutionAdapter` Protocol，`run(task_id, task_context, output_schema, idempotency_key)` | `src/aios/execution.py:60-61` |
| `execute_task`（幂等守卫 → claim → `route_task` → RUNNING → TaskContext → `adapter.run` → schema 校验 → Artifact → `complete_task`） | `src/aios/execution.py:187` |
| `execute_task` 依赖 `Orchestrator` / `complete_task` | `src/aios/execution.py:40` |
| `execute_task` 依赖 `scheduler.route_task` | `src/aios/execution.py:41` |
| `LLMExecutionAdapter` | `src/aios/execution.py:519` |
| `LLMExecutionAdapter.run` | `src/aios/execution.py:571` |
| 本地执行 metadata 仅含 attempts | `src/aios/execution.py:629` |
| `WorkerResult.usage` 字段 | `src/aios/worker_contract.py:138` |
| usage 值强校验：numeric / finite / non-negative | `src/aios/worker_contract.py:159-163` |
| `WorkerCapabilities` 要求 `"usage"` 能力 | `src/aios/worker_contract.py:74` |
| 适配器工厂 `build_execution_adapter` | `src/aios/adapters/factory.py` |
| **usage → cost 桥接点** | `src/aios/adapters/worker_delegated.py:106-108` |

`execute_task` 的三个调用点：

- `src/aios/api/app.py:708`（endpoint `execute_task_endpoint`）
- `src/aios/api/app.py:2557`
- `src/aios/review.py:972`

### 2.3 预算 / 成本权威（Delegation 域）

| 事实 | 位置 |
| --- | --- |
| `BudgetExceededError` | `src/aios/delegation.py:131` |
| `assert_trust_delegable`（gate 之一） | `src/aios/delegation.py:144` |
| `check_budget` 定义 | `src/aios/delegation.py:157` |
| `check_budget` **唯一调用点**（远程 submit 之前） | `src/aios/delegation.py:282` |
| `r.cost = float(info["cost"])` | `src/aios/delegation.py:443` |
| `_accrue_budget` 定义 | `src/aios/delegation.py:476` |
| `_accrue_budget` 唯一调用 | `src/aios/delegation.py:336` |
| **`Project.budget_used` 唯一写入点** | `src/aios/delegation.py:491` |
| `build_delegated_provenance`（把 cost / usage 写入 provenance） | `src/aios/delegation.py:554` |
| `Project.budget_limit` / `budget_used` 列 | `src/aios/models.py:253` / `:257` |
| `DelegatedRun` 表（`cost` / `usage`） | `src/aios/models.py:454` |
| `Task.estimated_cost` | `src/aios/models.py:378` |

### 2.4 Workforce 侧（无执行后端、无成本列）

| 事实 | 位置 |
| --- | --- |
| `BenchmarkOutcome` | `src/aios/workforce.py:1251` |
| `BenchmarkAdapter` Protocol | `src/aios/workforce.py:1267` |
| `_DefaultBenchmarkAdapter`（返回 `trusted=False` 占位，**无执行后端**） | `src/aios/workforce.py:1281` |
| `run_benchmark`（幂等 adopt、SAVEPOINT、fail-closed `status="unknown"`） | `src/aios/workforce.py:1327` |
| `BenchmarkResult` 表（**无 cost / usage 列**） | `src/aios/models.py:1715-1750` |
| `workforce_trial.py` 唯一函数 `create_trial_from_approval` | `src/aios/workforce_trial.py:51` |
| P-9 写入点：仅校验 `source_event_type` / `source_event_id` **非空** | `src/aios/workforce_cost_evidence.py:98-101` |
| `idempotency_key = f"{source_event_type}:{source_event_id}"` | `src/aios/workforce_cost_evidence.py:113` |
| 审计 `project_id=None, task_id=None` | `src/aios/workforce_cost_evidence.py:138-139` |

### 2.5 Employee / Agent 生命周期

| 事实 | 位置 |
| --- | --- |
| `EmployeeStatus` 单成员 `ACTIVE`，零出边 | `src/aios/models.py:1996` |
| `Employee` 表（`agent_id` docstring 明示为软引用 NO ACTION） | `src/aios/models.py:2083` |
| `uq_employee_trial` 唯一约束 | `src/aios/models.py`（`Employee.__table_args__`） |
| `CostEvidence` 表（双 FK RESTRICT + `idempotency_key` UNIQUE） | `src/aios/models.py:2143` |
| `agent_registry` **无 Agent 删除函数** | `src/aios/agent_registry.py`（全文件） |
| `set_agent_enabled`（owner-only，停用 = `UNAVAILABLE` 软停） | `src/aios/agent_registry.py:202` |
| 唯一 `session.delete`（删 `AgentCapability` 行，非 Agent） | `src/aios/agent_registry.py:296` |
| `promote_to_employee` | `src/aios/workforce_employee.py:329` |
| `release_candidate` | `src/aios/workforce_employee.py:433` |
| Workforce 唯一物理删除先例 `purge_recommendation`（owner-only、非 terminal 则 409、全列审计快照） | `src/aios/workforce_recommendation.py:667`，`session.delete(rec)` `:729` |

FK 实测（`PRAGMA foreign_key_list`）：

- `employee`：`job_version_id` / `job_id` / `trial_id` / `candidate_id` = **RESTRICT**；`agent_id` = **NO ACTION**。
- **指向 `agent` 的 11 个 FK 全部 NO ACTION**：`agent_capability`、`execution_assignment`、`task_context`、`task`（×2）、`delegated_run`、`review_result`、`review_assignment`、`agent_secret`、`candidate`、`employee`。

### 2.6 API / 错误翻译

| 事实 | 位置 |
| --- | --- |
| `_translate` 原样透传 `ServiceError.status_code` | `src/aios/api/app.py:301` |
| **无全局 `exception_handler`** | `src/aios/api/app.py`（grep 全空） |
| **无 `@app.delete` 路由** | `src/aios/api/app.py`（grep 全空） |
| **无 Workforce 路由**（`/workforce`、`/jobs`、`/employees`、`/trials` 全空） | `src/aios/api/app.py`（grep 全空） |
| `IntegrityError` **无统一翻译**，各模块自行 except | `agent_registry.py:392`、`review.py:307/818/1089/1379`、`content_draft.py` 多处 |
| `Event` outbox（`project_id` 非空、`idempotency_key` UNIQUE、`status=PENDING`） | `src/aios/models.py:532-545` |
| `append_event` | `src/aios/services.py:57` |
| `_replay`（冲突 / 缺失资源均 409） | `src/aios/services.py:78` |
| `ServiceError` | `src/aios/services.py:37` |

---

## 3. W1–W6 Baseline（本设计的起点）

| 波次 | 内容 | 合并结果 |
| --- | --- | --- |
| W1+W2 | Discovery + Evaluation | PR#2 → `bf2f5c1` |
| W3-A/B/C/D | Match / Benchmark / Recommendation / Approval+Trial | PR#3/#4/#5(`225de0a`)/#7/#8/#10(`3f313ad`) |
| W4 | Employee（`promote_to_employee`，trial → employee） | PR#12 → `6a4fd9f` |
| W5 | Cost Evidence 记账（additive 表 + writer + 审计同 SAVEPOINT） | PR#14 → `fedd388` |
| W6 | Decision-Freeze Invariants（tests-only，10 条不变量） | PR#15 → `d888a93` |

W6 冻结的 9 项决策（DR-D1-1~D1-4、DR-D4-1~D4-5）中，与 W7 直接相关的三条：

- **DR-D1-1 = (d)**：不造 Workforce 原生成本源事件；**不伪造** `delegated_run` / `benchmark` / `trial` 冒充 producer。
- **DR-D1-2 = (a)**：复用 Delegation 的执行 + gate + ledger，**禁止第二预算权威**。
- **DR-D1-4 = (b)**：**不建 source-event registry**，P-9 留 OPEN，由 caller 负责源事件身份真实性。
- **DR-D4-1 = (A)**：不加 `TERMINATED`，Employee 永久保留。

W7 的立场：**这四条全部继续有效，W7 不推翻任何一条。** W7 只做两件事——(1) 把"执行边界"从隐含约定变成显式契约；(2) 把 F-1 / F-2 的"要不要改 FK"变成有证据的决策题。

---

## 4. Execution Boundary（Track A Q1–Q2）

### Q1：Job / Trial / Benchmark 是否真有执行入口？

**答：没有。**

| 对象 | 是否存在执行入口 | 证据 |
| --- | --- | --- |
| `Job` / `JobVersion` | 否。纯编排元数据 | `models.py:1436` / `:1462`；`workforce.py` 无执行函数 |
| `Trial` | 否。只有创建函数 | `workforce_trial.py:51` 是该文件**唯一**函数 |
| `Benchmark` | **名义上有，实质无后端** | `run_benchmark` `workforce.py:1327`；默认适配器 `_DefaultBenchmarkAdapter` `workforce.py:1281` 返回 `trusted=False` 占位；`BenchmarkAdapter` Protocol `:1267` 是可替换 seam 但 V1 无真实实现 |

**结论（fact）**：Workforce 域内**不存在**任何会产生可计费副作用的执行路径。`run_benchmark` 的 `trusted=False` 是显式的"这不是可信证据"声明，而非"低成本执行"。

### Q2：第一个 execution producer 属于哪个 domain？

**答：按现有 repo 事实，属于 Delegation 域（与 Workforce 无关）。**

```
WorkerResult.usage["cost"]        worker_contract.py:138   （worker 自报 usage）
        ↓
response["cost"] = usage["cost"]  adapters/worker_delegated.py:106-108
        ↓
r.cost = float(info["cost"])      delegation.py:443        （DelegatedRun.cost）
        ↓
project.budget_used += cost       delegation.py:491        （唯一 ledger 写入）
        ↓
Artifact.provenance_json          delegation.py:554        （build_delegated_provenance：含 delegated_run_id / cost / usage）
```

这条链的所有权清晰：

- **producer** = remote worker（外部），经 `WorkerClient` / `WorkerResult` 契约进入；
- **ingest + 记账 owner** = `delegation.py`；
- **ledger owner** = `Project.budget_used`（`models.py:257`）；
- **receipt** = `Artifact.provenance_json`。

**它不能被 Workforce 复用的原因（fact，非推断）**：

1. 该链锚定 `Task → Project`（`execute_task` 依赖 `scheduler.route_task` `execution.py:41` 与 `orchestrator.complete_task` `:40`）；
2. Workforce 五个模块对 `delegation` / `project` / `task` **零 import**，`project_id` / `task_id` 只以 `None` 字面量出现在 `append_audit`（`workforce.py:677-1688`、`workforce_cost_evidence.py:138-139`）；
3. W5 设计已明令：**禁止把 `delegated_run.id` 当作 Workforce 可归因的源事件**（属 Delegation 域，冒充即伪造）。

`[DECISION REQUIRED] DR-W7-5`：未来是否允许 Workforce 通过 Delegation 执行（从而产生 Workforce 可归因成本）。W7 **不拍板**，但把约束写死（§19）。

---

## 5. Execution Producer Ownership（Track A Q3–Q4）

### Q3：Workforce 是否应该直接调用 Delegation？

**答：不应该。W7 建议维持"零直接调用"，并把它升级为显式不变量。**

理由（全部 repo-evidence-backed）：

1. **循环依赖风险**：Delegation 不依赖 Workforce（无 import），Workforce 若 import Delegation 即形成单向新边；虽然此刻不成环，但一旦 Delegation 侧引入任何 Workforce 引用（例如成本归因回写），环立刻成立。
2. **预算权威唯一性**：`check_budget` 的唯一调用点在 `delegation.py:282`。若 Workforce 也发起远程执行，就必须**也**过这个 gate —— 那意味着 Workforce 要么复用 Delegation 的执行（等于交出执行权），要么自建 gate（**违反 DR-D1-2**）。
3. **零耦合是现状资产，不是欠债**：Workforce 当前对 Project/Task 零引用，使它可以独立演进、独立测试（W6 的 10 条不变量无需 Project fixture）。

### Q4：是否需要统一的 execution contract / adapter seam？

**答：repo 已有统一 seam，Workforce 也已有自己的 seam；W7 不新增 seam，只把两者的关系定义清楚。**

| seam | 归属 | 事实 |
| --- | --- | --- |
| `ExecutionAdapter` Protocol + `execute_task` | 核心执行域 | `execution.py:60` / `:187` |
| 适配器选择 | 核心执行域 | `adapters/factory.py`（`AIOS_DEEPSEEK_HARNESS_ENABLED` + `agent.config_ref` 前缀 `deepseek-harness+file://` → Harness；否则 `LLMExecutionAdapter()`） |
| `BenchmarkAdapter` Protocol + `run_benchmark` | **Workforce 域** | `workforce.py:1267` / `:1327` |

关键观察（fact）：`ExecutionResult`（`execution.py:45`）**没有 usage / cost 字段**。这意味着：

> **即便 Workforce 接入核心 `ExecutionAdapter` seam，也拿不到任何成本数据。**
> 核心执行 seam 是**无成本计量**的（`LLMExecutionAdapter` 只产出 `attempts` metadata，`execution.py:629`）。

因此"统一 seam"这个问题的正确答案是：**成本不是 seam 的属性，而是 adapter 实现的属性**。只有 `worker_delegated` 这条远程 worker 路径会带出 `cost`（`worker_delegated.py:106-108`），而它在 Delegation 域内。

W7 结论：

- **不新增**统一 execution contract（无 producer，新增即空契约）；
- **保留**两个既有 seam 的边界：`BenchmarkAdapter` 是 Workforce 的执行 seam，`ExecutionAdapter` 是核心执行 seam；
- 若未来要桥接，桥接点是 **adapter 实现**，不是 **contract 定义**（见 §19.3）。

---

## 6. Delegation Boundary（Track A Q5）

### Q5：Delegation 如何保持 budget gate / `Project.budget_used` / ledger 的权威？

现状（fact）：

| 权威 | 唯一性证据 |
| --- | --- |
| Budget gate | `check_budget` 定义 `delegation.py:157`；**全仓唯一调用** `:282`，位于远程 submit **之前** |
| Ledger 写入 | `project.budget_used = float(project.budget_used) + cost` —— **唯一** `delegation.py:491`，在 `_accrue_budget`（`:476`）内，`_accrue_budget` 唯一调用 `:336` |
| Ledger 数据 | `Project.budget_limit` `models.py:253` / `budget_used` `:257` |
| 成本记录 | `DelegatedRun.cost` `models.py:454`，唯一写入 `delegation.py:443` |
| Receipt | `Artifact.provenance_json` via `build_delegated_provenance` `delegation.py:554` |

**W7 建议的不变量（设计，不实现）：**

- **I-B1**：`Project.budget_used` 的写入者集合大小恒为 1（= `delegation.py:491`）。
- **I-B2**：`check_budget` 的调用者集合 ⊆ {`delegation.py:282`}。
- **I-B3**：Workforce 模块集合 ∩ （读写 `Project.budget_*` 的模块集合）= ∅。
- **I-B4**：Workforce 模块集合 ∩ （`check_budget` 调用者集合）= ∅。

这四条今天**已经成立**（否则 W5/W6 的测试不会绿），W7 的价值在于把它们从"碰巧成立"变成"被测试钉死"。

---

## 7. Budget Authority（Track A Q6）

**唯一预算权威 = Delegation。** 证据见 §6。

明确的反模式（W7 明令禁止，将写成不变量）：

| 反模式 | 为什么禁止 |
| --- | --- |
| Workforce 建第二个 budget gate | 违反 DR-D1-2；两个 gate 必然漂移（一个放行一个拦） |
| Workforce 读写 `Project.budget_*` | 破坏 I-B1 / I-B3；`Project` 是 Delegation 域的聚合根 |
| `cost_evidence` 充当预算快照 | `cost_evidence` 是投影；预算必须由权威 ledger 回答 |
| 用 `Task.estimated_cost`（`models.py:378`）做 Workforce 闸门 | 估值是**计划值**，不是**实际发生值**；`budget_used` 才是实绩 |

**当前 repo 事实**：Workforce 没有任何上述行为（零 import、零读写、零调用）。

---

## 8. Cost Authority（Track A Q7）

成本的"权威"必须分层回答，否则会出现"谁是成本真相"的歧义：

| 层级 | 权威 | 事实 |
| --- | --- | --- |
| **成本发生**（谁真的花了钱） | remote worker | `WorkerResult.usage["cost"]` `worker_contract.py:138` |
| **成本计量**（谁把它变成数字） | Delegation | `delegation.py:443` |
| **成本归集**（谁的账本） | `Project.budget_used` | `delegation.py:491` |
| **成本留痕**（谁能事后证明） | `Artifact.provenance_json` | `delegation.py:554` |
| **Workforce 归因**（这笔钱算在哪个 Employee / Job 上） | **当前无权威** | Workforce 无原生源事件（§4），`BenchmarkResult` 无 cost 列（`models.py:1715-1750`） |

**关键结论（fact + 结论）**：

> 前四层权威**已经存在且唯一**；第五层（Workforce 归因）**目前没有数据源，因此没有权威**。
> 这不是"还没建"，而是"输入不存在"。在第一个 Workforce 可归因 producer 出现之前，任何"Workforce 成本权威"都是伪造。

这与 **DR-D1-1 = (d)** 完全一致。W7 重申：**不伪造第五层**。

---

## 9. Cost Evidence Boundary（Track A Q8）

### Q8：source event 的 producer / consumer / owner 分别是谁？

| 角色 | 应为 | 当前 repo 事实 |
| --- | --- | --- |
| **Producer**（产生源事件的实体） | 真正花钱的执行 | **不存在**。唯一能花钱的是 Delegation 远程路径，而它归属 Delegation 域、不可借用 |
| **Consumer**（消费源事件的实体） | `record_cost_evidence` | `workforce_cost_evidence.py:65` |
| **Owner**（定义源事件语义与真实性） | 见下 | **未定**（`[DECISION REQUIRED]`，DR-D1-3 在 W6 已"暂不拍板"） |

`cost_evidence` 当前的**输入契约**（fact，来自 `workforce_cost_evidence.py`）：

| 字段 | 校验 | 位置 |
| --- | --- | --- |
| `source_event_type` | 仅非空 | `:98-101` |
| `source_event_id` | 仅非空 | `:98-101` |
| `idempotency_key` | 由 `{type}:{id}` 派生 | `:113` |
| 幂等 | UNIQUE 约束 + adopt 语义 | `:113` + `models.py:2143` |
| 审计 | 证据 + 审计同 `begin_nested()` SAVEPOINT | `workforce_cost_evidence.py` |
| `project_id` / `task_id` | 恒 `None` | `:138-139` |

**W7 对 cost_evidence 边界的定义（契约，不改代码）：**

1. `cost_evidence` 是 **evidence / projection**，不是 ledger、不是 gate、不是权威。
2. 它**只允许**记录**已发生**的成本，**不允许**估算、预测、回填。
3. 它的 `source_event_*` 是**引用**，不是**所有权** —— `cost_evidence` 不保证被引用行存在（这正是 P-9）。
4. 它**永不**写 `Project.budget_*`，**永不**调 `check_budget`。
5. 它**不加** `Project` FK（Workforce 链零 `project_id`，见 `:138-139`）—— 加 FK 会凭空造出一条 Workforce → Project 的边，违反 §6/I-B3。

---

## 10. P-9 Source Event Trust Model（Track A Q10）

### 10.1 问题重述

`record_cost_evidence` 只校验 `source_event_type` / `source_event_id` **非空**（`workforce_cost_evidence.py:98-101`），**不校验被引用行是否存在**。后果：可以写入一条指向不存在事件的成本证据，且**幂等键仍有效**（`{type}:{id}` 唯一），后续无法用幂等机制纠错。

W6 决策 **DR-D1-4 = (b)**：不建 registry，caller 负责。**但 P-9 仍标 OPEN。** 本节按 W7 要求做 A–E 全维度比较。

### 10.2 五方案比较

| 维度 | **A. Caller responsibility**（现状，DR-D1-4=(b)） | **B. Source-event registry**（新表 + 每类事件 FK） | **C. Execution receipt**（执行层落 receipt 行，evidence 引用 receipt） | **D. FK / 跨域 FK**（`cost_evidence` 直接 FK 到源表） | **E. 其他：读时校验 / 对账作业** |
| --- | --- | --- | --- | --- | --- |
| **Ownership** | caller（无归属） | Workforce 拥有 registry（但它不产生事件 → 越权） | **执行层拥有 receipt**（谁执行谁拥有，天然正确） | 跨域：Workforce → Delegation / Benchmark，Workforce 无权 | 无归属，后置审计 |
| **Coupling** | 零 | 高：每新增事件类型都要改 registry + FK | 中：只认一张 receipt 表，事件类型无关 | **最高**：跨域 FK，且 `delegated_run` 被 W5 明令禁止 | 低 |
| **Transaction boundary** | 无（跨事务引用，天然弱） | 写入 registry 需与源事件同事务 → 要求 Workforce 参与别域事务 | **receipt 与执行同事务**（执行层自己写），evidence 后置引用 | FK 强制跨事务存在性，但跨域事务不可能 | 无 |
| **Replay** | 幂等键 `{type}:{id}` 天然重放安全 | registry 行唯一 → 重放安全 | receipt id 稳定 → 重放安全 | FK 保证存在 → 重放安全 | 对账可发现漂移，但不阻止 |
| **Failure semantics** | 静默接受悬空引用（当前缺口） | 写入时 FK 失败 → 显式报错 | 写入时 FK 失败 → 显式报错 | 写入时 FK 失败 → 显式报错 | 事后发现，已污染 |
| **Migration cost** | **0** | 新表 + N 个 FK + 回填（**高**） | 1 张 receipt 表 + 1 个 FK（**中**，且属执行域） | 跨域 FK + SQLite 表重建（**高**） | 0（但需后台作业） |
| **Cross-domain dependency** | 无 | Workforce 需知道所有事件类型（**违反边界**） | Workforce 只认 receipt（**单向、低**） | **Workforce ⇸ Delegation 硬依赖，成环风险真实** | 无（但需读权限） |
| **是否伪造 producer** | 不伪造，但也不验证 | 不伪造 | 不伪造（receipt 由真执行产生） | **等于把 Delegation 事件认作 Workforce 事件 → 冒充** | 不伪造 |
| **与 DR-D1-1=(d) 一致性** | 一致 | 一致 | 一致 | **冲突** | 一致 |

### 10.3 否决理由（明确）

- **否决 B（registry）**：Workforce 不产生任何源事件（§4 已证），却要拥有一张"所有源事件"的注册表 —— 这是**在没有所有权的地方建立所有权**，且每新增事件类型都要改 Workforce 的 schema。DR-D1-4=(b) 已否决，W7 维持。
- **否决 D（跨域 FK）**：`cost_evidence` 若 FK 到 `delegated_run`，等于把 Delegation 域的事件认作 Workforce 可归因成本源 —— **正是 W5 明令禁止的"冒充"**；且会造出 Workforce ⇸ Delegation 的硬依赖边，与 §6 的 I-B3/I-B4 直接冲突。FK 到 `benchmark_result` 看似可行，但 `BenchmarkResult` **无 cost 列**（`models.py:1715-1750`），引用它无法证明"成本"，只能证明"跑过一次 benchmark"。

### 10.4 推荐（repo-evidence-backed）

> **推荐：目标方案 = C（execution receipt）；V1 维持 = A（caller responsibility）。**

**为什么 C 在 repo 里有天然落点（fact）**：

`build_delegated_provenance`（`delegation.py:554`）已经把 `delegated_run_id` / `cost` / `usage` 写进 `Artifact.provenance_json` —— **这在事实上就是一个 execution receipt**，只是它属于 Delegation 域、锚定 `Task`。

因此 C 的正确形态是：

```
Workforce 可归因执行
   → 执行层（归属待定，见 DR-W7-5）落一条 receipt 行（含 cost / usage / 幂等键）
   → record_cost_evidence(source_event_type="<receipt 类型>", source_event_id=<receipt.id>)
   → cost_evidence 加一个 FK 到 receipt 表（单表、单 FK、additive）
```

**为什么不是现在就做 C**：receipt 的 **producer 不存在**（§4/§8）。在没有 producer 时加 receipt 表 + FK，就是"为不存在的事件建容器" —— 与 DR-D1-1=(d) 的精神（不伪造）相悖，且会给 caller 制造"可以记账了"的假象。

**W7 的分段主张：**

| 阶段 | 方案 | 迁移 |
| --- | --- | --- |
| **现在（W7 及之前）** | **A**：caller 负责；P-9 保持 OPEN；用**测试级不变量**钉死"现状是 A" | **0** |
| **第一个 Workforce 可归因 producer 落地时** | 切 **C**：producer 同事务落 receipt；`cost_evidence` 加单 FK | **1 次 additive 迁移** |

`[DECISION REQUIRED] DR-W7-1`：是否采纳 C 作为目标方案，以及切换触发条件（"第一个 producer 落地"由谁认定）。
`[DECISION REQUIRED] DR-W7-1b`：receipt 表的归属域（执行域 / Workforce 域 / 新域）—— 在 DR-W7-5（是否允许 Workforce 经 Delegation 执行）有答案之前无法定。

---

## 11. Employee Lifecycle（Track B 前置：domain ownership 与生命周期语义）

在讨论 F-1 / F-2 **之前**，必须先把"谁拥有 Employee 生命周期"证明清楚（用户明确要求：不得因发现 F-1/F-2 就直接改 FK）。

### 11.1 Employee 的域归属

| 证据 | 说明 |
| --- | --- |
| Employee 的创建者 | `promote_to_employee` `workforce_employee.py:329`（Workforce 域，唯一写入者） |
| Employee 的父引用 | `job_version_id` / `job_id` / `trial_id` / `candidate_id` 全部 **RESTRICT**（PRAGMA 实测） |
| Employee 的下游引用者 | `cost_evidence.employee_id`（RESTRICT，`models.py:2143`）—— **V1 唯一** |
| 其他域是否引用 employee | **否**（全仓检索无 delegation / task / project / review 侧引用） |
| 是否存在 Employee 删除写入者 | **否**（全仓无 `session.delete(Employee)`） |
| 是否存在 Employee 状态机 | `EmployeeStatus` 单成员 `ACTIVE`，零出边 `models.py:1996` |

**结论（fact）**：**Employee 的生命周期 100% 归属 Workforce 域**，且**当前生命周期只有"创建"一个动作** —— 没有终止、没有暂停、没有删除。

### 11.2 生命周期语义（当前）

```
Candidate --(create_trial_from_approval)--> Trial --(promote_to_employee)--> Employee(ACTIVE)
                                                                                  │
                                                                           （无出边，终态）
```

- `uq_employee_trial`：一个 Trial 最多产生一个 Employee（不可重复晋升）。
- 四个 RESTRICT 父 FK 的语义：**Employee 存在 ⇒ 其四个父对象不可被删**。这是**保护上游**，不是保护 Employee。
- `cost_evidence.employee_id` RESTRICT 的语义：**cost_evidence 有行 ⇒ Employee 不可被删**。这是**唯一保护 Employee 的 FK**，但 V1 表为空 ⇒ **实际无保护**。

### 11.3 "永久保留"是设计还是欠债？

**是设计（fact）**：

- DR-D4-1 = (A) 明确"不加 TERMINATED，永久保留"；
- DR-D4-3 = (a) "purge 永久禁止"；
- 实现层：`EmployeeStatus` 单成员、零出边、无 terminate writer、无 purge writer。

因此 **Employee 删除当前不是"缺少保护"，而是"不存在这个动作"**。这直接决定 F-1 / F-2 的处理方式（见下两章）。

---

## 12. F-1 Analysis（Employee 无 DB 级删除保护）

### 12.1 事实陈述

| # | 事实 | 证据 |
| --- | --- | --- |
| F-1-a | Employee 的四个父 FK 是 RESTRICT，保护的是**父对象**，不是 Employee | PRAGMA 实测 |
| F-1-b | 唯一引用 employee 的子 FK 是 `cost_evidence.employee_id`（RESTRICT） | `models.py:2143` |
| F-1-c | `cost_evidence` 表 V1 **预期 0 行**（无 Workforce 可归因源事件，§8） | W5 设计 + §4 |
| F-1-d | 全仓**没有** `session.delete(Employee)` | 全仓检索 |
| F-1-e | 因此：DB 层**不阻止**删除 Employee；只靠"没有代码删它"的约定 | 由 a–d 推出 |

**F-1 是真缺口，但当前风险 = 0**（无写入者 + 无下游数据）。

### 12.2 可选处置（含成本）

| 方案 | 做法 | 迁移 | 评估 |
| --- | --- | --- | --- |
| **F1-i** 什么都不做（维持约定） | — | **0** | V1 可接受；风险取决于"未来没人加 delete" |
| **F1-ii** 应用级守卫 | 若将来出现 delete writer，必须走一个恒 409 的 `purge_employee`（参照 `purge_recommendation` `workforce_recommendation.py:667` 的门 + 审计快照模式） | **0** | 只在实际引入 writer 时才有意义 |
| **F1-iii** 测试级不变量 | 断言"repo 中不存在 Employee 删除写入者 / `EmployeeStatus` 无 terminal 成员" | **0** | **W7 推荐**：零迁移、可 CI 钉死、不预设未来 |
| **F1-iv** DB 级约束 | 需要一张**必然有行**的子表或 CHECK 约束 | 需新表/表重建 | **不推荐**：为约束而造表 = 为不存在的语义建 schema |

### 12.3 W7 建议

> **F-1：W7 不改 FK、不加约束、不加代码。采 F1-iii（测试级不变量）。**

理由：`Employee` 当前无删除写入者（F-1-d），加任何 DB 约束都是为**尚未存在的动作**付费，且会锁死 DR-D4-1 / DR-D4-3 未来被重新决策的空间。测试级不变量能在**有人真的引入 delete writer 时立刻失败**，把决策点推迟到真正需要决策的时刻 —— 这正是"不替 owner 拍板"。

`[DECISION REQUIRED] DR-W7-2`：是否要在**尚无 delete writer 的情况下**预先实现一个恒 409 的 `purge_employee`（F1-ii）作为防御性 API？W7 建议**否**。

---

## 13. F-2 Analysis（`employee.agent_id` 为 NO ACTION）

### 13.1 事实陈述

| # | 事实 | 证据 |
| --- | --- | --- |
| F-2-a | `employee.agent_id → agent.id` 的 `on_delete` = **NO ACTION** | PRAGMA 实测 |
| F-2-b | **全仓 11 个指向 `agent` 的 FK 全部 NO ACTION**（`agent_capability`、`execution_assignment`、`task_context`、`task`×2、`delegated_run`、`review_result`、`review_assignment`、`agent_secret`、`candidate`、`employee`） | PRAGMA 实测 |
| F-2-c | `agent_registry.py` **没有任何 Agent 删除函数** | 全文件检索 |
| F-2-d | Agent 的"停用"是**软停用**：`set_agent_enabled` `agent_registry.py:202`（owner-only）→ `status=UNAVAILABLE` | `agent_registry.py:202` |
| F-2-e | `agent_registry.py` 唯一的 `session.delete`（`:296`）删的是 **`AgentCapability` 行**，不是 Agent | `agent_registry.py:296` |
| F-2-f | `Employee` 模型 docstring 明示 `agent_id` 为**软引用** | `models.py:2083` |

### 13.2 Domain ownership（用户要求先证明）

**Agent 生命周期权威 = `agent_registry`（Agent Registry 域），不属于 Workforce。**

- Workforce 只**引用** Agent（`candidate.agent_id`、`employee.agent_id`），不拥有它；
- Agent 的生命周期语义由 `agent_registry` 定义，当前语义是：**"软停用 + 无物理删除写入者"**；
- 因此 `employee.agent_id` 用 NO ACTION 是**与全仓既有约定一致**（11/11），不是 W4 的遗漏。

### 13.3 风险判定

> **F-2 当前是"理论不匹配"，不是"活跃数据风险"。**

理由链：

1. NO ACTION 只在**父行被删**时才有意义；
2. Agent 行**没有删除写入者**（F-2-c/e）；
3. 真实语义是软停用（F-2-d），软停用后 Employee 仍指向一个 `UNAVAILABLE` 的 Agent —— 这是**引用语义**，不是**完整性破坏**。

### 13.4 若改为 RESTRICT 的代价

| 代价项 | 说明 |
| --- | --- |
| **迁移** | SQLite 不改 `CREATE TABLE` 无法变更 FK `ondelete` → 需 `batch_alter_table` **表重建**（`cost_evidence` 的 additive 迁移是新建表，性质完全不同） |
| **跨域强依赖** | Workforce 的 `employee` 表将对 `agent` 表产生删除期硬约束，把 Agent Registry 的生命周期决策绑进 Workforce schema |
| **语义冲突** | 若未来要支持"停用并清理 Agent"，RESTRICT 会阻塞；NO ACTION 至少留了（危险但可用的）空间 |
| **一致性伪问题** | 只改 `employee` 一处会破环 11/11 的一致性；全改则是**跨 10 张表的大迁移**，远超 W7 范围 |

### 13.5 W7 建议

> **F-2：W7 不改 FK。改为把"Agent 生命周期权威 = agent_registry，Agent 物理删除不是受支持操作"写成测试级不变量。**

`[DECISION REQUIRED] DR-W7-3`：若未来引入 Agent 物理删除写入者，是否在**同一波**把 11 个 `agent` FK 统一改为 RESTRICT（表重建大迁移），还是维持 NO ACTION 由应用层保证？W7 建议**维持 NO ACTION + 应用层保证**，但**留给 owner 决定**。

---

## 14. D4-4 Analysis（Employee terminated 子语义）—— 不拍板

### 14.1 W6 的批准是有条件的

W6 决策记录：**"DR-D4-4 批准但**未实现**（依赖 DR-D4-1=B，未选）"**。

而 DR-D4-1 最终选了 **(A) 不加 TERMINATED，永久保留**。

### 14.2 因此 D4-4 当前不适用（fact）

| D4-4 子语义 | 是否适用 | 原因 |
| --- | --- | --- |
| terminal 状态语义 | **不适用** | `EmployeeStatus` 只有 `ACTIVE` 单成员（`models.py:1996`），无 terminal 成员 |
| replay adopt 语义 | **不适用** | 无状态迁移 ⇒ 无"重放时如何采纳 terminated"的场景 |
| "terminated 仍可记账" | **不适用** | 无 terminated 实体；且 `cost_evidence` V1 无源事件（§8） |

**结论：W7 不实现 D4-4，也不把它纳入 W7 的任何设计约束。** 在 `EmployeeStatus` 出现第二个成员之前，D4-4 是一个**无对象可描述**的规范。

### 14.3 待决

`[DECISION REQUIRED] DR-W7-4`：D4-4 是 (a) **继续作为递延项保留**（等 `EmployeeStatus` 出现 terminal 成员时自动激活），还是 (b) **正式作废**（若 DR-D4-1=(A) 是永久立场，则 D4-4 永远无对象）？

W7 倾向 **(a) 保留但不激活** —— 因为 DR-D4-1=(A) 本身是一个**可再决策**的决策，不应由 W7 代劳宣布永久。但**不替 owner 拍板**。

---

## 15. Future HTTP/API Boundary（明确区分「当前 repo 事实」与「未来 API contract」）

### 15.1 当前 repo 事实（FACT）

| # | 事实 | 证据 |
| --- | --- | --- |
| A-1 | `_translate` **原样透传** `ServiceError.status_code`：`HTTPException(status_code=error.status_code, detail=error.detail)` | `api/app.py:301` |
| A-2 | **没有**全局 `exception_handler`（`grep exception_handler` 全空） | `api/app.py` |
| A-3 | **没有** `@app.delete` 路由 | `api/app.py` |
| A-4 | **没有** Workforce 路由（`/workforce`、`/jobs`、`/employees`、`/trials` 全空） | `api/app.py` |
| A-5 | `IntegrityError` **没有统一翻译**，各模块自行 `except IntegrityError` | `agent_registry.py:392`、`review.py:307/818/1089/1379`、`content_draft.py` |
| A-6 | 409 **今天是 service 层决定** | `workforce_employee.py:122/170/177/252/354/371/456` 等 |
| A-7 | `_replay` 中"冲突 / 缺失资源"→ 409 | `services.py:78` |

**推论（fact）**：由于没有全局 handler 也没有 IntegrityError 统一翻译，**任何未捕获的 `IntegrityError` 今天会表现为 500，而不是 409**。RESTRICT 的完整性错误**不会**自动变成 409。

### 15.2 未来 API contract（设计建议，W7 不实现）

> ⚠️ 以下为**未来契约**，**不是当前 repo 事实**；W7 **不创建任何 HTTP route**。

1. **409 的产生方式**：由 **service 层**显式 `raise ServiceError(status_code=409, ...)`，经 `_translate`（`api/app.py:301`）透传。**不依赖** `IntegrityError`。
2. **DB 级 RESTRICT 的处理**：必须在 **service 层** `except IntegrityError` 并翻译成 `ServiceError(409)`。**禁止**让 IntegrityError 冒到框架层变 500。
3. **统一翻译器**：建议（**不在 W7 实现**）提供一个 `_translate_integrity_error(exc) -> ServiceError` 单一 helper，替换 `agent_registry.py:392`、`review.py:307/818/1089/1379`、`content_draft.py` 的分散 except。这属于**横切重构**，超出 W7 范围，需单独立项。
4. **Workforce 路由出现时的映射表**（契约，待 DR）：

| 场景 | 状态码 | 产生层 |
| --- | --- | --- |
| 删除 Employee（若未来有） | **409**（永久保留） | service（约定层，非 DB） |
| 删除 Employee 但有 cost_evidence 行 | **409** | DB RESTRICT → service 翻译 |
| 重复晋升同一 Trial | **409** | service（`uq_employee_trial`） |
| Trial 未批准就晋升 | **409/422**（现有 service 已定） | service（`workforce_employee.py`） |
| 写 cost_evidence 指向不存在源事件 | 当前 **201**（P-9 缺口） | 见 §10 |
| 幂等重放同一 source_event | **200 adopt**（不报错） | `workforce_cost_evidence.py:113` |

`[DECISION REQUIRED] DR-W7-6`：未来 Workforce HTTP 路由出现时，是否要求"先落地统一 IntegrityError 翻译器"作为前置条件？

---

## 16. FK / Schema Impact

| 对象 | 当前 | W7 是否改动 | 依据 |
| --- | --- | --- | --- |
| `employee.job_version_id` / `job_id` / `trial_id` / `candidate_id` | RESTRICT | **不改** | §11.1 保护上游，语义正确 |
| `employee.agent_id` | **NO ACTION** | **不改** | §13（11/11 一致 + 无删除写入者 + 表重建代价） |
| `employee.uq_employee_trial` | UNIQUE | **不改** | W4 既定 |
| `cost_evidence.employee_id` | RESTRICT | **不改** | W5 既定，是唯一 Employee 下游保护 |
| `cost_evidence.job_version_id` | RESTRICT | **不改** | W5 既定 |
| `cost_evidence` → Project FK | **不存在** | **不加** | §9.5（Workforce 链零 `project_id`，`:138-139`） |
| `cost_evidence` → 源事件 FK | **不存在** | **不加（W7）** | §10.4（无 producer） |
| `BenchmarkResult` cost/usage 列 | **不存在** | **不加** | §4（无执行后端 ⇒ 无数据可存） |
| `EmployeeStatus` 成员 | 仅 `ACTIVE` | **不加** | DR-D4-1=(A) |
| Agent 相关 11 个 FK | 全 NO ACTION | **不改** | §13.4 |

**W7 schema 影响 = 0。**

---

## 17. Migration Strategy

### 17.1 W7 迁移影响：**零**

- 无 additive 迁移（无新表）；
- 无表重建迁移（无 FK `ondelete` 变更）；
- Alembic head 保持 `20260904_0001_workforce_cost_evidence`；
- `src/` 与 `alembic/` **零改动**。

### 17.2 分类：什么需要迁移 / 什么零迁移 / 什么必须等

| 类别 | 事项 | 迁移成本 |
| --- | --- | --- |
| **零迁移（W7 可做）** | 测试级不变量：无 Employee 删除写入者、`EmployeeStatus` 无 terminal 成员、Workforce 不读写 `Project.budget_*`、Workforce 不调 `check_budget`、`Project.budget_used` 单一写入者、`check_budget` 单一调用者 | **0** |
| **零迁移（但需先决策）** | 应用级恒 409 的 `purge_employee`（F1-ii） | **0**，但需 DR-W7-2 |
| **需 additive 迁移（未来）** | P-9 切 C：receipt 表 + `cost_evidence` 单 FK | 1 次 additive |
| **需表重建迁移（未来，不推荐）** | `employee.agent_id` NO ACTION → RESTRICT；或 11 个 agent FK 统改 | 高（SQLite 表重建） |
| **必须等 producer** | 任何 receipt / 源事件 / cost 归因方案（§10.4） | 阻塞于 DR-W7-5 |
| **绝对不能在 W7 做** | ① 实现 HTTP route；② 加 `TERMINATED`；③ 建 source-event registry；④ 建第二个 budget ledger / gate；⑤ 改任何 FK；⑥ 给 `BenchmarkResult` 加 cost 列（无后端）；⑦ 让 Workforce import delegation；⑧ 任何 push / PR / merge | — |

---

## 18. Failure / Replay / Idempotency

### 18.1 幂等现状（fact）

| 机制 | 位置 | 语义 |
| --- | --- | --- |
| `cost_evidence` 幂等键 `{type}:{id}` | `workforce_cost_evidence.py:113` | UNIQUE + adopt（重放同一源事件→ 同一行，不重复记账） |
| `run_benchmark` 幂等 adopt | `workforce.py:1327` | 同 run_id 复用既有结果 |
| `uq_employee_trial` | `models.py` | 一 Trial 一 Employee，重复晋升冲突 |
| `Event` outbox（`idempotency_key` UNIQUE、`status=PENDING`） | `models.py:532-545`、`services.py:57` | 事件层幂等 |
| `_replay` 冲突/缺失 → 409 | `services.py:78` | 重放冲突语义 |

### 18.2 W7 关注的三个失败语义

1. **P-9 悬空引用的重放**：幂等键 `{type}:{id}` 对**不存在**的事件同样稳定 ⇒ 一旦写入悬空证据，**重放不会自愈**，只会反复 adopt 同一条错误行。这是 P-9 的真实危害（不是"引用不存在"，而是"错误不可通过重放纠正"）。
   → 支持 §10.4 的 C 方案：FK + receipt 让**首次写入就失败**，优于事后对账。

2. **成本重复计账（double charge / double accrual）**：
   - 权威侧：`Project.budget_used` 唯一写入 `delegation.py:491`，`_accrue_budget` 唯一调用 `:336`，`r.cost` 唯一写入 `:443` ⇒ **权威侧不存在重复计账路径**；
   - 证据侧：`cost_evidence` 幂等键保证同源事件只记一次 ⇒ **证据侧不存在重复记账**；
   - **但**：证据侧与权威侧**没有对账关系**。若同一笔成本既进 `budget_used` 又进 `cost_evidence`（未来桥接后），**没有任何机制保证两处一致**。
   → W7 建议列为未来不变量（设计，不实现）：**`cost_evidence` 与 `DelegatedRun.cost` 之间必须可追溯（receipt id），且不得被当作对账真相。**

3. **Workforce 自建 gate / ledger 的失败语义**：一旦 Workforce 有自己的 gate，就会出现"Delegation 放行 / Workforce 拦截"或反之的不一致，且**两者都没有回滚对方的能力**。
   → 不变式 I-B2 / I-B4（§6）必须在任何桥接发生**之前**就位。

### 18.3 事务边界（设计）

- `record_cost_evidence`：证据 + 审计同 `begin_nested()` SAVEPOINT（现有实现）⇒ 原子；
- receipt 方案（未来的 C）：**receipt 必须与执行同事务**；`cost_evidence` 是**后置**引用，允许跨事务（由 FK 保证存在性，不要求同源事务）。

---

## 19. Architecture Boundary Proof

### 19.1 待证命题

> `Workforce → Execution Contract/Adapter → Delegation → Budget Gate → Authoritative Ledger → Cost Evidence` 是否是最合理的边界？

### 19.2 逐段核验

| 段 | 是否存在 | 证据 | 判定 |
| --- | --- | --- | --- |
| Workforce → Execution Contract/Adapter | **部分存在**：Workforce 有自己的 `BenchmarkAdapter`（`workforce.py:1267`），但**无真实实现**（`:1281` `trusted=False`） | §4 | ⚠️ seam 有，实现无 |
| Execution Contract/Adapter → Delegation | **不存在** | Workforce 零 import delegation；`execute_task` 锚定 `Task/Project`（`execution.py:40-41`） | ❌ **无桥** |
| Delegation → Budget Gate | **存在且唯一** | `check_budget` `delegation.py:157`，唯一调用 `:282`，submit 之前 | ✅ |
| Budget Gate → Authoritative Ledger | **存在且唯一** | `delegation.py:491`（唯一写入 `Project.budget_used`） | ✅ |
| Ledger → Cost Evidence | **不存在** | `cost_evidence` 无 Project FK、无 `delegated_run` FK；`project_id=None`（`workforce_cost_evidence.py:138-139`） | ❌ **无桥** |

### 19.3 判定

> **命题的后半段（Delegation → Gate → Ledger）是 repo 事实，且唯一、正确、无需改动。**
> **命题的前后两段桥（Workforce → Execution → Delegation、Ledger → Cost Evidence）今天都不存在。**

因此，把这条链写成"既定架构"是**不准确的**——它是一张**目标图**，其中两座桥尚未建，且**建桥的前提（Workforce 可归因的 producer）不存在**（§4/§8）。

**W7 的修正表述（推荐架构边界）：**

```
【Workforce 域】
  Job / JobVersion / Candidate / Trial / Employee
        │
        ├─ 执行 seam（域内）：BenchmarkAdapter ── V1: _DefaultBenchmarkAdapter (trusted=False, 无后端)
        │
        └─ 记账（域内）：cost_evidence  ◄── evidence / projection，非权威
                              ▲
                              │ 引用（P-9：不校验存在性，OPEN）
                              │
                        【未来的 receipt】（C 方案，未定）
                              ▲
                              │ 由执行层同事务产生
══════════════════════════ 域边界（当前零边）══════════════════════════
【执行 / Delegation 域】
  Task → DelegatedRun → remote worker
        │
        ├─ check_budget（delegation.py:157，唯一调用 :282）── Budget Authority（唯一）
        ├─ DelegatedRun.cost（:443）── Cost 计量（唯一）
        ├─ Project.budget_used（:491）── Authoritative Ledger（唯一）
        └─ Artifact.provenance_json（:554）── Execution Receipt（既有，属 Delegation 域）
```

### 19.4 三权唯一性证明（已证）

| 权威 | 唯一实体 | 唯一性证据 | 结论 |
| --- | --- | --- | --- |
| **Budget Authority** | `check_budget` + `BudgetExceededError` | `delegation.py:157` / `:131`；唯一调用 `:282`；Workforce 零调用 | ✅ 唯一 |
| **Execution Authority** | `execute_task` + `ExecutionAdapter` | `execution.py:187` / `:60`；工厂 `adapters/factory.py` | ✅ 唯一（但**无成本计量**） |
| **Cost Authority** | `DelegatedRun.cost` + `Project.budget_used` | `delegation.py:443` / `:491` | ✅ 唯一 |
| **Cost Evidence** | `cost_evidence` | `models.py:2143`；永不写 budget、永不调 gate | ✅ 是投影，**不是权威** |

**`cost_evidence` 是 evidence / projection / accounting，不是 budget authority** —— 这一点由下列事实证明：
- 它不含任何金额上限/阈值字段（无 limit / threshold 列）；
- 它的 writer 不做任何"是否超预算"判断（`workforce_cost_evidence.py:98-101` 只校验非空）；
- 它不写 `Project.budget_*`（无 import、无字段）；
- 它的 `project_id` 恒为 `None`（`:138-139`）。

`[DECISION REQUIRED] DR-W7-5`：是否允许 Workforce 通过 Delegation 执行（即建第一座桥）？这是**唯一能真正产生 Workforce 可归因成本**的路径，也是 P-9 / C 方案 / cost_evidence 价值的前置。**W7 不拍板。**

---

## 20. Test Matrix（**只设计，不实现**）

> 以下为 W7 实现阶段（若获批）的测试设计草案。本章**不产生任何测试文件**。

### 20.1 Group A — Execution Boundary（6）

| ID | 断言 | 方法 |
| --- | --- | --- |
| A1 | Workforce 模块集合不 import `delegation` / `project` / `task` / `budget` | AST import 扫描 |
| A2 | Workforce 不调用 `check_budget` | AST call 扫描 |
| A3 | Workforce 不读写 `Project.budget_limit` / `budget_used` | AST 属性扫描 |
| A4 | `Project.budget_used` 在 repo 中只有一个写入点 `delegation.py:491` | AST 赋值扫描 |
| A5 | `check_budget` 在 repo 中只有一个调用点 `delegation.py:282` | AST call 扫描 |
| A6 | `BenchmarkResult` 表无 `cost` / `usage` 列（防止有人偷偷加） | `inspect()` 列断言 |

### 20.2 Group B — P-9（4）

| ID | 断言 | 方法 |
| --- | --- | --- |
| B1 | **现状锁定**：`record_cost_evidence` 对不存在的 source event **仍成功**（证明 P-9 是 OPEN 且方案是 A） | 直接调用，断言写入成功 |
| B2 | `idempotency_key == f"{type}:{id}"`，重复写入 adopt 不新增行 | 写两次，断言 count==1 |
| B3 | `cost_evidence` 无跨域 FK（无 `project` / `delegated_run` / `benchmark_result` FK） | `inspect().get_foreign_keys()` |
| B4 | 写入后 `project_id` / `task_id` 恒为 None（审计链不含 Project） | 审计行断言 |

### 20.3 Group C — Employee Lifecycle（5）

| ID | 断言 | 方法 |
| --- | --- | --- |
| C1 | repo 中不存在任何 Employee 删除写入者（无 `session.delete(<Employee>)` / `.delete()` 于 employee 查询集） | AST + grep |
| C2 | `EmployeeStatus` 成员集合 ⊆ {`ACTIVE`}（无 terminal 成员） | 枚举断言 |
| C3 | `employee` 的 `job_version_id` / `job_id` / `trial_id` / `candidate_id` 均为 RESTRICT | `inspect()` FK（注意 `ondelete` 在 `options`） |
| C4 | `employee.agent_id` 为 NO ACTION（锁定 F-2 现状，防止有人悄悄改） | `inspect()` FK |
| C5 | `agent_registry` 无 Agent 删除函数（Agent 生命周期 = 软停用） | AST 函数扫描 |

### 20.4 Group D — Boundary / Integration（5）

| ID | 断言 | 方法 |
| --- | --- | --- |
| D1 | 无 Workforce HTTP 路由注册（`app.routes` 中无 `/workforce` `/jobs` `/employees` `/trials`） | route 扫描 |
| D2 | `_translate`（`api/app.py:301`）原样透传 `ServiceError.status_code`（含 409） | 直接调用 |
| D3 | 无全局 `IntegrityError` → 409 翻译器（锁定"409 是 service 层决定"这一现状） | grep |
| D4 | 指向 `agent` 的 FK **全部** NO ACTION（11/11 一致性） | `inspect()` 全表 FK |
| D5 | Workforce 五个模块的 `append_audit` 调用中 `project_id` / `task_id` 恒为 `None` 字面量 | AST 字面量扫描 |

### 20.5 测试纪律（沿用 W6/W5 教训）

- `select` 必须从 `sqlmodel` 导入（从 `sqlalchemy` 导入对 `table=True` 类返回不可变 Row）；
- SQLite 反射：`get_foreign_keys()["options"]["ondelete"]`；`get_indexes()["unique"]` 是 int 0/1，**不能**用 `is True`；
- pytest 必须 `--basetemp=<仓库内目录>`（`E:\Temp` 有 PermissionError）；
- **绝不并发**跑 pytest（C 盘压力 → sqlite disk I/O error 假失败）；
- schema 段之后、校验段之前必须 `engine.dispose()`（SQLite + QueuePool 陈旧 schema 视图陷阱）。

---

## 21. Proposed Invariants（**设计，不在 W7 实现**）

| ID | 不变量 | 类型 | 迁移 |
| --- | --- | --- | --- |
| **W7-I1** | Workforce 对 Delegation / Project / Task / Budget **零 import** | 结构 | 0 |
| **W7-I2** | Workforce **永不**调 `check_budget` | 结构 | 0 |
| **W7-I3** | Workforce **永不**读写 `Project.budget_limit` / `budget_used` | 结构 | 0 |
| **W7-I4** | `Project.budget_used` 写入者集合 = {`delegation.py:491`} | 结构 | 0 |
| **W7-I5** | `check_budget` 调用者集合 ⊆ {`delegation.py:282`} | 结构 | 0 |
| **W7-I6** | `cost_evidence` 无跨域 FK（无 Project / DelegatedRun / BenchmarkResult） | schema | 0 |
| **W7-I7** | `cost_evidence` 的 `project_id` / `task_id` 恒 `None` | 行为 | 0 |
| **W7-I8** | 不存在 Employee 删除写入者 | 结构 | 0 |
| **W7-I9** | `EmployeeStatus` ⊆ {`ACTIVE`}（无 terminal） | schema | 0 |
| **W7-I10** | 指向 `agent` 的 FK 全部 NO ACTION（一致性锁定） | schema | 0 |

> 全部为 **zero-migration**，与 W6 的 10 条不变量同一手法（tests-only）。

---

## 22. Explicit Non-Goals（W7 明确不做）

1. ❌ 不实现任何 HTTP route（Workforce 或 其他）；
2. ❌ 不实现统一 `IntegrityError` → `ServiceError` 翻译器（横切重构，需单独立项）；
3. ❌ 不加 `EmployeeStatus.TERMINATED` 或任何 terminal 成员；
4. ❌ 不建 source-event registry（DR-D1-4=(b)）；
5. ❌ 不建第二个 budget gate / ledger（DR-D1-2=(a)）；
6. ❌ 不改任何 FK（含 `employee.agent_id`）；
7. ❌ 不给 `BenchmarkResult` 加 cost / usage 列（无执行后端 ⇒ 无数据）；
8. ❌ 不让 Workforce import `delegation`；
9. ❌ 不造 / 不伪造任何 execution producer、receipt、源事件；
10. ❌ 不产生任何 Alembic 迁移；
11. ❌ 不改 `src/` / `tests/` / `alembic/`；
12. ❌ 不 push、不开 PR、不 merge。

---

## 23. `[ASSUMPTION]` 清单

| ID | 假设 | 若不成立的后果 |
| --- | --- | --- |
| **A-W7-1** | "`cost_evidence` V1 预期 0 行"（沿用 W5 结论）在 `d888a93` 上仍成立 | 若已有行，则 F-1 的"实际无保护"结论需重估（但方向不变：仍无删除写入者） |
| **A-W7-2** | `BenchmarkAdapter` 未来会被替换为真实实现，届时它是 Workforce 的**唯一**执行 seam | 若未来走别的 seam（如直接调 `execute_task`），§5 的"两个 seam 并存"结论需重写 |
| **A-W7-3** | `agent_registry` 未来也不会引入 Agent 物理删除（软停用是长期语义） | 若引入，F-2 从"理论不匹配"升级为"活跃风险"，DR-W7-3 需立即决 |
| **A-W7-4** | `Artifact.provenance_json` 中的 `cost` / `usage` 是**只读留痕**，不参与任何对账 | 若未来有人用它对账，则 §18.2 的"无对账关系"结论需改 |
| **A-W7-5** | Workforce 未来若需真实执行，**首选**是经 Delegation（而非自建执行后端） | 若选自建，则 §19 的整张架构图需重画，且 DR-D1-2=(a) 需重新审视 |
| **A-W7-6** | 本设计所引行号基于 `d888a93` 工作副本；任何后续 `src/` 变更会使行号漂移 | 行号漂移不改变结论，仅影响引用精度（结论均由语义支撑） |

---

## 24. `[DECISION REQUIRED]` 清单

| ID | 决策题 | 选项 | W7 倾向（**不代替 owner 拍板**） |
| --- | --- | --- | --- |
| **DR-W7-1** | 是否采纳 **C（execution receipt）** 作为 P-9 目标方案？ | (a) 采纳 C，V1 维持 A；(b) 永久维持 A；(c) 采纳 B/D | **(a)** —— repo 已有 receipt 形态（`delegation.py:554`），C 是唯一"不越权、不跨域、可重放"的方案 |
| **DR-W7-1b** | receipt 表归属哪个域？ | 执行域 / Workforce 域 / 新域 | 阻塞于 DR-W7-5，无法给倾向 |
| **DR-W7-2** | 是否在**尚无 delete writer** 时预先实现恒 409 的 `purge_employee`？ | (a) 否（维持约定 + 测试不变量）；(b) 是（预置守卫） | **(a)** —— 无 writer 时的守卫是死代码，且锁死未来决策空间 |
| **DR-W7-3** | 若未来引入 Agent 物理删除，是否把 11 个 `agent` FK 统一改 RESTRICT？ | (a) 维持 NO ACTION + 应用层保证；(b) 统一改 RESTRICT（表重建大迁移） | **(a)** —— 表重建代价高，且 NO ACTION 是既有 11/11 约定 |
| **DR-W7-4** | D4-4（terminated 子语义）的处理？ | (a) 保留为递延项，待 `EmployeeStatus` 有 terminal 成员时激活；(b) 正式作废 | **(a)** —— DR-D4-1=(A) 本身可再决策，W7 不代劳宣布永久 |
| **DR-W7-5** | 是否允许 Workforce 通过 Delegation 执行（建第一座桥）？ | (a) 允许（复用 Delegation 执行+gate+ledger）；(b) 不允许（Workforce 永不产生可计费执行）；(c) 暂不决 | 无倾向 —— 这是**业务**决策，不是技术决策；W7 只提供成本/边界分析 |
| **DR-W7-6** | 未来 Workforce HTTP 路由出现时，是否要求"统一 IntegrityError 翻译器"作为前置？ | (a) 是；(b) 否（各 service 自行翻译） | **(a)** —— 否则 RESTRICT 会漏成 500（§15.1 A-7） |

---

## 25. Recommended Next Step

**推荐路径（按依赖顺序）：**

1. **先决 DR-W7-5**（Workforce 能否经 Delegation 执行）。它是 DR-W7-1b、C 方案、乃至 `cost_evidence` 是否有存在意义的**总开关**。在它有答案之前，P-9 的任何实现都是在为不存在的事件建容器。
2. **W7 实现（若获批）建议限定为 zero-migration tests-only**，即 §21 的 W7-I1…I10（与 W6 同手法），**不触碰 `src/` 与 `alembic/`**。这能把 W7 的边界结论**钉死在 CI 上**，且不引入任何需要 owner 先决策的 schema 变更。
3. **P-9 / F-1 / F-2 的 schema 动作全部挂起**，等对应 DR 有答案再单独立项（各自都会是独立波次，且 F-2 若选 (b) 会是高风险表重建迁移）。
4. **不要**在 DR-W7-5 有答案之前就实现 receipt 表。

**交付状态：**

> **DESIGN PASS WITH DECISIONS REQUIRED**

理由：Track A（执行边界）与 Track B（Employee 生命周期）的**取证与设计目标已全部完成**，所有结论均有 `file:line` 证据支撑，无 inference 冒充 fact，无提前实现，未伪造任何 producer。但 **6 项决策题（DR-W7-1、DR-W7-1b、DR-W7-2、DR-W7-3、DR-W7-4、DR-W7-5、DR-W7-6）必须 owner 拍板**，其中 **DR-W7-5 是总开关**，阻塞其余多项。

---

### 附录：本设计未修改的文件（承诺核对）

| 目录/文件 | 状态 |
| --- | --- |
| `src/aios/**` | **未修改**（只读取证） |
| `tests/**` | **未修改** |
| `src/alembic/**`（含 `alembic/`） | **未修改** |
| `docs/workforce/Workforce_W7_Design_V1.md` | **新增**（本文件，唯一产物） |
| Alembic head | `20260904_0001_workforce_cost_evidence`（未变） |
| git | 分支 `docs/w7-design` @ base `d888a93`；本文件提交前 worktree 仅含未跟踪的 `.pytest_run/` |
| push / PR / merge | **无** |
