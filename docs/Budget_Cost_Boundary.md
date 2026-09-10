# 成本与预算边界声明（GAP-3 Stage 1）

> **状态**：已生效（文档级声明，2026-09-10）。本声明**不改变任何代码行为、不改数据模型、零迁移**。
> **来源**：架构 checkpoint GAP-3（`gap234_architecture_decision_2026-09-09.md` §二，Q3-1 拍板"是"）。
> **适用范围**：`Project.budget_limit` / `Project.budget_used` / `check_budget` / Usage Metering 报表口径。

---

## 1. 一句话边界

`Project.budget_limit` / `Project.budget_used` 治理的口径是：**远程委托（delegated）执行中"已测量的货币花费"**。

LOCAL 执行（`DelegationMode.LOCAL`，进程内 `LLMExecutionAdapter`）的货币花费**不在** `budget_used` 口径内——**可见（metering 报告得出），但不控（不阻断、不计提）**。

这不是遗漏，是**口径选择**：预算面只治理"有货币真值可依"的那部分花费。

---

## 2. 为什么是"不控"，而不是"没有花费"

成本铁律：**cost 必须是 measured（provider / 适配器实际报告的货币值）；token 数不是货币，估计值不是 measured。**

证据链（file:line 基准 = 本声明合入后的 main；函数名才是稳定锚点，行号仅便于首次定位）：

1. `execution.py:722-745` `LLMExecutionAdapter._finish_local_run(...)` —— 只接受 `usage`，**没有 cost 形参**；调用 `complete_local_run` 时从不传 cost。
2. `execution_run.py:359-415` `complete_local_run(..., cost: float = 0.0)` —— 默认 `0.0`；`if cost:` 为假时不写 `cost` 列（`models.py:525` `cost: float = 0.0`）。
3. `delegation.py:270-325` `accrue_run_budget` —— 仅当 `cost > 0` 才累加进 `budget_used`；`None` / `0` **绝不伪造**成扣费。

结论：LOCAL run 终态化后 usage（token）落库、`cost` 恒 `0.0`、`budget_used` 不动。

这是"绝不伪造成本"铁律的**直接后果**，不是缺陷——**宁可盲，不可编**。缺失的是价格源（Stage 2），不是机制。

---

## 3. 口径矩阵

| 维度 | Delegated（`remote_api` / `a2a` / `mcp` / `workstation`） | LOCAL（`local`） |
|---|---|---|
| 谁写 `cost` | 远端终态化路径 `complete_run` 传入 provider 实测值（迟到 callback 只落证据、不入账，见 GAP-4） | **无写者** |
| 计入 `budget_used` | 是（ACCRUABLE 终态 且 `cost > 0`） | **否** |
| `check_budget` 覆盖 | 是（含 GAP-2 在途估计投影） | **否**（无货币信号可比） |
| metering 可见性 | `measured_spend` / `accrued_measured_spend` | `run_count` + `no_measured_cost_run_count` |
| token/usage 记录 | 有（`usage`，schema 随 adapter 异构） | 有（LOCAL verbatim） |
| 当前缺口 | 无 | **Stage 2 前无价格源**（见 §8） |

**关键澄清**：LOCAL 并非被架构排除在预算之外。`complete_local_run` 一旦传入 `cost > 0`，LOCAL run 走的是**同一条** `accrue_run_budget` 入账路径（`execution_run.py:411-413`）。今日不计提，唯一原因是没有可信价格源。

---

## 4. 可见性：metering 已经能看到 LOCAL（零改动即可）

`usage_metering.py:123-150` `_project_runs`：`measured = [run for run in runs if run.cost is not None and run.cost > 0]`。

因此一个 LOCAL run（`cost == 0.0`）：

- 计入 `run_count` 与 `runs_by_status` → **执行量可见**
- 计入 `no_measured_cost_run_count` → **"执行了但没有货币成本"可见**
- **不**计入 `measured_spend` / `accrued_measured_spend` → **不污染成本真值**

实测（一次性探针脚本，未入库；同项目 1 个 LOCAL run + 1 个 remote run）：

```text
[A] local terminalized=True  mode=local
local  run: cost=0.0 usage={'prompt_tokens': 1200, 'completion_tokens': 340, 'model': 'gpt-x'}
remote run: cost=3.0
project.budget_used = 3.0                       # 只来自 remote

run_count = 2                                   # LOCAL 计入执行量
measured_run_count = 1
no_measured_cost_run_count = 1                  # LOCAL 落进"无货币成本"桶
measured_spend = 3.0 / accrued_measured_spend = 3.0
budget_reconciliation.matches = True
```

---

## 5. "对账通过"≠"花费全在预算内"

`budget_reconciliation`（`usage_metering.py:202-241`）的 `matches=True` 只证明 **accrual 与 `budget_used` 一致**（即 W6 单写者没被绕过）。它**不**证明所有真实花费都进了预算：上例中 `matches=True` 与 `no_measured_cost_run_count=1` 同时成立。

**判读规则**：看到 `no_measured_cost_run_count > 0`，即存在**预算口径之外**的执行量。该桶是"口径边界"的仪表盘，不是异常告警。

---

## 6. 与 GAP-2 在途投影的关系

`check_budget`（`delegation.py:223-253`）：

```text
projected = budget_used + Σ(在途 run 的 Task.estimated_cost) + 本次 attempt 的 est
```

- 在途集合 `INFLIGHT_RUN_STATUSES`（`delegation.py:186-190`，`{submitted, running}`）按**状态**定义，**含 LOCAL**；但 LOCAL 同步终态化，实际恒为空集 → 不改变今日行为。
- 投影用的是 `estimated_cost`（**估计**），仅用于门禁，绝不写回任何成本列 → **不污染 measured 口径**，也不破坏对账恒等式。

---

## 7. 不变量（本声明一条都不触碰）

| 不变量 | 状态 |
|---|---|
| W6 / BA-1：`Project.budget_used` 唯一写者 = `accrue_run_budget` | 不变 |
| 零迁移（Alembic head 仍 `20260909_0004`） | 不变 |
| 无新治理面（无价目表 / 无配置表 / 无新实体） | 不变 |
| estimated 永不混入 measured | 不变（拒绝选项见 §9） |
| late callback 证据永不追溯计费 | 不变（GAP-4） |

---

## 8. Stage 2 门槛（roadmap 备忘，本 PR 不做）

- **触发条件**：Capacity-aware routing 设计启动时（Q3-3）。**硬前置门槛**——无 LOCAL cost，成本感知路由缺少输入信号。
- **配置形态**：env JSON `AIOS_MODEL_PRICING`（model → 每 token 单价，input / output 分开计价），**不建 DB 配置表**（避免新增治理面）。
- **落点**：`complete_local_run` 从已记录 usage 推导 cost（舍入规则需明确）→ 走**既有** `accrue_run_budget` 入账 → `check_budget` 自动覆盖。
- **影响**：LOCAL 由"可见不控"变为"可见且受控"。届时本声明 §1 / §3 需同步更新。

---

## 9. 已考虑并拒绝的选项

| 选项 | 拒绝原因 |
|---|---|
| 用 `Task.estimated_cost` 顶替 LOCAL 花费计入 accrual | 把"估计"混入"已测量" → 污染 `measured_spend` SSoT，破坏对账恒等式（`measured_spend = accrued + cancelled_with_cost`） |
| 现在就做价目表 | 定价策略 / 模型维护 / 配置面三项都需专门拍板；routing 未启动前无消费方 —— 过早 |
| 硬编码 token → 货币折算常量 | 与 provider 无关的价格必然失真，且制造第二个成本真值源 |

---

## 10. 证据索引

| 位置 | 内容 |
|---|---|
| `src/aios/models.py:81` | `DelegationMode.LOCAL`（与 remote 共用同一 `DelegatedRun` 行类型） |
| `src/aios/models.py:525` | `DelegatedRun.cost: float = 0.0` |
| `src/aios/execution.py:722-745` | `_finish_local_run` 只传 usage |
| `src/aios/execution_run.py:359-415` | `complete_local_run(cost=0.0)` |
| `src/aios/delegation.py:186-253` | `INFLIGHT_RUN_STATUSES` / `projected_inflight_cost` / `check_budget` |
| `src/aios/delegation.py:270-325` | `accrue_run_budget`（唯一预算写者） |
| `src/aios/usage_metering.py:123-150` | `_project_runs`（measured vs no-measured 分桶） |
| `src/aios/usage_metering.py:202-241` | `budget_reconciliation`（只报不修） |
| `src/aios/api/usage.py:1-36` | 4 个 owner-only 只读 metering 端点 |

---

## 11. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-10 | 初版（GAP-3 Stage 1）：确立"budget = delegated measured spend；LOCAL 可见不控"边界，附实测证据与 Stage 2 门槛 |
