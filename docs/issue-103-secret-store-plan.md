# Issue #103 — 持久化 secret-store 后端 实现计划

> **计划-only 文档**：本 PR 仅含设计计划，**零实现代码**。实现须另开 PR（见 §11），同样走 Codex 评审 → `gate:merge` → owner 授权合并。
> 关联：Issue #103（跟踪载体）、计划 §0.6 推迟项出处 `docs/issue-99-v4-plan.md`、实现 PR #102（已合 main `99d6ed8`）。
> 修订：本版已根据 Codex R1（4×P1 + 1×P2）钉死 token 绑定/HMAC 标签、独立事务补偿、任意行 fail-closed 降级、吊销一致性边界、401/503 语义；R2（2×P1 + 1×P2）补 `row_mac` 行级绑定、bootstrap 先提交 agent 再 issue、KEK 就绪先于格式短路；R3（1×P1 + 1×P2）将 `issue` 改为 dual-mode（`session=None` 独立提交供 rotate 补偿 / 传入 session 与 bootstrap 原子提交），彻底解决跨事务 FK 与「消耗令牌却未签发」的崩溃窗口，并修正 row_mac 移植测试为可落地设置（先删源行再移植、保留目标原 row_mac）。R4（1×P1 + 1×P2）将 `revoke` 亦改为 dual-mode（默认 `session=None` 独立提交供 rotate 补偿、bootstrap 传 `session=caller_txn` 使废旧与签发原子），并改写 §7 行级绑定测试为「移动源行 `row_mac`」以真正隔离 agent_id 绑定属性。R5（1×P1 + 1×P2）进一步将 `issue` 默认路径改为「开单条 store 会话并向下传递 `revoke`」使废旧+签发原子（消「已废旧却未签发」窗口），并限定 §4.6 `issue` 提交语义仅适用于 `session=None` 路径、与 bootstrap 不提交契约一致。

## 0. 范围与铁律

- **目标**：为 V4 `AgentSecretStore` 提供进程外持久化后端，替换默认内存实现，使一次性 bearer 凭据在重启 / 多副本下仍可解析与吊销。
- **绝对边界（继承 V4 受控纪律）**：
  1. **受控单迁移**：若需新增表，单 PR 单 Alembic head 前进一；fail-closed downgrade（**任意行存在即中止**）；复用 `agent` 表优先，必要时才新增最小表。
  2. **无明文泄漏**：静态存储**只保存 KEK 派生的 HMAC 标签**（无可逆密文、无明文）；bearer 明文绝不落库或写日志；`secret_ref` 仍仅作不透明句柄。
  3. **fail-closed**：后端不可用 / 完整性失败 → 拒绝签发与解析，**绝不降级内存兜底或明文回退**。
  4. **接口与补偿语义不变**：`issue()` / `resolve()` / `revoke()` 签名（`revoke` 新增可选 `session` 形参、默认 `None` 自开 store 会话独立提交，保持补偿不变量）、`rotate_credential` 审计失败吊销补偿（PR #102 `460ab9d`）保持；**补偿 `revoke()` 须以默认 `session=None` 独立提交、在调用方 rollback 后仍生效**；`issue` 默认路径内部废旧 + 签发于**同一 store 事务原子提交**（开单条 store 会话向下传递 `revoke`，消除「已废旧却未签发」窗口）；bootstrap 路径经传入 `session=caller_txn` 使废旧 + 签发与 agent 行原子。
  5. **默认关闭 / opt-in**：内存实现为默认，持久化后端经显式配置启用。
  6. **协作协议**：分支 → TDD 实现 → Codex 评审 → 置 `gate:merge`/`next:owner`/`status:blocked` → **等 owner `QLM1234` 授权合并，绝不自动 merge**。

## 1. 背景与动机

V4 中台（PR #102，main `99d6ed8`）的 `src/aios/secrets_store.py` 默认 **内存** `AgentSecretStore`：

- `issue(agent_id)` → 生成一次性明文 `credential` + 不透明 `secret_ref`，写入内存字典；返回明文仅此刻出现于 bootstrap 响应体。
- `resolve(token)` → 内存查表返回 `agent_id`，否则 `None`。
- `revoke(agent_id)` → 删除内存记录。
- agent 行仅存 `secret_ref = "secret://agent/{id}"`，真实密钥不落库。

**问题**（进程重启 / 多副本不可接受）：

1. 进程重启 → 已签发 bearer 失效，须重新 `rotate_credential`；
2. 多 worker 水平扩展 → 各实例 secret-store 不一致，`resolve()` 跨实例 401；
3. 重启后旧 `secret_ref` 指向空记录，`revoke()` 无法真正吊销历史凭据。

对「个人 AI 公司操作系统」长期运行场景不可接受。该后端在 `docs/issue-99-v4-plan.md` §0.6 显式推迟，本轮以本计划 + 后续实现 PR 收口。

## 2. 现状剖析（main `99d6ed8`）

| 文件 | 角色 |
|---|---|
| `src/aios/secrets_store.py` | `AgentSecretStore` 类 + 内存默认实现（`_by_token`/`_by_agent` dict）；`get_secret_store()` 工厂（暂单例内存） |
| `src/aios/agent_registry.py` | `create_agent_via_bootstrap` / `upsert_agent` 调 `get_secret_store().issue()`；`rotate_credential` 签发新明文 + 补偿 `revoke()`（audit 失败路径） |
| `src/aios/api/security.py` | `authenticate_agent` 用 `resolve(token)` 校验 bearer |
| `src/aios/models.py` | `Agent.secret_ref`（不透明句柄，已落库） |

**关键不变量**：明文 credential 生命周期仅限 `issue()` 返回 → 调用方（bootstrap 响应 / `rotate_credential` 返回）一次性传递 → 之后只凭 `secret_ref` + 后端 `resolve()` 校验。任何持久化后端**不得**把明文写入可恢复存储。

## 3. 目标与非目标

**目标（In）**：
- 进程外持久化 bearer 凭据（以 HMAC 标签形式，无可逆密文），重启 / 多副本可解析、可吊销。
- 保持 `issue/resolve/revoke` 接口与补偿逻辑不变。
- fail-closed、opt-in、无明文泄漏。

**非目标（Out）**：
- 不改动 V4 六门禁（A–F）已落地语义（bootstrap 单次、relay 复用、审计等）。
- 不替换 `secret_ref` 句柄模型（仍不透明）。
- 不做密钥轮换服务 / PKI（仅消费外部 KMS 提供的密钥）。
- 不扩张受控迁移边界（零或单步 head 前进）。

## 4. 设计决策

### 4.1 持久化后端选型（候选对比）

| 方案 | 优点 | 缺点 | 适配 |
|---|---|---|---|
| A. DB 存储 + KEK 派生 HMAC 标签（SQLite/PG，`agent_secret` 表，HMAC-SHA256 + 主密钥 env） | 零外部依赖、复用现有 DB、事务一致、多副本天然共享、标签不可反推 token（强于密文） | 主密钥须安全分发；标签随 DB 备份 | **推荐默认**（opt-in） |
| B. KMS 信封加密（本地存密文 + KMS 解密数据密钥） | 密钥不落应用主机、合规强 | 需 KMS 可达、增加延迟、fail-closed 更敏感 | 生产增强档（可选，未来插拔） |
| C. 外部 Vault / 专有 secret 服务 | 最专业 | 引入重依赖、运维成本 | 超出本期，列未来 |

**推荐**：方案 A 为默认实现（opt-in），抽象 `AgentSecretStore` 保留，使 B/C 后续可插拔。**本计划实现 PR 只交付 A**。

> 选型 A 采用 **HMAC 标签**而非「可逆密文」：我们从不需恢复 token，只需证明持有。HMAC-SHA256(KEK, token) 是单向、不可伪造、可索引的表示——比存密文更强（无可逆密钥风险），且天然防明文泄漏。

### 4.2 最终 Token 绑定方案（确定性，不再推迟到实现 PR）

**主密钥（KEK）**：环境变量 `AIOS_SECRET_MASTER_KEY`（32 字节，hex/base64），缺失即 fail-closed。

**存储记录（最终 schema）**：
```sql
CREATE TABLE agent_secret (
    agent_id   VARCHAR(...) PRIMARY KEY REFERENCES agent(id),
    token_tag  BLOB NOT NULL,        -- HMAC-SHA256(KEK, token)，单向查找键，不可反推 token
    row_mac    BLOB NOT NULL,        -- HMAC-SHA256(KEK, agent_id || token_tag)，行级绑定的密码学校验值
    created_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP NULL
);
CREATE UNIQUE INDEX uq_agent_secret_token_tag ON agent_secret(token_tag);
```
- `token_tag` UNIQUE 索引是必需的 lookup 索引（访问路径），与 V4 `20260729_0001` 的「零多余索引」不矛盾（此处查找列本身就是访问路径，最小必要条件）。
- `row_mac` 提供**行级密码学绑定**：将 `token_tag` 绑定到其 `agent_id`，防止标签被移植到别的 agent 行（见下「绑定与防替换」）。
- 无 `ciphertext` / `nonce` 列：无需恢复 token，只需证明持有 + 验证绑定。

**流程**：
- `issue(agent_id)`：
  1. `revoke(agent_id, session=session)`（先废旧 token；`session` 为 `issue` 自有 store 会话（默认 `None` 时 `issue` 开单条 store 会话 S 并传入）或调用方事务，废旧与 upsert 同事务原子，见 §4.5）；
  2. `token = "aios_ag_" + secrets.token_urlsafe(32)`（明文 bearer，仅此刻返回）；
  3. `token_tag = HMAC-SHA256(KEK, token)`；`row_mac = HMAC-SHA256(KEK, agent_id || token_tag)`；
  4. upsert 行 `(agent_id, token_tag, row_mac, now, NULL)`（每 agent 单活跃 token，覆盖旧）；
  5. 返回 `token`（明文，一次性）。
  - **事务边界**：默认 `issue(agent_id)`（`session=None`）内部**开一条 store 自有会话 S**，将 `S` 同时传给内部 `revoke(agent_id, session=S)` 与第 4 步 upsert，末尾 `S.commit()` 一次——废旧 + 签发在同一 store 事务内原子提交，杜绝中间崩溃导致的「已废旧却未签发」孤儿态（R5-P1）。bootstrap 传入 `session=caller_txn` 时不自管提交，废旧 + upsert + agent 行皆在 T1 原子提交（§4.5）。
- `resolve(token)`（**KEK/后端就绪检查先于一切 token 特定短路**，解决 P2 排序矛盾）：
  1. 若 KEK 缺失 / 后端不可用 → 抛 `SecretStoreUnavailable`（统一 **503**，对**所有**输入——无论 token 格式如何）；
  2. 否则 `token` 缺失或格式校验失败 → 返回 `None`（**401**，与 unknown/revoked 一致，见 §6 / G1）；
  3. `tag = HMAC-SHA256(KEK, token)`；`SELECT agent_id, row_mac FROM agent_secret WHERE token_tag = ? AND revoked_at IS NULL`；
  4. 命中则**校验 `HMAC(KEK, agent_id || token_tag) == row_mac`**；不匹配 → 视为完整性失败，抛 `SecretStoreUnavailable`（**503**，非 `None`）；
  5. 匹配 → 返回 `agent_id`；未命中 → `None`（401）。
- `revoke(agent_id, session=None)`：`UPDATE agent_secret SET revoked_at = now WHERE agent_id = ?`（幂等；不存在则 no-op；`session=None` 走 store 独立事务，`session=caller_txn` 则并入调用方事务）。
- **绑定与防替换**：`row_mac` 使 `token_tag` 无法被移植到别的 agent 行——即便攻击者获 DB 写权限把某行 `token_tag` 复制到另一 agent 行，`row_mac` 校验必失败 → 解析被拒绝（503 完整性失败），而非错误地解析为被替换的 agent。这弥补了「`HMAC(KEK, token)` 不含 agent_id」的绑定缺口。
- **AAD**：本方案已用 `row_mac` 绑定 `agent_id`；若未来叠加 AES-GCM 信封，AAD 仍设为 `agent_id` 作纵深防御。

**接口保持**：`issue/resolve/revoke` 签名不变；`resolve` 在认证路径仍返回 `agent_id | None`。

### 4.3 密钥管理

- KEK 仅来自 env / KMS，**绝不写库、绝不进版本控制**。
- 沿用仓库 `redact_secrets` 脱敏约定：任何日志 / 审计 / 异常不得含明文 token 或 KEK。
- 配置缺失或格式错误 → `get_secret_store()` 工厂抛 `SecretStoreUnavailable`（fail-closed），不回退内存。

### 4.4 数据模型（受控单迁移）

- **优先复用 `agent` 表**：经评估，凭据状态以独立 `agent_secret` 表承载更清晰（句柄 `secret_ref` 仍留 `agent` 表，密文等价物 `token_tag` 落独立最小表）。
- **最小新增表** `agent_secret`（见 §4.2 schema）：单 PK（`agent_id`）+ 一个必需 lookup 索引（`token_tag` UNIQUE），无多余索引。
- Alembic：单 head 前进一（`20260729_0001` → 下一序号），**复用 #88 / V4 `20260729_0001` 的同款 fail-closed downgrade 契约，且更严格**：
  - **`downgrade()` 在触碰任何 DDL 前，只要 `agent_secret` 表存在任意行（活跃 OR 已吊销）即抛 `RuntimeError`**。即使全是已吊销行，它们也是真实的「静态存储凭据记录」，静默 DROP 表会丢失它们，弱于 `20260729_0001`（其约定为「任意已填充状态即中止」）。本计划采用同样严格的「任意行即中止」。
  - 仅当表**完全为空**时才允许无损降级（DROP 表 + 索引）。
- 同步 bump：`tests/test_knowledge_models.py`、`test_review_binding_migration.py`、`test_work_log.py` 的 HEAD 常量。

### 4.5 接口契约（不变）+ 事务所有权

```python
class AgentSecretStore(Protocol):
    def issue(self, agent_id: str, session: Any | None = None) -> str: ...  # 返回一次性明文 credential；session 传入则写入该事务（bootstrap 原子），否则独立提交（rotate 补偿）
    def resolve(self, token: str) -> str | None: ...  # 返回 agent_id 或 None（认证路径）
    def revoke(self, agent_id: str, session: Any | None = None) -> None: ...  # 使该 agent 活跃 token 失效（默认独立提交供 rotate 补偿；传入 session 则与 bootstrap 原子提交）
```

- `InMemoryAgentSecretStore` 保留为默认（opt-in 关闭持久化时），dict 变更无事务，天然满足独立补偿。
- `EncryptedDbAgentSecretStore`（新增）实现同契约，**内部加解密 + 独立 DB 会话**。
- `get_secret_store()` 工厂按 `AIOS_SECRET_STORE_BACKEND`（默认 `memory`，可选 `encrypted_db`）选择；`encrypted_db` 要求 KEK 就绪，否则 fail-closed。
- **事务所有权（关键，dual-mode，解 P1-2 / P1-a / P1-d / R5-P1）**：`issue` 与 `revoke` **均为 dual-mode**，统一语义为「**传具体 session 即用该会话，传 None 则自开 store 会话**」：
  - **`issue(agent_id, session=None)`**：若 `session=None`，`issue` **开一条 store 自有会话 S**，将 `S` 同时传给内部 `revoke(agent_id, session=S)` 与 upsert（第 4 步），末尾 `S.commit()` 一次——废旧与新发在**同一 store 事务内原子**提交，消除「已废旧却未签发」的崩溃窗口（R5-P1）；若传入 `session`（bootstrap），则废旧 + upsert 写入该调用方事务 T1，与之原子提交且 `issue` 不自管 commit。
  - **`revoke(agent_id, session=None)`**：默认 `session=None` → 自开 store 会话**独立提交**（供 `rotate_credential` 补偿：在调用方 `session.rollback()` 后仍生效，无孤儿）；传入 `session` → 并入调用方事务（bootstrap 内部由 `issue` 传入同一 S/T1，使废旧与签发原子）。
  - **理由**：`rotate_credential` 补偿 `revoke()` 须以默认独立提交在调用方 rollback 后存活；而 `issue` 内部废旧 + 签发须原子（默认路径同 store 事务、bootstrap 路径同 T1）。若 `issue` 的 `session=None` 仅把 `None` 转发给 `revoke` 让其自开独立事务，则废旧先提交、upsert 后提交，中间崩溃 → 旧 token 已吊销但新 token 未签发，与「`issue` 拥有单条独立提交 store 事务」矛盾——故 `issue` 必须开单一 store 会话并向下传递。内存默认实现因无事务，天然满足。
- **Bootstrap 事务序列（原子 + 严格单次，解 P1-a / P1-d）**：bootstrap 调 `issue(agent.id, session=caller_txn)`，将凭据写入**同一调用方事务 T1**，与 agent 行（含 `bootstrap_token_ref` 消费记录）**原子提交**——FK 在同一事务内可见（PG/SQLite 均无跨事务问题），且若 T1 回滚则 agent 与凭据皆不落库、令牌**未被消费**、客户端可用同一令牌重试（保留 V4 严格单次下的重试/回滚语义）。唯一残留崩溃窗口是「T1 已提交但明文未送达客户端」：凭据已签发但未交付，令牌已消费（重放 → 401，符合严格单次），由 owner 经 `rotate_credential` 补发（同 `rotate_credential` 文档的丢失凭据恢复路径），**绝不留下已签发却无法吊销的孤儿凭据**。`rotate_credential` 不传 session，走独立会话，其补偿模型不变。实现须在支持库（SQLite / PG）上加测试覆盖 bootstrap 原子提交与回滚重试。

### 4.6 fail-closed 语义 + 补偿时序

- KEK 缺失 / 格式错 → 工厂抛 `SecretStoreUnavailable`，`rotate_credential` / bootstrap 中止（不签发）。
- DB 连接失败 / 存量行损坏（完整性失败）→ `resolve()` 抛 `SecretStoreUnavailable`（不返回 `None` 静默放行，也不降级内存）。
- **`resolve()` 就绪检查顺序（解决 P2-c 排序矛盾）**：KEK/后端就绪检查须在任何 token 格式短路**之前**（见 §4.2 / §6）——KEK 不可用时对所有输入（含 malformed）统一 `SecretStoreUnavailable`（503），仅当就绪后才对 malformed/unknown/revoked 返回 `None`（401）。这样「运营故障」与「认证失败」二分不被输入格式破坏。
- **写入与补偿时序**：
  - `issue()`（**默认 `session=None`**）在自己的 store 事务中提交 token 标签（独立提交，调用方 rollback 不影响）；当传入 `session=caller_txn`（bootstrap）时**不自管提交**，废旧 + 签发并入 T1 原子提交（见 §4.5）。
  - 调用方（`rotate_credential`）在审计写入 / 业务 commit 失败时，调用 `store.revoke(agent_id)`（**默认 `session=None`，走 store 独立事务独立提交**），即使随后调用方 `session.rollback()`，已签发的 token 标签仍被置 `revoked_at`，无孤儿活跃 bearer。此即 PR #102 `460ab9d` 补偿不变量的持久化保持。（bootstrap 路径的 `revoke` 因传入 `session=caller_txn` 而并入 T1，不属于此类补偿——其原子性由 §4.5 事务所有权保证。）
  - 写入失败（store 自身 commit 异常）→ `issue()` 不返回明文；调用方按既有逻辑处理。

### 4.7 吊销一致性边界（确定性）

- **生效时点**：吊销在 `revoke()` 事务**提交后**生效。
- **允许的在途窗口**：一个 `resolve()` 若在 `revoke()` 提交**之前**已读取到活跃行（同一请求在途），仍可能认证通过——这是有界且可接受的（该 token 正被该请求主动使用）。
- **强制保证**：任何在 `revoke()` 提交**之后**开始的 `resolve()`（含其他副本，read-committed 隔离保证提交后读可见 `revoked_at`）**必须**对该已吊销 token 返回 `None`。**无需行锁**——DB 的 read-committed 隔离已保证提交后读一致。
- **确定性并发门**（替代「仅两实例顺序覆盖」）：测试须序列化证明——session B `revoke()` 提交后，session A（或另一副本）发起的**新** `resolve()` 返回 `None`；并文档化上述在途窗口为可接受。

## 5. 受控迁移纪律（重申）

- 单 PR 单 head 前进；零或单步。
- fail-closed downgrade（**`agent_secret` 任意行存在即 `RuntimeError` before DDL**，含已吊销行）。
- 复用 `agent` 表优先；新增表最小化 + 单 PK + 单个必需 lookup 索引。
- bump 三处测试 HEAD 常量；`alembic` 单 head 校验。

## 6. 安全边界与语义区分（解决 P2）

- bearer 明文绝不落库 / 日志 / 审计详情；`secret_ref` 仅句柄；静态仅存 `token_tag`（HMAC）。
- KEK 仅 env/KMS，不落库、不进版本控制。
- 沿用 `redact_secrets`；异常与审计只记 `agent_id`、动作、成功/失败，不记 token 或 KEK。
- **`resolve()` 语义严格二分**（外部可观测，且 KEK/后端就绪检查先于 token 格式短路）：
  - **运营路径（503，最高优先级）**：KEK 缺失 / DB 不可用 / 存量损坏（含 `row_mac` 校验失败）→ 抛 `SecretStoreUnavailable`，端点返回 **503**。此检查在**任何** token 处理之前，故即便输入 malformed，KEK 不可用时也返回 503（不泄露 token 格式）。
  - **认证路径（401）**：仅在 KEK/后端就绪后，malformed / unknown / revoked token → 结果**完全一致**（认证失败，无存在性差异泄漏，满足 G1）。`resolve()` 对这三类返回 `None`，端点统一 401。
  - 外部只两信号：**401**（token 无效/未知/已吊销，且 KEK 就绪）vs **503**（存储故障，与 token 无关）。由此 G1 可测：KEK 就绪下，对 unknown 与 revoked token 注入，断言端点均返回 401 且响应不可区分；另测 KEK 缺失下 malformed 输入也返回 503。

## 7. 测试计划

- **单元**（`tests/test_secret_store.py`）：
  - 无明文/标签落库：直接读 `agent_secret` 表确认无 token 明文、仅有 `token_tag` + `row_mac`；且 `token_tag` 不可反推 token（HMAC 单向性）。
  - 签发→解析闭环（按 `token_tag` 查找）；`revoke` 后 `resolve` 返回 `None`。
  - **行级绑定校验（隔离 `row_mac`）**：建**源行** `agent_secret(src_id, token_tag_src, row_mac_src)`（`row_mac_src = HMAC(KEK, src_id || token_tag_src)`）与独立的**目标行** `agent_secret(dst_id, token_tag_dst, row_mac_dst)`（`row_mac_dst` 按 `dst_id` 算，与源行无 `token_tag` 冲突）。测试**删除源行后，将源行的 `row_mac_src` 连同其 `token_tag_src` 移植进目标行**——即目标行变为 `agent_secret(dst_id, token_tag_src, row_mac_src)`。此时 `resolve(token_src)` 按 `token_tag_src` 命中目标行（`dst_id` 行），重算 `HMAC(KEK, dst_id || token_tag_src)` 与存储的 `row_mac_src`（按 `src_id` 算）**不符** → 抛 `SecretStoreUnavailable`（**503**，证明绑定校验拦截了「`token_tag` 被移植到别的 agent 行」攻击；若缺失 `row_mac` 绑定则会错误解析为 `dst_id`）。此设置**真正隔离 agent_id 绑定属性**：`row_mac` 须以行自身 `agent_id` 重算，移植来的 `row_mac_src` 校验必失败。（注：`UNIQUE(token_tag)` 禁止同 tag 两行并存，故先删源行再移植，且目标行原 `token_tag_dst` 已被 `token_tag_src` 替换故不冲突。）
  - KEK 缺失 → 工厂抛 `SecretStoreUnavailable`（fail-closed）。
  - **KEK 不可用 + malformed 输入 → 仍 503**（验证就绪检查先于格式短路，P2-c）。
  - 运营故障（DB 错 / 存量损坏）→ `resolve` 抛 `SecretStoreUnavailable`（**非**静默 `None`）。
  - 写入失败 → 不返回明文 + 补偿 `revoke` 独立提交生效（无孤儿）。
- **集成**：
  - 重启模拟：新 store 实例（同 DB）可 `resolve` 旧 token 并 `revoke`。
  - 多副本模拟：两 `EncryptedDbAgentSecretStore` 指向同 DB，`revoke` 全局生效。
  - 接 `agent_registry.rotate_credential` / bootstrap，确认补偿吊销（独立事务）在多副本下正确。
  - **Bootstrap 原子序列**（SQLite / PG 均覆盖）：`issue(agent.id, session=txn)` 与 agent 行原子提交；断言凭据可用；T1 回滚则 agent 与凭据皆不落库、令牌未被消费可重试；T1 提交后明文未送达则由 owner `rotate_credential` 补发，无孤儿活跃 bearer。
- **并发门**：`revoke()` 提交后，新 `resolve()`（另会话/副本）必返回 `None`（确定性，替代仅顺序两实例覆盖）。
- **迁移测试**（加表）：空态无损 downgrade；**存在任意行（活跃 OR 已吊销）downgrade fail-closed `RuntimeError`**（两个用例，均触发）。

## 8. 验收门禁（适配 V4 六门禁）

- **G1（身份/凭据边界）**：明文绝不落库；静态仅 `token_tag` + `row_mac`（HMAC 单向，不可反推 token）；`row_mac` 密码学绑定 `agent_id` 防标签移植；`resolve` 对 unknown/revoked/malformed 返回一致的 401，无存在性泄漏（§6）。
- **G2（幂等/单次）**：`issue` 每次新随机 token（并刷新 `row_mac`）；`revoke` 幂等；重启 / 多副本下 `resolve` 结果稳定。
- **G3（fail-closed）**：KEK/DB/完整性（`row_mac` 校验）任一失败 → 拒绝签发/解析（503，且就绪检查先于 token 格式短路），无降级、无 `None` 静默放行。
- **G4（迁移受控）**：单 head 前进；**任意行即 fail-closed downgrade**；三处 HEAD 常量 bump；`alembic` 单 head。
- **G5（审计/补偿）**：`rotate_credential` 审计失败吊销补偿保持且**独立事务提交**（§4.5/§4.6）；secret 操作不泄露 token/密文到审计。
- **G6（默认安全/opt-in）**：默认内存；`encrypted_db` 须显式配置 + KEK 就绪，否则 fail-closed。

## 9. 实现顺序（§11 TDD，另开实现 PR）

1. **T1 抽象与工厂**：`get_secret_store()` 按 backend 选择 + KEK 加载与 fail-closed。*测试：工厂选择、KEK 缺失抛 `SecretStoreUnavailable`。*
2. **T2 令牌标签核心**：HMAC 工具——`token_tag = HMAC-SHA256(KEK, token)` 与 `row_mac = HMAC-SHA256(KEK, agent_id || token_tag)`（确定性、格式校验、绑定校验）+ 单元测试（已知向量、篡改/格式失败检测、`row_mac` 不匹配检测）。
3. **T3 持久化存储**：`EncryptedDbAgentSecretStore`（dual-mode `issue`/`revoke`——`session=None` 时 `issue` 开单条 store 会话使废旧+签发原子提交 / `session=txn` 原子写入 + `resolve` + `token_tag` 查找 + `row_mac` 校验）。`secret_ref` 仍由调用方写 `agent` 表。*测试：签发→解析→吊销闭环、无明文落库、row_mac 移植防护（移动源行 `row_mac` 触发 503）、运营故障抛错、补偿独立提交（外部 `revoke(None)` 在调用方 rollback 后仍生效）、bootstrap 传 session 使废旧+签发+agent 行原子提交与回滚重试（令牌未被消费可重放，SQLite/PG）。*
4. **T4 迁移**：`agent_secret` 表 Alembic 迁移（单 head + **任意行 fail-closed downgrade**）+ 三处 HEAD bump + 迁移测试（空态 / 活跃行 / 已吊销行 三用例）。
5. **T5 集成**：重启 / 多副本模拟 + **并发吊销门**；接 `rotate_credential` / bootstrap 补偿验证。
6. **T6 收口**：`ruff` + 全量 `pytest -q` + exact-head CI 绿；更新 `docs` 说明 opt-in 配置与 401/503 语义。

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| KEK 分发不安全 | 仅 env/KMS；缺失即 fail-closed；文档明确 |
| `token_tag` 碰撞 | HMAC-SHA256 32 字节，碰撞不可行；测试覆盖已知向量 |
| 迁移 downgrade 丢凭据 | 复用 V4 fail-closed 契约且更严：**任意行（含已吊销）即 `RuntimeError`** |
| 多副本缓存致吊销延迟 | 本期不引入缓存，DB 为单一事实源；read-committed 保证提交后一致 |
| 补偿被调用方 rollback 撤销 | store 写入走**独立事务独立提交**，补偿 `revoke` 在调用方 rollback 后仍生效 |
| 性能（每次 resolve 一次 HMAC + 索引查） | bearer 校验为低频鉴权路径，开销可忽略；必要时再评估缓存 |

## 11. 依赖与关联

- Issue #103（跟踪载体，已开）
- 计划 §0.6 推迟项：`docs/issue-99-v4-plan.md`
- 实现基础：PR #102（已合 main `99d6ed8`）、`src/aios/secrets_store.py`、`agent_registry.py`（`rotate_credential` 补偿 `460ab9d`）
- 迁移范式：复用 `alembic/versions/20260729_0001_agent_self_registration.py` 的 fail-closed downgrade 模式（并收紧为「任意行即中止」）
- **本计划合并后，实现 PR 须另开**（`feat/issue-103-secret-store-impl`，base 本计划 HEAD），按 §9 TDD + 本门禁，走 Codex 评审 → `gate:merge` → owner 合并。
