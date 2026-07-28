# Issue #92 实施计划 — AI 员工工作日志「半自动采集」（四平台适配器）

> 基于最新 `main`（`1938c20`，Alembic head `20260728_0009`，#88 MVP 已合并）。
> 本文件是**实施计划**，不含任何实现代码。仅用于架构评审（Codex + owner）。
> 关联 Issue：#92。前置依赖：#88（MVP，已合并）。后续：#93（V3 LLM 价值判定，须本 Issue 合并后才启动）。

---

## 0. 范围与铁律（来自 Issue #88 / #92 与 owner 追加约束）

V2 只做一件事：**让 AI 团队（Codex / Hermes / WorkBuddy / Coze）的工作产出能半自动汇入 AIOS 工作日志**，减少 #88 MVP 的人工粘贴负担。

**必须遵守的约束（owner 明确 + 继承自 #88 铁律）：**

1. **复用 #88 主线，不重写**：`Artifact(type=WORK_LOG)`、`POST /work-logs`、`attest_work_log`、`KnowledgeHarvester`、`ContentFeed`、`_owner_cli` 认证全部复用。V2 **不引入任何新表、不改任何 #88 模型/迁移**。
2. **attestation 信任边界不变**：适配器只 produce `WorkLogSubmit` 草稿（UNVERIFIED）；`attest_work_log` 仍是 WORK_LOG 达到 `APPROVED` 的唯一路径；`owner_approve_review` 已拒绝 WORK_LOG，本计划不改动此不变量。
3. **不自动 APPROVE**：采集产出的日志必须人工 attest 后才能进入收割/知识沉淀链路（沿用 #88 的 fail-closed 证据要求）。
4. **不信任平台自报身份**：平台原始产出里的「我是谁/我做了什么」一律视作不可信数据，仅作 `metadata_json` 展示/筛选输入；`project_id`、owner 身份只能来自采集配置与认证边界。
5. **不改动 #88 模型 / 迁移 / 信任边界**：V2 不新增任何 SQLModel 表、不新增 Alembic 迁移、`owner_approve_review` 守卫与 `provenance` 信任链一律不动。唯一必要的契约扩展（均为可选参数 / JSON 列内增键，**无迁移**、不动信任边界）：
   - `WorkLogSubmit` 增加可选字段 `source_platform: str | None`，由 `submit_work_log` 写入 `metadata_json.source_platform`，用于 feed 展示/筛选；
   - `attest_work_log` 增加可选覆写参数 `should_enter_kb` / `content_value`，owner 在 attest（人工动作）时裁定 KB 资格，仅改 `metadata_json` 两判定字段，不触动证据三件套 / `provenance`。
   若未来需把采集源 Agent 写入 `provenance`，作为独立评审项（不阻塞 V2）。
6. **凭据不入库**：各平台 API key / token 走环境变量或外部 secret store（复用 `Agent.secret_ref` 字段语义，但 V2 不强制落库），绝不以明文写入 `Artifact` / `metadata_json`。
7. **fail-closed**：任一适配器异常、网络失败、规范化失败 → 该条采集中止并记 `AuditLog(collector.error)`，不影响主线与其它平台采集；不得静默吞错或伪造日志。
8. 按 TDD 顺序：适配器 → 脚本 → 测试 → 验收（§9、§11）。

---

## 1. 复用的现有事实（代码已确认，基于 `1938c20`）

| 组件 | 现状（来自 #88 MVP） | 本计划用法 |
|------|------|-----------|
| `POST /work-logs`（`app.py`） | 必填 `Idempotency-Key` 头；body 含 `project_id`(必填)、`report_type`、`task_ref?`、`produced_by_agent_id?`、`execution_assignment_id?`、7 汇报字段；`actor=authenticate_owner` | 作为**外部集成路径**（如 Codex/Coze 远程 push）可选使用，契约一致；V2 本地 CLI 采集脚本走服务层（见 §5/§6），不经此 HTTP 端点 |
| `WorkLogSubmit`（#88 schema） | 7 汇报字段 + `project_id`(必填) + `report_type` + `task_ref?` + `produced_by_agent_id?` + `execution_assignment_id?` | **最小扩展**：加可选 `source_platform: str | None`（V2 新增），由 `submit_work_log` 写入 `metadata_json.source_platform`，无迁移 |
| `WorkLogService.submit_work_log`（`work_log.py`） | 校验 actor=owner、project 存在；幂等（同键 200 / 异载荷 409）；只产 `UNVERIFIED` | 复用，并多接一个可选 `source_platform` 写入 metadata（**同时纳入 `_request_fingerprint` 的 business_fields**，确保同键异来源平台 → 409 而非误 replay）；不改校验/信任边界 |
| `attest_work_log`（`work_log.py`） | `BEGIN IMMEDIATE` 并发仲裁 + exactly-one 证据 + `risk_level=L1`；APPROVED 唯一路径 | 复用，人工 attest 采集草稿 |
| `ContentValueJudge`（`work_log.py`） | 纯启发式；`should_enter_kb` / `content_value` / `content_angle` | 复用，采集草稿经人工 attest 后同等参与收割 |
| `KnowledgeHarvester` / `ContentFeed` | 只消费 `APPROVED` 日志；scope/排序/分页已定义（合并 logs+facts 单窗口 `[offset:offset+limit]`，按 `(created_at,id) DESC` 排序） | 复用 + **最小扩展**：`get_content_feed` 加可选 `source_platform` filter；**响应/分页语义（修正 v15/v16 P1）**：①**无 `source_platform`** 时行为与 #88 完全一致（扁平 `list` + 合并单窗口，零回归）；②**提供 `source_platform`** 时进入「分片过滤视图」——返回**结构化响应** `{ "work_logs": [...], "facts": [...] }`，两分片**各自独立分页**且永不通算为 `2*limit`：新增 `log_limit`/`log_offset`（缺省沿用 `limit`/`offset`）作用 `work_logs` 分片、`fact_limit`/`fact_offset`（缺省沿用 `limit`/`offset`）作用 `facts` 分片；平台 filter **仅**作用于 `work_logs` 分片（facts 永不被平台过滤、fact 窗口只取决于 `fact_limit`/`fact_offset`，与 log 数量/匹配无关），故 facts 字面「不受影响」且全局每分片页长受限、偏移可描述；每个分片内部仍按 `(created_at,id) DESC` 排序；API `GET /content-feed?source_platform=` 加该查询参数 |
| `scripts/_owner_cli.py` | 共享 owner 认证边界（HTTP + CLI 复用） | 采集脚本复用此模块注入 `actor` |
| `Agent`（#88 迁移 0009） | 已含 `platform` / `external_ref` 字段 | 采集配置绑定一个已登记 `Agent`（platform 标记来源），但 V2 **不**把该 agent 写入 `provenance.produced_by_agent_id`（见 §0.5） |
| `Idempotency-Key` 契约（#88 §5） | `work_log:{project_id}:{sha256(client_key)[:32]}` | 采集脚本先按 `--agent-ref` 解析为唯一 `Agent`，用其不可变 `Agent.id` 生成稳定幂等键（如 `collector:{agent_id}:{external_id}`，`Agent.id` 全局唯一、无 `external_ref` 共享碰撞风险），保证重跑幂等、跨 UTC 午夜安全 |

---

## 2. 模块划分（新增 `src/aios/collectors/`，不新建表）

```
src/aios/collectors/
├── __init__.py
├── base.py            # BaseCollector 抽象：fetch_raw → normalize → WorkLogSubmit
├── codex.py           # CodexAdapter（codex CLI / API 拉取会话摘要）
├── hermes.py          # HermesAdapter（读服务器上 Hermes 产出文件/DB）
├── workbuddy.py       # WorkBuddyAdapter（控制面导出 artifact / 任务结果）
└── coze.py            # CozeAdapter（Coze API / Webhook 回传 JSON）
```

- 每个 adapter 都是 `BaseCollector` 子类，职责单一：**把某平台的原始产出规范化成 `WorkLogSubmit`**，它本身不碰 DB、不调 `submit_work_log`——提交由脚本层统一负责（保持「采集」与「入库」分离，便于测试与 fail-closed）。
- 不新建任何 SQLModel 表，不新增 Alembic 迁移（Alebmic head 保持 `20260728_0009`）。

---

## 3. 适配器设计

### 3.1 `BaseCollector`（抽象基类）

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class RawLog:
    external_id: str          # 平台侧唯一标识（用于幂等键派生）
    captured_at: str          # ISO8601 UTC，平台产出时间
    raw: dict                 # 平台原始负载（不可信，仅展示/规范化输入）
    source_platform: str      # "codex" | "hermes" | "workbuddy" | "coze"

class BaseCollector(ABC):
    platform: str

    @abstractmethod
    def fetch_raw(self, *, agent, since=None) -> list[RawLog]:
        """拉取平台原始产出。网络/解析失败须抛 CollectorError（不静默）。"""

    @abstractmethod
    def normalize(self, raw: RawLog, *, project_id: str) -> "WorkLogSubmit":
        """把一条 RawLog 映射成 WorkLogSubmit。纯函数：输入 RawLog + 已验证的 project_id，输出不含 DB/网络/时间依赖，便于穷尽测试。"""
```

- `normalize` 是**纯函数**：输入 `RawLog` + 采集配置传入的已验证 `project_id`、输出 `WorkLogSubmit`，不依赖 DB / 网络 / 时间（`project_id` 由脚本 `--project-id` 注入，非来自平台原始负载，见 §5）。这是测试的核心面（§8）。
- `fetch_raw` 的失败（API 超时、token 失效、schema 漂移）统一包装为 `CollectorError`；脚本层捕获后记 `AuditLog` 并跳过该平台，**不影响其它平台与主线**（§0.7）。

### 3.2 四平台适配器

| 适配器 | 采集方式（fetch_raw） | 凭据 | 备注 |
|--------|----------------------|------|------|
| `CodexAdapter` | 调 Codex CLI / API 拉取近期会话/任务输出（如 `codex` 本地会话、或 ChatGPT 版 Codex 的对话摘要导出） | `CODEX_API_KEY` env | 设计文档 §中期：ChatGPT 经 MCP connector 或定时 push 摘要；V2 采用 collector pull 模式，MCP 仅作可选传输，**不新建 MCP server** |
| `HermesAdapter` | 直读服务器上 Hermes 的产出（已跑在 `47.90.161.151`，有 `smart_router` / `hermes config`）；读 agent 输出文件或本地 DB | 服务器本地文件权限 / `HERMES_CONFIG_PATH` | 设计文档 §中期：Hermes 本就跑在服务器，直接读产出写 Artifact |
| `WorkBuddyAdapter` | WorkBuddy 是 AIOS 控制面，导出其 artifact / 任务结果为结构化 JSON | 本地会话凭证 | 设计文档 §中期：WorkBuddy 天然可写；V2 走「导出 → 规范化」 |
| `CozeAdapter` | Coze Open API 拉取工作流运行结果，或接收 Coze Webhook 回传的 JSON | `COZE_API_KEY` / Webhook secret | 设计文档 §中期：Coze 用 API + Webhook 回传 |

- 每个 adapter 在 `fetch_raw` 内只负责「取数 + 解析成 `RawLog`」，不负责「理解内容」——内容理解交给 `normalize`。
- 采集配置（哪个 `Agent`、`project_id`、`since`）由脚本命令行参数 / 配置文件传入，不写死在 adapter 内。

---

## 4. 规范化映射（RawLog → WorkLogSubmit，7 汇报字段 + source_platform）

`WorkLogSubmit`（#88 schema）字段：`project_id`(调用方填)、`report_type`、`what_done`、`why`、`problem`、`solution`、`new_knowledge`、`content_value?`、`should_enter_kb?`、`content_angle?`。

V2 规范化**额外注入** `metadata_json.source_platform`（平台来源标记，用于 feed 筛选/展示，不进 provenance）；`project_id` 由采集配置（脚本 `--project-id`）显式传入 `normalize(raw, project_id)`，写入 `WorkLogSubmit.project_id`：

```json
{
  "report_type": "daily",
  "what_done": "...", "why": "...", "problem": "...",
  "solution": "...", "new_knowledge": "...",
  "content_value": "high | medium | low | none",
  "should_enter_kb": true,
  "content_angle": "...",
  "source_platform": "codex | hermes | workbuddy | coze"
}
```

各平台 `normalize` 的映射约定（纯函数，可穷尽测试）：

| 字段 | Codex | Hermes | WorkBuddy | Coze |
|------|-------|--------|-----------|------|
| `what_done` | 会话目标 / 任务描述 | agent 产出主题 | artifact 标题 | 工作流名称 + 触发 |
| `why` | 任务背景 | 内容需求背景 | 任务上下文 | 业务触发原因 |
| `problem` | 遇到的阻塞 | 内容痛点 | 待解决问题 | 流程卡点 |
| `solution` | 采取的行动 | 成稿/策略 | 交付物 | 工作流输出 |
| `new_knowledge` | 关键结论 / 踩坑 | 可复用方法论 | 新认知 | 沉淀经验 |
| `content_value` | 默认 `low`（**不**自动 medium；采集日志默认不可收割，KB 资格由 owner attest 覆写 `should_enter_kb`/`content_value` 决定，避免绕过 attest 时裁定） | 同左 | 同左 | 同左 |
| `should_enter_kb` | 默认 `False`；owner 可在 attest 时通过可选参数覆写为 `True`（见 §6） | 同左 | 同左 | 同左 |
| `content_angle` | `new_knowledge` 前 80 字 | 同左 | 同左 | 同左 |

- `normalize` **不**填 `produced_by_agent_id` / `task_ref` → 规避 #88 §6 的 task_ref 必填（采集活动不锚定 task），日志以 project-scoped、无 agent 归属落库，符合「复用不重写」铁律（§0.5）。
- `source_platform` 仅作展示/筛选维度；`provenance_json` 保持 #88 现状（由认证边界 owner + 无 agent 归属）。
- 平台原始负载中的任何「身份/权限」声明一律丢弃，不进入 `WorkLogSubmit`。
- `source_platform` 在 `ContentFeed` 提供按平台筛选与展示能力（§1 / §8 测试），形成「采集标记 → feed 区分平台」闭环；但**不**进入 `provenance` 信任链。
- 采集日志的 KB 资格严格由 owner 在 attest 时裁定（§6）：默认 `content_value=low` + `should_enter_kb=False` 双重确保默认不可收割，杜绝「长文本自动 medium 被 harvest 吞入」绕过人工决策。

---

## 5. 认证与凭据（复用 `_owner_cli`）

- 采集脚本统一经 `scripts/_owner_cli.py` 注入 `actor: ActorContext(kind="owner", ...)`（与 #88 四个脚本同机制），**绝不**在 collector / 脚本内自铸 owner 身份（`submit_work_log` 仍校验 `actor.kind=="owner"`）。
- 平台凭据：
  - 只从环境变量 / 外部 secret store 读取（`CODEX_API_KEY` / `COZE_API_KEY` / `HERMES_CONFIG_PATH` 等）。
  - **绝不**写入 `Artifact` / `metadata_json` / 任何 DB 行。
  - `Agent.secret_ref` 字段（#88 迁移 0009 已存在）语义保留为「指向外部 secret 的引用」，V2 不强制落库、不读取明文。
- 采集脚本须显式传 `--project-id`（对应 #88 `project_id` 必填）与采集目标 `--agent-ref`（定位已登记 `Agent`，仅用于配置绑定与日志溯源展示，不写 provenance）。
- **提交路径（修复 P2）**：V2 本地 CLI 采集脚本经 `_owner_cli` 取得已认证 `actor` 后，**直接调用 `WorkLogService.submit_work_log(session, ..., actor=owner_actor, source_platform=...)`（服务层）**，与 #88 的 `scripts/submit_work_log.py` 同机制；**不经** `POST /work-logs` HTTP 端点（`authenticate_owner` 从 HTTP 头独立构造 actor，`_owner_cli` 无法把 actor 注入 HTTP 请求，强行走 HTTP 会 401）。`POST /work-logs` 仅作为 Codex/Coze 等**远程外部**集成的可选路径，届时由各适配器带 owner 认证头调用。

---

## 6. 半自动语义与信任边界（草稿 → 人工 attest）

- **采集 = 产草稿**：脚本先按 `--agent-ref` 解析出唯一 `Agent`，对其每条 `WorkLogSubmit` 直接调 `WorkLogService.submit_work_log`（带稳定 `Idempotency-Key=collector:{agent_id}:{external_id}`，`source_platform` 透传）→ 产出 `UNVERIFIED` 的 `Artifact`（服务层调用，见 §5）。
- **人工 attest = 唯一晋级路径 + KB 资格裁定**：owner 在 owner console / `scripts/attest_work_log.py` 逐条检视草稿 → `attest_work_log(artifact_id, actor, *, should_enter_kb=None, content_value=None)` → `APPROVED` → 方可进入 `KnowledgeHarvester` 收割与 `ContentFeed` 展示。**最小扩展**：`attest_work_log` 接受可选 `should_enter_kb` / `content_value` 覆写参数，owner 在 attest（人工动作）时据草稿内容裁定 KB 资格；未传则沿用提交时 metadata 值。该覆写只改 `metadata_json` 的两个判定字段，并**原子重算 `Artifact.checksum`**（沿用 #88 `submit_work_log` 的 `sha256(canonical_json(metadata))` 派生规则），保证 checksum 与内容一致、**不**触动证据三件套 / `provenance` / 信任边界（解决「low 值日志永久不可收割」矛盾 + checksum 陈旧问题）。
  - **覆写冲突 fail-closed（防 owner 决策静默丢失）**：已 `APPROVED` 的二次 attest，若请求含 `should_enter_kb`/`content_value` 覆写且与现有 metadata 冲突 → `ServiceError(409, "conflicting attestation override")`；无覆写或值一致 → 幂等 no-op 返回（与 #88 证据完整性一致）。绝不静默忽略 owner 决策。
  - **覆写审计轨迹（KB 资格决策的不可抵赖记录）**：每条 attestation 的 `AuditLog` 必须**统一快照**覆写前 / 覆写后的 `should_enter_kb` 与 `content_value` 值——即记录字段恒含 `prev_should_enter_kb` / `prev_content_value` / `next_should_enter_kb` / `next_content_value` **外加**现有 `review_status`（原 attestation `AuditLog` 仅记 `review_status`，不足以解释「一条采集日志为何变得可收割」，现统一扩展为含前后判定值）。**无论是否提供覆写，均记录这 4 个字段**：无覆写时 `prev_* == next_*`（`should_enter_kb` 沿用提交值、`content_value` 沿用 `low`），仍为显式快照，保证所有 attestation 审计轨迹 schema 一致、可统一断言（不区分「有无覆写」两态，杜绝实现/测试契约歧义）。该快照是信任决策的一部分，与 checksum 重算一并落库，确保任何 KB 资格变更可事后追溯（§8 补 `test_attest_override_audit_trail` 断言）。
- **接口暴露（owner 输入面，必需的闭环）**：HTTP `POST /work-logs/{id}/attest` 接受可选 body `{ "should_enter_kb": bool, "content_value": str }`（无 body 时沿用 metadata 默认，保持向后兼容）；`scripts/attest_work_log.py` 在逐条确认时提示 owner 是否覆写 `should_enter_kb`（默认否）并可选填 `content_value`，二者透传给 `attest_work_log`；`content_value` 覆写须经枚举校验（`high|medium|low|none`），非法值 → 422，与 #88 提交契约一致。§8 补对应接口/CLI 测试。
- **信任边界零改动**：
  - 适配器产物与人工 attest 之间无任何自动桥接；`should_enter_kb` 默认 `False`，收割门槛与 #88 一致。
  - `owner_approve_review` 仍拒绝 `WORK_LOG`（#88 已有），V2 不触碰。
  - `attest_work_log` 的 `BEGIN IMMEDIATE` 并发仲裁、exactly-one 证据、fail-closed 409 全部复用。
- **幂等**：同平台同 `external_id` 重跑 → 同 `Idempotency-Key` → 200 replay no-op（§1 末行）；采集脚本可安全地周期性重跑。

---

## 7. 脚本（非技术可跑；复用 `_owner_cli`）

- `scripts/collect_from_codex.py` / `collect_from_hermes.py` / `collect_from_workbuddy.py` / `collect_from_coze.py`：参数 `--project-id`（必填）、`--agent-ref`（必填，定位已登记 Agent）、`--since`（可选，ISO8601）、`--dry-run`（只打印将提交的 `WorkLogSubmit`，不入库）。
  - 流程：经 `_owner_cli` 认证取得 owner `actor` → 按 `--agent-ref` 解析为唯一 `Agent`（取其不可变 `id`）→ **平台归属校验（fail-closed，修正 v18 P2）**：断言 `Agent.platform == adapter.platform`（如 Codex 脚本必须绑定 `platform="codex"` 的 Agent）；不匹配 → 该平台运行**直接拒绝**（记 `AuditLog(collector.config_error)` + 计入 `platform_errors` + 非零退出），**绝不**用无关 Agent 的 id 作幂等命名空间、也绝不把脚本硬编码的 `source_platform` 落到错误 Agent 名下（避免来源标记/筛选/配置绑定被污染）→ 再实例化对应 `Adapter`（绑定已校验 `Agent` 与 `--project-id`）→ `fetch_raw` → **逐条** `try: normalize(raw, project_id=...) → submit_work_log(actor=owner_actor, source_platform=adapter.platform, idempotency_key="collector:{agent_id}:{external_id}")`；任一条 `normalize` / `submit_work_log` 异常 → 记 `AuditLog(collector.record_error)` + 跳过该条、继续后续记录（**不**中止平台），并累加 `record_errors` 计数。
  - **聚合失败退出（fail-closed 不可静默丢失，修正 v17 P1）**：脚本维护两类失败计数——`platform_errors`（平台级 `fetch_raw` 抛 `CollectorError` 取数失败）与 `record_errors`（单条 normalize/submit 异常，已逐条跳过并审计）。`platform_errors > 0` **或** `record_errors > 0` → 脚本最终返回**非零退出码**（如 `exit 1`），以便 cron / `collect_all.py` 触发重试与告警，**避免「取数成功但个别记录被丢弃」被误判为成功、进而推进 `--since` 丢失重试/告警路径**；已成功入库的数据不受影响。平台级 `fetch_raw` 抛 `CollectorError`（取数失败）→ 该平台整体中止、记 `AuditLog(collector.error)`、打印错误、继续其它平台。
- **必含组件** `scripts/collect_all.py`：依次跑四平台（Codex→Hermes→WorkBuddy→Coze），是「平台级失败隔离 + 聚合退出」契约的**唯一承载者**（单平台脚本无法提供跨平台隔离，修正 v17 P2）；任一平台 `fetch_raw` 抛 `CollectorError` 时该平台中止但其余平台继续，最终**聚合**所有平台的 `platform_errors` + `record_errors` 返回统一非零退出码（与单平台脚本同一聚合规则）。该脚本为 cron 入口，缺失则 fail-closed 隔离契约无法满足，故列为 V2 **必交付**范围（§8 补 `test_collect_all_aggregates_exit_status`）。

---

## 8. 测试清单（TDD）

**适配器（纯函数 normalize）**
- `test_codex_normalize_basic` / `test_hermes_normalize_basic` / `test_workbuddy_normalize_basic` / `test_coze_normalize_basic`：给定 fixture `RawLog` + `project_id` → 断言 `WorkLogSubmit` 字段映射正确（含 `project_id` 填充与 `source_platform` 注入）。
- `test_normalize_injects_source_platform`：输出 `WorkLogSubmit.source_platform` 精确等于 adapter.platform（metadata 层断言留给服务集成测试）。
- `test_normalize_does_not_set_agent_provenance`：输出**不**含 `produced_by_agent_id` / `task_ref`（规避 #88 §6 不变量）。
- `test_normalize_drops_untrusted_identity`：原始负载里的「agent/owner 声明」字段被忽略，不出现在输出。
- `test_normalize_content_value_default`：无论 `new_knowledge` 长短/是否含关键词，归一化输出 `content_value` 恒为 `low`（默认不可收割，KB 资格由 owner attest 覆写决定，避免启发绕过 harvest）。
- `test_normalize_should_enter_kb_default_false`：默认 `False`。

**fetch_raw 失败路径（fail-closed）**
- `test_codex_fetch_api_timeout_raises_collector_error`：mock API 超时 → `CollectorError`（不返回脏数据）。
- `test_coze_fetch_bad_schema_raises`：schema 漂移 → `CollectorError`。
- `test_hermes_fetch_missing_config_raises`：配置缺失 → `CollectorError`。

**集成（adapter → POST /work-logs → attest）**
- `test_collector_to_endpoint_full_chain`：mock `fetch_raw` → 脚本经 `_owner_cli` 认证后调 `WorkLogService.submit_work_log` 服务层（与 CLI 同路径，非 HTTP 端点）→ 落 `UNVERIFIED` `Artifact`，`metadata.source_platform` 正确、`provenance` 无 agent 归属、`Idempotency-Key=collector:{agent_id}:{external_id}` 生效。
- `test_collector_idempotent_replay`：同 `external_id` 重跑 → 同 `artifact_id`，无第二行（幂等键生效）。
- `test_collector_draft_not_harvestable`：`UNVERIFIED` 采集草稿即使 `content_value=high` 也不产候选（复用 #88 收割前置）。
- `test_collector_attest_then_harvestable`：采集草稿默认 `content_value=low` + `should_enter_kb=False`（不可收割）；attest 时**覆写** `should_enter_kb=True`（或 `content_value=high`）后，草稿进入收割（与 #88 同链路，验证默认拒绝 + 显式 opt-in 闭环）。
- `test_feed_log_entries_include_source_platform`：feed 返回的工作日志条目含 `source_platform` 字段（来自 `metadata_json`）。
- `test_feed_filter_by_source_platform`：用**有界 fixture**（固定 `log_limit`/`fact_limit`/`offset`）断言——`GET /content-feed?source_platform=codex&log_limit=L&fact_limit=F&log_offset=O&fact_offset=O'` 返回**结构化响应**，`resp["work_logs"]` 条目**仅含** `source_platform=codex` 匹配项且数量 ≤ L；`resp["facts"]` 的 id 集合 == 直接对 fact 排序取 `[fact_offset:fact_offset+F]` 的 fact 子集（与 log 数量/是否匹配平台无关，证明 facts 独立分页、不受 log filter 影响且全局页长不爆 `2*limit`）；另构造一条非匹配平台日志（如 hermes）验证其存在也不改变 `resp["facts"]` 集合；每个分片内部按 `(created_at,id) DESC` 排序，scope/排序不变量不变。

**脚本层**
- `test_collect_script_dry_run_no_db_write`：`--dry-run` 不落库。
- `test_collect_script_injects_owner_actor`：缺/非 owner 凭证 → 拒绝（复用 `_owner_cli`）。
- `test_collect_script_rejects_mismatched_agent_platform`：`--agent-ref` 解析出的 `Agent.platform` 与脚本 adapter 的 `platform` 不符（如 Codex 脚本绑定 `platform="hermes"` 的 Agent）→ 该平台运行被拒绝（非零退出 + `AuditLog(collector.config_error)` + 计入 `platform_errors`），**无任何日志**以错误来源标记落到该 Agent 命名空间下（验证来源/幂等命名空间不被污染）。
- `test_collect_script_one_platform_error_continues`：某平台 `CollectorError` → 其它平台仍采集、退出码合理、记 `AuditLog`。
- `test_collect_script_one_record_error_continues`：某条记录的 `normalize` / `submit_work_log` 抛异常 → 该条记 `AuditLog` 并跳过、其余记录仍入库、不触发平台级中止；**且脚本最终退出码非零**（单条记录失败计入 `record_errors` 聚合，避免 cron 误判成功推进 `--since`）。
- `test_attest_endpoint_accepts_override`：`POST /work-logs/{id}/attest` 带 body `{"should_enter_kb": true}` → artifact 晋级 APPROVED 且 `metadata.should_enter_kb==true`（覆写生效、向后兼容无 body）。
- `test_attest_cli_prompt_override`：`scripts/attest_work_log.py` 在确认时选择覆写 `should_enter_kb` → 透传至 `attest_work_log`，metadata 更新。
- `test_attest_override_recomputes_checksum`：attest 覆写 `should_enter_kb`/`content_value` 后 `Artifact.checksum` 随之重算（与 `sha256(canonical_json(metadata))` 一致），无陈旧 checksum。
- `test_attest_rejects_invalid_content_value`：attest body 传 `content_value="urgent"` → 422（枚举校验与 #88 提交一致），artifact 不被覆写。
- `test_attest_conflicting_override_409`：已 APPROVED 日志二次 attest 带与现有 metadata 冲突的覆写 → 409 fail-closed（owner 决策不静默丢失）；值一致或无覆写 → 幂等 no-op。
- `test_collect_all_aggregates_exit_status`：`collect_all.py` 跑四平台，构造某一平台 `fetch_raw` 抛 `CollectorError` + 另一平台单条记录 `normalize` 失败 → 断言：故障平台中止但其余平台仍采集入库、最终 `collect_all` 退出码非零（聚合 `platform_errors`+`record_errors`），且故障明细（哪平台/哪记录）记入 `AuditLog` 可查（验证必含组件满足跨平台失败隔离契约）。
- `test_attest_override_audit_trail`：attest 覆写 `should_enter_kb=True`（或 `content_value=high`）后，对应的 attestation `AuditLog` 必须快照覆写前/后值——`prev_should_enter_kb=False`/`next_should_enter_kb=True`（及 `prev_content_value=low`/`next_content_value=high` 当覆写 content_value）且记录 `review_status`；断言 `AuditLog` 行**恒含**这 4 个前后值字段（无论本次 attest 是否提供覆写：无覆写时 `prev_*==next_*`，仍为显式记录，与 §6 统一 schema 一致），验证 KB 资格变更可审计、杜绝「可收割却无解释」。

**不变量回归（确保 V2 未破坏 #88）**
- 复用 #88 的 `test_work_log.py` / `test_api_work_log.py` 全量通过（CI 已覆盖）。
- `test_owner_approve_review_still_rejects_work_log`：V2 未改动此守卫（回归）。

---

## 9. 验收命令

```bash
# lint（ruff 0.15.22, line-length 100）
aios-v0/.venv/Scripts/python -m ruff check src tests alembic

# 聚焦测试（新增 collectors + 集成）
pytest tests/test_collectors.py -q

# 端点/主线回归（确保未破坏 #88）
pytest tests/test_work_log.py tests/test_api_work_log.py -q

# 全量（以 exact-head CI 为准）
pytest -q
```

验收门槛：聚焦 + 全量 `pytest` 绿；`ruff` 绿；**exact-head CI 绿**；Alembic head 仍为 `20260728_0009`（V2 无迁移）。

---

## 10. Out of scope（不扩大）

- `ContentValueJudge` 的 LLM 自动打分（**#93 V3**）。
- AIOS 统一 Agent 中台、自动注册实体（**V4**）。
- 自动 attest / 自动 APPROVED（永远人工，信任边界铁律）。
- 暴露 MCP server 给外部 Agent 调用（V2 是 collector **pull** 模式；MCP 仅作 ChatGPT adapter 的可选传输，不新建 server）。
- 日志修改/删除端点、候选自动审核、实时 Hermes 推送、任何写外部副作用。
- 任何 #88 **模型 / 迁移 / 信任边界**代码的改动（V2 仅最小扩展，均无迁移、不动信任边界：①`WorkLogSubmit` 可选字段 `source_platform`；②`ContentFeed` 的 `source_platform` filter/返回字段；③`attest_work_log` 可选覆写参数 `should_enter_kb`/`content_value`，仅改 `metadata_json` 两判定字段，不触动证据三件套/`provenance`；不触碰 `Artifact` 模型、`owner_approve_review` 守卫）。

---

## 11. TDD 实施顺序（实现 PR 采用）

1. **collectors 包骨架**：`__init__.py` + `base.py`（`BaseCollector` / `RawLog` / `CollectorError`）+ 四个空 adapter（仅 `platform` 常量 + 方法签名）。
2. **normalize 映射**：逐平台实现 `normalize` 纯函数 + §8 适配器单测（先红后绿）。
3. **fetch_raw**：逐平台实现取数 + 失败路径测试（mock 网络/文件）。
4. **脚本**：四个 `collect_from_*.py`（单平台）+ 必含的 `collect_all.py`（跨平台聚合入口）+ `--dry-run` + `_owner_cli` 集成 + §8 脚本测试（含 `test_collect_all_aggregates_exit_status`）。
5. **集成测试**：adapter → `WorkLogService.submit_work_log`（服务层，经 `_owner_cli` 认证）→ `attest_work_log`（可选覆写 `should_enter_kb`）全链路 + 幂等 + fail-closed。
6. **验收**：§9 全绿 + exact-head CI 绿 + #88 回归全绿。

---

## 12. 与 #88 / #90 的关系

- **#88（MVP，已合并 `1938c20`）**：提供全部基础设施（端点、attest 信任边界、harvest、feed、迁移 0009）。V2 是其「采集侧」的扩展，**不改动模型/迁移/信任边界**；仅最小扩展 `WorkLogSubmit` 可选字段（§0.5）。
- **#90（MVP 实施计划）**：本计划沿用在 #90 中确立的契约风格与铁律（幂等、provenance、fail-closed、scope），并显式继承 #88 §6 的归属校验不变量（通过「不传 `produced_by_agent_id`」规避 task_ref 必填，而非修改它）。
- **#93（V3 LLM 价值判定）**：依赖本 Issue 采集落地后的数据规模；V3 才引入 `LlmJudge` 异步重判，V2 的 `content_value` 仍走 #88 启发式。

---

## 13. v1 修订摘要（对照 Codex 评审两点）

| # | 评审阻断点 | 落点 |
|---|-----------|------|
| 1 (P1) | 未改的 `WorkLogSubmit` 无 `source_platform` / `metadata_json` 透传，投递会被 Pydantic 忽略、筛选/测试失败 | §0.5 / §1：最小扩展 `WorkLogSubmit` 加可选 `source_platform`，由 `submit_work_log` 写入 `metadata_json`（JSON 列内增键，无迁移）；展示/筛选维度落实，不进 provenance |
| 2 (P2) | `normalize` 仅收 `RawLog`（无 `project_id`），无法构造必填的 `WorkLogSubmit.project_id` | §3.1 / §4：`normalize(raw, *, project_id)` 显式接收已验证 `project_id`（来自脚本 `--project-id`，非平台负载），纯函数性保持 |
| 3 (P1, v2) | 持久化 `source_platform` 但声明 `ContentFeed` 不变、无 feed 实现/测试，承诺能力无法交付 | §1 / §4 / §8：`ContentFeed` 加 `source_platform` filter + 返回字段（API `?source_platform=`），补对应测试，形成采集→feed 闭环 |
| 4 (P2, v2) | `_owner_cli` 无法把 actor 注入 `POST /work-logs` HTTP 端点，脚本会 401 | §5 / §6 / §7：V2 本地 CLI 脚本经 `_owner_cli` 认证后**直接调 `WorkLogService.submit_work_log` 服务层**（与 #88 脚本同机制），HTTP 端点仅留作远程外部集成可选路径 |
| 5 (P1, v3) | 同平台多 agent/账号时 `collector:{platform}:{external_id}` 幂等键碰撞，replay 错 artifact 或对异 payload 409 | §1 / §6 / §7：幂等键加 `agent_ref` 命名空间 → `collector:{platform}:{agent_ref}:{external_id}` |
| 6 (P1, v3) | fail-closed 仅捕获 fetch_raw，单条 normalize/submit 失败会中止循环/平台 | §7 / §8：逐条 `try` 包裹 normalize+submit，单条失败记 `AuditLog`+跳过、继续后续；平台级中止仅保留给 `fetch_raw` 的 `CollectorError` |
| 7 (P1, v4) | 新 `source_platform` 未纳入 `_request_fingerprint`，同键异来源平台会误 replay 旧 artifact 而非 409 | §1：扩展 `submit_work_log` 时把 `source_platform` 一并纳入幂等指纹 business_fields，与 #88 §5 机制一致 |
| 8 (P1, v5) | 同平台多 agent 共享 `external_ref` 时 `agent_ref` 进键仍碰撞（`external_ref` 非唯一/未索引） | §1 / §6 / §7：先解析 `--agent-ref` 为唯一 `Agent`，用其不可变 `Agent.id` 进键 → `collector:{agent_id}:{external_id}` |
| 9 (P1, v5) | `attest_work_log` 只收 artifact_id+actor，无法在 attest 时改 `should_enter_kb`/`content_value`，low 值日志永久不可收割，与「attest 时决定资格」矛盾 | §0.5 / §6：最小扩展 `attest_work_log` 接受可选 `should_enter_kb`/`content_value` 覆写（仅改 metadata 两判定字段，不动证据/信任边界） |
| 10 (P2, v5) | §8 集成测试写「经 POST /work-logs」，与 §5–7 服务层路径矛盾 | §8：测试改为经 `_owner_cli` 认证后调 `WorkLogService.submit_work_log` 服务层（与 CLI 同路径） |
| 11 (P1, v6) | §10 排除 `attest_work_log` 改动，与 §0.5/§6 要求的 attest 覆写参数矛盾，导致 low 日志无法收割 | §10：把 `attest_work_log` 可选覆写 `should_enter_kb`/`content_value` 明确纳入 V2 最小扩展范围（不动证据/信任边界） |
| 12 (P2, v6) | `test_normalize_injects_source_platform` 断言 `metadata_json.source_platform`，但 normalize 只返回 `WorkLogSubmit.source_platform`（metadata 由 submit 后生成） | §8：改断言 `output.source_platform`，metadata 断言留给服务集成测试 |
| 13 (P2, v6) | §11 步5 写「adapter → POST /work-logs → attest」，与 §5/§8 服务层路径矛盾 | §11：改「adapter → `WorkLogService.submit_work_log`（服务层）→ `attest_work_log`（可选覆写）」 |
| 14 (P1, v7) | attest 覆写参数只加在服务层，HTTP 端点无 body、CLI 无参数，owner 无法经 console/CLI 覆写，low 日志仍不可收割 | §6：HTTP `POST /work-logs/{id}/attest` 接受可选 body + `scripts/attest_work_log.py` 加覆写提示/参数，二者透传；§8 补接口/CLI 测试 |
| 15 (P2, v8) | 平台级 `CollectorError` 时 exit 0，cron/`collect_all` 把失败当成功，故障/凭证过期无告警 | §7：聚合平台级失败，最终返回非零退出码（已入库数据不受影响），保证自动化可检测 |
| 16 (P1, v9) | attest 覆写 metadata 但 `Artifact.checksum` 陈旧（checksum 由 #88 从 canonical metadata 派生，下游用作内容身份） | §6：覆写时原子重算 `Artifact.checksum`（同 #88 派生规则）；§8 补 `test_attest_override_recomputes_checksum` |
| 17 (P2, v10) | `source_platform` filter 仅描述过滤日志，但 `get_content_feed` 还追加 facts，导致 `?source_platform=codex` 可能返回无 platform 的 fact，与测试「仅匹配条目」矛盾 | §1 / §8：明确 filter 仅作用于 work_log 条目，KnowledgeFact 条目不受其影响（始终返回合格 facts）；测试同步修正 |
| 18 (P2, v11) | normalize 默认 `content_value=medium`（长文本），普通 attest 后 harvest 因 medium 自动收割，绕过 owner KB 资格裁定 | §4：采集日志 `content_value` 默认 `low` + `should_enter_kb=False` 双重确保默认不可收割，资格严格由 attest 覆写决定 |
| 19 (P2, v11) | attest 接受任意 `content_value` 字符串直写 metadata，与提交枚举 `high|medium|low|none` 不一致，非法值致 feed/harvest 解释紊乱 | §6 / §8：attest 覆写 `content_value` 须经同枚举校验，非法 → 422；补 `test_attest_rejects_invalid_content_value` |
| 20 (P1, v12) | 已 APPROVED 二次 attest 带不同覆写被静默忽略，owner 决策看似成功未持久化 | §6：已 APPROVED + 冲突覆写 → 409 fail-closed；值一致/无覆写 → no-op；§8 补 `test_attest_conflicting_override_409` |
| 21 (P2, v12) | `test_normalize_content_value_default` 仍要求长文本变 medium，破坏「默认 low 防自动收割」安全规则 | §8：改为所有归一化 collector 记录 `content_value` 恒为 `low` |
| 22 (P2, v13) | `test_feed_filter_by_source_platform` 断言过滤/不过滤 facts 计数相等，但分页位移会改变 fact 在页中的位置/计数 | §8：改无界 fixture + 断言 facts **集合**一致（先资格筛选再分页），消除分页歧义 |
| 23 (P2, v13) | `test_collector_attest_then_harvestable` 仅 attest 不满足 harvest 资格（默认 low/False），测试会失败，与 §4 默认拒绝矛盾 | §8：测试改为 attest 时覆写 `should_enter_kb=True`（或 content_value=high）才进入收割 |
| 24 (P2, v14) | attest 覆写 `metadata_json`/`checksum` 属信任决策，但计划未要求 attestation `AuditLog` 捕获覆写前后 `should_enter_kb`/`content_value`（现有仅记 `review_status`），导致「采集日志为何可收割」无审计轨迹 | §6：覆写发生时 attestation `AuditLog` 须快照前后值（`prev_/next_should_enter_kb` + `prev_/next_content_value`），无覆写时前后值相等仍显式记录；§8 补 `test_attest_override_audit_trail` 断言 |
| 25 (P1, v15) | `source_platform` filter 仅作用于 work_log，但 #88 `ContentFeed` 将 logs+facts 合并单窗口 `[offset:offset+limit]` 排序切片；log 过滤后 facts 会位移进/出页，「facts 不受影响」仅无界 fixture 掩盖、生产分页行为未定义 | §1 / §8：明确**分片独立分页**——无 filter 时与 #88 完全一致；有 filter 时平台 filter 仅作用于 log 分片（`limit`/`offset` 对 log/fact 分片分别独立应用，facts 永不被平台过滤）。最终形态由 v16 收口为**结构化响应 `{work_logs, facts}` + 显式 `log_limit`/`fact_limit` 分页参数**（见条目 27），避免 `2*limit` 破坏全局页长 |
| 26 (P2, v15) | §6 审计段先要求无覆写也记 4 个前后值字段、旋即又说无覆写时沿用现有 review-status-only 记录，与 §8 测试（期望 4 字段）矛盾，实现依后句会测失败 | §6：统一契约——**任何 attestation 均记 4 个前后值字段**（无覆写时 `prev_*==next_*`，仍为显式快照），不再写「沿用 review-status-only」，§8 测试明确「恒含 4 字段」 |
| 27 (P1, v16) | v15 的「分片各自独立应用 limit」会使 `source_platform` 过滤时返回至多 `2*limit` 条（limit=100→200），破坏 #88 全局页长/offset 语义 | §1 / §8：过滤视图改**结构化响应** `{work_logs, facts}`，新增 `log_limit`/`log_offset` + `fact_limit`/`fact_offset`（缺省沿用 `limit`/`offset`）分别作用两分片，每分片页长受限、偏移可描述；facts 仍只取决于自身分页、与 log 无关；无过滤时完全沿用 #88 扁平列表 |
| 28 (P1, v17) | `fetch_raw` 成功但单条 normalize/submit 失败时，计划仅审计跳过、明确「不计入失败状态」，cron 会误判成功并推进 `--since`，丢失该记录重试/告警路径 | §7：脚本维护 `platform_errors` + `record_errors` 两类计数，任一 >0 即返回非零退出码；单条记录失败已逐条跳过并审计、但仍计入 `record_errors`，杜绝「取数成功但丢记录」被误判成功 |
| 29 (P2, v17) | 四脚本各采单平台，而「某平台失败不影响其它平台」的必需行为只能由 `collect_all.py` 提供，但该脚本当前标为「可选」，实现可据此省略唯一满足失败隔离契约的组件 | §7 / §8 / §11：`collect_all.py` 由「可选封装」升级为**必含组件**（跨平台失败隔离 + 聚合退出唯一承载者），列为 V2 必交付，§8 补 `test_collect_all_aggregates_exit_status` 断言其聚合非零退出 |
| 30 (P2, v18) | `--agent-ref` 解析出的 Agent 若属于其它平台，脚本仍会用硬编码 `source_platform` + 无关 Agent.id 作幂等命名空间落库，污染来源标记/筛选/配置绑定（如 Codex 脚本绑定 Hermes Agent） | §7 / §8：解析 Agent 后增**平台归属校验** `Agent.platform == adapter.platform`，不匹配则 fail-closed 拒绝该平台运行（记 `AuditLog(collector.config_error)` + 计入 `platform_errors` + 非零退出），绝不落到错误 Agent 命名空间；补 `test_collect_script_rejects_mismatched_agent_platform` |

> 本文件止步于计划。批准后由实现 PR 按 §11 顺序落地；评审通过 + exact-head CI 绿后，依铁律设 `gate:merge` 等 owner `授权合并`。
