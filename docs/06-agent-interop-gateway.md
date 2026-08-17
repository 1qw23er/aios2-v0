# ADR 06 — Agent Interoperability Gateway（#57）

## DeepSeek Harness Worker Adapter V1

DeepSeek Harness is an opt-in execution worker behind the existing delegated lifecycle. The layering is `DeepSeekHarnessWorkerClient -> WorkerDelegatedAdapter -> DelegatedExecutionAdapter -> execute_task`; `DelegatedExecutionAdapter` remains responsible for `DelegatedRun`, trust and budget gates, polling, retries, audit, provenance, and result ingestion into the existing Artifact path.

V1 negotiates discovery, submission, status, events, adapter-side terminal-event result aggregation, usage, runtime references, and conditional fail-closed permissions. The vendor-neutral worker interface retains cancellation and checkpoint resume, but this provider advertises `cancellation=false` and `checkpoint_resume=false`; either call returns `unsupported_capability` and never simulates success.

The adapter is disabled unless `AIOS_DEEPSEEK_HARNESS_ENABLED=true` and the assigned Agent has a `deepseek-harness+file://...` configuration reference. The fixed JSON-RPC runner takes its Cordis manifest as the final positional argument while `DSH_CORDIS_CONFIG` wins over argv, so admission requires that final argument to resolve to the exact hash-pinned manifest and the child environment pins `DSH_CORDIS_CONFIG` to the same absolute path. The manifest's enabled top-level plugin rows, not a parallel declaration, must mount `@deepseek-ai/dsh-fs-sandbox` and `@deepseek-ai/dsh-sandbox-policy`; runtime plugin attestation is checked when supplied. The default Harness filesystem composition is not accepted. Any command/path/hash/plugin mismatch stops before credential resolution, process launch, `initialize`, and `session/prompt`.

- 状态：实现中（first slice #103 + 加固 #104 已合并；审计/配置切片见 PR #60；控制台管理 #61、结果溯源持久化 #62 已实现于本分支，待评审合并）
- 关联：Epic #57，V1 史诗 #26，韧性 #55；范围外 #44（调度阻塞路径）、#53（KnowledgeFact 约束）
- 安全/成本深潜见 `docs/05-gateway-security-cost.md`（本 ADR 只定契约与规则，不重复细节）

## 1. 目标

把现有 `ExecutionAdapter` 架构扩展为：AIOS 能把**一个任务**委托给**外部闭源 agent**（不掌握其模型 / prompt / 记忆 / 工具），并收回一个经过 schema 校验的结构化 Artifact。这是从 V1（单一 LLM 多角色扮演）走向产品愿景（一个由不同真实 agent 组成的"个人 AI 公司"）的 V1.1 一步。

**范围红线**（设计评审已锁定）：
- 不实现 vendor 专属集成逻辑（只在 adapter 内部，绝不在编排 / Task 服务里）。
- 不做通用权限框架、不做 plugin 市场、不做 model gateway、不做多 agent 聊天总线。
- 外部 agent 只能**提交结果**，绝不能直接改 `Task` / `Approval` / `KnowledgeFact` / 下游工作流状态。

## 2. Agent 执行契约（10 项，vendor-neutral）

每个外部 agent 集成都必须满足：

1. **能力发现** — AIOS 如何得知 agent 能做什么（skills / 输出类型 / 约束）。
2. **任务提交** — 请求工作的标准信封。
3. **不可变上下文投递** — 投影后的 TaskContext 一次性投递、冻结，远端不得修改。
4. **远端运行身份** — 每次委托有稳定 remote id，AIOS 可引用。
5. **状态轮询或回调** — 进度 / 终态通过 poll **或** push callback。
6. **结构化 Artifact 返回** — 结果以匹配 schema 的 Artifact 返回。
7. **失败与重试** — 失败语义 + 哪一侧重试（adapter 侧重试，绝不把远端自有逻辑塞进编排）。
8. **取消** — 如何取消在途远端运行。
9. **幂等** — 相同 `(task, idempotency_key)` 不得重复执行或重复应用。
10. **溯源 / 用量 / 审计** — 谁产生、token / 成本用量、审计轨迹。

## 3. 四种 adapter 模式

| 模式 | 是什么 | 用于 |
|---|---|---|
| `remote_api` | 对可编程 agent 服务的 HTTP/gRPC 调用 | 暴露 API 的 agent（首对中的 A） |
| `a2a` | Agent-to-Agent 协议协作 | 为 agent 互操作设计的远端 agent |
| `mcp` | Model Context Protocol 工具/上下文桥 | 通过 MCP 暴露工具/上下文的 agent |
| `workstation` | 导出任务 → agent 在外部工作站跑 → 导入结果 | 不可编程 / 闭源 agent（首对中的 B） |

**规则**：
- **MCP 与 A2A 不等价**：MCP 主要是工具/上下文连通（同步调用）；A2A 是远端 agent 任务协作（可异步）。
- 编排 / Task 服务中**无 vendor 专属行为**，只在 adapter 内部。
- 外部 agent 只能提交结果，不得直接改 `Task`/`Approval`/`KnowledgeFact`/下游状态。
- 每个结果以**未校验 Artifact** 进入，先过 schema 校验再完成任务。
- agent 凭证用 **secret 引用**，绝不进入 `TaskContext` / `Artifact` / `AuditLog` 载荷。
- 上下文按 agent + task **最小权限投影**。
- 既有 `LLMExecutionAdapter` 仍是合法本地 adapter（零改动）。
- 复用既有 workstation 导入/导出，不重建。
- 浏览器自动化明确为非核心、仅实验性。
- 不做通用 plugin 市场 / model gateway / 多 agent 聊天总线。

## 4. 数据模型（最终，已按实现落地）

- **`Agent`** = DB 托管的 registry（`agent` 表）。字段：`id, name, role, adapter_type, delegation_mode(=模式), capabilities, permissions, cost_policy, endpoint, config_ref, secret_ref(不透明), callback_url, enabled, limitations, status, trust_level(#104 单信任轴), timeout_s(本切片, 默认300), max_retries(本切片, 默认3)`。Q1 决议 = **DB 托管**：owner 可从控制台增/启/停 agent，无需改码重启。
- **`DelegatedRun`**：`id, project_id, task_id, agent_id, delegation_mode, secret_ref(不透明), status(SUBMITTED|RUNNING|SUCCEEDED|FAILED|CANCELLED|EXPIRED), idempotency_key(唯一), attempt, remote_run_id, remote_status, context_ref, callback_url, cost, usage, error, submitted_at, finished_at, created_at`。
- **`Artifact`**（扩展）：`+adapter_id, +source(local|delegated:<mode>), +provenance_json`（远端 run id / 用量 / agent 身份 / 重试 attempt）。结果以未校验 Artifact 入库。
- **`Project`**（扩展）：`+budget_limit`(0=不强制) `+budget_used`(累计)；成本超预算 **硬阻断**（#104 / Q3）。
- **`Secret`**：仅外部密钥库；`secret_ref` 是不透明句柄，绝不持久化到 `TaskContext`/`Artifact`/`AuditLog`。
- **不改** `Task` / `Approval` / `KnowledgeFact` / `Event` 结构。
- `AuditLog` 事件类型（本切片新增枚举 `AuditEvent`）：`agent.discover | agent.delegate | agent.result_received | artifact.validated | delegation.failed | delegation.cancelled`。

## 5. Adapter 接口（`ExecutionAdapter` 扩展）

```python
class DelegatedAdapter(Protocol):
    def discover_capabilities(self) -> dict: ...              # 契约点 1
    def submit(self, *, delegated_run, projected_context,
               output_schema, remote_callback_url) -> dict: ... # 2,3（不可变上下文）
    def status(self, *, delegated_run) -> dict: ...            # 5（轮询）
    def cancel(self, *, delegated_run) -> None: ...            # 8
    def ingest_result(self, *, delegated_run) -> dict: ...     # 回调/webhook 路径
```

- `LLMExecutionAdapter` 只保留 `run()` → 仍是合法本地 adapter，**零改动**。
- `DelegationEnvelope` = `{task_id, output_schema, projected_context, idempotency_key, callback_url?, timeout_s}`。
- `CapabilityManifest` = `{skills, output_types, constraints, supports_callback}`。
- 基础类 `DelegatedExecutionAdapter` 提供统一 `run()`：投影 → 信任门(#104) → 预算门(#104) → 重试循环 → 提交 → 等待完成(轮询/回调) → 摄入 → schema 校验 → 预算累加 → 返回。

## 6. 执行状态机

```
READY ─submit()─▶ SUBMITTED ─▶ RUNNING ─┬─ status()/callback = succeeded ─▶ SUCCEEDED
                                         ├─ status()/callback = failed    ─▶ (retry? ─▶ SUBMITTED w/ new attempt) ─┐
                                         ├─ timeout                        ─▶ CANCELLED ─▶ (retry? ─▶ SUBMITTED) ──┤
                                         └─ cancel()                       ─▶ CANCELLED                         (retry loop)
                                                                                                        ▼
                                                                                            attempts exhausted ─▶ TASK FAILED
```
- 每次重试 = 新的 `DelegatedRun` + 新 `idempotency_key`（沿用 #55 owner 重跑语义）。
- `TASK FAILED` → 既有 `FAILED→RESET→rerun` 保留；owner 可从控制台重跑。
- 超时（per-agent `timeout_s`）→ 该次运行标记 `EXPIRED`，由重试/错误路径最终归为 `FAILED` 并携带超时原因。

## 7. 安全模型

- **凭证**：`Agent.secret_ref` 仅不透明句柄；按调用从外部密钥库解析，**绝不**写入 `TaskContext`/`Artifact`/`AuditLog`（`AuditLog` 全局按 key + 高熵形态脱敏，含 Authorization 头）。
- **最小权限投影**：`project_external_context()` 严格 allowlist（objective / instructions / acceptance_criteria / dependency_outputs），剥离 internal 知识库上下文（approved_facts / decisions / policies），且脱敏任何 secret 值。
- **结果不可信**：每个外部结果以**未校验 Artifact** 进入，过 schema 校验后才完成任务；外部 agent 从不调用变更端点，只通过签名回调或轮询结果返回。
- **无直接变更**：外部 agent 不得写 `Task`/`Approval`/`KnowledgeFact`/下游状态；只有编排在收到已校验 Artifact 后才可。
- **单信任轴**（#104）：`AgentTrustLevel`(internal/verified_external/experimental)；`experimental` 硬阻断委托。**非通用权限框架**。

## 8. 幂等规则

- `idempotency_key = H(task_id, agent_id, attempt)`。
- API/A2A adapter **必须**接受并尊重该 key（adapter 侧去重返回已有结果，不重复执行）。
- workstation 模式：导出包携带该 key，导入时校验后再建 Artifact。
- 相同 key + 相同 adapter → 返回既有 `ExecutionResult`，绝不重复应用。

## 9. 超时 / 恢复（Q3 已决）

- 每委托 `timeout_s`（agent 可配，默认 300s）；超时 → 尽力 `cancel()` → `EXPIRED` → 重试或 `TASK FAILED`。
- **重试**：指数退避，最多 `max_retries`（默认 3，agent 可配），复用 #55 自愈模式。
- **成本预算**：超预算 → **硬阻断**（Q3 决议，软警告被否），拒绝委托、标记任务 `BLOCKED`、通知 owner，绝不静默超支。

## 10. 首对 agent（及理由）

- **A — Hermes（remote_api 模式）**：已在 ali-server 运行的 OpenAI 兼容 API，我们掌握其配置。真实可编程 agent，最小胶水即可端到端走完 `submit → poll/callback → ingest`，证明网关是真实的而非 mock。
- **B — 扣子/Coze 无码 bot（workstation 导出/导入 模式）**：owner 手动驱动的闭源不可编程 agent；AIOS 导出任务包 → owner 粘贴进 Coze → 结果贴回 → AIOS 作为未校验 Artifact 导入。证明 workstation 模式对无 API 的 agent 可用，且编排中零 vendor 代码。

两者满足首片 C（同一 AIOS 任务，任一种 adapter，产出相同的已校验 Artifact 契约）。

## 11. 设计评审已决问题

- **Q1. AgentAdapter registry：DB 托管还是仅配置？** → **DB 托管**（`Agent` 表即 registry；owner 可增/启/停，无需改码）。
- **Q2. 回调 vs 轮询默认？** → **按模式强制**，非 adapter 自决：`remote_api`/`a2a` 默认回调（推结果到签名 `callback_url`），不能推则定时轮询 `status()`；`mcp` 同步工具调用（无轮询/回调）；`workstation` 导入即完成（无轮询，owner 驱动的导入事件即终态信号）。
- **Q3. 成本超支？** → **硬阻断**（标记 `BLOCKED` + 通知 owner），软警告被否。

## 12. 实现进度（slices）

| Slice | 内容 | 状态 |
|---|---|---|
| #103 | 首片 A+B+C：Hermes remote_api + Coze workstation adapter + `DelegatedRun` + `run()` 生命周期 + 迁移 `20260719_0001` | 已合并 |
| #104 | 安全/成本加固：secret 边界、审计脱敏、上下文最小权限、预算硬阻断、单信任轴 + 迁移 `20260719_0002` | 已合并 |
| PR #60 | 审计事件枚举(6 类) + 发射；每 agent `timeout_s`/`max_retries` + 迁移 `20260719_0003` | 评审中 |
| #61 | 控制台 agent 管理（register/list/enable-disable）：`aios/agent_registry.py` 服务 + REST `GET/POST /agents`、`PUT /agents/{id}/enabled` + 控制台 `GET/POST /owner/agents*` + 审计 `agent.registered`/`agent.enabled_changed` + 测试 `test_gateway_agent_registry.py` | 已实现（本分支，待评审合并） |
| #62 | 委托结果溯源持久化：`build_delegated_provenance()` + `execution.py` 用持久化 run 重建溯源（修 detached-run 漂移 bug），每结果落 `adapter_id`/`source`/`provenance_json` 未校验 Artifact + 测试 `test_gateway_provenance.py` | 已实现（本分支，待评审合并） |
| 本 ADR | 契约级网关 ADR（10 项契约 + 4 模式 + 已决规则 + 无 vendor 不变量） | 本文件 |

## 13. 后果

- **正向**：AIOS 可作为"个人 AI 公司"编排异构真实 agent；owner 无需改码即可雇佣/停用 agent（DB registry）；所有委托可审计、可溯源、成本硬上限。
- **负向 / 代价**：外部 agent 的可靠性取决于其 API/callback 契约；workstation 模式依赖 owner 手动搬运（非实时）；审计发射点多（discover 每次调用都写一行，best-effort）。
- **不变**：本地 `LLMExecutionAdapter` 行为不变；`Task`/`Approval`/`KnowledgeFact` 结构不变；无通用权限框架。
