# W7 / DR-W7-5 Analysis — May Workforce Execute via Delegation?

> **本轮是决策分析，不是 implementation。**
> 未修改任何代码；未修改 `Workforce_W7_Design_V1.md`；未创建迁移；未 push / 未开 PR / 未 merge。
> 唯一产物：本文件 `docs/workforce/Workforce_W7_DR-W7-5_Analysis.md`。

| 项 | 值 |
| --- | --- |
| Branch | `docs/w7-design` |
| 上一 exact-head（本分析起点） | `20fb63b0dddd297b71d34f9f5e2cd3475df9b74b` |
| Base（未变） | `d888a930ccee1294f8bff017aca607cbb05141fa` |
| Alembic head | `20260904_0001_workforce_cost_evidence`（未变） |
| 关联文档 | `docs/workforce/Workforce_W7_Design_V1.md`（只读引用，未修改） |

> ## ⚑ 最终状态：`DR-W7-5 ANALYSIS PASS — BUSINESS DECISION REQUIRED`
>
> 技术层面证据充分、结论明确（推荐 Option B）；但 DR-W7-5 主决策本身是**业务归属问题**，repo evidence 无法代为拍板，必须 owner 显式决策。详见 §14 / §15。

**取证声明**：本章所有事实均为**本轮重新执行** `grep` / `sed` / 源码阅读所得，行号基于 `20fb63b`。凡引用 W7 Design V1 的结论，均已回到代码复核；两处与 W7 文档不同的地方在 §2.9 明确标注为**修正**。

---

## 1. Question

> **DR-W7-5：Workforce 是否允许经 Delegation 执行？**

分解为三个可判定子问题：

- **Q1（结构）**：repo 当前是否存在一条**可执行**的 `Workforce → Delegation` 路径？若不存在，缺的是什么？
- **Q2（安全）**：假设要打开这条路径，现有的 budget gate / ledger / receipt / idempotency 机制能否**原样承接**，还是会出现绕过或双重记账？
- **Q3（归属）**：打开这条路径所需的**缺失环节**，是技术缺口（可被工程补齐）还是业务决策（必须由 owner 拍板）？

本文的核心立场（先行声明，避免读者误读）：

> **不因 Option A 看起来更"完整"而默认选择 A。**
> 本文把"技术可行性"与"业务授权"严格分离；凡 repo evidence 只能证明"能做"而不能证明"该做"的，一律标记为 `[DECISION REQUIRED — BUSINESS]`。

---

## 2. Current Repo Evidence（本轮重新核查）

### 2.1 执行入口：`execute_task` 的硬前置条件

| # | 事实 | 位置 |
| --- | --- | --- |
| E-1 | `execute_task(session, task_id, idempotency_key, *, adapter, actor="agent")` | `execution.py:187-194` |
| E-2 | 幂等守卫：`Artifact.external_result_id == f"exec:{idempotency_key}"` **且** `Artifact.task_id == task_id` | `execution.py:202-213` |
| E-3 | **Task 必须存在**，否则 404 | `execution.py:215-217` |
| E-4 | Task 必须 `READY`（`FAILED` 可恢复为 `READY` 重试），否则 **409** | `execution.py:219-256` |
| E-5 | **认领 agent**：`route_task(session, task_id, f"exec:{idempotency_key}:route", commit=False)`；返回 None → 409 | `execution.py:258-261` |
| E-6 | 构建不可变 `TaskContext`：`ContextService(session).build_context(task_id, assignment.id)` | `execution.py:293` |
| E-7 | 适配器调用：`adapter.run(task_id=..., task_context=context, output_schema=task.output_schema, idempotency_key=...)` | `execution.py:297-302` |
| E-8 | provenance 落盘：`Artifact.provenance_json = build_delegated_provenance(...)` | `execution.py:362-364` |

**E-5 的关键含义（本轮重点复核）**：执行 agent 由 **`route_task`** 决定，不由调用方决定。

- `RoutingMode.MANUAL` → 直接返回 None（不可执行）：`scheduler.py:146-150`
- `RoutingMode.FIXED` → 取 `task.assigned_agent_id`：`scheduler.py:152-171`
- `BEST_AVAILABLE` / `PREFERRED_WITH_FALLBACK` → 按 `required_capabilities` 对**全部 Agent** 排序选取：`scheduler.py:172-189`
- `Task.routing_mode` 默认 `FIXED`：`models.py:372`

⇒ **调用方（`execute_task`）无法指定"由哪个 agent 执行"**。唯一间接手段是预先把 `task.assigned_agent_id` 设为目标 agent 并把 `routing_mode` 设为 `FIXED`。

### 2.2 适配器选择：delegation 是 4 重 opt-in

`build_execution_adapter(session, task_id)`（`adapters/factory.py:22-46`）：

| 门槛 | 条件 | 位置 |
| --- | --- | --- |
| 1 | `AIOS_DEEPSEEK_HARNESS_ENABLED == "true"`，否则直接返回 `LLMExecutionAdapter()` | `factory.py:24-25` |
| 2 | `task.assigned_agent_id` 必须存在且能取到 Agent | `factory.py:26-27` |
| 3 | `agent.config_ref` 必须以 `deepseek-harness+file://` 开头 | `factory.py:19, 28` |
| 4 | 本地配置文件存在且可解析（command / cwd / manifest / manifest_sha256 / active_plugins） | `factory.py:30-39` |
| 5 | `agent.secret_ref` 必须是 `env://…` 且环境变量存在 | `factory.py:44, 49-56` |

只有全部满足才返回 `WorkerDelegatedAdapter(...).as_execution_adapter()`，即 `_WorkerLifecycleAdapter(DelegatedExecutionAdapter)`（`worker_delegated.py:29-31, 154`）。

⇒ **Delegation 不是默认执行路径，而是逐 agent、逐部署显式开启的。**

### 2.3 Delegation 内部：预算 gate 是**条件触发**

`DelegatedExecutionAdapter.run`（`delegation.py:243`）：

| # | 事实 | 位置 |
| --- | --- | --- |
| E-9 | 最小权限投影后再外发：`projected = self._project_context(task_context)` | `delegation.py:258, 352-373` |
| E-10 | 信任 gate：`assert_trust_delegable(self.agent)` | `delegation.py:262` |
| E-11 | **预算 gate 是条件的**：`if ctx_project_id is not None:` 才调 `check_budget` | `delegation.py:275-282` |
| E-12 | 重试循环：`attempt` 递增，每轮 `self._create_run(task_id, idempotency_key, attempt)` | `delegation.py:286-287` |
| E-13 | 成本落库：`r.cost = float(info["cost"])` / `r.usage = info["usage"]` | `delegation.py:442-445` |
| E-14 | **仅在成功路径**调用 `self._accrue_budget(run)` | `delegation.py:336` |
| E-15 | 失败/超时 → `_record_failed`（`:495`）或 `EXPIRED`（`:467-474`），**不 accrue** | `delegation.py:338-340, 466-474` |

**E-11 是本轮最重要的发现之一**：

```python
ctx_project_id = getattr(task_context, "project_id", None)   # :275
...
if ctx_project_id is not None:                                # :277
    ...
    check_budget(s, project, est)                             # :282
```

`check_budget` 依然是**唯一**预算 gate（`delegation.py:157`，唯一调用点 `:282`），但**当上下文没有 project_id 时，整个 gate 被静默跳过**。

### 2.4 `DelegatedRun` 与 `Artifact` 的 project 约束

| # | 事实 | 位置 |
| --- | --- | --- |
| E-16 | `DelegatedRun.project_id: str`（**非空**）FK → `project.id` | `models.py:469` |
| E-17 | `DelegatedRun.task_id: str`（非空）FK → `task.id` | `models.py:470` |
| E-18 | `DelegatedRun.idempotency_key` **UNIQUE** = `H(task_id, agent_id, attempt)` | `models.py:476-478`, `delegation.py:397` |
| E-19 | `DelegatedRun.cost: float = 0.0`；`usage: dict` | `models.py:487-488` |
| E-20 | `_create_run` 取 `project_id=task.project_id` | `delegation.py:391-401` |
| E-21 | `Artifact.project_id: str`（**非空**）FK → `project.id` | `models.py:395` |
| E-22 | `Task.project_id: str | None`（**可空**） | `models.py:365` |

⇒ **E-16 + E-22 的组合后果**：若一个 `project_id=None` 的 Task 进入 delegation，`_create_run` 会写入 `project_id=None` → **非空约束冲突**；若它有 project，则 E-11 的 gate 生效。二者都不会"静默无预算地跑通"，但错误信息会是一个 DB 完整性错误（500 级），而非业务性的 409。

### 2.5 成本记账与 provenance

| # | 事实 | 位置 |
| --- | --- | --- |
| E-23 | `_accrue_budget`：`cost <= 0` 直接 return；`project = s.get(Project, persisted.project_id)`；**project 为 None 则静默不记账** | `delegation.py:484-493` |
| E-24 | `Project.budget_used` 唯一写入点 = `delegation.py:491` | `delegation.py:491` |
| E-25 | `build_delegated_provenance` 产出：`agent_id / agent_name / mode / delegated_run_id / remote_run_id / remote_status / attempt / cost / usage / secret_ref / submitted_at / finished_at` | `delegation.py:554-579` |
| E-26 | provenance 写入 `Artifact.provenance_json`，**仅当** `isinstance(adapter, DelegatedExecutionAdapter)` | `execution.py:351-364` |
| E-27 | `Artifact` 落库时 `project_id=task.project_id` | `execution.py:384` |

### 2.6 Workforce 侧：零可执行引用（复核 W7 结论，成立）

5 个 Workforce 模块：`workforce.py`、`workforce_trial.py`、`workforce_employee.py`、`workforce_recommendation.py`、`workforce_cost_evidence.py`。

| # | 事实 | 位置 |
| --- | --- | --- |
| E-28 | import 仅限：`aios.agent_registry`（`get_agent`/`list_agents`）、`aios.audit`、`aios.models`、`aios.services`、`aios.actor`、workforce 内部模块 | `workforce.py:76-99` 等 |
| E-29 | `Project` / `Task` / `delegation` / `budget` / `DelegatedRun` 在 Workforce 模块中**仅出现在 docstring / 注释**，无一处可执行代码引用 | 全量 grep 复核 |
| E-30 | `workforce_cost_evidence.py:8-14` 明写："never call `delegation.check_budget`… the only realized cost, `DelegatedRun.cost`, belongs to the delegation domain, bound `Task -> Project`… `delegated_run.id` must never be reused as if it were Workforce-attributable" | `workforce_cost_evidence.py:8-14` |
| E-31 | `workforce_employee.py:29`："no cost gate — the Workforce chain has no Project to bind one to" | `workforce_employee.py:29` |
| E-32 | `CostEvidence` docstring 同样明令禁止复用 `delegated_run.id` | `models.py:2143+` |

### 2.7 两条链**结构上是互不相交的两棵树**（本轮最关键的结构发现）

| 链 | 根 | 锚定 | 证据 |
| --- | --- | --- | --- |
| **Workforce** | `BusinessGoal(owner="human_ceo")` | **owner** | `models.py:1396-1410` |
| **Execution / Delegation** | `Project(budget_limit, budget_used)` | **budget** | `models.py:244-258` |

- `BusinessGoal` → `RequiredWork` → `Job` → `JobVersion` → `Candidate` → `Trial` → `Employee`
- `Project` → `Task` → `DelegatedRun` / `Artifact`

| # | 事实 | 位置 |
| --- | --- | --- |
| E-33 | `BusinessGoal` **无 `project_id` 字段**（grep 计数 = 0） | `models.py:1396-1410` |
| E-34 | `Project` **无 `business_goal_id` 字段**（grep 计数 = 0） | `models.py:244-258` |
| E-35 | `BusinessGoal` 在 `src/` 中**只被** `workforce.py` 与 `models.py` 引用 | 全量 grep |

⇒ **两棵树之间不存在 join key、不存在桥接表、不存在共同祖先。**

### 2.8 归属 / 幂等 / 事务机制

| # | 事实 | 位置 |
| --- | --- | --- |
| E-36 | `append_event(session, *, project_id: str, ...)` —— **outbox 事件强制要求非空 project_id** | `services.py:57-59` |
| E-37 | `_replay`：key 冲突 → 409；引用资源缺失 → 409 | `services.py:78-93` |
| E-38 | `record_cost_evidence`：owner-only（`_assert_owner_actor`）、要求 `job_version_id`（**非空**）、`amount` 非空、`source_event_type/id` 非空 | `workforce_cost_evidence.py:94-111` |
| E-39 | 幂等键 `f"{source_event_type}:{source_event_id}"` UNIQUE；证据 + 审计**同一 SAVEPOINT** | `workforce_cost_evidence.py:113, 128-145` |
| E-40 | 审计恒 `project_id=None, task_id=None` | `workforce_cost_evidence.py:137-139` |
| E-41 | `run_benchmark` 幂等 = `(candidate_id, benchmark_version_id, run_id)` UNIQUE + SAVEPOINT 吸收并发；fail-closed 写 `status="unknown"` | `workforce.py:1327-1362`, `uq_benchmark_result_run` |
| E-42 | `BenchmarkAdapter.run(candidate, benchmark_version) -> BenchmarkOutcome` —— **签名中无 task / project** | `workforce.py:1267-1277` |
| E-43 | `run_benchmark` **无 actor 参数**（不owner-gated） | `workforce.py:1327-1337` |
| E-44 | 默认适配器 `_DefaultBenchmarkAdapter` 返回 `trusted=False`，**无执行后端** | `workforce.py:1281-1296` |
| E-45 | `BenchmarkResult` 无 `cost` / `usage` 列 | `models.py:1715-1750` |

### 2.9 本轮两处**修正**（与 W7 Design V1 不同，必须记录）

#### 修正 ①：`Task.actual_cost` 是死列，且存在第二个（非持久化）预算机制

- `Task.actual_cost: float = 0.0`（`models.py:379`）—— **在 `src/` 中没有任何写入者**；全仓唯一同名出现是 `judging/llm.py:336-342` 的**局部变量**。
- `JudgeBudget`（`judging/verdict.py:95-130`）是 `@dataclass`（**非 `table=True`**，judging 模块无 `table=True`、不引用 `Project` / `budget_used`），是**运行级、进程内**预算：`max_usd` / `spent_usd` / `can_afford` / `reserve` / `reconcile`。

**对结论的影响**：不推翻"`Project.budget_used` 是唯一**持久化**权威账本"，但修正了"repo 只有一个预算机制"的隐含印象 —— repo 的既有**先例**是：

> 权威持久化账本（`Project.budget_used`，Delegation 独占写入）+ 域内**运行级、非持久化**预算（Judging）。

这个先例对后续讨论有价值，但本文**不据此设计任何 Workforce 预算**（明确禁止项，见 §4.5）。

#### 修正 ②：`check_budget` 并非无条件触发

W7 Design V1 §6 表述为"`check_budget` 唯一调用点 `:282`，位于远程提交之前" —— 正确，但**不完整**。本轮补上：该调用被 `if ctx_project_id is not None`（`delegation.py:277`）包裹 ⇒ **project-less 上下文会整体跳过预算 gate**。

这一条直接改变 Option A 的风险画像（见 §3.6、§7.3）。

---

## 3. Option A — Allow Workforce → Delegation

### 3.1 目标架构逐段核验

按题面给出的链路逐段验证（"✅ 已存在 / ⚠️ 部分存在 / ❌ 不存在"）：

```
Workforce Job / Trial            ❌ 无 project、无 task、无执行触发点（§2.7, E-33/E-34）
        ↓
Execution Boundary               ⚠️ 有 seam（BenchmarkAdapter）但签名无 task/project（E-42）
        ↓
Delegation                       ✅ 存在，但 4 重 opt-in 且要求 Task+Project（§2.2, E-16）
        ↓
Budget Gate                      ✅ check_budget 唯一，但**条件触发**（E-11）
        ↓
DelegatedRun                     ✅ 存在，project_id 非空（E-16）
        ↓
Execution Receipt                ✅ 已存在 = Artifact.provenance_json（E-25/E-26）
        ↓
CostEvidence                     ❌ 与 DelegatedRun 之间**无任何列可关联**（§3.5）
```

**结论：链路的 7 段中，2 段完全不存在（首、尾），1 段部分存在。**

### 3.2 Workforce 如何发起 execution？（Q：机制是否存在）

**现状：不存在任何机制。**

要发起一次 delegation，按 §2.1 必须同时具备：

1. 一个 `Project`（`create_project` 要求存在；`services.py:165-166` 校验）
2. 一个 `Task`（`services.py:180`；且 `Task.project_id` 指向该 Project）
3. `task.status == READY`（E-4）
4. `task.routing_mode == FIXED` 且 `task.assigned_agent_id` 指向目标 agent（否则 `route_task` 自行选 agent，E-5 / §2.1）
5. 该 agent 满足 §2.2 的 4 重 opt-in
6. 该 agent `trust_level ∈ {INTERNAL, VERIFIED_EXTERNAL}`（`delegation.py:141, 150`）
7. 一个 `Idempotency-Key`（API 层 Header；`api/app.py:692`）

Workforce 侧**一个都不具备**。

### 3.3 Delegation 是否继续拥有唯一 budget authority？

**是 —— 前提是 Task 带 project_id。**

- `check_budget` 仍是唯一 gate（`delegation.py:157`，唯一调用 `:282`）；
- `Project.budget_used` 仍是唯一写入点（`:491`）；
- Option A 本身**不要求**新增第二个 gate 或账本。

但见 §3.6：**"前提是 Task 带 project_id"** 这一条不是自动成立的。

### 3.4 `check_budget` 是否仍是唯一预算 gate？

**是**（`delegation.py:282` 是唯一调用点）。已由合并的 W6 测试钉死：

- `test_budget_used_has_exactly_one_writer`（`tests/test_workforce_w6_invariants.py:191`）

### 3.5 `DelegatedRun.cost` 如何成为真实 cost source？—— **结构性缺口（P0）**

`DelegatedRun.cost` 确实是**唯一真实的、已实现的**成本源（`delegation.py:443`，来自 `WorkerResult.usage["cost"]`）。但它与 `cost_evidence` 之间**没有任何列可建立归属关系**：

| 一侧 | 归属锚 | 另一侧 | 归属锚 | 是否可关联 |
| --- | --- | --- | --- | --- |
| `DelegatedRun` | `project_id` + `task_id` | `CostEvidence` | **`job_version_id`（非空）** + `employee_id?` | ❌ 无公共列 |

`CostEvidence.job_version_id` **非空**（`models.py:2192-2194`），而 `DelegatedRun` 没有 `job_version_id`，`JobVersion` 没有 `project_id` / `task_id`。

⇒ **"这次 delegation 的 cost 应该记到哪个 JobVersion 名下"这个问题，在当前 schema 下没有答案。** 唯一的三种填补方式：

| 方式 | 是否被禁止 |
| --- | --- |
| (a) 调用方以参数传入 `job_version_id` | 技术上可行（`record_cost_evidence` 本来就收参数），但归属由调用方**断言**而非由数据**证明** ⇒ 与 W5 的"诚实约束（I4）"冲突 |
| (b) 新增跨域 FK（`delegated_run → job_version` 或 `cost_evidence → delegated_run`） | **本轮明确禁止**；且 `CostEvidence` docstring 已明令禁止复用 `delegated_run.id`（E-32） |
| (c) 新增映射表 | 属新建设计，**本轮不做** |

**这是 Option A 的 P0 阻塞点。**

### 3.6 如何避免绕过预算 gate？（本轮新发现的风险）

`check_budget` 被 `if ctx_project_id is not None` 包裹（E-11）。同时 `Task.project_id` 是**可空**的（E-22），而 `DelegatedRun.project_id` **非空**（E-16）。

⇒ 若 Option A 以"Workforce 创建一个 project-less Task"的方式实现，会出现：

| 情形 | 结果 |
| --- | --- |
| Task 无 project | `check_budget` **被跳过**；随后 `_create_run` 写 `project_id=None` → 非空约束冲突 → **未翻译的完整性错误**（无全局 `IntegrityError` 处理器，见 W7 §15.1） |
| Task 有 project | gate 正常生效；`_accrue_budget` 正常记账 |

⇒ **不是"可以绕过"，而是"会以 500 级错误崩溃"**。但这是**实现细节的偶然防护**，不是设计意图；任何让 `DelegatedRun.project_id` 变可空或引入默认 Project 的改动，都会立刻把它变成**真正的静默绕过**。

### 3.7 Execution Receipt 应由哪个 domain 产生？

**由 execution / delegation 域产生 —— 而且它已经存在。**

`Artifact.provenance_json`（E-25/E-26）已包含 `delegated_run_id` / `cost` / `usage` / `attempt` / `agent_id` / `mode` / `secret_ref`（仅句柄）/ 时间戳，由 `execution.py:362` 在**执行域内**写入。

- **产生者**：execution 域（`execute_task`），非 Workforce，非 Delegation 业务代码单独写。
- **归属**：附属于 `Artifact`，而 `Artifact.project_id` 非空（E-21）⇒ receipt 天然是 **Project 域对象**。
- **本文不新建 receipt 表**（明确禁止项）。

### 3.8 `CostEvidence` 如何消费 receipt？

**当前不能合法消费。** 三种可能形态与各自障碍：

| 形态 | `source_event_type` / `source_event_id` | 障碍 |
| --- | --- | --- |
| 引用 `delegated_run` | `"delegated_run"` / `<run.id>` | **被 W5 合并契约明令禁止**（E-30 / E-32） |
| 引用 `artifact` | `"artifact"` / `<artifact.id>` | 未被明文禁止；但 `Artifact` 属 Project 域（E-21），Workforce 引用它即形成跨域引用（虽无 FK）；且 receipt 内仍无 `job_version_id` ⇒ §3.5 的归属缺口依然存在 |
| 引用未来 Workforce 原生事件 | — | **不存在**（E-44/E-45：无执行后端、无 cost 列） |

⇒ 即便选"引用 artifact"，也只解决"证明成本发生"，**不解决"这笔成本属于哪个 JobVersion"**。

### 3.9 如何保证 replay / idempotency / at-most-once？

| 层 | 现有机制 | 在 Option A 下是否够用 |
| --- | --- | --- |
| `execute_task` | `exec:{idempotency_key}` + task_id 双条件命中即返回既有 Artifact（E-2） | ✅ 够用 |
| `DelegatedRun` | `H(task_id, agent_id, attempt)` UNIQUE（E-18） | ⚠️ **重试产生新 run**，`attempt+1` ⇒ 同一逻辑执行可有多条 run（E-12） |
| `Project.budget_used` | 仅成功路径 accrue（E-14） | ❌ **失败/超时的 run 若远端已计费，永不计入账本**（E-15） |
| `cost_evidence` | `{type}:{id}` UNIQUE + 同 SAVEPOINT（E-39） | ✅ 幂等本身可靠，但依赖 `source_event_id` 真实 |
| Workforce 侧 | `run_benchmark` 三元组 UNIQUE（E-41） | ✅ 但仅限 benchmark，与 delegation 无关 |

⇒ **at-most-once 在"记账"层面已有一个既有漏洞（失败 run 的成本漏记）**；在"归属"层面则完全无机制（§3.5）。

### 3.10 如何避免 Workforce 与 Delegation 双重记账？

必须区分两种"双重"：

| 类型 | 是否会发生 | 说明 |
| --- | --- | --- |
| **双重扣费（double charge）** | ❌ 不会 | 只有 `Project.budget_used` 一个账本，唯一写入点（E-24） |
| **双重表述（double representation）** | ✅ **会** | 同一笔 `DelegatedRun.cost` 既进 `Project.budget_used`，又进 `cost_evidence`，**且两者之间没有对账键**（§3.5） |

"双重表述"是否算问题，取决于一个**尚未定义的语义**：

> `cost_evidence` 记录的是 (a) `Project.budget_used` 中某一部分的**投影/子集**，还是 (b) 与 Project 预算**并行**的另一笔花费？

- 若是 (a)：需要显式对账键（receipt id），且必须能证明 `sum(cost_evidence) ⊆ budget_used`；
- 若是 (b)：等于承认第二套账 —— **与 DR-D1-2=(a) 直接冲突**。

**这是一个业务语义问题，不是技术问题。** ⇒ `[DECISION REQUIRED — BUSINESS]`

### 3.11 如何避免 Workforce ↔ Delegation 循环依赖？

**当前依赖方向**：

- `workforce.py` → `aios.agent_registry`、`aios.audit`、`aios.models`、`aios.services`（E-28）
- `execution.py` → `aios.orchestrator`（`:40`）、`aios.scheduler`（`:41`）、`aios.services`（`:42`）、`aios.delegation`（`:340`，函数内 import）
- `delegation.py` → `aios.models`、`aios.audit`、`aios.worker_contract` 等
- **没有任何 execution / delegation 模块 import workforce**

⇒ 若 Option A 让 workforce import delegation/execution，**新增的是单向边，当前不成环**。

但注意：一旦 delegation 侧未来需要"回写 Workforce 归属"（例如把 run 归因到 job_version），**反向边立刻成环**。⇒ 必须在打开 Option A 的同时把"单向性"钉死（见 §11）。

### 3.12 是否需要 migration？

**取决于采用哪种桥接方式**（见 §12）：

| 桥接方式 | 迁移 | 是否触碰已合并的 W6 不变量 |
| --- | --- | --- |
| A-1：给 Workforce 表加 `project_id` 列 / FK | additive 迁移 | ❌ **直接违反** `test_workforce_chain_has_no_project_or_delegation_reference` |
| A-2：新建 Workforce↔Project 映射表 | additive 迁移 | ⚠️ 表级检查只查 Workforce 14 表；映射表若命名为 workforce 表则违反 |
| A-3：不改 schema，由调用方以**参数**传入 project/task/job_version | **零迁移** | ⚠️ 不违反现有 AST/表级测试，但**违反已合并的书面契约**（E-30/E-31/E-32） |

⇒ **"零迁移"是可能的，但那恰恰是最危险的路径**：它绕过所有机器可检查的不变量，把禁止事项降级为"靠人记住"。

### 3.13 最小 execution contract 应包含哪些字段？

**答案：最小契约已经存在**，即 `ExecutionAdapter.run`（`execution.py:60-61`）签名 + `Task` 的必填项。本文**不新增契约**，只列出**当前已经必须提供**的内容：

| 类别 | 字段 | 来源 |
| --- | --- | --- |
| 适配器入参 | `task_id` | `execution.py:61` |
| 适配器入参 | `task_context`（不可变 `TaskContext`） | `execution.py:293` |
| 适配器入参 | `output_schema`（来自 `task.output_schema`） | `execution.py:300`, `models.py:376` |
| 适配器入参 | `idempotency_key` | `execution.py:61` |
| Task 前置 | `project_id`（**非空才触发预算 gate**） | E-11 / E-22 |
| Task 前置 | `status == READY` | E-4 |
| Task 前置 | `routing_mode` + `assigned_agent_id`（决定执行 agent） | E-5 / §2.1 |
| Task 前置 | `estimated_cost`（gate 的输入） | `delegation.py:281`, `models.py:378` |
| Agent 前置 | `trust_level ∈ {INTERNAL, VERIFIED_EXTERNAL}` | `delegation.py:141, 150` |
| Agent 前置 | `config_ref` / `secret_ref`（delegation opt-in） | `factory.py:28, 44` |

**缺失项（本文不设计）**：一个能表达"这次执行归属某个 `job_version`"的载体。它在当前 schema 中不存在（§3.5）。

---

## 4. Option B — Do Not Allow Yet

### 4.1 当前 Workforce 为什么不能安全进入 execution

| # | 阻塞 | 证据 |
| --- | --- | --- |
| B-1 | **无 producer**：`_DefaultBenchmarkAdapter` 无执行后端，返回 `trusted=False` | `workforce.py:1281-1296` |
| B-2 | **无成本列**：`BenchmarkResult` 无 `cost` / `usage` | `models.py:1715-1750` |
| B-3 | **无 join key**：Workforce 树（BusinessGoal 根）与 Project 树互不相交 | E-33 / E-34 / E-35 |
| B-4 | **归属无解**：`cost_evidence.job_version_id` 非空，而 `DelegatedRun` 与之无公共列 | §3.5 |
| B-5 | **契约禁止**：W5 明令 `delegated_run.id` 不得复用为 Workforce 可归因 | E-30 / E-32 |
| B-6 | **CI 已钉死禁止**：Workforce 14 表不得有 `project_id`、不得 FK 到 project/task/delegated_run | `tests/test_workforce_w6_invariants.py:128-150` |
| B-7 | **agent 语义不匹配**：候选池按"enabled + capabilities"筛选，**不过滤 trust_level**；而 delegation 要求 trust ∈ {INTERNAL, VERIFIED_EXTERNAL} | `workforce.py:622-629` vs `delegation.py:141, 150` |
| B-8 | **agent 选择权不在 Workforce**：`route_task` 决定执行 agent，Workforce 的 Candidate→Agent 绑定不是其输入 | E-5 / §2.1 |
| B-9 | **actor 模型不匹配**：Workforce = owner-only（`_assert_owner_actor`）；执行 API = `actor="agent"`；`resolve_agent_actor` 仅限网关 | `actor.py:53-76`, `api/app.py:708` |
| B-10 | **预算 gate 条件触发**：project-less 上下文整体跳过 `check_budget` | E-11 |
| B-11 | **账本既有漏洞**：失败/超时 run 的远端成本永不计入 `budget_used` | E-14 / E-15 |
| B-12 | **outbox 不可用**：`append_event` 强制非空 `project_id`，Workforce 无法进入 Event/replay 体系 | E-36 |

### 4.2 哪些 producer / contract / ownership 缺失

| 缺失 | 类别 | 说明 |
| --- | --- | --- |
| Workforce 可归因的 **cost producer** | producer | 不存在（B-1/B-2）；**且 W6 已冻结 DR-D1-1=(d)：不得伪造** |
| Workforce ↔ Project 的 **归属契约** | contract | 不存在（B-3/B-4） |
| "谁为一次 Workforce 执行付费"的 **owner** | ownership | 不存在；`Project.budget_*` 的 owner 是项目，不是 Job/BusinessGoal |
| Workforce 侧的 **执行 actor** | governance | 不存在（B-9） |
| Workforce agent **信任准入**语义 | contract | 不存在（B-7） |

### 4.3 `cost_evidence` 在这种状态下应保持什么定位

> **`cost_evidence` 应继续保持"零行的、schema-only 的记账契约"，不承接任何 Delegation 成本。**

具体边界（沿用 W5/W6 已冻结条款）：

- 是 **evidence / projection**，不是 ledger、不是 gate、不是权威（W7 §9）；
- 只记录**已发生且可归属到 JobVersion** 的真实成本；
- **不**复用 `delegated_run.id`（E-30/E-32）；
- **不**读写 `Project.budget_*`、**不**调 `check_budget`（由 `test_workforce_modules_never_reference_delegation_domain` 钉死）；
- V1 预期 0 行 —— **这一状态不是缺陷，而是"没有可诚实记录的事实"的正确表达**。

### 4.4 什么条件满足后才能重新打开 execution decision

建议的**重开闸门**（全部满足才重新评估）：

| # | 闸门 | 判定方式 |
| --- | --- | --- |
| G-1 | 存在 Workforce 可归因的 cost producer（真实执行后端，非占位） | repo 中可指出 producer 的 `file:line` |
| G-2 | 归属语义已由 owner 定义（见 §15 的 DR-W7-5a） | owner 书面决策 |
| G-3 | 存在合法归属载体（不靠调用方断言、不靠跨域 FK） | 设计 + 通过 W6 既有不变量测试 |
| G-4 | `DelegatedRun` 与 `cost_evidence` 的对账关系已定义 | 设计文档 + 决策 |
| G-5 | 失败/超时 run 的成本记账语义已修复或明确接受（B-11） | 决策或修复 |

### 4.5 本轮明确**不设计**的内容（合规声明）

按题面禁令，本文**没有**设计：

- ❌ Workforce 自己的 budget gate
- ❌ Workforce 自己的 cost ledger
- ❌ Workforce 自己的真实 cost producer
- ❌ 跨域 FK（用于强行闭环）
- ❌ 当前不存在的 execution receipt 表

§3.7 指出的 `Artifact.provenance_json` 是**已存在的** receipt，不是新设计。§2.9 提到的 `JudgeBudget` 是**已存在的**他域先例，仅作参照，未据此为 Workforce 设计任何预算。

---

## 5. Ownership Analysis

| 对象 | Owner（域） | 证据 |
| --- | --- | --- |
| `Project` / `budget_limit` / `budget_used` | **Delegation / Project 域** | `delegation.py:491` 唯一写入；`models.py:244-258` |
| `Task` | 服务层（`services.py:180`、`review.py:1231/1728`、`customer_service.py:989`） | 创建点全在 Workforce 之外 |
| 执行 agent 选择 | **Scheduler**（`route_task`） | E-5 / `scheduler.py:114-189` |
| `DelegatedRun` / 成本计量 | **Delegation** | `delegation.py:388-493` |
| Execution Receipt | **Execution**（`execute_task` 写 `Artifact.provenance_json`） | E-26 |
| `Job` / `JobVersion` / `Candidate` / `Trial` / `Employee` | **Workforce**（owner-actor） | `workforce*.py`；`_assert_owner_actor` |
| `BusinessGoal` | **Workforce**（且只被 Workforce 引用） | E-35 |
| `Agent` 生命周期 | **Agent Registry**（软停用，无物理删除） | W7 §13 |
| `cost_evidence` | **Workforce**（记账，非权威） | `workforce_cost_evidence.py` |

**关键观察**：`Project` 的 owner 与 `Job` 的 owner 在代码层面是**两个不同的世界** —— 前者按预算锚定，后者按 owner（`human_ceo`）锚定，且**没有把二者绑起来的任何字段**。

⇒ "一次 Workforce 执行该由谁的项目预算承担"这个问题，**在 repo 中没有可回答的数据结构**。这是 ownership 缺口，不是实现缺口。

---

## 6. Execution Boundary Analysis

| 边界 | 现状 | 评价 |
| --- | --- | --- |
| Workforce 的执行 seam | `BenchmarkAdapter`（`workforce.py:1267`） | ✅ 存在，但签名无 task/project（E-42），V1 无后端（E-44） |
| 核心执行 seam | `ExecutionAdapter` + `execute_task`（`execution.py:60/187`） | ✅ 存在，但**锚定 Task→Project**（E-3/E-4/E-5） |
| 两个 seam 之间 | **无连接** | 无 import、无适配、无共享类型 |
| 跨域调用 | Workforce → `agent_registry`（只读） | ✅ 唯一既有跨域边，指向**共享 SSoT**，与 W6 测试的 `SHARED_SSOT_TABLES` 一致 |

**边界判定**：

> 当前的执行边界是**清晰的**：Workforce 有"评估用"的执行 seam（benchmark），核心有"生产用"的执行 seam（task execution），两者**有意不相连**。
> Option A 的本质是把这两个 seam 焊起来；而焊接点必须解决 §3.5 的归属问题，否则焊出来的是一条**无法归因**的执行通道。

---

## 7. Budget Authority Analysis

### 7.1 唯一性（复核通过）

| 断言 | 证据 | 状态 |
| --- | --- | --- |
| `check_budget` 是全仓唯一预算 gate | `delegation.py:157`，唯一调用 `:282` | ✅ |
| `Project.budget_used` 唯一写入点 | `delegation.py:491` | ✅（并由 `test_budget_used_has_exactly_one_writer` 钉死） |
| Workforce 不引用 budget 符号 | `test_workforce_modules_never_reference_delegation_domain`（AST 扫描 `check_budget` / `budget_used` / `DelegatedRun` / `delegated_run`） | ✅ |

### 7.2 Option A 下的保持方式

**可以保持** —— Delegation 不需要让渡任何权威：

- Workforce 不新增 gate（明确禁止）；
- Workforce 不新增 ledger（明确禁止）；
- 预算仍由 `check_budget` + `Project.budget_used` 独占。

### 7.3 但存在两个**已有的**薄弱点（本轮复核发现，非 Option A 引入）

| # | 薄弱点 | 证据 | 严重度 |
| --- | --- | --- | --- |
| W-1 | `check_budget` 条件触发：project-less 上下文整体跳过 gate | `delegation.py:277` | **P1**（当前因 `DelegatedRun.project_id` 非空而未爆炸，属偶然防护） |
| W-2 | `_accrue_budget` 仅成功路径调用；失败/超时 run 的远端成本漏记 | `delegation.py:336` vs `:338-340, 466-474` | **P1**（既有账本不完整） |

⇒ 这两点在 Option B 下同样存在，**不是"打开 Option A 才有"的风险**，但会**放大** Option A 的后果（一旦 Workforce 开始产生可计费执行，漏记与绕过的影响面从"项目内"扩大到"招聘链"）。

---

## 8. Cost Authority Analysis

| 层级 | 权威 | 证据 | 在 Option A 下 |
| --- | --- | --- | --- |
| 成本发生 | remote worker | `worker_contract.py:138` | 不变 |
| 成本计量 | `DelegatedRun.cost` | `delegation.py:443` | 不变 |
| 成本归集（权威账本） | `Project.budget_used` | `delegation.py:491` | 不变 |
| 成本留痕（receipt） | `Artifact.provenance_json` | `delegation.py:554` + `execution.py:362` | 不变 |
| **Workforce 归因** | **无** | §3.5 | **仍然无** —— 这是 Option A 不可用的根因 |

**结论**：Option A **不会**新增成本权威，也**不会**解决"Workforce 归因无权威"这一问题。`DelegatedRun.cost` 是真实的成本源，但它**在结构上无法被归属到 JobVersion**。

---

## 9. Receipt Analysis

| 问题 | 答案 | 证据 |
| --- | --- | --- |
| receipt 是否已存在？ | **是** = `Artifact.provenance_json` | E-25 / E-26 |
| 由哪个域产生？ | **Execution 域**（`execute_task` 内写） | `execution.py:351-364` |
| 何时产生？ | 仅当适配器是 `DelegatedExecutionAdapter` | `execution.py:351` |
| 内容是否含 cost？ | 是（`cost` / `usage` / `attempt`） | `delegation.py:573-574` |
| 是否防泄漏？ | 是，只带 `secret_ref` 句柄，经 `redact_secrets` 投影 | `delegation.py:576`, `delegation.py:373` |
| 归属维度？ | `project_id` + `task_id`（**无 job_version**） | E-21 / E-27 |
| Workforce 能否消费？ | 当前**不能合法消费**（§3.8） | E-30 / E-32 |

⇒ receipt 这一环**不是缺口**；缺口在 receipt 与 Workforce 归属维度之间的**维度不匹配**（Project/Task vs JobVersion）。

---

## 10. Idempotency / Replay Analysis

| 机制 | 位置 | 覆盖范围 | Option A 下 |
| --- | --- | --- | --- |
| `execute_task` 幂等 | `execution.py:202-213` | 同 task + 同 key ⇒ 返回既有 Artifact | ✅ 可复用 |
| `DelegatedRun` UNIQUE | `models.py:478` | `H(task, agent, attempt)` | ⚠️ 重试产生新行 |
| `_replay` outbox | `services.py:78-93` | **要求非空 project_id**（E-36） | ❌ Workforce 不可用 |
| `cost_evidence` UNIQUE | `workforce_cost_evidence.py:113` | `{type}:{id}` + 同 SAVEPOINT | ✅ 幂等可靠，但依赖 id 真实 |
| `run_benchmark` UNIQUE | `workforce.py:1354-1362` | 三元组 | ✅ 但仅限 benchmark |

**at-most-once 判定**：

- **执行层**：✅ 有（幂等键 + Artifact 复用）
- **账本层**：⚠️ 有漏洞 —— 失败 run 成本漏记（W-2）
- **归属层**：❌ **完全没有** —— 没有机制保证"同一笔成本只被归属一次"，因为**没有任何归属记录**（§3.5）

⇒ 即便 Option A 打开，**at-most-once 在 Workforce 归因这一层无法成立**，因为它缺少的不是一个去重键，而是**一个可去重的实体**。

---

## 11. Dependency / Cycle Analysis

**当前依赖图（单向、无环）**：

```
workforce.*  ──► agent_registry   (共享 SSoT，只读)
workforce.*  ──► audit / models / services / actor
execution    ──► orchestrator / scheduler / services / models
execution    ──► delegation (函数内 import, :340)
delegation   ──► models / audit / worker_contract
(没有任何 execution/delegation 模块 import workforce)
```

| 情形 | 是否成环 |
| --- | --- |
| Option A：workforce → execution/delegation | ❌ 不成环（新增单向边） |
| Option A + 未来 delegation 需要回写 job_version 归因 | ✅ **成环** |
| Option B（维持现状） | ❌ 无环 |

**既有机器检查**：`test_workforce_chain_has_no_project_or_delegation_reference` 要求 Workforce 表的对外引用 ⊆ `{agent, capability}`（`SHARED_SSOT_TABLES`）。⇒ 任何指向 project/task/delegated_run 的**表级**引用都会被 CI 拒绝。

⇒ **环不是当前风险；风险在于 Option A 一旦打开，"回写归因"几乎是必然的下一步，那时成环。** 因此单向性必须在打开 Option A 的**同一波**钉死，而不是事后补。

---

## 12. Migration Impact

| 方案 | schema 变更 | 是否触碰已合并不变量 | 备注 |
| --- | --- | --- | --- |
| **Option B（本文推荐）** | **零** | 否 | 与当前状态一致 |
| Option A-1（Workforce 表加 project_id/FK） | additive 迁移 | ❌ 违反 `test_workforce_chain_has_no_project_or_delegation_reference` | 需先修改已合并测试 |
| Option A-2（映射表） | additive 迁移 | ⚠️ 取决于命名/归属 | 需评估 |
| Option A-3（纯参数传递，零 schema） | **零** | ⚠️ 不违反机器检查，但**违反已合并书面契约**（E-30/E-31/E-32） | **最危险**：禁止事项变为"靠人记住" |

**本文不产生任何迁移。**

---

## 13. Security / Governance Boundary

| 维度 | 现状 | Option A 的影响 |
| --- | --- | --- |
| **Actor** | Workforce = owner-only（`_assert_owner_actor`, `actor.py:79`）；执行 API = `actor="agent"`（`api/app.py:708`）；`resolve_agent_actor` 仅网关可用（`actor.py:64-76`） | 需定义"owner 触发 agent 域执行"的授权模型 —— **当前不存在** |
| **信任** | delegation 要求 `trust ∈ {INTERNAL, VERIFIED_EXTERNAL}`（`delegation.py:141,150`）；Workforce 候选池**不过滤 trust**（`workforce.py:622-629`） | 可能出现"候选可入职但不可执行"的不一致 |
| **最小权限** | 外发上下文经 `_project_context` 白名单投影 + `redact_secrets`（`delegation.py:352-373`, `:627-645`） | ✅ 可原样复用 |
| **密钥** | `secret_ref` 仅句柄；实际值调用时解析，不入 TaskContext/Artifact/AuditLog（`models.py:461-463`） | ✅ 可原样复用 |
| **审计** | delegation 侧审计带 `project_id`/`task_id`；Workforce 侧恒 `None`（`workforce_cost_evidence.py:137-139`） | 跨链审计**无法关联**（无公共键） |
| **HTTP** | 无 Workforce 路由；`_translate` 透传 `ServiceError.status_code`；无全局 `IntegrityError` 翻译（W7 §15.1） | 完整性错误会漏成 500 |

⇒ **治理缺口**：Option A 会让 owner-actor 触发一笔**花费真实金钱**的动作，而当前 repo **没有任何针对 owner 触发执行的预算授权/审计关联机制**。

---

## 14. Recommendation

> **推荐：Option B —— 当前不允许 Workforce 经 Delegation 执行。**

**但必须明确区分两件事**：

| 层面 | 结论 | 性质 |
| --- | --- | --- |
| **技术层面** | repo evidence **足以**判定：当前不存在安全、可归属、可对账的 `Workforce → Delegation` 路径 | ✅ 技术结论，本文可判定 |
| **业务层面** | "Workforce 是否**应该**被允许花费预算、以及花谁的钱" | ❌ **本文不可判定** |

**推荐理由（全部 repo-evidence-backed）**：

1. **结构性缺口（P0）**：`cost_evidence.job_version_id` 非空，而 `DelegatedRun` 与之无公共列 ⇒ **归属无解**（§3.5）。这不是"还没写"，而是"没有可写的地方"。
2. **契约禁止（P0）**：W5 合并契约明令 `delegated_run.id` 不得作为 Workforce 可归因源事件（E-30/E-32）。
3. **CI 已钉死（P0）**：Workforce 14 表不得有 `project_id`、不得 FK 到 project/task/delegated_run（`tests/test_workforce_w6_invariants.py:128`）。打开 Option A 需要**主动破坏一条已合并的绿色不变量**。
4. **无 producer（P1）**：`_DefaultBenchmarkAdapter` 无后端、`BenchmarkResult` 无成本列（E-44/E-45）；W6 已冻结"不得伪造"（DR-D1-1=(d)）。
5. **既有账本不完整（P1）**：失败/超时 run 成本漏记（W-2）；预算 gate 条件触发（W-1）。
6. **语义不一致（P1）**：agent 准入（capability vs trust）、agent 选择权（scheduler vs Workforce）、actor（owner vs agent）三处不匹配（B-7/B-8/B-9）。

**不推荐 Option A 的理由不是"它不完整"，而是"它的缺口全部落在业务归属层，工程无法单独补齐"**：

- 工程可以补 producer、可以补迁移、可以补 receipt；
- 但工程**不能**回答："一次 Job 的执行该从哪个项目的预算里出钱？"—— 这个问题的答案不在代码里。

**本推荐的有效期**：到 §4.4 的 G-1…G-5 闸门被满足为止。这不是永久否决。

---

## 15. `[DECISION REQUIRED]`

### 15.1 主要决策

> **`[DECISION REQUIRED — BUSINESS] DR-W7-5`**
> **Workforce 是否允许经 Delegation 执行（即：Workforce 是否被授权产生真实、可计费的执行）？**
>
> - 选 **(a) 允许** ⇒ 必须先解决 §15.2 的 DR-W7-5a/5b/5c，并接受修改已合并的 W6 不变量测试；
> - 选 **(b) 暂不允许**（本文推荐）⇒ 维持现状，`cost_evidence` 保持 0 行定位，直到 §4.4 的 G-1…G-5 满足；
> - 选 **(c) 永久不允许** ⇒ 则 `cost_evidence` 的定位需重新评估（是否还有存在意义），属独立议题。
>
> **本文不给 (a)/(b)/(c) 的业务判断。** 技术证据支持 (b) 作为**当前**状态，但 (a) vs (c) 是业务路线选择。

### 15.2 若选 (a)，必须先行拍板的子决策

| ID | 决策题 | 为什么是业务决策 |
| --- | --- | --- |
| **DR-W7-5a** | 一次 Workforce 执行的成本从**谁的预算**出？（既有 Project？新建 Workforce 预算概念？） | 决定"谁付钱"，纯业务 |
| **DR-W7-5b** | `cost_evidence` 与 `Project.budget_used` 的语义关系：(a) 投影/子集（需对账键）vs (b) 并行账（违反 DR-D1-2） | 决定"是否会双重表述"，语义选择 |
| **DR-W7-5c** | Workforce 是否有权**指定执行 agent**（覆盖 scheduler 的 `route_task`）？ | 决定 Workforce 的"选人"价值主张是否延伸到执行；触及既有 Scheduler 权威 |
| **DR-W7-5d** | owner-actor 触发可计费执行，是否需要**独立授权/配额**？ | 治理与风险敞口，业务决定 |
| **DR-W7-5e** | 候选准入是否要引入 **trust_level** 维度（当前只看 capability）？ | 改变招聘漏斗语义，业务决定 |

### 15.3 技术侧待决（可由工程推进，但仍需 owner 确认优先级）

| ID | 题目 | 说明 |
| --- | --- | --- |
| **DR-W7-5f** | 是否修复 W-1（`check_budget` 条件触发）？ | 独立于 Option A/B 的既有薄弱点 |
| **DR-W7-5g** | 是否修复 W-2（失败 run 成本漏记）？ | 同上；影响账本完整性 |
| **DR-W7-5h** | `Task.actual_cost`（`models.py:379`）死列：删除、启用、还是保留？ | 本轮新发现，需清理决策 |

---

## 16. Next Gate

**若 owner 选 (b)（本文推荐）**：

1. W7 实现限定为 **zero-migration tests-only**，把本分析的结论钉成机器可检查的不变量（建议新增，不实现于本轮）：
   - Workforce 模块不 import `execution` / `delegation` / `scheduler` / `orchestrator` / `adapters`（当前已成立）；
   - `DelegatedRun` 与 `cost_evidence` 之间**不存在** FK（当前已成立）；
   - `cost_evidence.job_version_id` 保持非空（防止有人放宽）；
   - `Task.actual_cost` 无写入者（锁定现状，配合 DR-W7-5h）。
2. `cost_evidence` 保持"0 行 + 契约就绪"定位，不新增 caller。
3. 待 §4.4 的 G-1…G-5 满足后，重新开一份 DR-W7-5 分析。

**若 owner 选 (a)**：

1. **必须先**拍板 DR-W7-5a / 5b / 5c（§15.2）——三者不定，任何实现都是在猜。
2. 明确接受"修改已合并的 W6 不变量测试"，并在**同一 commit** 中给出替代不变量（不能只删不补）。
3. 单项处理 W-1 / W-2（DR-W7-5f / 5g）——不应与 Option A 混在同一波。

**本轮不做**：实现、迁移、push、PR、merge、修改 W7 Design V1。

---

## 附录 A：本轮取证覆盖清单（对照题面要求）

| 要求核查项 | 是否重新核查 | 关键位置 |
| --- | --- | --- |
| `execution.py` | ✅ | `:187-302`, `:330-400`, `:519`, `:571`, `:629` |
| `delegation.py` | ✅ | `:100-174`, `:243-349`, `:352-493`, `:554-579` |
| Workforce execution / trial / benchmark | ✅ | `workforce.py:1267-1362`；`workforce_trial.py` |
| `DelegatedRun` | ✅ | `models.py:454-492`；`delegation.py:388-493` |
| `Artifact.provenance_json` | ✅ | `models.py:403-405`；`execution.py:351-364`；`delegation.py:554-579` |
| `CostEvidence` | ✅ | `models.py:2143-2200`；`workforce_cost_evidence.py:65-147` |
| `Project.budget_used` | ✅ | `models.py:257`；`delegation.py:476-493` |
| `check_budget` | ✅ | `delegation.py:157-171`, `:275-282` |
| service / API boundary | ✅ | `services.py:57-93,165-200`；`api/app.py:688-712`；`actor.py:53-80` |
| idempotency / audit / transaction | ✅ | `execution.py:202-213`；`models.py:478`；`workforce_cost_evidence.py:113,128-145`；`workforce.py:1354-1362`；`services.py:57-93` |

## 附录 B：未修改承诺核对

| 项 | 状态 |
| --- | --- |
| `src/` | **未修改** |
| `tests/` | **未修改** |
| `alembic/` | **未修改** |
| `docs/workforce/Workforce_W7_Design_V1.md` | **未修改** |
| 新增文件 | 仅本文件 |
| push / PR / merge | **无** |
