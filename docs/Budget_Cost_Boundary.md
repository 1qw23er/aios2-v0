# 成本与预算边界声明（GAP-3 Stage 1 声明 + Stage 2 价目表）

> **状态**：已生效。Stage 1（2026-09-10）= 纯文档声明，**零代码行为变化**；Stage 2（2026-09-10）= 落地 env 价目表，**改变 LOCAL run 的入账行为**（见 §8），但**零迁移、不新增预算写者**。
> **来源**：架构 checkpoint GAP-3（`gap234_architecture_decision_2026-09-09.md` §二，Q3-1/Q3-2/Q3-3 拍板"是"）。
> **适用范围**：`Project.budget_limit` / `Project.budget_used` / `check_budget` / Usage Metering 报表口径 / `aios.model_pricing`。

---

## 1. 一句话边界

`Project.budget_limit` / `Project.budget_used` 治理的口径是：**"已测量的货币花费"**——远程委托由 provider 实测值入账，LOCAL 则在 owner 配置价目表后由 usage 推导入账。

LOCAL 执行（`DelegationMode.LOCAL`，进程内 `LLMExecutionAdapter`）的货币花费**默认不在** `budget_used` 口径内——**可见（metering 报告得出），但不控（不阻断、不计提）**。

**Stage 2 之后是有条件的例外**：当且仅当 owner 配置了价目表（env `AIOS_MODEL_PRICING`）**且**该 run 的模型在表内，LOCAL 才会由已记录 usage 推导出 cost 并走同一条 `accrue_run_budget` 入账——此时它**受控**。未配置价目表、或模型不在表内时，边界与 Stage 1 完全一致（可见不控）。

这不是遗漏，是**口径选择**：预算面只治理"有货币真值可依"的那部分花费，而价格必须由 owner 提供，系统绝不替他猜。

---

## 2. 为什么是"不控"，而不是"没有花费"

成本铁律：**cost 必须是 measured（provider / 适配器实际报告的货币值）；token 数不是货币，估计值不是 measured。**

证据链（file:line 基准 = 本声明合入后的 main；函数名才是稳定锚点，行号仅便于首次定位）：

1. `execution.py:722-755` `LLMExecutionAdapter._finish_local_run(...)` —— 只传 `usage`；Stage 2 起同时传 `model=self.model`，cost 仍不在调用方产生。
2. `execution_run.py:359-426` `complete_local_run(..., cost: float = 0.0, model=None)` —— `cost` 默认 `0.0`；为假且给了 `usage` 时回落到价目表推导（Stage 2），仍为假则不写 `cost` 列（`models.py:525` `cost: float = 0.0`）。
3. `delegation.py:274-330` `accrue_run_budget` —— 仅当 `cost > 0` 才累加进 `budget_used`；`None` / `0` **绝不伪造**成扣费。

结论（无价目表时）：LOCAL run 终态化后 usage（token）落库、`cost` 恒 `0.0`、`budget_used` 不动。

这是"绝不伪造成本"铁律的**直接后果**，不是缺陷——**宁可盲，不可编**。缺失的是价格源，不是机制；Stage 2 补的正是这个价格源（见 §8），入账机制一行未改。

---

## 3. 口径矩阵

| 维度 | Delegated（`remote_api` / `a2a` / `mcp` / `workstation`） | LOCAL（`local`，无价目表 / 模型不在表内） | LOCAL（模型在价目表内，Stage 2） |
|---|---|---|---|
| 谁写 `cost` | 远端终态化路径 `complete_run` 传入 provider 实测值（迟到 callback 只落证据、不入账，见 GAP-4） | **无写者** | `complete_local_run` 从 usage + 价目表**推导** |
| 计入 `budget_used` | 是（ACCRUABLE 终态 且 `cost > 0`） | **否** | 是（同一条 `accrue_run_budget`） |
| `check_budget` 覆盖 | 是（含 GAP-2 在途估计投影） | **否**（无货币信号可比） | 是（已入账的成本被门禁读到；LOCAL 自身**无前置门禁**） |
| metering 可见性 | `measured_spend` / `accrued_measured_spend` | `run_count` + `no_measured_cost_run_count` | `measured_spend` / `accrued_measured_spend` |
| token/usage 记录 | 有（`usage`，schema 随 adapter 异构） | 有（LOCAL verbatim） | 有（LOCAL verbatim，推导的输入） |
| 剩余缺口 | 无 | 无（这是**配置缺失**，非架构缺口） | 无 |

**关键澄清**：LOCAL 从未被架构排除在预算之外。`complete_local_run` 一旦拿到 `cost > 0`（无论 caller 直传还是价目表推导），走的是**同一条** `accrue_run_budget` 入账路径。Stage 2 只是让它"拿得到"。

---

## 4. 可见性：metering 已经能看到 LOCAL（零改动即可）

`usage_metering.py:125-152` `_project_runs`：`measured = [run for run in runs if run.cost is not None and run.cost > 0]`。

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

`budget_reconciliation`（`usage_metering.py:204-244`）的 `matches=True` 只证明 **accrual 与 `budget_used` 一致**（即 W6 单写者没被绕过）。它**不**证明所有真实花费都进了预算：上例中 `matches=True` 与 `no_measured_cost_run_count=1` 同时成立。

**判读规则**：看到 `no_measured_cost_run_count > 0`，即存在**预算口径之外**的执行量。该桶是"口径边界"的仪表盘，不是异常告警。

---

## 6. 与 GAP-2 在途投影的关系

`check_budget`（`delegation.py:223-271`）：

```text
projected = budget_used + Σ(在途 run 的 Task.estimated_cost) + 本次 attempt 的 est
```

- 在途集合 `INFLIGHT_RUN_STATUSES`（`delegation.py:186-190`，`{submitted, running}`）按**状态**定义，**含 LOCAL**；但 LOCAL 同步终态化，实际恒为空集 → 不改变今日行为。
- 投影用的是 `estimated_cost`（**估计**），仅用于门禁，绝不写回任何成本列 → **不污染 measured 口径**，也不破坏对账恒等式。

---

## 7. 不变量（Stage 1 声明 + Stage 2 实现，全部未破）

| 不变量 | 状态 |
|---|---|
| W6 / BA-1：`Project.budget_used` 唯一写者 = `accrue_run_budget` | 不变 |
| 零迁移（Alembic head 仍 `20260909_0004`） | 不变 |
| 无新治理面（无 DB 配置表 / 无新实体） | 不变（Stage 2 的价目表是 **env 配置**，不入库） |
| estimated 永不混入 measured | 不变（拒绝选项见 §9） |
| late callback 证据永不追溯计费 | 不变（GAP-4） |

---

## 8. Stage 2：价目表（已落地）

**触发条件**原为 Capacity-aware routing 设计启动时（Q3-3）——无 LOCAL cost，成本感知路由缺输入信号。现已实施。

### 8.1 配置形态

env JSON `AIOS_MODEL_PRICING`，**不建 DB 配置表**（避免新增治理面）：

```json
{
  "deepseek-ai/deepseek-v4-pro": {"input_per_1m": 1.0, "output_per_1m": 2.0},
  "my-finetune": {"input_per_1m": 0.0, "output_per_1m": 0.0}
}
```

- **单位**：货币 / **1,000,000** tokens，input 与 output 分开计价。货币种类 = owner 给 `Project.budget_limit` 用的那一种，本模块**不做任何汇率换算**。
- **模型标识**：`complete_local_run(model=...)` 由 adapter 传入 `self.model`；caller 未传时，退回 provider 在 `usage["model"]` 里自报的模型名。
- **token 键**：`prompt_tokens` / `input_tokens`（输入）与 `completion_tokens` / `output_tokens`（输出），按序取第一个可用值——usage schema 随 adapter 异构。
- **舍入**：`cost = round((in_tok * input_per_1m + out_tok * output_per_1m) / 1e6, 6)`。
- **读取时机**：每次 LOCAL run 终态化时读一次，**不缓存**——配置变更下次终态化即生效（进程 env 变更仍需重启才可见）。

### 8.2 fail-safe 规则（与铁律同构）

| 情形 | 结果 |
|---|---|
| 未配置 `AIOS_MODEL_PRICING` | 无 cost（= Stage 1 行为，落 `no_measured_cost_run_count`） |
| 模型不在表内 | 无 cost——**不设默认价、不跨模型推断** |
| JSON 非法 / 顶层不是对象 / 条目缺字段或非数值或为负 | 该条目被跳过并 `logger.warning`；**不抛异常**（价格 bug 绝不能让执行失败） |
| usage 缺 token 键（如只有 `total_tokens`） | 无 cost（无法拆分 input/output） |
| 只有一侧 token 可测 | 按可测侧计价，另一侧按 0 计（已记录，非猜测） |
| 单价为 0 | `cost = 0.0`（真实测量结果：该模型免费），不是 None |
| caller 显式传 `cost > 0` | **显式值优先**，推导只是 fallback |

### 8.3 落点与不变量

`complete_local_run` 推导出 cost → 写入 `DelegatedRun.cost` → 走**既有** `accrue_run_budget` 入账 → `check_budget` 读到已入账成本。

| 不变量 | 状态 |
|---|---|
| W6 / BA-1：`budget_used` 唯一写者 = `accrue_run_budget` | 不变（价目表只产出一个 float，不写 `budget_used`） |
| 零迁移 | 不变（无新列、无新表；模型名不入 `DelegatedRun`，无 model 列） |
| 无新治理面 | 不变（env 配置，非 DB 表） |
| estimated 永不混入 measured | 不变（`Task.estimated_cost` 仍与推导无关） |
| late callback 证据永不追溯计费 | 不变（GAP-4） |

**未做**：LOCAL **前置门禁**（在调用本地模型前先 `check_budget`）——价目表只让已发生的 LOCAL 花费进账并被后续门禁读到，不做事前拦截。这是有意的范围收敛，非遗漏。

**已知后果（接受）**：配置了价目表后，LOCAL 花费仍可把 `budget_used` 推过 `budget_limit`（本地同步调用不受门禁约束）；被拦下的是**之后的远程委派**（`check_budget` 读到超额即抛 `BudgetExceededError`）。若未来要在调用前拦截 LOCAL，需单独设计。

---

## 9. 已考虑并拒绝的选项

| 选项 | 拒绝原因 |
|---|---|
| 用 `Task.estimated_cost` 顶替 LOCAL 花费计入 accrual | 把"估计"混入"已测量" → 污染 `measured_spend` SSoT，破坏对账恒等式（`measured_spend = accrued + cancelled_with_cost`） |
| 硬编码 token → 货币折算常量 | 与 provider 无关的价格必然失真，且制造第二个成本真值源 |
| **给未列出的模型一个"默认价"** | 等价于替 owner 猜价格 —— 与"绝不伪造成本"同源的禁令；宁可落 `no_measured_cost_run_count` 让缺口可见 |
| **在 `DelegatedRun` 上落 model 列** | 需要迁移；且模型名在终态化时刻由 caller 已知，读时不用存（见 §8.3） |
| **LOCAL 前置门禁（调用前 `check_budget`）** | 超出"补价格源"的范围；LOCAL 是同步进程内调用，owner 自控。本轮**只让花费可入账并被后续门禁读到** |
| ~~现在就做价目表~~ | 原判断"过早"已随 routing 门槛到期而解除（2026-09-10 实施，见 §8） |

---

## 10. 证据索引

| 位置 | 内容 |
|---|---|
| `src/aios/models.py:81` | `DelegationMode.LOCAL`（与 remote 共用同一 `DelegatedRun` 行类型） |
| `src/aios/models.py:525` | `DelegatedRun.cost: float = 0.0` |
| `src/aios/execution.py:722-755` | `_finish_local_run` 传 usage + `self.model` |
| `src/aios/execution_run.py:359-426` | `complete_local_run(cost=0.0, model=None)`（Stage 2 推导入口） |
| `src/aios/delegation.py:186-271` | `INFLIGHT_RUN_STATUSES` / `projected_inflight_cost` / `check_budget` |
| `src/aios/delegation.py:274-330` | `accrue_run_budget`（唯一预算写者） |
| `src/aios/usage_metering.py:125-152` | `_project_runs`（measured vs no-measured 分桶） |
| `src/aios/usage_metering.py:204-244` | `budget_reconciliation`（只报不修） |
| `src/aios/model_pricing.py:1-170` | 价目表：`load_model_pricing` / `derive_run_cost`（Stage 2） |
| `src/aios/api/usage.py:1-37` | 4 个 owner-only 只读 metering 端点 |

---

## 11. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-10 | 初版（GAP-3 Stage 1）：确立"budget = delegated measured spend；LOCAL 可见不控"边界，附实测证据与 Stage 2 门槛 |
| 2026-09-10 | Stage 2 落地：env `AIOS_MODEL_PRICING` 价目表 + `complete_local_run` 推导 cost。§1 / §3 改为"有条件例外"，§8 由"门槛备忘"改为"已落地"，§9 补三条本轮拒绝选项 |
