# Issue #93 实施计划 — AI 员工工作日志「LLM 价值判定」（WorkLogValueJudge）

> 基于最新 `main`（`a649ecc`，Alembic head `20260728_0009`，#88 MVP + #92 V2 半自动采集均已合并）。
> 本文件是**实施计划**，不含任何实现代码。仅用于架构评审（Codex + owner）。
> 关联 Issue：#93。前置依赖：#88（MVP，已合并）、#92（V2 半自动采集，已合并于 `a649ecc`）。后续：#93 合并后才进入 V4（中台化）评估（V4 不在 #88 原始范围，另行规划）。

---

## 0. 范围与铁律（来自 Issue #88 / #92 / #93 与 owner 追加约束）

V3 只做一件事：**在 V2 采集数据就绪后，用可选的 LLM 辅助对 WorkLog 做价值重判**，缓解 #88 纯启发式在长尾语义上的召回不足。

**必须遵守的约束（owner 明确 + 继承自 #88/#92 铁律）：**

1. **复用主线，不重写**：`Artifact(type=WORK_LOG)`、`submit_work_log`、`attest_work_log`（含 #92 覆写）、`ContentValueJudge`（启发式）、`KnowledgeHarvester`、`ContentFeed`、`_owner_cli` 全部复用。V3 **不引入任何新表、不改任何 #88/#92 模型/迁移**。
2. **信任边界不变（铁律）**：LLM 判定仅作「建议草稿」。`attest_work_log` 仍是 WORK_LOG 达到 `APPROVED` 的唯一路径；`owner_approve_review` 已拒绝 WORK_LOG，本计划不改动此不变量。
3. **绝不自动 APPROVE / 绝不自动进入 KB**：任何 LLM 输出都**不能**直接设置 `content_value` 或 `should_enter_kb` 的「已提交」值；`should_enter_kb` 默认 `False`、采集/重判产物默认 `content_value=low`（继承 #92 默认拒绝）。KB 资格只由 owner 在 attest（人工动作）时经 override 参数裁定。
4. **继承 V2 默认拒绝 KB 边界（本计划显式重申）**：
   - 采集/重判产出的 `WorkLogSubmit` 或 verdict draft **永不**带 `should_enter_kb=True`；
   - verdict draft 写入 `metadata_json["llm_verdict_draft"]`（**纯建议**，不触碰已提交的 `content_value` / `should_enter_kb` / `review_status`）；
   - 仅 `attest_work_log(should_enter_kb=..., content_value=...)`（人工）能把 draft 建议「采纳」为已提交值。
5. **不信任 LLM 输出为事实**：LLM 给出的 `value_score` / 理由一律视作可变建议；映射成 `suggested_content_value` 后只进 draft；映射规则本身确定性、可单测、可被 owner 覆盖。
6. **凭据不入库**：NVIDIA / DeepSeek key 只从环境变量读取，绝不以明文写入 `Artifact` / `metadata_json` / 任何 DB 行；`AuditLog` 快照只记模型名/provider/token/成本/延迟，**不记 key**（复用 `redact_secrets`）。
7. **fail-closed**：任一 LLM 调用超时 / 限流 / 鉴权失败 / 返回非法 → 该条重判回退 `HeuristicJudge` 并记 `AuditLog(action="work_log.judge", after_snapshot 含 fallback=True)`（与 Gate E 一致，fallback 仍属「判定」审计，可经同一 action 查询），不影响主线与其它日志重判；不得静默吞错或伪造判定。
8. **无新 Artifact 类型、无新迁移**：verdict draft 仅作为 `metadata_json` 内新增键（与 #92 `source_platform` 同模式，JSON 列内增键，无迁移）；`Artifact` 模型、`owner_approve_review` 守卫、`provenance` 信任链一律不动。
9. 按 TDD 顺序（§11）：接口/判定器 → LlmJudge 路由+回退 → 后台重判脚本 → 测试 → 验收。

---

## 1. 复用的现有事实（代码已确认，基于 `a649ecc`）

| 组件 | 现状（#88 + #92） | 本计划用法 |
|------|------|-----------|
| `WorkLogSubmit`（`#88` schema + #92 `source_platform`） | 7 汇报字段 + `project_id` + `source_platform?` + `content_value?` + `should_enter_kb?` | 复用；V3 **不改**字段。LLM 只产出 draft，不改提交值 |
| `WorkLogService.attest_work_log`（#88 + #92 覆写） | `BEGIN IMMEDIATE` + exactly-one 证据 + `risk_level=L1`；接受可选 `should_enter_kb`/`content_value` 覆写（#92） | **唯一** KB 资格裁定路径；LLM draft 经此被 owner 采纳/覆盖 |
| `ContentValueJudge`（`work_log.py`，#88 启发式） | 纯启发式产出 `content_value` / `should_enter_kb` / `content_angle` | 升级为 `HeuristicJudge`（接口一致），作**默认 + 回退** |
| `Artifact.metadata_json`（#88 JSON 列） | 已含 `source_platform`（#92 写入）、attest 覆写值 | V3 增 `llm_verdict_draft` 键（仅建议，无迁移） |
| `Agent`（#88 迁移 0009：`platform`/`external_ref`） | 采集配置绑定来源 | 重判脚本复用 `--project-id`/`--agent-ref` 同机制（仅用于范围筛选，不写 provenance） |
| `AuditLog`（#57 审计表） | 4 字段 before/after 快照 + `redact_secrets` | 每次判定（含回退/成本）记 `AuditLog(judge.*)` |
| `KnowledgeHarvester` / `ContentFeed` | 只消费 `APPROVED` 日志 | 复用，V3 不改变其消费前置（draft 不触发收割） |
| `scripts/_owner_cli.py` | owner 认证边界 | 重判脚本复用注入 `actor` |

---

## 2. 模块划分（新增 `src/aios/judging/`，不新建表）

```
src/aios/judging/
├── __init__.py
├── verdict.py      # ValueVerdict dataclass + WorkLogValueJudge ABC + JudgeBudget（成本预算/预留）
├── heuristic.py    # HeuristicJudge（升级 ContentValueJudge 逻辑，默认+回退）
└── llm.py          # LlmJudge（NVIDIA 免费主 + DeepSeek 付费备，确定性回退；构造注入 JudgeBudget）
```

- `WorkLogValueJudge` 是判定接口；`HeuristicJudge` 与 `LlmJudge` 两实现；重判脚本统一经接口调用，不直接依赖具体实现。
- 不新建任何 SQLModel 表，不新增 Alembic 迁移（Alembic head 保持 `20260728_0009`）。

---

## 3. 判定接口与 Verdict

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ValueVerdict:
    value_score: float          # 0.0–1.0，语义价值分
    confidence: float           # 0.0–1.0，模型/启发式置信度
    reason: str                 # 人类可读理由（短，<200 字）
    source: str                 # "heuristic" | "llm"
    provider: str               # 实际判定 provider："heuristic" | "nvidia" | "deepseek"（Gate E 审计用）
    model: str | None           # LLM 模型名（heuristic 为 None）
    suggested_content_value: str  # 由 (value_score, confidence) 确定性映射：high|medium|low|none
    fallback: bool = False      # 本次是否由 LLM 回退到启发式（或脚本级异常兜底）
    prompt_tokens: int = 0      # prompt token 数（heuristic=0；Gate E 审计用）
    completion_tokens: int = 0  # completion token 数（heuristic=0；Gate E 审计用）
    cost_usd: float = 0.0       # 本次判定成本（NVIDIA 免费=0；DeepSeek 付费=估算）
    latency_ms: int = 0         # 本次判定延迟
    # —— 付费尝试的可审计痕迹（即便最终回退到启发式也要保留，供 Gate E/C 对账）——
    attempted_provider: str | None = None   # 实际尝试过的付费 provider（即使最终回退），如 "deepseek"；纯 heuristic 默认路径为 None
    attempted_model: str | None = None      # 尝试的模型名，如 "deepseek-chat"
    reserved_cost_usd: float = 0.0          # 本次付费尝试前 reserve 的 est_cost（可能已被计费）；heuristic 默认 0
    attempt_latency_ms: int = 0             # 本次付费尝试（含失败）耗时；失败回退时用于审计追溯
    fallback_reason: str | None = None      # 回退原因，如 "deepseek_timeout"（失败但可能已被计费）

class WorkLogValueJudge(ABC):
    @abstractmethod
    def judge(self, artifact: Artifact) -> ValueVerdict:
        """输入一个 WORK_LOG Artifact，产出 ValueVerdict。纯判定，不写库。"""


@dataclass
class JudgeBudget:
    """运行级成本预算（Gate C 共享对象）。由重判脚本创建并注入 LlmJudge。

    - ``max_usd``：运行总预算上限（来自 --max-cost-usd）。
    - ``spent_usd``：已占用（预留 + 对账后净额）。**保守上界**：失败（可能已被计费）的调用也保留预留，故 spent 永不低估真实花费。
    - ``remaining()``：剩余预算。
    - ``can_afford(est_usd)``：最坏情况估算成本是否不超剩余预算（预留前校验）。
    - ``reserve(est_usd)``：分发付费调用**前**预留最坏情况成本（保守占用，防「失败也被计费」致超支）。
    - ``reconcile(est_usd, actual_usd)``：付费调用结束后对账，退还多预留部分（est-actual，因 actual<=est），保证预算永不低估也不高估。失败（可能已被计费）**不**调用，保留预留。
    NVIDIA 免费池（cost=0）不经过此预算；只有 DeepSeek 付费调用前才 ``can_afford`` 校验 + ``reserve`` 预留。
    """
    max_usd: float
    spent_usd: float = 0.0

    def remaining(self) -> float:
        return self.max_usd - self.spent_usd

    def can_afford(self, est_usd: float) -> bool:
        return est_usd <= self.remaining()

    def reserve(self, est_usd: float) -> None:
        """分发付费调用前预留最坏情况成本 est_usd；调用方须先 ``can_afford``。"""
        self.spent_usd += est_usd

    def reconcile(self, est_usd: float, actual_usd: float) -> None:
        """调用结束后对账：actual<=est，退还多预留 (est-actual)。失败（可能已被计费）不调用。"""
        self.spent_usd -= (est_usd - actual_usd)
```

- `judge()` 是**纯函数式**：输入 `Artifact`（读其 `metadata_json` / 7 汇报字段），输出 `ValueVerdict`，不直接写库（写库由重判脚本统一负责，见 §7）。
- `suggested_content_value` 由确定性映射 `score_to_content_value(score, confidence)` 产生（阈值可单测），**仅作建议**；绝不反向写入 `artifact.metadata_json["content_value"]`。
- `HeuristicJudge` 构造无需 budget（免费、无网络）；`LlmJudge(__budget: JudgeBudget)` 在构造时持有脚本注入的共享预算，并在**每次发起付费（DeepSeek）调用前**经 `budget.can_afford(est_cost)` 校验 + `budget.reserve(est_cost)` 预留（见 §4 / §5 Gate C），从而把「预算拦截」落在 LLM 瀑布内部、经公共 `judge()` 路径可达，脚本无需也无法窥探内部路由。付费调用无论成功/失败可能已被计费，故预留后**不立即退回**：成功经 `budget.reconcile(est_cost, actual_cost)` 退多预留，失败（超时/非200/非法 JSON，可能已被计费）**保留预留**，预算永不低估、永不超支。
- `HeuristicJudge.judge`：复用 #88 `ContentValueJudge` 逻辑（长文本/关键词启发），`source="heuristic"`、`model=None`、`cost_usd=0`、`fallback=False`，**永远可用、无网络依赖**（回退保障）。

---

## 4. LlmJudge 路由（NVIDIA 免费主 + DeepSeek 付费备）

| 维度 | 主（primary） | 备（fallback） |
|------|---------------|----------------|
| Provider | **NVIDIA NIM 免费池** `deepseek-ai/deepseek-v4-pro` | **DeepSeek 付费** `deepseek-chat` |
| 凭据 env | `NVIDIA_NIM_API_KEY`（`nvapi-...`） | `DEEPSEEK_API_KEY`（`sk-...`） |
| 路由 | 中国直连、免费、避开 503 的 flash 档 | 双轨并行；免费池不稳/限流时回退 |
| 成本 | 0（免费池） | 按 token 估算（见 §5 cost gate） |
| 失败处理 | 超时/429/鉴权/非200/非法 JSON → 回退 DeepSeek | DeepSeek 再失败 → 回退 `HeuristicJudge`（见 §6 Gate D） |

- `LlmJudge` 内部实现「NVIDIA → DeepSeek → Heuristic」三级瀑布；**最终兜底一定是 `HeuristicJudge`**，保证重判循环永不因 LLM 不可用而中断（fail-closed，§0.7）。
- **预算拦截（Gate C，落在瀑布内部）**：`LlmJudge` 构造时持有脚本注入的 `JudgeBudget`。瀑布到达 DeepSeek 分支、**发起付费调用前**，先用「最坏情况 token 数 × 单价」估算 `est_cost` 并 `budget.can_afford(est_cost)` 校验：若 `not can_afford` → **绝不发起 DeepSeek 调用**，直接返回 `HeuristicJudge` 保底 verdict（`fallback=True`、`provider="heuristic"`），`budget.spent_usd` 不变；仅当 `can_afford` 通过才 `budget.reserve(est_cost)` 预留、发起调用。**调用结束按结果对账**：成功 → `budget.reconcile(est_cost, actual_cost)` 退还多预留（actual<=est，故实际净占用=actual），verdict 填 `provider="deepseek"`/`model`/`cost_usd=actual`/`latency_ms`；失败（超时/429/非200/非法 JSON，可能已被 API 计费）→ **保留预留、不调用 reconcile**，`spent_usd` 保持 est_cost 占用，并返回 `HeuristicJudge` 保底 verdict，但**必须被打标**以保留付费尝试痕迹：`attempted_provider="deepseek"`、`attempted_model="deepseek-chat"`、`reserved_cost_usd=est_cost`、`attempt_latency_ms=<本次尝试耗时>`、`fallback=True`、`fallback_reason="deepseek_<error>"`（如 `deepseek_timeout`）。NVIDIA 免费池（`cost=0`）始终优先、不受预算限制。由此保证运行累计成本**永不超过** `max_usd`（每次分发前已被 can_afford 拦截，且预留为最坏情况上界、对账仅退不减），回退 verdict 仍携带付费尝试的可审计字段供 Gate E 对账，且拦截点经公共 `judge()` 路径可达（测试可经 `LlmJudge(budget).judge(artifact)` 验证，无需窥探内部路由）。
- 路由决策参考现有 `smart_router` 策略（日常 DeepSeek 付费快、进化 NVIDIA 免费慢）；但 V3 以「NVIDIA 免费优先以控成本」为准，DeepSeek 仅作付费备。
- 模型名固定写死在代码常量（`PRIMARY_MODEL` / `FALLBACK_MODEL`），不来自用户输入，防 prompt-injection 篡改路由。

---

## 5. 六道门禁（owner 显式要求，逐门定义 + 测试）

> 六门是 V3 的硬契约核心。Codex 评审将逐门核对。

### Gate A — LLM-call 门（何时/如何调用 LLM）
- LLM **仅**在后台重判路径（`scripts/rejudge_work_logs.py`）调用，**绝不**出现在 `submit_work_log` / `attest_work_log` / 任何同步请求路径；LLM 判定对用户写操作零阻塞。
- 每次 LLM 调用：固定 `PRIMARY_MODEL`、确定性 system prompt（来自代码常量，非 artifact 内容注入）、超时（如 20s）、单次请求（无并发 fan-out）。
- 同 `(artifact_id, model)` 重判幂等：draft 整体覆盖（最新 verdict 覆盖旧 draft），不产生多行。
- LLM 异常（超时/限流/鉴权/非200/非法 JSON/低解析置信度）→ 走 Gate D 回退，**不**把异常抛给重判主循环。
- 测试：`test_llm_judge_only_in_background_path`（在 submit/attest 路径 mock 断言无 LLM 客户端构造）、`test_llm_call_timeout_uses_fallback`。

### Gate B — Credential 门（密钥不落库）
- `NVIDIA_NIM_API_KEY` / `DEEPSEEK_API_KEY` **仅**从环境变量读取；provider 客户端在调用时从 env 解析，**不缓存明文、不写库**。
- `Artifact` / `metadata_json` / `AuditLog` 中**绝不**出现 key 原文；`AuditLog` 只记 `model` / `provider` / `token` / `cost` / `latency`（复用 `redact_secrets` 对快照二次过删）。
- 缺 key（如仅配了 NVIDIA 没配 DeepSeek）→ 该 provider 在瀑布中跳过，不报错、不把 key 名写库。
- 测试：`test_credentials_never_persisted`（重判后断言 `Artifact.metadata_json` 与 `AuditLog` 快照均不含 `nvapi-`/`sk-` 前缀）、`test_redact_secrets_on_judge_audit`。

### Gate C — Cost 门（免费优先 + 预算守护）
- NVIDIA 免费池优先（成本 0）；DeepSeek 付费仅作备，且**每次调用估算成本**（`prompt_tokens + completion_tokens` × 单价）。
- 运行级预算（**预留-对账，杜绝超支**，经 `JudgeBudget` 共享对象落地，见 §3）：重判脚本接受 `--max-cost-usd`（缺省一个保守上限，如 0.5），并据此**创建** `JudgeBudget(max_usd=max_cost_usd)`、**注入**给 `LlmJudge(__budget=judge_budget)`（构造时持有）。**预算拦截点落在 `LlmJudge.judge()` 瀑布内部**（见 §4）：每次瀑布到达 DeepSeek 付费分支、**发起调用前**，先 `budget.can_afford(est_cost)` 校验（`est_cost`=最坏情况 token×单价）——若 `not can_afford`，**绝不发起该付费调用**，直接改走 `HeuristicJudge`（免费保底），`budget.spent_usd` 不变；仅当 `can_afford` 通过才 `budget.reserve(est_cost)` 预留、发起调用。**结束对账**：成功 → `budget.reconcile(est_cost, actual_cost)` 退还多预留（actual≤est）；失败（超时/非200/非法 JSON，**可能已被 API 计费**）→ **保留预留、不 reconcile**，`spent_usd` 占用 est_cost。预算对象由脚本持有、可被测试直接断言（无需窥探 `LlmJudge` 内部路由）。NVIDIA 免费池（`cost=0`）不受此限，始终优先。由此保证运行累计成本**永不超过** `--max-cost-usd`（每次分发前已被拦截，预留为最坏上界，对账仅退不减，且失败也被计费的情形已保守计入）。
- 连续失败阈值：N 次（如 3）连续 LLM 失败 → 本运行剩余日志统一回退 `HeuristicJudge`（防雪崩 + 控成本）。
- 每次判定（含回退）`AuditLog` 记 `cost_usd` / `latency_ms` / `model`，供成本对账。
- 测试：`test_cost_budget_disables_llm`（模拟「下一次付费调用最坏成本将超剩余预算」→ 该付费调用**绝不发起**、对应日志改走 heuristic，断言无 DeepSeek 请求且预算无预留/无扣减）、`test_budget_reserved_on_failed_paid_call`（DeepSeek 接受但超时失败 → 预留被保留、不 reconcile、后续 can_afford 正确收窄，且 verdict 与 `AuditLog` 均携带 `attempted_provider`/`reserved_cost_usd`/`fallback_reason` 使对账可见）、`test_consecutive_failure_disables_llm`。

### Gate D — Fallback 门（确定性回退）
- 瀑布：`LlmJudge` 先 NVIDIA；NVIDIA 失败/限流/超时/非法 → DeepSeek；DeepSeek 失败 → `HeuristicJudge`。**最终兜底恒为 `HeuristicJudge`**（无网络依赖，永远可用）。
- 每次回退记 `AuditLog(action="work_log.judge", after_snapshot 含 fallback=True, fallback_reason=...)`（与 Gate E 同一 action，可统一查询），`ValueVerdict.fallback=True`、`source` 反映实际来源。
- 回退**绝不**中断重判循环、绝不丢日志：原 artifact 仍得到一条 verdict（启发式保底）。
- 测试：`test_llm_fallback_chain_nvidia_to_deepseek_to_heuristic`（mock 各级失败）、`test_fallback_never_raises`（全失败也应返回 heuristic verdict 而非抛异常）。

### Gate E — Audit 门（每次判定可审计）
- **每次**判定（LLM 成功 / LLM 回退 / 启发式保底 / 脚本级异常兜底）均写 `AuditLog(action="work_log.judge", resource_id=artifact.id, project_id=..., before_snapshot={prev_draft_summary}, after_snapshot={verdict_provider, attempted_provider, attempted_model, model, value_score, confidence, suggested_content_value, prompt_tokens, completion_tokens, cost_usd, reserved_cost_usd, latency_ms, attempt_latency_ms, fallback, fallback_reason})`；`ValueVerdict` 已携带 `provider` / `prompt_tokens` / `completion_tokens`（heuristic 默认 `"heuristic"` / `0` / `0`），**且付费尝试的可审计痕迹字段 `attempted_provider` / `attempted_model` / `reserved_cost_usd` / `attempt_latency_ms` / `fallback_reason` 在付费回退时必填**（见 §4）——即便最终 verdict 是 heuristic，审计也能看到「曾尝试 deepseek、预留 est_cost、耗时 N ms、因 X 回退」，故 Gate C 的预算对账（reserved vs spent）可追溯、不丢付费尝试；`redact_secrets` 已确保无 key 泄漏。
- draft 写入 `metadata_json["llm_verdict_draft"]` 本身也记一条 `AuditLog`（artifact id + draft 摘要，不含原始明文 key）。
- 审计轨迹可据 `action="work_log.judge"` 查询，支持成本/质量复盘。
- 测试：`test_judge_audit_logged`（断言 `AuditLog` 行存在且含 model/cost/latency、不含 key）、`test_draft_write_audited`。

### Gate F — Human-override 门（LLM 草稿绝不自动晋级）
- `llm_verdict_draft` 是**纯建议**：写 `metadata_json["llm_verdict_draft"]`，**绝不**改 `review_status` / `content_value` / `should_enter_kb` 的「已提交」值。
- WORK_LOG 的 `APPROVED` 仍只经 `attest_work_log`（人工）；`owner_approve_review` 仍拒 WORK_LOG（#88 不变量，V3 不动）。
- attest UI/CLI 把 `llm_verdict_draft.suggested_content_value` + `reason` 展示给 owner；owner **采纳**=传 `should_enter_kb=True` + `content_value=<建议>`（复用 #92 覆写）；**覆盖**=传任意值；两路径都经人工 attest，无任何自动晋级。
- 任何代码路径都**不得**把 `suggested_content_value` 直接写入已提交的 `content_value`（单测锁死）。
- 测试：`test_llm_draft_never_sets_committed_content_value`（重判后断言 `artifact.metadata_json["content_value"]` 仍是提交值、未变）、`test_draft_survives_attest_override`（attest 覆写后仍可追因到 draft 建议）、`test_owner_approve_review_still_rejects_work_log`（回归 #88/#92 守卫）。

---

## 6. Verdict Draft 存储（无迁移）

- 落点：`Artifact.metadata_json["llm_verdict_draft"]`，结构：
  ```json
  {
    "value_score": 0.0–1.0,
    "confidence": 0.0–1.0,
    "reason": "human-readable",
    "source": "heuristic | llm",
    "provider": "heuristic | nvidia | deepseek",
    "model": "deepseek-ai/deepseek-v4-pro" | "deepseek-chat" | null,  # 实际 provider 模型串：NVIDIA 主 / DeepSeek 备 / 启发式为 null
    "suggested_content_value": "high|medium|low|none",
    "fallback": false,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cost_usd": 0.0,
    "latency_ms": 0,
    "attempted_provider": "deepseek | null",   # 曾尝试的付费 provider（即便最终回退）；纯 heuristic 为 null
    "attempted_model": "deepseek-chat | null",  # 尝试的模型名
    "reserved_cost_usd": 0.0,                    # 本次付费尝试前 reserve 的 est_cost（可能已被计费）；启发式为 0
    "attempt_latency_ms": 0,                     # 本次付费尝试（含失败）耗时
    "fallback_reason": "deepseek_timeout | null", # 回退原因（失败但可能已被计费）
    "judged_at": "ISO8601 UTC"
  }
  ```
  > 注：付费尝试的审计痕迹字段（`attempted_provider` / `attempted_model` / `reserved_cost_usd` / `attempt_latency_ms` / `fallback_reason`）在「曾尝试 DeepSeek 但回退」时填充，使 `llm_verdict_draft` 与 `AuditLog(action="work_log.judge")` 一致——即便最终 verdict 是 heuristic，owner 在 attest 时也能看到「曾尝试付费判定、预留 est_cost、因 X 回退」，预算对账（reserved vs spent）不丢付费尝试。
- 该键是 `metadata_json`（#88 JSON 列）内的新增键，**无 Alembic 迁移**，Alembic head 保持 `20260728_0009`。
- draft 写入**不**触发 `KnowledgeHarvester` 收割（收割只看 `APPROVED` + `should_enter_kb`）；draft 不改变这两者，故零回归。
- `ContentFeed` 可选地把 `llm_verdict_draft` 作为展示字段（只读展示，不改筛选语义），与 #92 `source_platform` 同模式。

---

## 7. 脚本（后台重判，复用 `_owner_cli`）

- `scripts/rejudge_work_logs.py`：参数 `--project-id`（必填）、`--agent-ref`（可选，按 `Agent.platform` 范围筛选）、`--since`（可选）、`--limit`（可选）、`--max-cost-usd`（可选，成本门上限）、`--dry-run`（**只枚举**将重判的目标日志并列出摘要，**不调用 `WorkLogValueJudge.judge()`、不发起 LLM、不写库、不写 `AuditLog`**；因不产生任何判定，Gate E「审计每次判定」自然不适用，无矛盾；去掉 `--dry-run` 才真实判定）。
  - 流程：经 `_owner_cli` 认证取得 owner `actor` → 定位目标日志（**范围**：`type=WORK_LOG` 且 `review_status` 仍 `UNVERIFIED` 且（无 `llm_verdict_draft` 或 `confidence` 低于阈值），即「未判定 / 低置信度」；绝不触碰已 `APPROVED` 日志）→ 逐条 `WorkLogValueJudge.judge(artifact)`（默认 `HeuristicJudge` 产出基线；对需 LLM 增强的走 `LlmJudge` 瀑布）→ 仅写 `metadata_json["llm_verdict_draft"]`（**不改** `review_status`/`content_value`/`should_enter_kb`），原子提交 + `AuditLog(judge.*)`。
  - **fail-closed 聚合 + 兜底 verdict（同 #92 §7，强化 Gate D）**：维护 `judge_errors` 计数；**单条 `judge` 异常（含 LLM 瀑布之外的 unexpected 异常）绝不静默跳过**——catch 后改产 `HeuristicJudge.judge(artifact)` 保底 verdict（保证每条日志都拿到一条 verdict，绝不丢日志），写 `metadata_json["llm_verdict_draft"]` 并记 `AuditLog(action="work_log.judge", after_snapshot 含 fallback=True, fallback_reason=<error>)`（与 Gate E 同一 action）；`judge_errors > 0` → 脚本返回非零退出码，供 cron 重试/告警。
  - **绝不**调用 `attest_work_log`、绝不修改 `review_status`、绝不写 `should_enter_kb=True`（人类 override 门，Gate F）。
- 该脚本是 #93 的「后台重判入口」；cron 周期调用，产出 draft 供 owner 在 attest 时参考。

---

## 8. 测试清单（TDD）

**判定接口 / 判定器**
- `test_heuristic_judge_baseline`：`HeuristicJudge.judge` 产出 `source="heuristic"`、`model=None`、`cost_usd=0`、`fallback=False`，`value_score` 在 [0,1]。
- `test_llm_judge_returns_verdict`：mock NVIDIA 返回 → `LlmJudge.judge` 产出 `source="llm"`、`model=PRIMARY_MODEL`、`suggested_content_value` 由分数确定性映射。
- `test_score_to_content_value_mapping`：阈值映射单测（高分→high、中→medium、低→low、极低→none）。
- `test_llm_draft_never_sets_committed_content_value`：重判后 `artifact.metadata_json["content_value"]` 仍是原提交值（Gate F 锁死）。
- `test_draft_survives_attest_override`：先重判产出 draft → owner 经 `attest_work_log(should_enter_kb=True, content_value=<建议>)` 采纳 → 断言「仅人工 attest 把 override 提交到 `metadata_json` 的 `content_value`/`should_enter_kb`」、且 `metadata_json["llm_verdict_draft"]` 建议仍保留可追因（不被 attest 覆盖或删除）；验证 judge→attest 采纳/覆盖链中 LLM draft 始终是「建议」、KB 资格只由人工裁定（Gate F + §11）。
- `test_owner_approve_review_still_rejects_work_log`：回归 #88/#92 守卫（V3 未改动）。

**LlmJudge 双轨 + 回退（mock provider）**
- `test_llm_fallback_chain_nvidia_to_deepseek_to_heuristic`：逐级 mock 失败，断言最终 `source="heuristic"`、`fallback=True`、不抛异常。
- `test_llm_call_timeout_uses_fallback`：NVIDIA 超时 → DeepSeek / 启发式。
- `test_fallback_never_raises`：全失败也应返回 heuristic verdict。
- `test_credentials_never_persisted`：重判后 `Artifact.metadata_json` 与 `AuditLog` 快照均无 `nvapi-`/`sk-` 前缀（Gate B）。
- `test_redact_secrets_on_judge_audit`：审计快照过删生效。

**成本 / 连续失败守护**
- `test_cost_budget_disables_llm`：构造 `JudgeBudget(max_usd)` 并注入 `LlmJudge(__budget)`；**不**依赖「累计成本先超支」的时序，而是断言「下一次付费调用的最坏情况估算 `est_cost` 已超过 `budget.remaining()`」这一既定状态——在该状态下，`LlmJudge.judge(artifact)` 经公共路径**绝不发起 DeepSeek 请求**（mock 的 DeepSeek 客户端断言未被调用）、返回 `HeuristicJudge` 保底 verdict（`provider="heuristic"`、`fallback=True`），且 `budget.spent_usd` 与 `budget.remaining()` 保持不变（无预留/无扣减）。验证 Gate C 的**调用前预留、永不过支**契约（而非「超支后停用」）。
- `test_budget_reserved_on_failed_paid_call`：构造 `JudgeBudget(max_usd)` 注入 `LlmJudge(__budget)`；mock DeepSeek 客户端使其「接受请求但随后超时失败」（抛超时异常）。**单元层**（直接调 `LlmJudge(budget).judge(artifact)`，保持 `judge()` 纯判定、不写库，见 §3/§7）断言：① 返回 `HeuristicJudge` 保底 verdict（`fallback=True`、`provider="heuristic"`），但**携带付费尝试痕迹**——`attempted_provider="deepseek"`、`attempted_model="deepseek-chat"`、`reserved_cost_usd=est_cost`、`attempt_latency_ms>0`、`fallback_reason="deepseek_timeout"`；② 分发前已 `budget.reserve(est_cost)` 被调用（mock budget 断言 reserve 已调用、`spent_usd` 增加 `est_cost`）；③ 因调用失败（可能已被计费）**未**调用 `reconcile`，`spent_usd` 仍等于 `est_cost`（保守保留、未退回）、`remaining()` 已扣除；④ 后续 `budget.can_afford(est_cost)` 因剩余减少而更可能触发拦截。**集成层**（经 `scripts/rejudge_work_logs.py` 消费该 verdict 并写库，见 §7）断言：⑤ 脚本写入的 `AuditLog(action="work_log.judge")` 的 `after_snapshot` 含 `attempted_provider="deepseek"`、`reserved_cost_usd=est_cost`、`attempt_latency_ms>0`、`fallback_reason="deepseek_timeout"`（与 verdict 一致），使 Gate C 预算对账（reserved vs spent）可追溯、不丢付费尝试。验证「失败也被计费」情形已计入预算且审计可见（Gate C/E 预留-对账契约），且 `judge()` 仍保持纯接口。
- `test_consecutive_failure_disables_llm`：N 次连续失败 → 剩余日志统一 heuristic（Gate C/D）。

**审计**
- `test_judge_audit_logged`：每次判定 `AuditLog(action="work_log.judge")` 含 provider/model/**prompt_tokens**/**completion_tokens**/cost/latency、无 key（**成功与回退两条路径均断言 token 字段**，Gate E）。
- `test_draft_write_audited`：draft 写入记审计。

**后台重判脚本 / 集成**
- `test_rejudge_writes_draft_only`：重判只写 `llm_verdict_draft`，**不改** `review_status`/`content_value`/`should_enter_kb`（Gate F + 零回归）。
- `test_rejudge_skips_approved`：已 `APPROVED` 日志不被重判触碰。
- `test_rejudge_dry_run_no_db_write`：`--dry-run` **不调用 `WorkLogValueJudge.judge()`**（mock judge 断言未被调用）、不写 `AuditLog`、不持久化 `llm_verdict_draft`；仅枚举并列出目标日志摘要（dry-run 不产生判定，Gate E 不适用）。
- `test_rejudge_one_error_continues`：单条 `judge` 抛异常 → 该条**仍拿到 `HeuristicJudge` 保底 draft**（不跳过、不丢日志）+ 记 `AuditLog(action="work_log.judge", fallback=True)`（与 Gate E 同一 action，可查询）、其余仍写 draft、脚本最终退出码非零（fail-closed 聚合，Gate D）。
- `test_rejudge_idempotent_overwrites_draft`：同 artifact 重判两次 → `metadata_json` 仅含**一个** `llm_verdict_draft` 键（第二次覆盖第一次，`judged_at` 更新），无重复持久化；验证 Gate A 幂等（同 `artifact_id` 重判只覆盖 draft，不增行）。
- `test_rejudge_injects_owner_actor`：缺/非 owner 凭证 → 拒绝（复用 `_owner_cli`）。
- `test_llm_judge_only_in_background_path`：在 `submit_work_log` / `attest_work_log` 路径断言无 LLM 客户端构造（Gate A）。

**不变量回归（确保 V3 未破坏 #88/#92）**
- 复用 #88 `test_work_log.py` / #92 `test_collectors.py` / `test_api_work_log.py` 全量通过（CI 覆盖）。
- Alembic head 仍为 `20260728_0009`（V3 无迁移）。

---

## 9. 验收命令

```bash
# lint（ruff 0.15.22, line-length 100）
aios-v0/.venv/Scripts/python -m ruff check src tests alembic

# 聚焦测试（新增 judging + 集成）
pytest tests/test_judging.py -q

# 主线 + V2 回归
pytest tests/test_work_log.py tests/test_api_work_log.py tests/test_collectors.py -q

# 全量（以 exact-head CI 为准）
pytest -q
```

验收门槛：聚焦 + 全量 `pytest` 绿；`ruff` 绿；**exact-head CI 绿**；Alembic head 仍为 `20260728_0009`（V3 无迁移）。

---

## 10. Out of scope（不扩大）

- **任何自动 attest / 自动 APPROVED**（永远人工，信任边界铁律，Gate F）。
- 新的 `Artifact` 类型、新的 SQLModel 表、新的 Alembic 迁移。
- 改动 `owner_approve_review` 守卫、`provenance` 信任链、`Artifact` 模型。
- 把 `suggested_content_value` 直接写入已提交 `content_value`（Gate F 锁死）。
- 实时 LLM 推送、写外部副作用（除 `metadata_json` draft + `AuditLog`）、任何把 LLM 调用塞进 `submit_work_log`/`attest_work_log` 同步路径（Gate A）。
- AIOS 统一 Agent 中台 / 自动注册实体（**V4**）。
- 把 LLM judge 做成对外 HTTP 服务 / MCP server（V3 仅内部判定器 + 后台脚本）。

---

## 11. TDD 实施顺序（实现 PR 采用）

1. **判定接口/verdict**：`verdict.py`（`ValueVerdict` + `WorkLogValueJudge` ABC）+ `score_to_content_value` 映射 + 单测（先红后绿）。
2. **HeuristicJudge**：升级 #88 `ContentValueJudge` 逻辑为接口实现 + 单测。
3. **LlmJudge 路由+回退**：NVIDIA→DeepSeek→Heuristic 瀑布 + Gate A/B/C/D 单测（mock provider）。
4. **后台重判脚本**：`scripts/rejudge_work_logs.py`（经 `_owner_cli`）+ Gate E/F + fail-closed 聚合 + §8 脚本测试。
5. **集成测试**：judge → draft 写入（不改 review_status）→ attest 采纳/覆盖全链路 + 幂等 + fail-closed。
6. **验收**：§9 全绿 + exact-head CI 绿 + #88/#92 回归全绿。

---

## 12. 与 #88 / #92 / #93 的关系

- **#88（MVP，已合并 `1938c20`）**：提供全部基础设施（端点、attest 信任边界、harvest、feed、迁移 0009）。V3 是其「价值判定」的可选增强，**不改动模型/迁移/信任边界**。
- **#92（V2 半自动采集，已合并 `a649ecc`）**：提供采集数据与默认拒绝 KB 边界（`content_value=low` + `should_enter_kb=False`）。V3 在 V2 数据之上做 LLM 重判，**继承并显式重申该默认拒绝边界**（§0.4）；V3 的 `llm_verdict_draft` 与 V2 的 `source_platform` 同为 `metadata_json` 内增键（无迁移）。
- **#93（本 Issue）**：引入可选 `LlmJudge`，把价值判定从纯启发式升级为「启发式默认 + LLM 增强建议 + 人工裁定」；LLM 仅产 draft，KB 资格仍由人工 attest override 决定。
- **V4（中台化）**：本 Issue 合并后才可评估，不在 #88 原始范围，另行规划。

---

## 13. v1 设计要点（对照 owner 六门要求）

| 门 | 落点 | 防什么 |
|---|------|--------|
| A LLM-call | §4 / §7 / §8：LLM 仅后台重判路径，绝不进 submit/attest 同步路径；固定模型常量、超时、**幂等**（test_rejudge_idempotent_overwrites_draft） | LLM 阻塞用户写操作 / prompt-injection 篡改路由 |
| B Credential | §4 / §5 Gate B / §8：key 仅 env、调用时解析、不缓存不落库；AuditLog 过删 | 密钥泄漏到 Artifact/metadata/审计 |
| C Cost | §4 / §5 Gate C / §8：NVIDIA 免费优先、**付费调用前 can_afford 校验 + reserve 预留、成功 reconcile 退多预留、失败保留预留**（超剩余预算绝不发起付费调用、失败也被计费计入预算）、连续失败停用 LLM（test_cost_budget_disables_llm / test_budget_reserved_on_failed_paid_call） | 成本失控 / 雪崩 |
| D Fallback | §4 / §5 Gate D / §7 / §8：NVIDIA→DeepSeek→Heuristic 瀑布，兜底恒 heuristic；**脚本级 unexpected 异常也兜底 Heuristic**（test_rejudge_one_error_continues） | LLM 不可用 / 异常致重判中断或丢日志 |
| E Audit | §5 Gate E / §6 / §8：每次判定（含回退/兜底）记 AuditLog(provider/attempted_provider/attempted_model/reserved_cost_usd/attempt_latency_ms/prompt_tokens/completion_tokens/cost/latency/fallback/fallback_reason)，redact_secrets；verdict 携带 provider+token+付费尝试字段（test_judge_audit_logged / test_budget_reserved_on_failed_paid_call） | 判定不可追溯 / 成本不可对账 |
| F Human-override | §0.3/§0.4 / §5 Gate F / §6 / §8：draft 只进 metadata_json，绝不写已提交 content_value/should_enter_kb；APPROVED 仅人工 attest | LLM 自动晋级绕过人工 KB 裁定（默认拒绝边界） |

> 本文件止步于计划。批准后由实现 PR 按 §11 顺序落地；评审通过 + exact-head CI 绿后，依铁律设 `gate:merge` 等 owner `授权合并`（**本计划 PR 不自动合并**）。
