# Workforce W8-P1 Design V1 — Employee Cost Closure (W8 归因 × W5 账本)

Status: IMPLEMENTED (this change) · Companion code: `src/aios/employee_cost.py`, `src/aios/api/employee_cost.py` · Tests: `tests/test_employee_cost.py`

## 1. 问题：两个"死代码"与 G-5

W1–W8 交付后，Workforce 有两块能力各自悬空：

| 组件 | 状态 | 原因 |
|---|---|---|
| `employee_for_task` / `employee_for_artifact`（W8-v2 归因） | 仅 2 个只读端点，零产品消费 | 归因结果没有下游 |
| `record_cost_evidence`（W5 账本 writer） | 契约完备（schema+幂等+审计）但 **0 caller、0 行** | 缺"Workforce 原生成本源事件" |

后者即 W1–W7 checkpoint 的悬置缺口 **G-5**：`DelegatedRun.cost`（唯一实测成本）归属 `Task → Project` 域，与 Workforce 无公共列（DR-W7-5 §3.7），故 W5 诚实约束（D-1.4）禁止伪造 caller。

## 2. 裁定：W8 关闭 G-5

W8-v2 执行桥改变了事实基础：经 `assign_work_to_employee` 创建的 Task **本身就是 Workforce 可归因实体**——它因 Employee↔Agent binding 而存在，且 `employee_for_task` 以 `Task.created_at` 锚定的半开区间给出永久稳定的归因。

**源事件裁定**：`source_event = ("employee_bridge_task", task.id)`。
**为何合法**：D-1.4 禁令的字面是"不得复用 `delegated_run.id`"——因为普通委托 run 属于执行域、不携带 Workforce lineage。bridge Task 是不同的事实，携带 lineage。本设计不触碰禁令字面：`delegated_run.id` 从不作为 source_event_id。

## 3. 闭环语义（一个 bridge Task 一行）

```
record_task_cost_evidence(session, task_id, actor, note=None) -> CostEvidence
  404  Task 不存在
  422  task_not_employee_attributable   employee_for_task -> None（非 bridge Task，拒绝伪造）
  422  no_measured_cost                 该 Task 全部 DelegatedRun.cost 之和 <= 0
       （I6：DelegatedRun.cost 默认 0.0，0 = "无测量" = 无行，绝不记 amount=0）
  409  cost_evidence_already_recorded   幂等键 employee_bridge_task:<task_id> 重放
  201  成功：一行 CostEvidence
       job_version_id = Employee.job_version_id（W5 聚合锚，NOT NULL 约束的合法来源）
       employee_id    = employee_for_task(task)（W8 归因，重绑定后历史不变）
       amount         = Σ DelegatedRun.cost（实测聚合）
```

行数语义：**每 Task 至多一行**（at-most-once，W5 I5）。记录之后再完成的 run 不追溯合并——V1 简化，记录时机由 owner 判断（成本图像定格后记录）。审计沿 W5 writer 的同 savepoint `cost_evidence.create`，本层不重复记审计。

汇总面：`employee_cost_summary(session, employee_id)` → `{employee_id, job_version_id, evidence_count, total_amount, rows}`；纯投影，CostEvidence 永远不是预算权威（W7-I8）。

## 4. 边界合规（逐条机械守卫核对）

| 守卫 | 结论 |
|---|---|
| W7-I1/I4/I5（`workforce*.py` 不碰执行域、不引用 Project/Task） | 新模块不匹配 `workforce*.py` glob（命名 `employee_cost.py`），守卫不适用；它引用 Task 属合法——守卫只约束 recruitment 域文件 |
| W8 seam（`employee_bridge.py` 禁 import `aios.workforce*`） | bridge 零改动；组合层 `employee_cost.py` 在两侧之上，import 方向合法（seam 扫描只覆盖 workforce glob + 两个 bridge 文件） |
| W6 路由守卫（`WORKFORCE_PREFIXES` 路由仅限 bridge） | 路由用 `POST /tasks/{id}/cost-evidence` 与 `GET /cost-evidence?employee_id=`，避开 `/employee*` 前缀；`BRIDGE_ALLOWED_ROUTES` 冻结集零改动 |
| **F-W5-B1"0 caller"守卫** | **按本设计修订**：`test_writer_has_no_caller_in_v1` 从"全域禁 import"收窄为"闭式 caller 白名单 = {employee_cost.py}"，并反向断言白名单非空。这是本 PR 唯一的守卫契约变更，依据即 §2 裁定 |
| delegation.py | 零改动（成本回写点 `_wait_for_completion` 不碰） |
| Migration | 零（CostEvidence 表 = W5 迁移 `20260904_0001`，已在 main） |

## 5. HTTP 面（owner-only，沿用 bridge 设计契约）

- `POST /tasks/{task_id}/cost-evidence` → 201 记录 / 404 / 422 / 409
- `GET /cost-evidence?employee_id=...` → 200 汇总 / 404
- 两端点均挂 `authenticate_owner`（owner-surface inventory 守卫）；`ServiceError → HTTPException` 经本地 `_translate` 拷贝（避免 app import 环）；路由在 `register_employee_cost_routes` 内构建、平铺 attach（`owner_inbox_routes` 先例），`dependency_overrides_provider=application` 保证测试可覆盖依赖。

## 6. 测试面（`tests/test_employee_cost.py`，12 用例）

happy path（字段逐一断言 + W5 审计行）/ 多 run 聚合 / 非 bridge Task 422 / 无实测成本 422 / Task 404 / 非 owner 403 / 重放 409 at-most-once（含审计不重复）/ 重绑定后归因历史正确（两 Task 同 Employee）/ 汇总投影 / 汇总 404 / API 201-409-200-404-422 全链 / API 非 bridge 422。

## 7. 已知边界与后续

- 记录后新增的 run 成本不合并（§3）；如需"最终成本"语义，后续可加 `supersede` 机制（新行 + 旧行 note 标注），V1 不做。
- `employee_for_artifact` 的产品消费（按产物聚合成本）留待 owner console 接入时一并做。
- 自动闭环（Task 完成事件触发记录）需要 execution 域反向依赖组合层，触碰 seam 哲学，V1 刻意不做——记录是 owner 显式动作。
