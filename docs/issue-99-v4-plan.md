# Issue #99 实施计划 — AIOS 统一 Agent 中台（自动注册实体 + 能力发现，并入 Agent Relay #77）

> 基于最新 `main`（`06fad34`，Alembic head `20260728_0009`，#88 MVP + #92 V2 + #93 V3 均已合并，#40 V1 测量已闭环）。
> 本文件是**实施计划**，不含任何实现代码。仅用于架构评审（Codex + owner）。
> 关联 Issue：#99。前置依赖：#57（Agent Interop Gateway，registry/secret_ref 基础已落地）、#88（MVP + `Agent.platform`/`external_ref` 迁移 `0009`）、#92（V2 采集）、#93（V3 判定）。并入：#77（Agent Relay V1）。

---

## 0. 范围与铁律（来自 Issue #99 与 owner 追加约束）

V4 是 #88/#92/#93 之后的下一阶段，目标把当前**owner 手工 seed 的静态部门 agent 体系**升级为 **AIOS 统一 Agent 中台**：agent 可**自注册**、能力可**发现**、任务可按能力**动态路由**；并把 **#77 Agent Relay** 并入，让 agent（ChatGPT / Codex / WorkBuddy / Hermes / Coze）经中台自报产物/工作日志/报告，owner 不再在多个工具间手工 copy。

**必须遵守的约束（继承自 #88/#92/#93 铁律 + owner 在 #40 评审中强化的可信身份边界）：**

1. **复用主线，不重写**：`Agent` / `Capability` / `AgentCapability` 模型、`agent_registry.py`、`route_task` 能力路由、`authenticate_owner` 全部复用；**工作日志信任链整体复用**——`attest_work_log`（WORK_LOG→APPROVED 唯一路径，永不动）、`WorkLogSubmit` 载荷形状、`Idempotency-Key` 幂等命名空间、UNVERIFIED 初始态一律沿用。`submit_work_log` 因 `#88 §7` 为 owner-only 硬校验且 provenance 绑 DB `ExecutionAssignment`，agent 不能直接走它；relay 用 **`relay_work_log` 这一共享底层创建原语的扩展入口**替代（复用同一 artifact 模型 / 幂等机制，并把 attest 显式委托给复用的 `attest_work_log`），**不另建 ingestion / attest 链路**。V4 **不引入新表**、只含**一个受控 Alembic 迁移**（在 `agent` 表加 `(platform, external_ref)` 部分唯一索引 + `bootstrap_token_ref` claim 列，见 §0.8/§3.3/§7）；不另建 registry 表。
2. **信任边界不变（铁律，#40 评审重申）**：任何 actor **不得在调用点手工构造** `ActorContext`。owner 经 `resolve_owner_actor()`、agent 经 `resolve_agent_actor(agent_id)`（由可信认证依赖解析）。agent 自注册 / relay **只能操作自身实体**（scope 限定 `external_ref` / 自身 `agent_id`），**绝不**伪造 owner 或他 agent。
3. **绝不自动 attest / 绝不自动 APPROVED（#88 铁律）**：relay 只做「未验证」工作日志的入站提交；`attest_work_log` 仍是 WORK_LOG 达 `APPROVED` 的唯一路径，且**仅 owner**（#88 §7）。relay endpoint 不得调用 attest、不得写 `review_status=APPROVED`、不得写 `should_enter_kb=True`。
4. **能力目录单一真相源（fail-closed）**：自注册声明的 capability 必须是 `Capability` 目录中**已存在**的 slug；未知 slug → **422**，绝不凭空造能力（capability 扩目录走既有评审 + `CAPABILITY_TAG_MAP_VERSION` bump 流程，不在 V4 内）。路由/知识投影只读 `AgentCapability` 关系行，自注册必须把这些关系行 upsert 正确，否则该 agent 在路由/知识层「不可见」。
5. **幂等（铁律）**：自注册两路径均幂等但语义不同——① bootstrap（`POST /agents/bootstrap`）**严格单次**：同元组并发 / 同令牌重放 → **401（零副作用、绝不建重）**；② self-update（`PUT /agents/self`，按 `actor.agent_id`）**幂等 upsert**：同 agent 重更新 = 更新同 `id`、不新建重复实体（并发由悲观写锁串行化、聚合级 last-writer-wins，绝不靠 `IntegrityError`）。relay 入站复用 `Idempotency-Key` 头（#88 §5/§6），同键同载荷→200 返回已有、异载荷→409、并发重复→由部分唯一索引 `IntegrityError` 重判，绝不静默丢或建重。
6. **凭据不入库**：agent 认证只经 `secret_ref` 不透明句柄或一次性注册令牌；实际 secret 只在外部分 secret store，绝不写入 `Agent` / `Artifact` / `AuditLog` 载荷；`AuditLog` 快照只记 agent_id / 动作 / 范围，**不记 key**（复用 `redact_secrets`）。
7. **审计可追溯**：每一次成功自注册（**含 bootstrap 严格 CREATE 与 self-update upsert 两条路径**）/ relay ingest 均写 `AuditLog(action="agent.self_registered" | "relay.work_log_ingested", ...)`，沿用 `agent_registry.append_audit` 既有机制；**agent 行不落库的 401 场景（bootstrap 碰撞 / 重放命中 / scope 越权）绝不写审计**（与 Gate B / §3.2/§3.3「碰撞拒绝不写审计」边界一致）。
8. **含一个受控迁移（默认，零新表）**：V4 仅用现有 `Agent` / `Capability` / `AgentCapability` 表，**不新建任何表**；但为保证自注册幂等 upsert 的并发安全（应用层 query-then-insert 存在竞态），**默认包含一个受控 Alembic 迁移**：在 `agent` 表加 `(platform, external_ref)` **部分唯一索引**（WHERE `external_ref IS NOT NULL`）使该键全局唯一，并新增 nullable `bootstrap_token_ref` 列（**无额外索引**，仅记录认领该元组的令牌句柄、承载 bootstrap 令牌 claim，见 §3.2）——元组唯一性已由部分唯一索引保证，此列只为把「令牌↔agent」绑定进同一 DB 事务，使消费原子且可回滚；两者同属这一个受控迁移、零新表。Alembic head 由 `20260728_0009` 前进**恰好一个**新 head（迁移评估见 §7，门禁见 §6 Gate F / §9）。
9. 按 TDD 顺序（§11）：安全依赖 → registry 扩展（upsert + capability 调和 + 目录/发现）→ relay 服务 → 端点 → 测试 → 验收。

---

## 1. 已存在、直接复用的现实（代码已确认，基于 `06fad34`）

| 组件 | 现状（#57/#88） | 本计划用法 |
|------|------|-----------|
| `Agent` 模型（`models.py:238`） | 已含 `platform` / `external_ref` / `capabilities`(JSON) / `status` / `trust_level` / `secret_ref` / `endpoint` / `callback_url` / `enabled` | 自注册 upsert 目标；`platform`+`external_ref` 作幂等键 |
| `Capability`（`models.py:284`） | `id` / `name`(unique) / `description`；seed 时已建目录 | 能力目录单一真相源；未知 slug → 422 |
| `AgentCapability`（`models.py:292`） | `agent_id` / `capability_id` / `priority` / `enabled`；**路由与知识投影只读此关系行** | 自注册须 upsert 这些行（当前 `register_agent` 只写 `Agent.capabilities` JSON，**不写此关系行**——这是 V4 必须补的核心 GAP） |
| `agent_registry.py` | `register_agent`（**owner-only**，每次**新建** Agent，无幂等 upsert）、`list_agents`、`get_agent`、`set_agent_enabled` | V4 扩展：**`create_agent_via_bootstrap`（bootstrap 严格 CREATE 原语：按授权 `(platform,external_ref)` 插入、部分唯一索引原子认领、碰撞即 401、绝不 upsert 补全）** + **`upsert_agent`（self-update 原语，按 `actor.agent_id` 定位既有实体、scope 锁定身份、capability 调和）** + `list_capabilities` + `list_agents(capability=)`；owner 既有 `register_agent` **保留不改** |
| `route_task`（`scheduler.py:114`） | 已实现 `BEST_AVAILABLE`（按 `required_capabilities` 找合格 agent 并 `_rank` 选区）、`PREFERRED_WITH_FALLBACK`；`_assert_required_capabilities` 校验 `AgentCapability` | **直接复用**——只要自注册正确 upsert `AgentCapability`，动态能力路由即生效，无需重写 |
| `WorkLogService.submit_work_log`（`work_log.py:208`） | 创建 `UNVERIFIED` 工作日志；**硬要求 owner actor**（`_assert_owner_actor`）；provenance 绑精确 `ExecutionAssignment` | V4 **行为保持不改**（Gate F）：其 owner-only 校验与 `ExecutionAssignment` 绑定不变；但 V4 将从中**抽取共享内部助手 `_create_unverified_work_log(...)`**（行为保持重构），`submit_work_log` 与新增 `relay_work_log` 均调用该助手——relay 复用同一 UNVERIFIED 创建 / `WorkLogSubmit` schema / `Idempotency-Key` 幂等机制，仅以认证身份替换 provenance（且不放宽 owner 路径）。attest 仍复用 `attest_work_log`（§5.1） |
| `WorkLogService.attest_work_log`（`#88`） | owner-only，WORK_LOG → APPROVED 唯一路径 | **不变**；relay 不调用 |
| `authenticate_owner`（`security.py:120`） | HTTP Basic → `resolve_owner_actor()` | 复用；owner 侧 register/console 不动 |
| `ActorContext` / `resolve_agent_actor`（`actor.py`） | agent actor 解析器已存在 | relay / 自注册认证依赖解析 agent actor |
| `append_audit`（`agent_registry.py`） | registry 既有审计 | 复用记录自注册 / relay |

---

## 2. V4 新增模块与端点（不新建表）

```
src/aios/
├── api/security.py          # 新增 authenticate_agent（secret_ref → resolve_agent_actor）+ authenticate_bootstrap_token（owner 签发作用域令牌 → 解析授权 (platform,external_ref)）
├── agent_registry.py        # 扩展两套语义隔离的原语：create_agent_via_bootstrap（bootstrap 严格 CREATE，碰撞 401、不 upsert 补全）+ upsert_agent（self-update，按 actor.agent_id 定位 + scope 锁定 + capability 调和）；+ list_capabilities + list_agents(capability=)
├── relay.py                 # 新增：relay_work_log（agent-authenticated ingest，复用 WorkLogSubmit 载荷形状）
└── api/app.py               # 新增端点（见 §3/§4/§5）
```

- 不新建任何 SQLModel 表；**含一个受控 Alembic 迁移**（§0.8/§7，在 `agent` 表加 `(platform, external_ref)` 部分唯一索引），Alembic head 由 `20260728_0009` 前进一个单一新 head。
- 端点复用既有 `authenticate_*` 依赖模式；新增 `authenticate_agent` 与 `authenticate_bootstrap_token` 两个依赖（§3.2）。

---

## 3. Agent 自注册契约（idempotent upsert by `external_ref`）

### 3.1 端点（create 与 update 两条路径分离）
- **首次注册 / 引导（create，引导令牌**单次消费**）**：`POST /agents/bootstrap`（**引导令牌认证**，见 §3.2）——agent 凭 owner 签发的**作用域引导令牌**（scoped bootstrap token，owner **签名**的自描述不透明令牌，含授权 `(platform, external_ref)` / 过期时间 / 唯一 `jti` 句柄，仅授权注册**恰好一个** `(platform, external_ref)` 身份）创建自身实体；无需已存在的 `agent_id`。**该令牌严格单次有效（按令牌，DB `bootstrap_token_ref` 列为准）**：令牌本身**无外部状态表**——其「已签发」有效性由 owner 签名 + 未过期 + scope 匹配一次性密码学验证（见 §3.2）；其「已消费」状态 = `agent` 表中是否存在 `bootstrap_token_ref = 该令牌 jti` 的行（即认领该元组的 agent 实体是否落库），**与 agent 创建在同一 DB 事务内原子提交、失败整体回滚，无分布式事务 / 无补偿 / 无外部 store**。已消费（该行已存在）的令牌重放 → 校验即见 `bootstrap_token_ref` 命中 → **401（认领步骤拒绝，且不重发凭证）**；元组已被他令牌 / 并发胜方认领 → 插入命中部分唯一索引 → **401（零副作用）**。bootstrap 成功时**一次性下发**该 agent 的后续自更新凭证（bearer secret），响应仅此一次返回；DB 仅持久化其外部句柄 `secret_ref`（绝不存明文凭证）。
- **持续自更新（update）**：`PUT /agents/self`（**agent 自身凭证认证**，见 §3.2）——已注册 agent 凭 bootstrap 一次性下发的自身凭证刷新实体；upsert 目标**一律按 `actor.agent_id` 定位**，请求体不得覆盖身份。若 agent 遗失凭证，由 owner 经 owner 认证的管理端点 `POST /agents/{id}/rotate-credential`（**owner-only**）直接为其轮换凭证，**不走 bootstrap**（bootstrap 严格单次：已 `consumed` 令牌恒 401，凭证仅创建时下发一次）；绝不复用旧令牌。
- 请求体（schema 新增 `AgentSelfRegister`）：`platform`、`external_ref`（必填，组成幂等键）、`name`、`role`、`adapter_type`、`delegation_mode?`、`capabilities`（capability slug 列表）、`endpoint?`、`callback_url?`、`config_ref?`、`limitations?`、`timeout_s?`、`max_retries?`。
- 响应：**专用 `AgentRegistrationResponse` DTO（不含 `secret_ref`）**——仅含安全字段（`id` / `platform` / `status` / 解析后的 `capabilities` / `name` 等调度可见字段）；bootstrap 响应**额外一次性**返回 `credential`（agent 后续自更新凭证明文，仅出现一次）。`secret_ref` 是外部 secret-store 句柄，DB 中持久化但**绝不进任何响应模型 / `AuditLog`**（§0.6 / §3.2 凭据隔离）——与一次性 `credential` 明文严格区分：`credential` 是唯一对外下发的机密，且只在此一次性响应中出现，绝不复用 / 绝不落库明文。

### 3.2 信任边界（Gate A）—— bootstrap 与 agent-auth 双通道
- **create 通道（bootstrap）**：`POST /agents/bootstrap` 经 `authenticate_bootstrap_token` 依赖。
  - **令牌校验（密码学 + DB 只读，无外部状态表）**：`authenticate_bootstrap_token` 按顺序验证引导令牌——① **签名验证**：用 owner 方签名密钥验签（令牌为 owner 签名的自描述 JSON：`{scope:(platform,external_ref), exp, jti}`），签名无效 → **401**；② **过期检查**：`exp` 已过 → **401**；③ **scope 匹配**：令牌授权的 `(platform, external_ref)` 须与请求体元组**完全一致**，越权 → **401**（scope 由令牌而非请求体决定）；④ **单次消费检查（DB 只读）**：`SELECT 1 FROM agent WHERE bootstrap_token_ref = :jti`——若已存在该行（令牌已被消费、agent 已落库）→ **401（认领步骤即拒绝，且不重发凭证）**。校验全程**不写**任何外部 store；令牌「已签发」有效性纯由密码学保证、「已消费」状态纯由 DB 行存在性保证（无外部 `issued`/`consumed` 状态表）。
  - **原子认领 + 创建（单 DB 事务，令牌 claim 与 agent 同行提交）**：在**同一 DB 事务**内按授权元组插入 `Agent` 行（含 `status=active` 与 `bootstrap_token_ref = :jti`，初始 `secret_ref` 占位），**`bootstrap_token_ref` 与 agent 行同 INSERT 提交**——令牌「已消费」即该行落库，二者**天然同一事务、原子提交、失败整体回滚，无分布式事务、无补偿、无外部 store**（直接满足「same-DB-commit 原子 claim + rollback」语义）。`(platform, external_ref)` 部分唯一索引保证元组原子认领——并发同元组插入**仅一个成功**，其余命中唯一索引 → **401（零副作用，绝不进入置备）**。提交成功 = 令牌持久 `consumed`（行已落库、列已置 jti）；即使 agent 日后被吊销 / 软删，只要行保留，`bootstrap_token_ref` 仍命中 → 令牌**持久不可复用**（满足严格单次）。
  - **置备 + 一次性下发（同一事务提交）**：在事务提交前生成 agent 自更新凭证（bearer secret）、将其外部句柄写入 `agent.secret_ref`（明文凭证仅一次性随响应返回，绝不入 DB），随后**随同一事务提交**。凭据下发 / DB 提交任一失败 → 整事务**回滚**（agent 行不落库、`bootstrap_token_ref` 不置 → 令牌视为未消费）、客户端可安全重试；仅当凭证成功生成、DB 事务成功提交、响应即将返回时才算 bootstrap 成功（**注：`secret_ref` 指向外部凭证 store，属「机密明文不入库」铁律 §0.6 的既有模式；其补偿仅针对「外部写成功但 DB 回滚」的孤儿凭证清理，与令牌 claim 的 DB 内原子性互不耦合**）。
  - **消费语义（严格一次性，DB `bootstrap_token_ref` 为准）**：令牌「已消费」= `agent` 表存在 `bootstrap_token_ref = 该 jti` 的行（在 bootstrap 成功提交时随 agent 行原子写入）。此后同令牌重放 → 校验步骤④即见命中 → **401 且绝不再次返回凭证**（响应仅此一次）。元组已被他令牌 / 并发胜方认领时，插入命中部分唯一索引 → 401（零副作用）。多令牌绑定同一元组时（`bootstrap_token_ref` 各不同），仅胜方令牌所在行落库（其 jti 被消费）、其余令牌保持「未消费」但因元组已占而恒 401（无需额外索引，元组部分唯一索引已保证）。`bootstrap_token_ref` 列**无唯一索引**（§0.8），其一致性由「令牌↔元组一一对应 + 元组部分唯一索引」间接保证。
  - **失败回滚（无补偿）**：凡在 agent 行成功提交**之前**的任何失败（校验除外、凭据分发、**DB 提交失败**）→ **整事务回滚**，agent 行不落库、`bootstrap_token_ref` 不置 → 令牌**视为未消费**、客户端可安全重试，**不烧令牌**（无需任何外部状态补偿——令牌消费状态本就随 agent 行原子落库 / 回滚）。仅有的补偿是「外部凭证 store 写成功但 DB 回滚」时清理 `secret_ref` 孤儿条目（见上 bullet，与令牌 claim 解耦）。
  - **投递失败恢复（独立流程，非 bootstrap 重放）**：若事务已提交（agent 已建、`bootstrap_token_ref` 已置）但 HTTP 响应投递失败致客户端未收到凭证，客户端重试同令牌 → 校验步骤④即见命中 → **401 且不重发凭证**（遵守一次性）；该 agent 的凭证恢复走 **owner 轮换流程**（§3.1），不通过 bootstrap 重放获取。
  - **凭据隔离**：`secret_ref` 仅是外部 store 句柄，`authenticate_agent` 凭 agent 出示的 bearer 凭证经外部 store 解析到 `agent_id` 并加载自身；DB 的 `secret_ref` **绝不进任何响应模型 / `AuditLog`**（仅 `AgentRegistrationResponse` 这类不含 `secret_ref` 的 DTO 对外）；一次性下发的 `credential` 明文**仅出现在 bootstrap 一次性响应**、**绝不入 `AuditLog`**——二者严格区分，凭据隔离边界统一（§0.6）。
- **update 通道（agent-auth）**：`PUT /agents/self` 经 `authenticate_agent` 依赖——agent 凭自身 `secret_ref` 解析的凭证认证，依赖内部解析出 `agent_id` 并调用 `resolve_agent_actor(agent_id)`。upsert 目标**仅按 `actor.agent_id` 加载**；请求体若夹带与自身 `(platform, external_ref)` 不一致的元组、或任何 `agent_id` / `owner_id` → **422/403 拒绝**（self-only scope 由服务端 actor 强制，非客户端声明）。
- 两通道共用铁律：`ActorContext` 一律由依赖解析，**调用点绝不手工构造**（#40 评审铁律）；agent 永远只能触达自身实体。

### 3.3 幂等 upsert 与并发安全（Gate B，铁律）
- **DB 唯一约束是默认实现（非可选退化）**：V4 含**一个**受控 Alembic 迁移，在 `agent` 表加 `(platform, external_ref)` **部分唯一索引**（WHERE `external_ref IS NOT NULL`），使 `(platform, external_ref)` 全局唯一——这是注册「并发同元组仅一个被创建」的硬保证，也是降级 fail-closed 的兜底（见 §3.2）。
- **首次注册并发（bootstrap，`POST /agents/bootstrap`，严格单次）**：两条并发同元组 bootstrap 均「未命中」→ 仅一条插入成功（部分唯一索引原子认领）并置 `bootstrap_token_ref`=jti（§3.2，随 agent 行同事务原子提交）；其余命中唯一索引 → **401（零副作用，绝不进入置备）**（§3.2）。最终仅一个 `Agent` 实体、反映胜方完整载荷；败方收到显式 **401（fail-closed，零副作用）**，**非静默丢弃**（显式 401 即非空响应、非吞没；碰撞拒绝不写审计，避免与即将因 `IntegrityError` 回滚的 INSERT 同事务纠缠，Gate B + §3.2）。此路径不进入「重判补完」——bootstrap 是严格单次认领，败方被拒、不写实体。
- **持续自更新并发（`PUT /agents/self`，reload 补完）**：目标恒为 `actor.agent_id` 已存在行，**不涉及元组创建**。读-改-写聚合须在**同一 DB 事务内以悲观写锁（`BEGIN IMMEDIATE` / `SELECT ... FOR UPDATE` 依引擎）串行化**——并发同 agent 更新进入临界区后被串行化，胜出事务在锁内完成「实体标量字段 + `Agent.capabilities` JSON + `AgentCapability` 关系行增删」的**整体调和并提交**，其余事务在其后提交并整体覆盖为各自完整载荷；两条并发同 agent 更新、载荷可区分（如 A: name=α / caps=[positioning]；B: name=β / caps=[xhs_adaptation]）→ 均落到同一行；最终实体须完整反映**最后提交方**的完整载荷（name 与 caps 一致，**非 A 的 name + B 的 caps 混合**、非静默丢弃某次合法更新），`AgentCapability` 与最后提交方精确对应、无残留——**行锁 / 悲观写锁保证聚合级 last-writer-wins，绝不靠 `IntegrityError` 触发（标量字段更新不会命中唯一索引冲突）**。绝不抛错、绝不建重、**绝不静默丢弃一次合法更新**（Gate B + §3.3）。
- 每次成功自注册（含 bootstrap CREATE 与 self-update upsert 两条路径）写 `AuditLog(action="agent.self_registered", after={...变更摘要..., upserted: bool})`：`create_agent_via_bootstrap`（bootstrap 成功认领落库）写 `upserted=False`、`upsert_agent`（self-update 按 `actor.agent_id`）写 `upserted=True`；审计随各自事务原子提交（Gate E + §0.7）。**碰撞败方 / 重放命中 / scope 越权等 agent 行不落库的 401 场景绝不写审计**（Gate B + §3.2/§3.3）。

### 3.4 能力调和（Gate C，核心 GAP 修复）
- 对请求 `capabilities` 中每个 slug：`SELECT Capability WHERE name=slug`；不存在 → **422**（fail-closed，绝不造能力）。
- upsert 对应的 `AgentCapability(agent_id, capability_id)`，按 `priority=50`、`enabled=True`（或请求可带 per-cap priority）；删除该 agent 不再声明、但曾存在的 `AgentCapability` 行（保持关系与 `Agent.capabilities` JSON 同步）。
- **这是让动态路由/知识投影「看见」自注册 agent 的关键**——只写 `Agent.capabilities` JSON 而不同步 `AgentCapability` 关系行，会使该 agent 在 `route_task` / `_assert_required_capabilities` 中「不可见」。

---

## 4. 能力目录与按能力发现

### 4.1 端点
- `GET /capabilities`（owner 或 agent 可读）→ 返回 `Capability` 目录（`name` / `description`）。
- `GET /agents?capability=<slug>`（复用既有 `GET /agents`，新增可选 `capability` 查询参数）→ 仅返回**启用且声明该能力**的 agent（经 `AgentCapability` 关系连接 + `enabled=True` 过滤）。未传参 → 维持既有全量列表行为。

### 4.2 契约
- `capability` 参数非目录 slug → **422**（与 §3.4 同源 fail-closed）。
- 发现结果不含 `secret_ref` 明文 / 凭据；仅暴露调度所需字段（id / name / platform / capabilities / status / trust_level）。

---

## 5. Agent Relay（并入 #77）— agent-authenticated 入站通道

### 5.1 端点
- `POST /relay/work-logs`（agent-authenticated，复用 `WorkLogSubmit` 载荷形状 + `Idempotency-Key` 头）→ 入站提交一条 `UNVERIFIED` 工作日志。**relay 复用 `WorkLogService` 的工作日志信任链**：V4 从 `submit_work_log` 抽取共享内部助手 `_create_unverified_work_log(...)`（行为保持重构，`submit_work_log` 外部契约不变，仍 owner-only + 绑 `ExecutionAssignment`），relay 的 `relay_work_log` 与 `submit_work_log` **共用该助手**完成 UNVERIFIED artifact 创建与 `Idempotency-Key` 幂等落库（助手接收 `actor`、据认证身份派生幂等**作用域** `scope`，并保持 #88 向后兼容：owner 通道 `scope=None` → 存储键维持 #88 既有 `work_log:<project_id>:<sha256(key)[:32]>`（预 V4 记录 replay 正确、Gate F 回归成立）；relay 通道 `scope="agent:<agent_id>"` → 存储键 = `work_log:<project_id>:agent:<agent_id>:<sha256(key)[:32]>`。两格式因 `:agent:` 字面段**结构互斥**，故 owner 与 agent 复用同键也互不收敛、互不暴露他方响应（见 §5.3 / Gate D））；`attest_work_log`（WORK_LOG→APPROVED 唯一路径）亦复用、relay 不调用。relay 是该助手的**第二个调用方**（agent-authenticated 入口），非平行链路、非重写创建 / attest 逻辑。
- **复用边界（消除歧义）**：`submit_work_log` 的 owner-only 校验与 `ExecutionAssignment` 绑定**保持不改**（Gate F）；relay 因 agent 在 AIOS 编排外自产报告、往往无 DB `ExecutionAssignment`，故 `relay_work_log` 跳过该绑定、改以「认证 agent id + `source_platform` + 可选外部 run ref」作为 provenance（§5.2，这是 #88 §6 约束的有据放宽，且仅影响 relay 路径，绝不改动 `submit_work_log`）。
- 复用既有 `WorkLogSubmit` schema（7 汇报字段 + `project_id` + `source_platform` + `content_value?` + `should_enter_kb?` + `content_angle?`），**不新增字段**。

### 5.2 信任边界与 provenance（Gate D）
- 经 `authenticate_agent` 解析 `agent_id`；`produced_by_agent_id` **一律取自认证身份**（禁止请求体覆盖成他 agent / owner），满足「provenance 由认证推导、绝不来自请求体」的 #40 边界。
- relay 复用 `submit_work_log` 的同一 UNVERIFIED 创建路径（经共享助手 `_create_unverified_work_log`，artifact 模型 + 幂等机制），仅以认证身份替换 provenance 来源；**attest 仍委托复用 `attest_work_log`**，relay 不持有也不调用 attest——信任链与 #88 铁律零妥协。
- **放松 #88 §6 的「必须绑精确 ExecutionAssignment」约束**：relay 承载的是 agent 在 AIOS 编排之外自产的报告（ChatGPT/Codex 等），往往没有 DB `ExecutionAssignment`。relay 以「认证 agent id + `source_platform` + 可选外部 run ref」作为 provenance，而非 DB ExecutionAssignment；这是消除 owner 手工搬运的必要放宽，且 provenance 仍真实可追溯（指向具体 agent + 平台）。
- **绝不**调用 `attest_work_log`、绝不写 `review_status=APPROVED`、绝不写 `should_enter_kb=True`（继承 #88/#93 默认拒绝；KB 资格只由 owner 在 attest 人工裁定）。

### 5.3 幂等（Gate B 同源 + Gate D 作用域隔离 + #88 向后兼容）
- 复用 `Idempotency-Key` 头 + **按认证身份作用域隔离**的命名空间存储键机制（同 #88 §5，但引入作用域段且仅作用于 relay 通道）：
  - **owner 通道（`submit_work_log`，`POST /work-logs`）**：`scope=None`，存储键**维持 #88 既有格式** `work_log:<project_id>:<sha256(key)[:32]>`——预 V4 已落库的记录 replay 仍能命中、不重复建；`Idempotency-Key` 头契约与重放行为不变，#88 既有 `submit_work_log` 单测全绿（Gate F 回归）。
  - **relay 通道（`relay_work_log`，`POST /relay/work-logs`）**：`scope="agent:<agent_id>"`，存储键 = `work_log:<project_id>:agent:<agent_id>:<sha256(key)[:32]>`。**同 agent 同键同载荷→200 返回已有、异载荷→409、并发重复→部分唯一索引 `IntegrityError` 重判**（同 #88 §5 语义保留）。
  - **结构互斥保证跨身份不收敛**：relay 键含字面 `:agent:` 段，与 owner 既有键（`<project_id>` 后紧跟 32 位 hex）**结构不可相等**，故「owner 提交」与「某 agent relay」复用同项目同键也各自落独立日志、互不暴露他方响应；不同 agent 之间（`agent_id` 不同）同样隔离。**这是对 #88 §5「共享跨来源命名空间」的修正，以捍卫 Gate D「provenance 由认证推导、幂等命名空间不得跨认证身份共享」的边界**。
- **实现要点**：`storage_idempotency_key(project_id, client_key, scope=None)` 增加可选 `scope` 参数；`submit_work_log` 传 `scope=None`（零行为变更），`relay_work_log` 传 `scope="agent:<agent_id>"`（按认证 actor 派生）。仅扩展键前缀，不改 `Idempotency-Key` 头契约。

### 5.4 与既有采集（#92）关系
- `#92 collectors` = **主动采集端**（AIOS 脚本去拉各平台目录）；relay = **中台侧接收端**（agent 主动推）。二者互补、互不替代；relay 入站日志与 collectors 入站日志在 `source_platform` / `metadata_json` 上同构，下游收割/feed 无需改动。

---

## 6. 六道门禁（owner 显式要求，逐门定义 + 测试）

### Gate A — 身份门（agent 不得伪造 owner / 他 agent）
- 自注册 / relay 端点一律经可信认证依赖；`ActorContext` 由依赖解析（`resolve_agent_actor` / `resolve_owner_actor`），**调用点禁止手工构造**（#40 铁律）。
- create 通道（`POST /agents/bootstrap`）经 `authenticate_bootstrap_token`：scope 由令牌绑定的 `(platform, external_ref)` 决定（非请求体），越权元组 → 401。update 通道（`PUT /agents/self`）经 `authenticate_agent`：upsert 目标一律按 `actor.agent_id` 加载，请求体身份元组 mismatch → 422/403。relay 的 `produced_by_agent_id` 取自认证身份，请求体覆盖 → 422/403。
- 测试：
  - `test_self_register_cannot_impersonate`：`PUT /agents/self` 请求体夹带他 `external_ref` / `agent_id` → **422/403 拒绝且零写入**（Gate A）。
  - `test_relay_provenance_derived_from_auth`：`POST /relay/work-logs` 请求体若夹带 `produced_by_agent_id` 与他/owner id → **422/403 拒绝**；正常请求落库 `produced_by_agent_id` = 认证 agent（Gate A/D）。
  - `test_bootstrap_token_single_use`：首次 `POST /agents/bootstrap` 成功（建 agent 行 + 置 `bootstrap_token_ref`=jti（DB 内原子消费）+ 一次性下发凭证）；同令牌重放 → 校验即见 `bootstrap_token_ref` 命中 → **401 且不重发凭证**（严格单次）；agent 置 `REVOKED`（行保留）后同令牌仍 401（令牌消费持久、复用不可）；并发重放仅一个插入成功、其余 401（Gate A）。
  - `test_bootstrap_token_failure_recoverable`：覆盖三类提交前失败——① 凭据下发 / 外部写失败；② **外部凭证 store 写成功但 DB 提交失败**（断言 agent 行回滚、`bootstrap_token_ref` 未置 → 令牌视为未消费，且外部 store 中 `secret_ref` 条目被补偿删除、无孤儿可用凭证）；③ 提交后 HTTP 投递失败——前两类 → 事务回滚、agent 行不留存（`bootstrap_token_ref` 不置、令牌视为未消费）、同令牌重试重建并下发、**不烧令牌**；第三类 → 命中 active 行 **401 且不重发凭证**（恢复走 owner 轮换，非 bootstrap）（Gate A）。
  - `test_owner_rotate_credential`：owner 经 owner 认证对「遗失凭证的 active agent」调用 `POST /agents/{id}/rotate-credential` → 签发新凭证（旧凭证失效），**不经 bootstrap**（Gate A + §3.1）。
  - `test_bootstrap_token_no_secret_leak`：bootstrap 响应仅一次性含 `credential`；`Agent` 响应与所有 `AuditLog` 中**不含** `secret_ref` 明文 / 凭证 / `nvapi-` / `sk-` 前缀（Gate A + §0.6）。

### Gate B — 自注册 / relay 幂等门
- **两注册子路径语义不同（消除 Gate B / §3.3 矛盾）**：自注册含「首次注册（bootstrap, `POST /agents/bootstrap`，**严格单次 CREATE**）」与「持续自更新（`PUT /agents/self`，按 `actor.agent_id` 的 **upsert 补全**）」两条**语义不同**的路径，不得混为一谈：
  - **bootstrap CREATE 路径（`POST /agents/bootstrap`）**：严格单次认领——同元组并发 / 同令牌重放 → 败方 **401（fail-closed，零副作用、不写审计，绝不进入置备、绝不重判补全）**（§3.2/§3.3）；此路径**不**适用「update 同 id / IntegrityError 重判补完」。
  - **self-update upsert 路径（`PUT /agents/self`）**：目标恒为 `actor.agent_id` 已存在行（不涉及元组创建竞态），并发更新由**悲观写锁（`BEGIN IMMEDIATE` / `SELECT ... FOR UPDATE` 依引擎）串行化**——在锁内完成「实体标量字段 + `Agent.capabilities` JSON + `AgentCapability` 关系行增删」的**整体调和并提交**，保证聚合级 last-writer-wins（**绝不靠 `IntegrityError` 触发**，标量字段更新不会命中唯一索引冲突）；**不静默丢弃一次合法更新**（§3.3）——此即「同 agent 重更新 → 更新同 `id`、不新建重复实体」的适用场景。
- relay 同 `Idempotency-Key` 同载荷 → 200 返回已有；异载荷 → 409；并发 → `IntegrityError` 重判不丢。
- 测试：`test_self_update_keeps_same_id`：同一认证 agent 连续两次 `PUT /agents/self`（可变字段 / caps 不同）→ `Agent.id` 不变、字段更新、`AgentCapability` 行数不膨胀（证明 self-update 幂等 upsert、非新建重复实体，Gate B/C）；`test_relay_idempotent_replay`：同键同载荷二次 → 200 + 单条日志；`test_relay_idempotent_conflict`：同键异载荷 → 409。**并发**：`test_bootstrap_concurrent_collision_rejected`：并发两个同元组 `POST /agents/bootstrap`（均持合法引导令牌）→ 仅创建**一个** `Agent` 实体（无重复、无 500）；胜方反映其完整载荷（name+caps 一致）；败方收到显式 **401**（fail-closed、不建实体、非静默丢弃、绝不重判补全）（bootstrap 严格单次，Gate B + §3.2/§3.3）；`test_self_update_concurrent_same_agent`：同一认证 agent 并发两次 `PUT /agents/self`、载荷可区分（A: name=α / caps=[positioning]；B: name=β / caps=[xhs_adaptation]）→ 仅一个 `Agent`；最终实体完整反映**最后写入方**的完整载荷（name+caps 一致，非混合、非静默丢弃），`AgentCapability` 与该方精确对应、无残留（证明 §3.3 并发自更新走「reload 后补完更新」而非丢弃）（Gate B + §3.3）；`test_relay_concurrent_idempotent`：并发 N 个同键同载荷 relay（**同一**认证 agent）→ 仅创建**一条** `UNVERIFIED` 日志（并发命中部分唯一索引 `IntegrityError` 重判收敛到同一行），无重复、无 500（Gate B）；`test_relay_idempotency_scoped_per_actor`：agent A 以键 K+载荷 P 提交→201（日志 `produced_by_agent_id`=A）；agent B 以**相同键 K + 相同载荷 P** 提交→**201 且为 B 的独立日志**（非 A 的 200 重放），其 `produced_by_agent_id`=B、绝不返回 A 的响应（证明幂等命名空间按认证身份作用域隔离）；owner `POST /work-logs` 与某 agent 同项目同键→同样各自独立、互不收敛（Gate D + §5.3）。

### Gate C — 能力目录门（未知 capability → 422）
- 自注册声明不在 `Capability` 目录的 slug → **422**，绝不造能力；仅 upsert 已存在的 `AgentCapability` 关系行（并使 `Agent.capabilities` JSON 与关系行同步）。
- 测试：`test_self_register_unknown_capability_422`；`test_self_register_syncs_agentcapability`：声明 `[positioning, xhs_adaptation]` → `AgentCapability` 出现这两行、`route_task` BEST_AVAILABLE 能借此把任务路由到该 agent（端到端证据：`test_self_registered_agent_routable`）。

### Gate D — Relay 归属门（provenance 不可伪造）
- relay 入站日志 `produced_by_agent_id` = 认证 agent；`review_status` 恒 `UNVERIFIED`；`should_enter_kb` 恒 `False`；绝不调 attest。
- relay 幂等命名空间按认证身份作用域隔离（§5.3）：同 `Idempotency-Key` 跨不同 agent / owner↔agent **不收敛**，杜绝把一方已建日志的响应返回给另一方（Gate D 与 #40 provenance 不可伪造同源）。
- 测试：`test_relay_never_attests`：relay 后 `review_status != APPROVED`、`should_enter_kb=False`；`test_relay_requires_auth`：缺失/错误 agent 凭证 → 401。

### Gate E — 审计门（注册 / relay 均可审计）
- 每次成功自注册均写 `AuditLog(action="agent.self_registered", after={upserted, agent_id, capabilities, platform, external_ref})`——**两条路径一致**：bootstrap CREATE（`POST /agents/bootstrap`，成功认领落库与 `bootstrap_token_ref` 同事务提交）写 `upserted=False`；self-update upsert（`PUT /agents/self`，按 `actor.agent_id` 更新）写 `upserted=True`。审计在**创建 / 更新事务内**随 agent 行原子提交，与 §0.7 审计铁律对齐。**注意边界**：bootstrap **碰撞败方 / 重放命中 / scope 越权**等 **401（fail-closed、零副作用、agent 行不落库）的场景绝不写审计**（§3.2/§3.3/Gate B 已锁定「碰撞拒绝不写审计」），审计只覆盖「agent 行成功提交」的成功路径。每次 relay ingest 写 `AuditLog(action="relay.work_log_ingested", after={artifact_id, agent_id, source_platform})`；快照只记非敏感范围/身份信息、**绝不记 key**（复用 `redact_secrets`）。
- 测试：`test_bootstrap_creates_audited`：成功 `POST /agents/bootstrap`（建 agent 行 + 置 `bootstrap_token_ref` 同事务提交）后断言 `AuditLog` 含 `action="agent.self_registered"` 且 `upserted=False`、无 `secret`/`nvapi-`/`sk-` 前缀（bootstrap CREATE 路径纳入审计门，Gate E + §0.7）；`test_self_register_audit_logged`（断言 self-update upsert 写 `agent.self_registered` 且 `upserted=True`）；`test_relay_ingest_audit_logged`：断言 `AuditLog` 含上述 action 且无 `secret`/`nvapi-`/`sk-` 前缀。

### Gate F — 受控单迁移 / 零新表 / attest 不变门
- V4 **不新建表**，但**含一个受控 Alembic 迁移**（§0.8/§7）：在 `agent` 表加 `(platform, external_ref)` **部分唯一索引**，**并同迁移加 nullable `bootstrap_token_ref` 列（无第二索引）**——两者同属此一个受控迁移、零新表、head 恰好前进一个新 head（非 `20260728_0009`），且须由测试断言（见 §9）。`attest_work_log` 路径、`owner_approve_review` 守卫、`submit_work_log` 的 owner 校验与 `ExecutionAssignment` 绑定**一律不动**（V4 仅做行为保持的内部助手抽取 `_create_unverified_work_log` 供 relay 复用，不改其外部契约）。
- 测试：`test_no_new_tables`：断言 `Agent`/`Capability`/`AgentCapability` 复用、无新 `SQLModel(table=True)`；`test_alembic_single_new_head`：断言 head 恰好前进一个新 head（非 `20260728_0009`）；`test_migration_adds_bootstrap_token_ref_no_second_index`：断言该受控迁移**仅**新增 `(platform,external_ref)` 部分唯一索引 + 一个 nullable `bootstrap_token_ref` 列、**无其他索引 / 无新表**（锁定 §0.8 迁移边界，防回归遗漏核心安全不变量）；`test_attest_path_untouched`：既有 attest 单测全绿无回归。

---

## 7. 存储与迁移（§0.8）

- **受控单迁移（默认）**：为保证自注册并发安全，`V4` 默认包含**一个** Alembic 迁移，在 `agent` 表加 `(platform, external_ref)` **partial unique index**（WHERE `external_ref IS NOT NULL`），使该键全局唯一——这是 bootstrap CREATE 路径「并发同元组仅一个被创建」的数据库层硬保证（Codex P1：索引须为默认实现而非可选退化）；self-update 路径另以悲观写锁串行化（§3.3）。**同迁移**另加 nullable `bootstrap_token_ref` 列（无第二索引，仅承载 bootstrap 令牌 claim，使令牌消费与 agent 行同事务原子提交，见 §0.8/§3.2）。两者同属**这一个**受控迁移、零新表。
- `AgentCapability` upsert 用 `(agent_id, capability_id)` 主键天然幂等（已含唯一约束），无需额外索引。
- 迁移落地须同步：
  - `alembic` head 由 `20260728_0009` 前进**恰好一个**新 head；
  - bump `tests/test_knowledge_models.py` 与 `tests/test_review_binding_migration.py` 的 `HEAD` 常量（既有约定）；
  - §9 验收与 Gate F 测试断言 head 为单一新 head。

---

## 8. 测试清单（TDD）

- `test_authenticate_agent_resolves_actor`：合法 agent 令牌 → `resolve_agent_actor` 产出正确 `agent_id`；错误令牌 → 401（Gate A）。
- `test_bootstrap_token_registers_scoped_identity`：合法作用域引导令牌 → `POST /agents/bootstrap` 创建令牌绑定的**恰好一个** `(platform, external_ref)` 实体并一次性下发凭证，返回其 `Agent.id`（Gate A）；`test_bootstrap_token_rejects_wrong_tuple`：令牌绑定 `(p1,r1)` 却请求 `(p2,r2)` → 401（scope 由令牌而非请求体决定）；`test_bootstrap_token_single_use`：首次 `POST /agents/bootstrap` 成功（建 agent 行 + 置 `bootstrap_token_ref`=jti（DB 内原子消费）+ 一次性下发凭证）；同令牌重放 → **401 且不重发凭证**（严格单次）；agent 置 `REVOKED`（行保留）后同令牌仍 401（令牌消费持久）；并发重放仅一个插入成功（Gate A）；`test_bootstrap_token_failure_recoverable`：覆盖凭据下发失败 / **外部凭证 store 写成功但 DB 提交失败（断言 agent 行回滚、`bootstrap_token_ref` 未置 → 令牌视为未消费，且外部 store 中 `secret_ref` 条目被补偿删除、无孤儿凭证）** / HTTP 投递失败三类；前两类→回滚无 agent 行（`bootstrap_token_ref` 不置、令牌视为未消费）、同令牌重试重建下发、不烧令牌；第三类→401 不重发（恢复走 owner 轮换）（Gate A）；`test_owner_rotate_credential`：owner 对遗失凭证的 active agent 调 `POST /agents/{id}/rotate-credential` 签发新凭证、旧失效、不经 bootstrap（Gate A）；`test_bootstrap_token_no_secret_leak`：响应仅一次性含 `credential`，`Agent` 响应与 `AuditLog` 无 `secret_ref` 明文 / 凭证 / `nvapi-` / `sk-` 前缀（Gate A + §0.6）。
- `test_bootstrap_creates_agent_and_capabilities`：合法作用域引导令牌首次 `POST /agents/bootstrap` → 新建 `Agent` + 对应 `AgentCapability` 行（bootstrap 严格 CREATE，Gate B/C）；`test_self_update_keeps_same_id`：同一认证 agent 连续两次 `PUT /agents/self`（可变字段 / caps 不同）→ `Agent.id` 不变、字段更新、`AgentCapability` 行数不膨胀（self-update 幂等 upsert、非新建重复实体，Gate B/C）；二者分离证明 bootstrap 只 CREATE、self-update 只 upsert，互不承担对方职责。
- `test_self_register_unknown_capability_422`：未知 slug → 422（Gate C）。
- `test_self_registered_agent_routable`：声明 capability 后，构造 `required_capabilities=[该cap]` 的 READY 任务，`route_task(BEST_AVAILABLE)` 选中该自注册 agent（端到端证明「注册→可见→可路由」）。
- `test_self_register_scope_locked`：`PUT /agents/self` 认证 agent A，请求体夹带 agent B 的 `external_ref`/`agent_id` → **422/403 拒绝且零写入**（A 与 B 均不被修改；不得静默忽略冒充企图，也不得改写 A）（Gate A）。
- `test_list_capabilities_returns_catalog`：`GET /capabilities` 返回既有目录。
- `test_list_agents_by_capability_filters`：`GET /agents?capability=positioning` 仅返回启用且声明该能力者；未知 slug → 422。
- `test_relay_requires_auth`：无/错凭证 → 401（Gate D）。
- `test_relay_ingests_unverified_with_idempotency`：合法 agent → 201 建 `UNVERIFIED` 日志；同键同载荷二次 → 200；异载荷 → 409（Gate B/D）。
- `test_bootstrap_concurrent_collision_rejected`：并发两个同元组 `POST /agents/bootstrap`（合法令牌）→ 仅一个 `Agent`；胜方反映完整载荷（name+caps 一致）；败方显式 **401**（fail-closed、不建实体、非静默丢弃、绝不重判补全，碰撞拒绝不写审计）（bootstrap 严格单次，Gate B + §3.2/§3.3）。
- `test_self_update_concurrent_same_agent`：同一认证 agent 并发两次 `PUT /agents/self`、载荷可区分（A: name=α / caps=[positioning]；B: name=β / caps=[xhs_adaptation]）→ 仅一个 `Agent`；最终实体完整反映**最后写入方**的完整载荷（name+caps 一致，非混合、非静默丢弃），`AgentCapability` 与该方精确对应、无残留（证明 §3.3 并发自更新 reload 后补完更新而非静默丢弃）（Gate B + §3.3）。
- `test_relay_concurrent_idempotent`：并发 N 个同 `Idempotency-Key` 同载荷 relay（**同一**认证 agent）→ 仅一条 `UNVERIFIED` 日志（并发 `IntegrityError` 重判收敛到同一行），无重复、无 500（Gate B）。
- `test_relay_idempotency_scoped_per_actor`：agent A 以键 K+载荷 P→201（`produced_by_agent_id`=A）；agent B 同键同载荷→**201 独立日志**（非 A 的 200 重放），`produced_by_agent_id`=B、不返回 A 响应；owner 通道与 agent 同项目同键→各自独立、互不收敛（证明幂等按认证身份作用域隔离，Gate D + §5.3）。
- `test_relay_provenance_derived_from_auth`：`POST /relay/work-logs` 请求体夹带 `produced_by_agent_id` 与他/owner id → **422/403 拒绝**；正常请求落库 `produced_by_agent_id` = 认证 agent（Gate A/D）。
- `test_relay_never_attests`：relay 后 `review_status=UNVERIFIED`、`should_enter_kb=False`（Gate D + #88 铁律）。
- `test_relay_audit_logged`：ingest 写 `AuditLog(action="relay.work_log_ingested")` 含 agent_id/source_platform、**无 key**（Gate E）。
- `test_bootstrap_creates_audited`：成功 `POST /agents/bootstrap`（建 agent 行 + 置 `bootstrap_token_ref` 同事务提交）后断言 `AuditLog` 含 `action="agent.self_registered"`、`upserted=False`、无 `secret`/`nvapi-`/`sk-` 前缀（bootstrap CREATE 路径审计覆盖，Gate E + §0.7）；**碰撞 401 / scope 越权场景断言无 `agent.self_registered` 审计**（边界一致，Gate B）。
- `test_self_register_audit_logged`：self-update upsert 写 `AuditLog(action="agent.self_registered", upserted=True)`（Gate E）。
- `test_no_new_tables`：无新 `SQLModel(table=True)`（Gate F）。
- `test_alembic_single_new_head`：断言 Alembic head 恰好前进一个新 head（Gate F）。
- `test_migration_adds_bootstrap_token_ref_no_second_index`：断言受控迁移**仅**新增 `(platform,external_ref)` 部分唯一索引 + 一个 nullable `bootstrap_token_ref` 列、**无其他索引 / 无新表**（锁定 §0.8 迁移边界，Gate F）。
- 回归：#88/#92/#93 既有 `submit_work_log` / `attest` / `route_task` / `register_agent`（owner）单测全绿——证明 owner 路径与既有 registry 未被改动（Gate F）。

---

## 9. 验收命令

```bash
# 1) 聚焦测试（V4 新增）
aios-v0/.venv/Scripts/python -m pytest tests/test_v4_agent_platform.py -q
# 2) 全量回归（#88/#92/#93/#57 主线不被破坏）
aios-v0/.venv/Scripts/python -m pytest -q
# 3) Lint（CI 实际跑的：src tests alembic）
aios-v0/.venv/Scripts/python -m ruff check src tests alembic
# 4) Alembic head 断言（应为恰好一个新 head，非 20260728_0009）
```

验收门槛：聚焦 + 全量 `pytest` 绿；`ruff` 绿；**exact-head CI 绿**；Alembic head 恰好前进一个新 head（单一受控迁移默认）。

---

## 10. Out of scope（不扩大）

- **复杂跨 agent 工作流编排**：V4 含「按能力动态路由」（复用既有 `BEST_AVAILABLE`），但**多 agent 协同编排 / 工作流引擎**留 V4 后续切片。
- **知识 / 资产中台聚合查询层**：artifact / knowledge_fact / work_log 的统一查询已由 #88/#92/#93 主线覆盖，本 Issue 不重复建设。
- **capability 目录扩写**：新增 capability slug 走既有评审 + `CAPABILITY_TAG_MAP_VERSION` bump，不在 V4 自注册内（Gate C 强制 422）。
- **relay 自动 attest / 自动 KB 准入**（永远人工，#88 铁律，Gate D/F）。
- 改 `submit_work_log` 的 owner 校验、`attest_work_log`、`owner_approve_review` 守卫（Gate F 锁死）。

---

## 11. TDD 实施顺序（实现 PR 采用）

1. **安全依赖**：`authenticate_agent`（基于 `secret_ref` 解析 `resolve_agent_actor`）+ `authenticate_bootstrap_token`（owner 签发作用域令牌 → 解析授权 `(platform, external_ref)`），均含 401 单测（Gate A；bootstrap 另含 scope 越权 401 测试）。
2. **registry 扩展**：`create_agent_via_bootstrap`（bootstrap 严格 CREATE 原语）+ `upsert_agent`（self-update，按 `actor.agent_id` 定位 + scope 锁定 + capability 调和，未知→422）+ `list_capabilities` + `list_agents(capability=)`，含 Gate B/C 单测 + `test_self_registered_agent_routable` 端到端。
3. **relay 服务**：`relay_work_log`（agent-authenticated，provenance 取自认证，UNVERIFIED，幂等），含 Gate D/E 单测。
4. **端点**：`POST /agents/bootstrap`（接 `authenticate_bootstrap_token`）、`PUT /agents/self`（接 `authenticate_agent`）、`POST /agents/{id}/rotate-credential`（**owner-only**，接 `authenticate_owner`，凭证遗失轮换）、`GET /capabilities`、`GET /agents?capability=`、`POST /relay/work-logs`（接 `authenticate_agent`），均接既有 `get_session`。
5. **测试 + 验收**：§8 全绿 + 全量回归 + §9 门槛 + exact-head CI 绿。

---

## 12. 与 #57 / #88 / #92 / #93 / #77 的关系

- **#57（Agent Interop Gateway，已合并）**：提供 `Agent`（含 `platform`/`external_ref`/`secret_ref`/`trust_level`）、`AgentTrustLevel`、`delegation_mode` 基础；V4 在其之上加「自注册 + 发现 + relay」，不另建表。
- **#88（MVP，已合并 `94a23f5`）**：`Agent.platform`/`external_ref` 迁移 `0009` 为中台实体奠定字段基础；`submit_work_log`/`attest_work_log` 信任链被 relay 复用（relay 不 attest）；`Idempotency-Key` 机制被 relay 复用。
- **#92（V2 半自动采集，已合并 `a649ecc`）**：collectors = 主动采集端；relay = 中台接收端，二者互补（§5.4）。
- **#93（V3 LLM 判定，已合并 `01f9183`）**：relay 入站日志走与 collectors 同构的 `source_platform`/`metadata_json`，下游 `WorkLogValueJudge` 重判无需改动。
- **#77（Agent Relay V1，OPEN）**：**并入本 Issue**——#77 的「消除 owner 在工具间手工搬运」目标由 §5 relay 端点落地；#77 合并后关闭或转为本 Issue 子项。

---

## 13. v1 设计要点（对照 owner 要求）

- **「自动注册实体」落到实处**：不是新造注册表，而是把既有 `agent_registry.register_agent`（owner-only、每次新建）**保留不动**，另新增两套语义严格隔离的原语——**`create_agent_via_bootstrap`（bootstrap 严格 CREATE：按授权元组插入、部分唯一索引原子认领、碰撞即 401、绝不 upsert 补全）** 与 **`upsert_agent`（self-update：按 `actor.agent_id` 定位既有实体、正确调和 `AgentCapability` 关系行）**；bootstrap 只 CREATE、self-update 只按既有 id 更新——这正是让动态路由/知识投影「看见」自注册 agent 的真实 GAP 修复。
- **「能力可发现」落到实处**：`GET /capabilities` 目录 + `GET /agents?capability=` 过滤，均基于既有 `Capability` / `AgentCapability`；未知 slug 一律 422（fail-closed，目录单一真相源）。
- **「agent 动态路由」已具备**：`route_task` 的 `BEST_AVAILABLE` / `PREFERRED_WITH_FALLBACK` 已实现并按 `AgentCapability` 选人；V4 只需确保自注册把能力写进关系行，路由即天然生效（§1、§3.4、§8 `test_self_registered_agent_routable`）。
- **「消除 owner 手工搬运」落到实处（#77）**：`POST /relay/work-logs` 让 agent 自报产物；provenance 由认证推导、UNVERIFIED、幂等、可审计；owner 仍只在 console 做 attest/准入的人工裁定——信任边界与 #88 铁律零妥协。
- **协议一致**：沿用 #88/#92/#93 的「零新表 / 受控单迁移（默认）/ 可信 actor 解析 / 默认拒绝 KB / fail-closed / 审计」全套铁律；本计划为**纯计划文档，零实现代码**，走 Codex 评审 → `gate:merge` → owner 授权合并 → 再开实现 PR。
