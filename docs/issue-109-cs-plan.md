# 实施计划（v1）：#109 客服 / 销转（微信 · 企微，V1.2-B）

> 关联 Issue **#109**；设计文档 `docs/plan-v1.2-ip-ops.md` §3 / §6 / §7 / §8。
> 状态：**待评审（PLAN PR）** — 本 PR 仅含本计划文档，零代码、零迁移。
> 协议：设计 / 假设 / 进度以 Issue · PR · 仓库文档为唯一事实源；严格遵循协作协议（Codex APPROVE + exact-head CI + owner 门禁，绝不自动 merge）。
> 流程：本计划经 `gate:merge` 由 owner 授权 squash-merge 到 `main` 后，**方可**开实现 PR `feat/issue-109-cs-impl`（TDD + Codex + 门禁）；未合并前不实现。

## 0. 已锁定的 owner 决策（§8 开放问题）

实现前需 owner 定的两项，已在本会话经 AskUserQuestion 锁定：

| # | 开放问题 | 结论 |
|---|----------|------|
| §8.1 | 企微具体接入方式 | **适配层 + Mock 适配器**：定义 `WeComAdapter` 抽象接口，MVP 用内存 / Mock 适配器跑通全流程（会话 / 应答 / 升级 / 审计 / 漏斗），**不依赖真实企微凭证，CI 可绿、可独立评审**；真实企微接入（`WeComAppAdapter` 企微应用消息 API / 微信客服 API）留作后续 issue。 |
| §8.4 | 「常规问答」置信度阈值初值 | **0.80**：仅当检索命中 FAQ / `KnowledgeFact` 且置信度 ≥ 0.80 且非升级类时，才允许 AI 代发；其余一律转人工 + 审计。初值保守，经 `AIOS_CS_AUTO_SEND_CONFIDENCE` 环境变量可调（仿 `content_draft.REVIEW_PASS_MIN_CONFIDENCE`）。 |

> 白名单语义：自动代发对象 = `常规问答` 白名单 = 「FAQ / `KnowledgeFact` 命中 **且** 置信度 ≥ 阈值 **且** 非升级类」。该白名单由检索命中 + 阈值 + 升级规则**派生**，不另存静态白名单表（保持单一事实源，避免漂移）。

## 1. 范围与边界（铁律）

- MVP 旅程（设计 §3）：企微会话接入（MVP=Mock 适配器）→ 会话 / 消息上下文 → FAQ + 已审核 `KnowledgeFact` 驱动应答建议 → 升级规则（报价 / 付款 / 承诺 / 投诉 / 隐私 / 低置信 → 转人工 + 审计）→ 有限自动发送（仅「常规问答」白名单代发）→ 线索漏斗（`visitor / lead / qualified / proposal / won`）+ 人工接管 + 跟进任务 → 所有对外发送审计（who / what / when / channel）。
- 设计 §5「明确不在首个 MVP 实现」与 §3「不自动」：
  - **不自动**：发送报价、收款、客户承诺、超出必要范围保留客户数据、客服全自动（无人工兜底）。
  - **有限自动**：仅「常规问答」白名单可 AI 代发；报价 / 付款 / 承诺 / 投诉 / 隐私 → **100% 人工**；其它（FAQ 命中但置信度 < 阈值）→ 人工确认后代发。
  - **漏斗 MVP 仅记录阶段，不自动推进**：阶段跃迁仅由 owner / 人工坐席显式触发。
- **铁律（复用既有原语）**：
  - 所有身份（actor / owner / agent / 人工坐席 / 提交者）**一律服务端派生**，请求体 / 查询 / 头不得指定。
  - 所有对外发送（含 AI 代发）**一律写 `AuditLog`**（who / what / when / channel），`redact_secrets` 自动脱敏（复用 `audit.append_audit`）。
  - 升级事件**写 `AuditLog`**（`action="cs.escalation"`），不触发任何自动生产动作。
  - 应答建议**绝不自动注入 `KnowledgeFact`、绝不自动创建知识对象**（与 #108-A / #110 同铁律）。
  - 默认确定性规则（关键词 / 词项重叠评分）+ 规则升级匹配，**零付费 LLM 调用**；真实语义匹配 / LLM 分类需显式凭据 + 开关 + cost owner 门控（留 impl 落地，默认关）。

## 2. 数据模型（需迁移，保持单 head）

### 2.1 新增 `StrEnum`（纯代码，VARCHAR，无迁移副作用）

- `CsChannel(StrEnum)`：`WECHAT_WORK = "wechat_work"`、`WECHAT_PUBLIC = "wechat_public"`、`MOCK = "mock"`（MVP 默认 `MOCK`）。
- `LeadStage(StrEnum)`：`VISITOR = "visitor"`、`LEAD = "lead"`、`QUALIFIED = "qualified"`、`PROPOSAL = "proposal"`、`WON = "won"`。
- `MessageDirection(StrEnum)`：`INBOUND = "inbound"`、`OUTBOUND = "outbound"`。
- `SenderType(StrEnum)`：`CUSTOMER = "customer"`、`AGENT = "agent"`、`OWNER = "owner"`。MVP 不引入独立「人工坐席」身份——**人工 = `owner`**（服务端派生 `ActorContext.kind=="owner"`，经 `authenticate_owner`），AI agent（`kind=="agent"`）不可执行人工专属动作（见 §4.5）。

### 2.2 新增表（需 Alembic 迁移，链式挂在 `20260730_0001` 之后，保持**单 head**）

`src/aios/models.py` 新增两个 `SQLModel(table=True)`：

- **`Conversation`**（`__tablename__="conversation"`）：
  - `id: str`（`new_id("conv")`，PK）
  - `project_id: str`（`FK project.id, index=True`）
  - `channel: CsChannel`（默认 `MOCK`，`sa_column=Column(String)`）
  - `external_conversation_ref: str | None`（企微侧会话 id；MVP Mock 可为 null）
  - `customer_ref: str | None`（客户标识，如企微 userid / openid；MVP Mock 自生成）
  - `lead_stage: LeadStage`（默认 `VISITOR`）
  - `assigned_human: str | None`（人工坐席 / owner 接管标识；服务端派生）
  - `created_at` / `updated_at`
  - `__table_args__`：无（保持简单；按 `project_id` 索引即可）
- **`Message`**（`__tablename__="message"`）：
  - `id: str`（`new_id("msg")`，PK）
  - `conversation_id: str`（`FK conversation.id, index=True`）
  - `project_id: str`（`FK project.id, index=True`，冗余以便审计 / 鉴权查询）
  - `direction: MessageDirection`
  - `sender_type: SenderType`
  - `body: str`（明文业务文本；**不存** header / cookie / 凭据 / 环境变量；`AuditLog` 仅记有界摘要，不存完整正文脱敏前原文之外的密文 — 依 #110 §5 不可信输入模型）
  - `confidence: float | None`（仅应答建议落库时填写，范围 0..1）
  - `is_auto_sent: bool = False`（是否 AI 代发）
  - `escalation_flag: bool = False`
  - `escalation_categories: list[str]`（`sa_column=Column(JSON)`，取值于 `{price, payment, promise, complaint, privacy, low_confidence, unknown}`）
  - `knowledge_fact_refs: list[str]`（`sa_column=Column(JSON)`，命中的 `KnowledgeFact.id` 列表）
  - `created_at`
- **`CsSuggestion`**（`__tablename__="cs_suggestion"`，应答建议持久化；**非** `Message` 草稿——`Message` 仅表示已发 / 已收消息，草案态不复存在，消除「`is_auto_sent=False` 兼表草稿与人工已发」的歧义）：
  - `id: str`（`new_id("sug")`，PK）
  - `conversation_id: str`（`FK conversation.id, index=True`）
  - `project_id: str`（`FK project.id, index=True`）
  - `decision: CsSuggestionDecision`（`StrEnum`：`AUTO_SEND = "auto_send"` / `HUMAN_CONFIRM = "human_confirm"` / `ESCALATE = "escalate"`）
  - `text: str`（建议文案；代发时须与发送 `text` **逐字相等**，防任意文本复用）
  - `confidence: float | None`
  - `escalation_categories: list[str]`（`sa_column=Column(JSON)`）
  - `knowledge_fact_refs: list[str]`（`sa_column=Column(JSON)`，命中 fact id）
  - `fact_revisions: dict[str, int]`（`sa_column=Column(JSON)`，`{fact_id: version}` 快照，用于代发时**陈旧性重校验**）
  - `consumed: bool = False`（原子一次性消费；代发成功后置 True，防重放）
  - `idempotency_key: str`（`UNIQUE`，建议生成幂等 + 代发一次性）
  - `created_at`

### 2.3 复用原语（零新增）

- **`AuditLog`**（`src/aios/audit.py`，append-only 惰性表）：
  - 对外发送审计：`action="cs.outbound_send"`（`after` = `{channel, direction, is_auto_sent, sender_type, body_bounded(≤512), message_id, conversation_id}`）。
  - 升级审计：`action="cs.escalation"`（`after` = `{categories, reason, conversation_id, message_id}`）。
  - 漏斗阶段审计：`action="cs.lead_stage"`（`before/after` = `{lead_stage}` + `reason`）。
  - 全部经 `append_audit(...)`，`redact_secrets` 自动脱敏；`body` 入 `after_snapshot` 前按 ≤512 字符有界 + PII redact（复用 #110 `redact_pii` 思路，本计划 §6 安全段）。
- **`KnowledgeFact`**（`models.py`）：仅**读取**已审核事实（`status=APPROVED`）用于应答建议检索，绝不写入 / 创建。
- **`ActorContext`**（`actor.py`）：所有身份服务端派生；`resolve_owner_actor` / `resolve_agent_actor` / `ActorContext.system`。
- **`ServiceError`**（`services.py`）：`403 / 404 / 409 / 422` 业务错误，路由层 `_translate` 转 HTTP。

### 2.4 迁移结论

- 新增 **一个** Alembic 迁移（如 `20260731_0001_customer_service.py`），`down_revision = "20260730_0001"`，创建 `conversation` / `message` / `cs_suggestion` **三表**；**不改动任何既有表**（尤其 `artifact` 表被触发器 `knowledge_candidate_validate_insert` 字面引用，须 raw ALTER 勿 batch recreate — 本迁移完全不涉及 `artifact`，天然规避）。
- 单 head 不变：`alembic heads` == 1 且 == 新迁移 rev；既有 `20260730_0001` 成为历史节点。§7 测试证明「fresh / upgrade / 单 head / 仅 1 个新增迁移文件 / 枚举往返」。

## 3. 适配层（企微接入，§8.1 决策）

### 3.1 `WeComAdapter` 抽象（`src/aios/adapters/wecom.py`）

- 定义 `WeComAdapter`（ABC / Protocol），方法：
  - `receive_inbound(session, *, project_id, external_conversation_ref, customer_ref, text) -> Conversation`
    —— 入站：建 / 取 `Conversation` + 建 `Message(direction=INBOUND, sender_type=CUSTOMER)`。
  - `send_message(session, *, conversation_id, text, actor, auto_send) -> Message`
    —— 出站：建 `Message(direction=OUTBOUND, ...)`，写 `AuditLog("cs.outbound_send")`。
  - `list_conversations(session, *, project_id) -> list[Conversation]`
- 抽象隔离传输细节，使服务层 / API 层不感知「真实企微」与「Mock」差异。

### 3.2 `MockWeComAdapter`（MVP 默认）

- 内存 / DB 直写实现，**不调用任何外部 API、不依赖真实企微凭证**；CI 可绿、可独立评审。
- 真实接入（后续 issue）：`WeComAppAdapter`（企微应用消息 API：`corpid+corpsecret+agentid` 事件回调 + 发应用消息）或 `WeComKfAdapter`（微信客服 API）。**不在本 #109 MVP 范围**——本计划仅定义接口 + Mock，真实适配器单独 issue + 单独 PR。

## 4. 服务层（新增 `src/aios/customer_service.py`，仿 `feedback.py` / `content_draft.py`）

复用 `BEGIN IMMEDIATE` 单事务（复用 `content_draft.py:604-605`：`session.rollback()` + `session.connection().exec_driver_sql("BEGIN IMMEDIATE")`）替代悲观行锁。

### 4.1 置信度阈值（§8.4 决策）

- `CS_AUTO_SEND_CONFIDENCE = float(os.getenv("AIOS_CS_AUTO_SEND_CONFIDENCE", "0.80"))`（模块级常量，默认 0.80，env 可覆）。

### 4.2 应答建议（`generate_suggestion`）

`generate_suggestion(session, actor, *, conversation_id, inbound_message_id, text) -> CsSuggestion`：
1. 加载 `project_id` 作用域 + 公司级（`project_id IS NULL`）的 `KnowledgeFact(status=APPROVED)`。
2. **确定性评分**（默认，零 LLM）：对入站 `text` 与每条 fact `statement` 做词项重叠 / 包含匹配，输出 `confidence ∈ [0,1]`（参考 `judging/heuristic.py` 的 `_HEURISTIC_CONFIDENCE` 量级；可叠加 `tags` 匹配加权）。
3. **升级规则判定**（确定性关键词 / 正则，零 LLM）：命中 `{price, payment, promise, complaint, privacy}` 任一类 → `escalation_categories` 标记；无任何 fact 命中 → `unknown`（视为低置信）。
4. **决策派生**（三分支，唯一事实源）：
   - `AUTO_SEND`：非升级 **且** 最佳 `confidence ≥ CS_AUTO_SEND_CONFIDENCE` **且** 有 fact 命中。
   - `HUMAN_CONFIRM`：非升级 **且** 有 fact 命中 **但** `confidence < 阈值`（建议文案，需人工确认后代发）。
   - `ESCALATE`：命中升级类 **或** 无 fact 命中（低置信 / 未知）→ 转人工 + 写 `AuditLog("cs.escalation")`。
5. 持久化一条 `CsSuggestion`（`decision` / `text` / `confidence` / `escalation_categories` / `knowledge_fact_refs` / `fact_revisions={fact_id: fact.version}` / `consumed=False` / `idempotency_key`），返回其 `id` 供 `send_message` 绑定（**注意：建议落 `CsSuggestion`，不落 `Message` 草稿**——`Message` 仅在真正发送 / 接收时创建，草案态不复存在，消除「`is_auto_sent=False` 兼表草稿与人工已发」的歧义）。

### 4.3 有限自动发送（`send_message`）

`send_message(session, actor, *, conversation_id, text, auto_send: bool, suggestion_id: str | None)`：
- **代发护栏（铁律，防任意文本复用 / 重放 / 陈旧事实，响应 Codex P1-1/P1-2）**：当 `auto_send=True` 时，在单 `BEGIN IMMEDIATE` 事务内（`session.rollback()` + `exec_driver_sql("BEGIN IMMEDIATE")`，仿 `content_draft.py:604-605`）：
  1. 按 `suggestion_id` 重读 `CsSuggestion`，要求 `conversation_id` 匹配、`decision==AUTO_SEND`、`consumed==False`（已消费 → 409 重放拒绝）；
  2. 要求发送 `text`（NFC 归一后）**逐字等于** `suggestion.text` —— 绑定不可变建议内容，杜绝「凭一条合法建议发任意文本」；不符 → `ServiceError(409, "suggestion text mismatch")`；
  3. **陈旧性重校验**：对 `suggestion.knowledge_fact_refs` 每条，要求对应 `KnowledgeFact` 仍存在、`status==APPROVED`、且 `version == fact_revisions[fact_id]`（被撤销 / 取代 / 改版 → 409 `stale knowledge fact`，绝不在事实失效后继续代发）；
  4. 置 `suggestion.consumed=True`（原子一次性消费）；建 `Message(direction=OUTBOUND, is_auto_sent=True, sender_type=AGENT, confidence=suggestion.confidence, knowledge_fact_refs=suggestion.knowledge_fact_refs, escalation_flag=False, escalation_categories=[])`；`append_audit("cs.outbound_send", after={channel, is_auto_sent:True, message_id, suggestion_id})`；`session.commit()`。
  - 幂等：`suggestion.idempotency_key` UNIQUE + `consumed` 原子位，重复 `send_message(同 suggestion_id)` 第二次 → 步骤 1 即 409（无双发）。
- **人工路径（`auto_send=False`，仅 `actor.kind=="owner"`，服务端派生人工身份）**：可发任意文本（含 ESCALATE / `HUMAN_CONFIRM` 类「人工确认后代发」），建 `Message(direction=OUTBOUND, is_auto_sent=False, sender_type=OWNER)` + `append_audit("cs.outbound_send", after={is_auto_sent:False, sender_type:"owner", ...})`。**这是「有限自动 + 人工兜底」核心：AI 仅能在白名单内代发（且受「内容绑定 + 一次性消费 + 陈旧事实重校验」三重约束），其余一律人工。**
- AI agent（`kind=="agent"`）**禁止**任何出站发送（无论 `auto_send` 与否）→ 人工路径前置 `_assert_owner_actor` 返回 403；代发路径本就只允许 `AUTO_SEND` 建议绑定、且建议由服务端生成，agent 无法注入任意文本。

### 4.4 升级（`record_escalation` / 由 `generate_suggestion` 触发）

- 升级事件写 `Message(escalation_flag=True)` + `append_audit("cs.escalation")`；`assigned_human` 由 owner 显式接管（`assign_human`）设置，AI 不自动指派。

### 4.5 线索漏斗（`set_lead_stage`）

`set_lead_stage(session, actor, *, conversation_id, stage: LeadStage, reason)`：
- **仅人工（owner）可推进**：MVP 权威人工身份 = `actor.kind == "owner"`（服务端派生，经 `authenticate_owner` + `_assert_owner_actor`）；AI agent（`kind=="agent"`）**禁止**推进漏斗 / 指派 / 建跟进任务 → 403。无独立「人工坐席」身份（见 §9）。
- MVP **仅记录阶段，不自动推进**：阶段跃迁显式触发，写 `append_audit("cs.lead_stage", before/after)`。
- 跟进任务：当阶段推进到 `QUALIFIED` / `PROPOSAL` 时，**提供** `create_followup_task(session, actor, *, conversation_id, title)`（owner / 人工显式调用建 `Task`），**绝不自动建 Task**（零自动生产动作，仿 #110 T30 不变量）。

### 4.6 鉴权（per-project 403，仿 #110 §5）

- `_can_view_conversation(actor, conv)`：owner（同项目）→ 可见全部；无关 agent（含同项目无关 agent）→ **403**（非 404、非静默过滤）。`assigned_human` 由 owner 显式接管设置（记 owner 身份），仅用于 UI 展示，不影响鉴权（鉴权仍按 owner / 无关 agent 二分）。
- `get_conversation` / `list_conversations` / `get_messages` 同项目 per-资源鉴权；无关 agent 整体 403。

## 5. API 端点（仿 `feedback.py` 端点，在 `src/aios/api/app.py`）

所有端点复用 `authenticate_owner` / `authenticate_agent` 服务端派生 actor；`ServiceError` 经 `_translate` 转 HTTP；路由前置静态路径防遮蔽（仿 #110 P1-1）。

- `POST /conversations` —— 建会话（来自 Mock 适配器 webhook / owner 控制台）；`channel` 默认 `MOCK`。
- `GET /conversations`（静态前置）→ `GET /conversations/{id}` —— 列表 / 详情，per-project 403。
- `POST /conversations/{id}/messages` —— 入站消息（customer），建 `Message(INBOUND)`。
- `GET /conversations/{id}/messages` —— 消息列表（有界分页 `limit≤100`）。
- `POST /conversations/{id}/suggest` —— 调 `generate_suggestion`；返回 `CsSuggestion`（decision / text / confidence / escalation / fact_refs）。
- `POST /conversations/{id}/send` —— 调 `send_message`；`auto_send` 护栏（非白名单 → 409）；审计。
- `POST /conversations/{id}/escalate` —— 显式升级（通常 `suggest` 已自动升级；此端点供人工强制升级）。
- `PATCH /conversations/{id}/stage` —— `set_lead_stage`（**owner only**）；非法 / 越权（AI agent）→ 409 / 403。
- `POST /conversations/{id}/assign` —— `assign_human`（owner 接管，记 `assigned_human=owner` 身份，供 UI 展示）。
- `POST /conversations/{id}/followup-task` —— `create_followup_task`（**owner 显式**，绝不自动）。

## 6. 安全模型（不可信输入，复用 #110 §5）

- **per-project 403**：无关 agent → 403（显式，非过滤）；响应不泄露无关会话 / 消息。
- **绝不落敏感数据**：消息 / 审计**不写** header / cookie / 凭据 / 环境变量；`append_audit` 经 `redact_secrets`。出站 `body` 入 `after_snapshot` 前 ≤512 字符 + PII redact（邮箱 / 手机号 / 身份证 / 银行卡模式 → `[REDACTED-PII]`）。
- **有界响应**：列表 `limit≤100`；消息 `body` 列表截断展示（≤200 字符 + 省略号），详情才返回完整；PII redact。
- **客户文本是数据不是指令**：入站文本**绝不作系统指令**进入 prompt 指令位；默认确定性评分（无 LLM），**天然无 prompt-injection 面**。即使启用真实语义匹配，文本仅作 user-data、关闭 tool-calling、禁止改变系统行为。
- **零付费默认**：默认确定性规则 + 规则升级匹配，0 次付费模型调用；真实 LLM 路径需显式凭据 + 开关 + cost owner 门控（impl 落地，测试仅断言默认零调用）。
- **不创建 `KnowledgeFact`**：应答建议只读 `KnowledgeFact`，绝不反向写入 / 建知识对象。

## 7. TDD 测试计划（`tests/test_customer_service.py` + `tests/test_cs_zero_migration.py`）

**模型 / 迁移**
- T1 迁移：fresh 库建 `conversation` / `message` / `cs_suggestion` 三表成功；`alembic heads` == 1 且 == 新迁移 rev；`migrations/versions/` **仅 1 个新增文件**；升级路径（存量库）无 schema 冲突。
- T2 枚举往返：`CsChannel` / `LeadStage` / `MessageDirection` / `SenderType` 创建 / 读取 round-trip。

**应答建议 + 置信度阈值（§8.4）**
- T3 `AUTO_SEND`：FAQ / fact 命中 + `confidence ≥ 0.80` + 非升级 → `decision==AUTO_SEND`；降低置信度到 0.79 → 变 `HUMAN_CONFIRM`（阈值边界精确）。
- T4 `HUMAN_CONFIRM`：fact 命中 + `confidence < 0.80` → `decision==HUMAN_CONFIRM`（建议但需人工确认）。
- T5 `ESCALATE`：命中 `price/payment/promise/complaint/privacy` 任一类 → `decision==ESCALATE` + `escalation_categories` 正确 + 写 `AuditLog("cs.escalation")`；无任何 fact 命中（unknown）→ `ESCALATE`。
- T6 阈值 env 可调：设 `AIOS_CS_AUTO_SEND_CONFIDENCE=0.70` → 原 0.75 置信度用例由 `HUMAN_CONFIRM` 变 `AUTO_SEND`（验证 env 生效，仿 `REVIEW_PASS_MIN_CONFIDENCE`）。

**有限自动发送护栏（铁律）**
- T7 `auto_send=True` 绑定 `AUTO_SEND` 建议且 `text==suggestion.text` 且事实未失效 → 成功代发，`Message.is_auto_sent=True` + `AuditLog("cs.outbound_send")`；`suggestion.consumed=True`。
- T7b **重放拒绝**：同一 `suggestion_id` 第二次 `send_message` → `consumed` 已 True → **409**（无双发）。
- T7c **文本失配**：`text != suggestion.text`（NFC 归一后）→ **409** `suggestion text mismatch`（防凭合法建议发任意文本）。
- T7d **陈旧事实**：代发前对应 `KnowledgeFact` 被撤销 / 取代 / 改版（`version` 变或 `status!=APPROVED`）→ 重校验失败 → **409** `stale knowledge fact`（绝不在事实失效后继续代发）。
- T8 `auto_send=True` 但建议为 `HUMAN_CONFIRM` / `ESCALATE` / 无建议 → **409 拒绝代发**（AI 不得越白名单）。
- T9 人工路径（`auto_send=False`，`actor.kind=="owner"`）可发任意文本（含 ESCALATE 类「人工确认后代发」），审计照常，`sender_type=OWNER`。

**升级 / 审计**
- T10 每次出站（含代发）均写 `AuditLog("cs.outbound_send")`；`after_snapshot.body` 有界 + PII redact + 无密文 / 高熵串泄漏（仿 #110 T25 / T25b）。
- T11 升级事件写 `Message(escalation_flag=True)` + `AuditLog("cs.escalation")`；`assigned_human` 不自动指派。

**线索漏斗**
- T12 `set_lead_stage`：`actor.kind=="owner"` 成功跃迁 `visitor→lead→qualified→proposal→won`，写 `AuditLog("cs.lead_stage")`。
- T13 AI agent（`kind=="agent"`）自行 `set_lead_stage` → **403**（`_assert_owner_actor`）；非法阶段 → 409；MVP 不自动推进（无自动跃迁路径测试）。
- T13b **人工专属端点负向授权**：AI agent（`kind=="agent"`）调用 `set_lead_stage` / `assign_human` / `create_followup_task` / 人工路径 `send_message(auto_send=False)` → 一律 **403**（证明 AI agent 无法使用任何人工专属端点）；仅 `actor.kind=="owner"` 可。
- T14 `create_followup_task` 仅 owner 显式建 `Task`；**断言全流程 0 自动建 Task**（仿 #110 T30 聚合隔离）。

**鉴权 / 不可信输入**
- T15 同项目无关 agent `get_conversation(id)` → **403**（显式，非过滤）；其 `list_conversations` 整体 → 403。
- T16 入站文本含注入语句 → 原样惰性存储 / 展示；**不触发**任何工具 / 指令 / 阶段变更。
- T17 字段超长 / 非法 UTF → 422；`unicodedata.NFC` 归一后一致。
- T18 默认零付费 LLM 调用（mock / 确定性路径 0 次付费模型调用）。

**聚合隔离 / 零迁移（综合）**
- T19 聚合隔离断言（显式，仿 #110 T30）：客服全流程断言——`KnowledgeFact` 行数 0 增（只读）；`Event` / `DelegatedRun` / `Task`（除非显式 `create_followup_task`）0 自动增；`artifact` 除自身外无内容 / 阶段被改；无 `knowledge_candidate` / `review_*` 因客服流程新增；`AuditLog` 仅增（出站 / 升级 / 漏斗审计）。
- T20 零迁移证明（见 §2.4）：fresh / upgrade / 单 head / 仅 1 新增迁移 / 枚举往返 / `AuditLog` 惰性。
- T21 exact-head CI 绿 + ruff 清；既有 tests 无回归（全量 pytest 通过）。

## 8. 验收标准（合并门禁）

- [ ] 所有 TDD 测试通过（exact-head CI 绿），含 §7 全部新增用例（T1–T21）。
- [ ] ruff 清。
- [ ] **单一 Alembic 迁移**，链式挂在 `20260730_0001` 后，**单 head 不变**；不涉及 `artifact` 表（规避触发器字面引用）。
- [ ] **「常规问答」白名单 = 命中 + 置信度 ≥ 0.80 + 非升级类**（env `AIOS_CS_AUTO_SEND_CONFIDENCE` 可覆，默认 0.80）。
- [ ] **有限自动发送护栏**：代发须绑定 `AUTO_SEND` 建议 + 文本逐字相等 + 一次性消费 + 陈旧事实重校验（否则 409）；非法代发（非白名单 / 文本失配 / 重放 / 事实失效）→ 409；人工路径（owner）可发任意文本。
- [ ] 升级规则（报价 / 付款 / 承诺 / 投诉 / 隐私 / 低置信 / 未知）→ 转人工 + `AuditLog("cs.escalation")`。
- [ ] 所有对外发送审计（who / what / when / channel），`redact_secrets` + PII redact + 有界。
- [ ] 线索漏斗仅人工推进，MVP 不自动推进；跟进任务仅显式建，**0 自动建 Task**。
- [ ] **应答建议绝不创建 `KnowledgeFact`**，只读已审核事实。
- [ ] per-project 403（无关 agent 显式 403，非过滤）；不可信输入安全模型（NFC / 上限 / 注入惰性 / 零付费默认）。
- [ ] Codex(`gpt-5.6-sol`) APPROVE。

## 9. 不在范围 / 开放问题

- 不在（MVP）：真实企微接入（`WeComAppAdapter` / `WeComKfAdapter`，留后续 issue）；自动报价 / 自动收款 / 支付对接；自动发布；未审批客户承诺；客服全自动；跨渠道自动同步客户数据。
- 已对齐（v1）：适配层 + Mock 适配器（§8.1）；置信度阈值 0.80（§8.4，env 可覆）；白名单派生单一事实源；升级规则确定性；有限自动 + 人工兜底；漏斗仅人工；审计 / 鉴权 / 不可信输入复用 #110 契约。
- 开放（impl 定）：真实语义匹配 / LLM 分类的凭据与 cost owner 落地（V4 secret-store）；owner 控制台入口形态（会话看板 UI）；`Customer` 实体是否独立（MVP 用 `customer_ref` 字符串，不建独立表）。
- **已决议（v1 响应 Codex P1-3）**：MVP 权威人工身份 = `actor.kind == "owner"`（服务端派生，经 `authenticate_owner` + `_assert_owner_actor`），**不引入独立「人工坐席」身份**；AI agent（`kind=="agent"`）被 `_assert_owner_actor` 一律拒绝于人工专属动作（漏斗推进 / 指派 / 建跟进 / 人工发送）。多坐席 / 权限分级留后续 issue。

## 10. 分支与评审

- 状态：**待评审（PLAN PR）**。
- 本 PR：`feat/issue-109-cs-plan` → `main`，**仅 `docs/issue-109-cs-plan.md`，零代码零迁移**。
- 评审门禁：Codex(`gpt-5.6-sol`) APPROVE → 设 `gate:merge` / `next:owner` / `status:blocked` + assignee `QLM1234` → owner 授权 squash-merge（精确 head 锁）→ 删分支 → 清标签 → **保持 #109 open**（实现 PR 闭环时才关）。
- 实现分支：`feat/issue-109-cs-impl`（base `main`），**本计划 merge 后才创建**：TDD 实现 → `codex review` APPROVE → exact-head CI 绿 → 设 `gate:merge` / `next:owner` / `status:blocked` → owner 授权 squash-merge → 关 #109、清门禁标签。**绝不自动 merge**。
