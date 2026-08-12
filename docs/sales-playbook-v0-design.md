# SalesPlaybook V0 — 修订架构 / 设计提案（REV 2）

> 状态：**仅设计（DESIGN ONLY）**。承接 owner 架构评审 **REQUEST_CHANGES**，本版为修订稿。
> 未经 owner 批准修订架构，**不得实现**。
> 定位不变：把提取的米核 YiWaiWai EBF 官方销售话术接入 AIOS，作为**只读销售辅助证据源**。
> 不是 CRM / 不是自动销售 / 不是自动发送 / 不阻塞 REAL LEAD-GEN EXPERIMENT #1。

---

## 0. 复用边界（铁律，未变）

已存在 `src/aios/customer_service.py`（#109, V1.2-B）：
- `CustomerService.generate_suggestion()` —— 取已审批 `KnowledgeFact` → 打分 → 推导 `decision`（ESCALATE / AUTO_SEND / HUMAN_CONFIRM）→ 写 `CsSuggestion`。
- `owner_confirm_suggestion()` / `send_message()` —— 人工确认后发出，`is_auto_sent=False`。
- `AUTO_SEND` 置信度契约（env `AIOS_CS_AUTO_SEND_CONFIDENCE`，默认 0.80）。

**SalesPlaybook = 挂在 `generate_suggestion` 上的只读检索 + 事实安全适配器**，产出仍落进既有 `CsSuggestion`。**不新建第二套会话/CS 子系统、不新建消息表、不改既有表结构。**

复用既有表：`Conversation` / `Message` / `CsSuggestion` / `Artifact` / `KnowledgeCandidate` / `KnowledgeFact`。
既有 `Artifact` 字段：`id, project_id(FK), type(ArtifactType), uri, checksum, review_status(ArtifactReviewStatus), metadata_json, ...`。**新增图片直接复用，不新增 Artifact 列/枚举值（cleanup C）。**

---

## 1. 修订后数据模型（corrected data model）

五张新表（只读源适配层）+ 复用既有表。

### 1.1 `SalesScriptSource` — 不可变源包（D1）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | PK (ssrc) | |
| source_type | str | 固定 `"mihe_ebf"`（V0 唯一合法值） |
| original_ebf_filename | str | **原始 EBF 文件名**（cleanup B：独立于导出 JSON） |
| extracted_manifest_filename | str | **结构化导出 JSON 文件名**（如 `02_完整结构化导出.json`，cleanup B） |
| source_file_hash | str (indexed) | **锚1** = SHA256(原始 EBF 字节)（D1） |
| extraction_manifest_hash | str (unique, indexed) | **锚2** = SHA256(规范化导出 JSON ‖ 有序归一化图片 SHA256 清单 ‖ 图引用映射)（D1） |
| source_version | str | 导出批次版本 |
| status | `SalesScriptSourceStatus` | ACTIVE / SUPERSEDED / INACTIVE（CHECK + 部分唯一索引） |
| imported_at | datetime | |
| entry_count | int | 审计用 |
| metadata_json | JSON | 首包验收统计（**非永久常量**，见 cleanup A） |

> 两个完整性锚分开：图片内容变了但文件名没变，锚1（原始 EBF 字节）仍能捕获；锚2 捕获导出结构与图清单的变化。两者独立、互补。

### 1.2 `SalesScriptEntry` — 一条官方话术（不可变，D2 去重）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | PK (ssent) | |
| source_id | FK→SalesScriptSource (ON DELETE RESTRICT) | 绑定到单一世代 |
| source_entry_id | str (indexed) | **逐字保留**官方条目 ID |
| product_scope | `SalesScriptScope` | MIHE_1_0 / MIHE_2_0 / COMMON（CHECK） |
| category | str | 逐字保留分类 |
| title | str | 逐字保留标题 |
| source_hash | str (indexed) | SHA256(归一化有序 segments)，不可变校验 |
| imported_at | datetime | |

> **不再存储 segments JSON**（D2）。图文段真相唯一归属 `SalesScriptSegment`，`SalesScriptEntry` 不另存可变副本。

### 1.3 `SalesScriptSegment` — 唯一权威图文结构（D2，取代旧 segments JSON + SalesScriptMedia）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | PK (ssseg) | |
| entry_id | FK→SalesScriptEntry (ON DELETE CASCADE) | |
| sequence | int | 段序（精确保留源顺序） |
| segment_type | `SalesScriptSegmentType` | TEXT / IMAGE（CHECK） |
| text_content | str \| null | TEXT 段必填，IMAGE 段必空 |
| artifact_id | str \| null | FK→Artifact(type=IMAGE) (ON DELETE RESTRICT)，IMAGE 段必填、TEXT 段必空 |
| caption | str \| null | 图注（如有） |
| **UNIQUE(entry_id, sequence)** | | 顺序唯一 |
| **CHECK** | | `(TEXT ⇒ text_content NOT NULL AND artifact_id IS NULL) OR (IMAGE ⇒ artifact_id NOT NULL AND text_content IS NULL)` |

> 单一真相：`SalesScriptEntry.segments`（旧）与 `SalesScriptMedia`（旧）两张冗余表→合并为这一张规范化表。74 唯一图→74 个 `Artifact` 行；76 引用→76 个 `SalesScriptSegment(segment_type=IMAGE)` 行。blob 不进任何脚本行。

### 1.4 `SalesScriptFactBinding` — 动态事实绑定（D3，结构化绑定到真实条目）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | PK (ssfb) | |
| entry_id | FK→SalesScriptEntry (ON DELETE CASCADE) | 结构性绑定，杜绝悬空 |
| fact_key | str | 事实键（如 `price`、`commission`） |
| fact_class | `SalesScriptFactClass` | PRICE/COMMISSION/MEMBERSHIP/CAPABILITY/URL/PROMO/OTHER |
| raw_span | str | 官方 EBF 原文片段（只读，逐字） |
| scope | `SalesScriptScope` | 事实所属版本域（须与 entry 兼容） |
| status | `SalesScriptFactStatus` | VERIFIED_CURRENT / NEEDS_REVIEW / STALE / VERSION_1_ONLY |
| reviewed_at | datetime \| null | |
| reviewed_by | str \| null | owner 身份 |
| binding_hash | str (unique, indexed) | SHA256(entry_id‖fact_key‖raw_span 归一化) —— 确定性导入幂等锚（D3） |

| 约束 | 说明 |
|---|---|
| UNIQUE(entry_id, fact_key, raw_span 归一化) | 或依赖 binding_hash 唯一 |
| CHECK `ck_ssfb_scope_compat` | `scope = 'common' OR scope = entry.product_scope` —— **禁止跨版本绑定** |

> 每个事实出现 = 一行；不再是 `entry_refs JSON(list[str])`（D3）。FK + scope 兼容 CHECK 同时杜绝悬空与跨版本。

### 1.5 `CsSuggestionSalesEvidence` — 建议↔证据关联（D5，**不**给 CsSuggestion 加 JSON 列）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | PK (ssev) | |
| suggestion_id | FK→CsSuggestion (ON DELETE CASCADE) | |
| entry_id | FK→SalesScriptEntry (ON DELETE RESTRICT) | |
| rank | int | 检索排名 |
| match_reason | str | 命中原因（category/keyword/compare） |
| fact_safety | `SalesScriptFactStatus` | 该条目所触及绑定的最弱事实态 |

| 约束 | 说明 |
|---|---|
| UNIQUE(suggestion_id, entry_id) | 一行一证据 |

> 既有 `CsSuggestion` 架构**完全不改**（D5）。证据通过双向 FK 关联表，可引用审计。

### 枚举（StrEnum→VARCHAR，纯代码新增）
```python
class SalesScriptScope(StrEnum):          # 持久化条目域
    MIHE_1_0 = "mihe_1_0"
    MIHE_2_0 = "mihe_2_0"
    COMMON   = "common"

class SalesScriptSourceStatus(StrEnum):    # D1 激活
    ACTIVE     = "active"
    SUPERSEDED = "superseded"
    INACTIVE   = "inactive"

class SalesScriptSegmentType(StrEnum):     # D2
    TEXT  = "text"
    IMAGE = "image"

class SalesScriptFactClass(StrEnum):       # D6 七类动态事实
    PRICE       = "price"
    COMMISSION  = "commission"
    MEMBERSHIP  = "membership"
    CAPABILITY  = "capability"
    URL         = "url"
    PROMO       = "promo"
    OTHER       = "other"   # 其他显式归类的可变政策事实

class SalesScriptFactStatus(StrEnum):      # §6
    VERIFIED_CURRENT = "verified_current"
    NEEDS_REVIEW     = "needs_review"
    STALE            = "stale"
    VERSION_1_ONLY   = "version_1_only"

class SalesScriptQueryScope(StrEnum):      # §5 运行时分类（非持久化）
    MIHE_1_0         = "mihe_1_0"
    MIHE_2_0         = "mihe_2_0"
    COMPARE_1_0_2_0  = "compare_1_0_2_0"
    UNKNOWN          = "unknown"

class SalesScriptEvidenceFactSafety(StrEnum):  # 同四态，用于证据行
    VERIFIED_CURRENT = "verified_current"
    NEEDS_REVIEW     = "needs_review"
    STALE            = "stale"
    VERSION_1_ONLY   = "version_1_only"
```

---

## 2. 源包完整性 + 激活契约（D1）

**两个完整性锚（导入时计算、持久化）：**
- `source_file_hash` = SHA256(原始 EBF 字节)
- `extraction_manifest_hash` = SHA256(规范化导出 JSON ‖ 有序归一化图片 SHA256 清单 ‖ 图引用映射)

**激活语义（每个 source_type 仅一个 ACTIVE）：**
1. 计算两锚；若 `extraction_manifest_hash` 已存在（ACTIVE 或 SUPERSEDED）→ **跳过导入（幂等）**。
2. 否则新插入源包（status 先置 ACTIVE），在**同一事务**内把同 `source_type` 的原 ACTIVE 翻为 SUPERSEDED。
3. 部分唯一索引 `UNIQUE(source_type, status) WHERE status='active'` 在 DB 层强制「每 type 至多一个 ACTIVE」；冲突则事务回滚。

**检索隔离：** 所有检索都 `JOIN SalesScriptSource AND status='active'` → 解析出恰好一个世代；旧世代翻 SUPERSEDED 后永不进入结果。**绝不混合两个 ACTIVE 世代。**

**文件名分离（cleanup B）：** `original_ebf_filename`（原始 EBF）与 `extracted_manifest_filename`（结构化 JSON）是两个独立字段。

---

## 3. 归一化段模型（D2）

- 图文真相**唯一**归属 `SalesScriptSegment`，`SalesScriptEntry` 不再存 `segments` JSON，旧 `SalesScriptMedia` 表删除。
- `entry.source_hash` = SHA256(按 sequence 排序的归一化 segments)；导入时计算、重导入时校验字节稳定 → 不可变证明。
- `UNIQUE(entry_id, sequence)` 保顺序；TEXT/IMAGE 两类通过 CHECK 互斥（TEXT 必有 text_content 且无 artifact_id；IMAGE 必有 artifact_id 且无 text_content）。
- 图片经 `artifact_id` 指向既有 `Artifact(type=IMAGE)`，blob 只存在 Artifact 一处。

---

## 4. 归一化事实绑定（D3）

- **一个事实出现 = 一行** `SalesScriptFactBinding`，结构化绑定到真实 `entry_id`（FK，非 JSON 引用列表）。
- `binding_hash` = SHA256(entry_id‖fact_key‖raw_span 归一化) 作确定性导入幂等锚；重复导入不产生重复行。
- **防悬空：** `ON DELETE CASCADE` 随条目删除；不存脱离条目的事实。
- **防跨版本：** CHECK `ck_ssfb_scope_compat` 要求 `scope='common' OR scope=entry.product_scope`。1.0 条目只能挂 1.0/COMMON 事实，2.0 条目只能挂 2.0/COMMON 事实 → 跨版本绑定被拒。
- 导入默认 `status=NEEDS_REVIEW`（**绝不自动 VERIFIED**）；只有 owner 单独复核的稳定事实才升 `VERIFIED_CURRENT`。

---

## 5. 运行时 scope 模型（含对比，D4）

**持久化条目域**：`MIHE_1_0` / `MIHE_2_0` / `COMMON`（不变）。

**运行时查询分类 `SalesScriptQueryScope`**（新增两类）：`MIHE_1_0` / `MIHE_2_0` / **`COMPARE_1_0_2_0`** / `UNKNOWN`。

| QueryScope | 检索条件 | 说明 |
|---|---|---|
| MIHE_1_0 | `product_scope IN ('mihe_1_0','common')` | 排除 2.0 专属 |
| MIHE_2_0 | `product_scope IN ('mihe_2_0','common')` | 排除 1.0 专属 |
| COMPARE_1_0_2_0 | `product_scope IN ('mihe_1_0','mihe_2_0','common')` | 支持「和之前扣子那个有什么区别」**而不被迫 UNKNOWN** |
| UNKNOWN | —— | fail-closed → 澄清建议（§8），不检索任何版本专属声明 |

**保守分类策略：**
- 显式版本证据（「2.0/新版本/合伙人后台」→ 2.0；「1.0/老版本/扣子」→ 1.0；「区别/对比/和之前」→ COMPARE）→ 分类。
- 弱/歧义证据 → **UNKNOWN / 请求澄清**，绝不凭弱关键词推断确定版本。

---

## 6. 事实安全 + AI 声明上限（D6 + D7）

**六类动态事实全纳入安全契约（D6）：** price / commission / membership / capability / url / promo，外加 `OTHER`（显式归类的其他可变政策事实）。

**状态行为：**
| 状态 | 建议生成时 |
|---|---|
| VERIFIED_CURRENT | 可断言 |
| NEEDS_REVIEW | 不得断言具体数字；标注「含待核验信息」 |
| STALE | 不得断言 |
| VERSION_1_ONLY（处于 2.0 语境） | 不可用，不得断言 |

**硬拒绝：** 任一关联绑定非 `VERIFIED_CURRENT` → 建议文本**不得主动断言价格/佣金/会员/能力/URL/促销**；改为省略或显式标注。

**AI 声明上限（D7）：**
- **允许改写**：语气 / 句序 / 称呼 / 简繁 / 组织。
- **禁止编造**：新能力 / 价格 / 佣金率 / 会员权益 / URL / 促销 / 保证 / 折扣承诺 / 性能声明。
- **证据天花板**（建议生成仅可用）：① VERIFIED_CURRENT 动态事实；② 当前适用且未被归类为可变事实声明的官方话术内容；③ 已审批 `KnowledgeFact`。

**SalesPlaybook 建议恒为 `HUMAN_CONFIRM`。**

---

## 7. CsSuggestion 证据集成（D5）

在既有 `generate_suggestion()` 内**并联**新增检索步：
- 现有 `KnowledgeFact` 路径不变；新增并行取 `SalesScriptEntry`（按 §5 scope + category）。
- **任一命中 → 强制 `decision = HUMAN_CONFIRM`**（压过 `AUTO_SEND` 置信度路径）。
- 证据写入 `CsSuggestionSalesEvidence`（suggestion_id, entry_id, rank, match_reason, fact_safety）—— **不改 `CsSuggestion` 任何列**。
- 内部产出须含：matched source entries（仅标题/分类给人看）、product scope、relevant images（Artifact URI）、fact-safety state、generated suggestion。
- 流程：客户消息 → `Message(INBOUND)` → 检索+事实安全 → `CsSuggestion(decision=HUMAN_CONFIRM)` + `CsSuggestionSalesEvidence[]` → owner 确认 → `Message(OUTBOUND, is_auto_sent=False)`。

---

## 8. UNKNOWN 澄清行为（D8）

- `QueryScope=UNKNOWN` 时：**不检索、不暴露任何版本专属销售声明**。
- 产生一个 `HUMAN_CONFIRM` **澄清建议**，内容仅含安全的产品版本澄清问题，例如：
  > 「你问的是之前的 1.0 扣子工作流版本，还是现在的 2.0？」
- 该建议**无 SalesScriptEntry 证据**（或空证据集），`decision=HUMAN_CONFIRM`，`is_auto_sent=False`。
- owner 澄清后重新分类再继续；澄清前绝不泄漏版本专属 claim。

---

## 9. 迁移影响（cleanup D：约束在建表时确立）

- **新增表**（单条 Alembic migration 一次建齐，约束在建表时确立，避免事后 raw-ALTER CHECK）：
  `sales_script_source` / `sales_script_entry` / `sales_script_segment` / `sales_script_fact_binding` / `cs_suggestion_sales_evidence`。
- **CHECK / FK / UNIQUE 在建表 DDL 内声明**：`ck_ss_entry_scope_member`、`ck_ssseg_type_nullability`、`ck_ssfb_scope_compat`、部分唯一索引 `(source_type, status) WHERE status='active'`、`UNIQUE(entry_id, sequence)`、`UNIQUE(suggestion_id, entry_id)`、`binding_hash` 唯一。
- **不改动** `KnowledgeFact` / `KnowledgeCandidate` / `Conversation` / `Message` / `CsSuggestion` 现有列（D5 已回避）。
- **枚举**均为 StrEnum→VARCHAR，纯代码新增，多数无需数据迁移；新表随迁移创建。
- **Alembic 单 head 必须保持**（本仓敏感点：SQLModel `select(Model)` 陷阱、不批量重建 `artifact` 表、`artifact` 表零改动）。
- 导入 = 幂等管理命令，**非 migration**。
- **图片**复用 `Artifact`（cleanup C）：`type=IMAGE, uri, checksum=SHA256(图片字节), review_status=复用 ArtifactReviewStatus(如 UNVERIFIED), metadata_json{original_ebf_filename, source_image_ref, source_entry_id}`，`project_id` 由导入提供。**不新增 Artifact 列/枚举值**。

---

## 10. 验收 / 对抗测试（设计期即定）

**首包验收（cleanup A：仅针对初始 EBF 包，非导入器永久常量）：**
- 150 条 / 61·82·7 scope 分布 / 74 唯一图 / 76 引用 / 0 缺失引用 导入干净、计数吻合。
- **这些数字是首包验收事实，导入器不硬编码为常量**；后续任意包允许任意计数。

**导入幂等：** 同 `extraction_manifest_hash` → 不重复源/条目/段；重跑为 no-op。

**源包完整性（D1）：** `source_file_hash`=SHA256(EBF 字节)；`extraction_manifest_hash`=SHA256(规范 JSON+图 SHA256 清单+引用映射)；重导入可重算一致。

**激活原子性（D1）：** 新包激活在单事务内翻旧 ACTIVE→SUPERSEDED、新→ACTIVE；部分唯一索引阻止双 ACTIVE；检索永不见两世代。

**段归一化（D2）：** 每个 TEXT 段有 text_content 且无 artifact_id；每个 IMAGE 段有 artifact_id 且无 text_content；`UNIQUE(entry_id, sequence)` 成立；顺序与源字节一致。

**无重复真相（D2）：** `SalesScriptEntry` 无 segments JSON；旧 `SalesScriptMedia` 已删；图片真相仅在 `SalesScriptSegment.artifact_id`。

**事实绑定归一化（D3）：** 一次出现一行；`binding_hash` 唯一；FK 防悬空；跨版本绑定被 scope 兼容 CHECK 拒绝（1.0 条目不能挂 2.0 事实）。

**版本隔离（D4）：** 2.0 问题只取 2.0+COMMON；1.0 问题只取 1.0+COMMON；COMPARE 取 1.0+2.0+COMMON；绝不混版本。

**对比支持（D4）：** 「和之前扣子那个有什么区别」→ `COMPARE_1_0_2_0`，非 UNKNOWN。

**UNKNOWN fail-closed（D4/D8）：** 弱/歧义 → UNKNOWN → 仅澄清建议，无版本专属检索。

**动态事实安全（D6）：** 任一 NEEDS_REVIEW/STALE/VERSION_1_ONLY(无效语境) 声明被抑制/标注；非 VERIFIED 时文本不断言价格/佣金；fact_safety 正确上报。

**AI 声明上限（D7）：** 生成器绝不引入新能力/价格/佣金/会员/URL/促销/保证/折扣/性能声明；仅改语气/句序/称呼/简繁/组织。

**CsSuggestion 证据（D5）：** 证据经 `cs_suggestion_sales_evidence` 双向 FK；`CsSuggestion` 架构不变；SalesPlaybook 建议恒 `HUMAN_CONFIRM`；结果 `Message.is_auto_sent=False`。

**Artifact 复用（cleanup C）：** 图片存为 `Artifact(type=IMAGE)` 复用 `ArtifactReviewStatus`；无新 Artifact 枚举/列。

**零 KnowledgeFact 写入：** 导入期与建议生成期均不触碰 KnowledgeFact/Candidate。

**官方/改写分离：** 「查看官方原话」返回逐字源；AI 建议文本可区分。

**对抗矩阵（pilot2 式）：**
- 无关条目（错类/错域）不入检索。
- 排除版本（1.0 事实答 2.0 问）被 scope 过滤拦截。
- 越界图片引用不上屏。
- 冻结源行跨重导入字节稳定（source_hash 稳定）。
- 重放（同消息再生成）产出相同建议 + 相同证据集（确定性）。
- 新包激活后旧 SUPERSEDED 世代永不入检索。
- UNKNOWN 注入版本关键词仍 fail-closed 到澄清（无版本 claim 泄漏）。
- 事实态翻转（VERIFIED→STALE）使原可断言 claim 在下一次生成被抑制（非陈旧依赖）。

---

## 11. 显式非目标（NON-GOALS）

- ❌ 微信连接器 / 自动发送
- ❌ CRM / 线索评分 / 跟进自动化 / 外呼营销
- ❌ 推荐引擎
- ❌ 向量库（除非后续严格论证）
- ❌ 新增付费 LLM 依赖（复用既有推理；检索确定性）
- ❌ 第二套会话/CS 子系统
- ❌ 重写官方源内容
- ❌ 自动折扣/促销承诺
- ❌ 把整段话术灌入 KnowledgeFact
- ❌ **本阶段实现**（仅设计；待 owner 批准修订架构）
- ❌ 阻塞 REAL LEAD-GEN EXPERIMENT #1

---

## 附录 A — 评审修订对照（owner D1–D8 + 清理 A–D 落点）

| 评审项 | 落点 |
|---|---|
| D1 双锚 + 激活原子性 | §1.1 / §2（source_file_hash + extraction_manifest_hash；部分唯一索引；单事务翻 SUPERSEDED） |
| D2 去重复真相 | §1.2/§1.3/§3（删 segments JSON + SalesScriptMedia；单一 `SalesScriptSegment`） |
| D3 事实绑定结构化 | §1.4/§4（entry_id FK；binding_hash；scope 兼容 CHECK） |
| D4 运行时含对比 | §5（COMPARE_1_0_2_0；保守分类） |
| D5 证据关联表 | §1.5/§7（`CsSuggestionSalesEvidence`；CsSuggestion 架构不动） |
| D6 全类事实安全 | §6（七类；非 VERIFIED 抑制/标注） |
| D7 AI 声明上限 | §6（允许改写 vs 禁止编造；证据天花板） |
| D8 UNKNOWN 澄清 | §8（仅安全澄清问题；无版本 claim） |
| 清理 A 数字非常量 | §10 首包验收段明确 |
| 清理 B 文件名分离 | §1.1（original_ebf_filename vs extracted_manifest_filename） |
| 清理 C 复用 Artifact 枚举 | §9 / §1.3（用 ArtifactReviewStatus，不新增） |
| 清理 D 建表即约束 | §9（CHECK/FK/UNIQUE 在建表 DDL，避免 raw-ALTER） |

## 附录 B — 安全契约 → 守卫映射（速查）

| 安全契约 | 实现守卫 |
|---|---|
| 不自动发送 | `decision` 强制 HUMAN_CONFIRM；`is_auto_sent=False` |
| 不自动外呼/触达 | 无 outbound 自动路径 |
| 不自动折扣/促销承诺 | 生成层不注入承诺文案 |
| 事实非 VERIFIED 不断言价格/佣金 | §6 硬拒绝 + fact_safety 上报 |
| 1.0 话术不答 2.0 问题 | §5 scope 过滤 + CHECK |
| 2.0 话术不答 1.0 问题 | §5 scope 过滤 + CHECK |
| 对比问题不被迫 UNKNOWN | §5 COMPARE_1_0_2_0 |
| UNKNOWN 版本 fail-closed | §5/§8 澄清建议 |
| 官方源不可变 | `source_hash` 校验 + 只读 |
| AI 改写可区分 | 「查看官方原话」独立视图 |
| 双 ACTIVE 世代不混 | §2 单事务激活 + 部分唯一索引 |
| 跨版本事实绑定 | §4 scope 兼容 CHECK 拒绝 |
