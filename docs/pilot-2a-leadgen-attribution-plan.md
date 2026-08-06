# PILOT-2A — 引流与内容归因引擎（设计稿 / DESIGN ONLY）

状态：**v2 修订稿（按 owner REQUEST_CHANGES_DESIGN 修订，待最终批准进入 2A-1）**
阶段：REAL BUSINESS PILOT · PILOT-2A
决策锚点：Issue #134
前置：PILOT-1（米核 2.0 只读抓取器，Issue #133）已上线并产出首个基线快照
本文件不含任何实现，不修改运行时代码，不新增外部凭证。

---

## 修订记录

- **v1（2026-08-05）**：初稿，10 项输出 + 三件必须先纠正的事实。
- **v2（2026-08-06）**：按 owner `REQUEST_CHANGES_DESIGN` 修订，战略方向不变，修 6 个合同级问题，并落定 D1/D2/D3 + G2/G11 三个决策点：
  1. §2 归因层级由 `L1 HIGH/L2 MEDIUM/L3 LOW/L0` 改为 **VERIFIED·DIRECT / CLICK_ASSOCIATED / EXPERIMENT_ASSOCIATED / AMBIGUOUS / UNATTRIBUTED**；**G3 证明米核注册链真正保存 campaign/ref 之前，系统禁止发出任何 "HIGH/直连注册归因"**。
  2. §6 首轮实验改为**单渠道、单内容、48h 串行**；严禁两渠道同时发；渠道须含目标用户，否则直接小红书单渠道串行。
  3. §4.1 原始层扩展为 **RAW EVIDENCE LAYER**（MiheSnapshot / PublicationEvent / ClickEvent / PlatformMetricSnapshot）；上层从原始证据层重建，而非"米核快照即一切真相源"。
  4. §2.5 新增 **AttributionProposal（可确定性重算）** 与 **Final Attribution（不可改写，仅 supersede）** 的分离。
  5. §0.1 / D3：13 条错误 KnowledgeFact **全部 deactivate**（12 客户行 + 1 基线事实），不物理删除、不改写已批准事实；新观测进 Pilot report / operational observation，不进 KnowledgeFact。
  6. §0.2 / D1 / G11：9 个批量账户标记为 **UNKNOWN_BATCH_COHORT**（owner 已说明为米核 1.0 迁移、非自然流量，但数据层统一以此标记，不计入自然基线、不删除、不宣称已证实）；基线改为"可用于当前实验基线的非批量历史注册观测：3 人 / 近 30 天"。
  - 附加（owner 已批准）：G2/D2 批准建自有跳转层，但**重定义**为"点击归因可达真值，注册归因仍为关联推断"；§3.3 决策阈值改为"≥1 个注册关联 → 建议复测同类 2~3 轮"；§8 建议引擎（2A-5）推后至真实数据轮次之后。
- **v3（2026-08-06）**：接受 Codex 对实现 PR #136（head `edb157668f50b13985dbb5e119ffc5f01da57a87`）的 4 项 blocking findings + 1 项 process-gate finding。PR #136 保持 **BLOCKED**（设计门未过）。新增 §11「评审硬合同补遗」，把 D1–D4 写成可评审的硬合同：D1（G3 前禁止持久化 `VERIFIED_DIRECT`/`DIRECT`/`HIGH` person 级注册归因，注册归因结果仅限 `EXPERIMENT_ASSOCIATED`/`AMBIGUOUS`/`UNATTRIBUTED`，点击仅证 campaign 收到点击）；D2（`FinalAttribution` 在 DB 边界强制 7 条不变量，不可弱化为仅应用层）；D3（staging create-all 守卫改为精确解析归一化路径比对、禁止一切子串识别、记录确切 staging DB 路径）；D4（Pilot-2 合同测试纳入正常 `pytest -q` 门，未被收集则 CI 无效）。

---

## 0. 先说三件必须先纠正的事

在展开设计之前，有三个既有事实与本阶段的新战略直接冲突，必须先摆在 owner 面前。

### 0.1 昨天的做法违反了新的数据路径规定（D3 已裁定：13 条全部停用）

PILOT-1 收尾时，我把 12 条米核客户原始行**直接**转成了 12 条 KnowledgeFact（外加 1 条基线事实），共 13 条。

owner 本次明确规定的数据路径是：

```
原始证据 → 规范化运营观测 → 归因 → 业务分析 → （可选）知识候选 → owner 评审 → KnowledgeFact
```

即：**原始行不得直接成为知识事实**；KnowledgeFact 也不应成为运营指标存储。

**owner 最终裁定（D3）**：13 条**全部 deactivate**。
- 12 条客户行 + 1 条运营基线事实 = 13 条全部 `inactive`；
- **不物理删除**，审计痕迹保留；
- **不改写**那 1 条已批准基线事实（不把它"改写"成观测基线表述）；
- 新的表述「截至 2026-08-05：观察到 X 个历史账户……」放入 **Pilot report / operational observation**，**不进 KnowledgeFact**；
- 只有当数据积累出稳定、可复用的结论（例如"电商商品图类内容连续多轮明显比泛 AI 创业内容更容易产生注册关联"）时，才走 `KnowledgeCandidate → owner 批准 → KnowledgeFact`。

### 0.2 "12 个客户"不是 12 个真实线索（D1 / G11 已裁定：UNKNOWN_BATCH_COHORT）

看快照里的注册时间戳：

| 注册时间（UTC） | 数量 | 说明 |
|---|---|---|
| 2026-08-05 01:00:22 ~ 01:05:26 | **9** | 5 分钟内集中出现，昵称均为"用户+手机后四位"，余额 0，8/9 从未登录 |
| 2026-07-09 / 07-07 / 07-03 | 3 | 分散在一个月内，各有一次登录 |

9 个账号在 5 分钟内成批出现、且几乎全部从未登录，高度可疑不是自然引流。

**owner 最终裁定（D1 / G11）**：
- 这 9 个账户标记为 **`UNKNOWN_BATCH_COHORT`**；owner 已说明其为米核 1.0 迁移账号（非自然流量），但数据层统一以 `UNKNOWN_BATCH_COHORT` 标记 —— **不计入自然注册实验基线、不删除、也不在系统里宣称"已证实是迁移账号"**。
- 因此正确的基线表述不是"自然注册 ≈ 3 人"，而是：

> **可用于当前实验基线的非批量历史注册观测：3 人 / 近 30 天。**
> 这 3 人是"非批量、分散、各有登录"的历史观测，只是"非批量"，**尚未被证明是自然引流**；9 个批量账户从基线中排除，单独保留供审计。

这个数字决定了后面所有实验设计的可解释性下限 —— 见 §3.5「小样本纪律」。

### 0.3 米核客户表**没有来源字段** —— 这是整个归因设计的地基

实测 `GET /api/channel/me/customers` 返回的客户对象字段全集：

```
id, nickname, avatar, phone(平台已打码), balance,
customerType(=direct), registeredAt(毫秒级 ISO), lastLoginAt,
totalRecharge, rechargeCount
```

- **没有** `source` / `ref` / `channel` / `inviteCode` / `utm` 任何来源标识；
- `customerType=direct` 是合伙人层级（直客 vs 下级合伙人的客户），**不是流量来源**；
- 好消息：`registeredAt` 是**毫秒精度时间戳**，`id` 是稳定 CUID，`phone` 平台侧已打码。

结论：

> **无法只靠米核的数据把"某个注册"对应到"某条内容"。**
> 归因必须由 AIOS 自己在上游建立，米核只提供"某时刻发生了一次注册"这一个下游事实。

这一条推翻了"接个来源参数就能归因"的天真设想，也直接决定了 §2 的层级形态与 §7 G3 的关键性。

---

## 1. 引流漏斗合同（Lead-gen Funnel Contract）

定义每一段的**事实来源、可得性、精度、延迟**。凡是拿不到的，就明确写"拿不到"，不许用估算填。

| # | 阶段 | owner 看到的名字 | 事实来源 | 可得性 | 时间精度 | 延迟 |
|---|---|---|---|---|---|---|
| S0 | CONTENT_PRODUCED | 已产出内容 | AIOS 内部 Artifact | ✅ 确定 | 秒 | 实时 |
| S1 | PUBLISHED | 已发布 | 发布记录（V0 手工确认） | ✅ 确定 | 分 | 人工 |
| S2 | SURFACE_METRIC | 曝光/阅读 | 平台后台（公众号阅读数 / 小红书笔记数据） | ⚠️ 部分，需人工录入 | 天 | T+1 |
| S3 | CLICK | 点击 | **AIOS 自有跳转层（尚不存在，G2 已批准自建）** | ❌ 当前不可得 | 秒 | 实时 |
| S4 | REGISTRATION | 新增注册 | 米核客户快照差分 | ✅ 确定（但无来源） | 毫秒 | T+1（按抓取节奏） |
| S5 | ACTIVATION | 使用/登录 | `lastLoginAt` | ✅ 确定 | 毫秒 | T+1 |
| S6 | RECHARGE | 充值 | `totalRecharge` / `rechargeCount` / 收益记录 | ✅ 确定 | 天 | T+1 |
| S7 | COMMISSION | 佣金 | 收益记录 / 资金流水 | ✅ 确定 | 天 | T+1 |

合同条款：

1. **S5–S7 本阶段只观测，不优化。** 北极星是 S4。
2. **S2 缺失时必须显示"暂无数据"，绝不显示 0。** 0 是一个测量结果，缺失不是。
3. **S3 是当前最大的缺口，但已批准自建（G2）。** 即便有了跳转层，点击真值也只覆盖"哪条内容带来点击"，**不自动覆盖"哪个点击的人完成了注册"**——见 §2.2 与 §7 G3。
4. **S4 是唯一由外部系统盖章的硬指标**，也是唯一不可伪造的业务证据。
5. 每一条新增注册**必须**带一个归因状态，不允许为空。未归因是合法状态，不是错误。

---

## 2. 归因模型

### 2.1 设计原则

- **宁可未归因，不可乱归因**（沿用 AIOS 的 fail-closed 传统）；
- **确定性可复算**：同一批输入重跑，结果逐字节一致 —— 这是 *AttributionProposal* 层的要求（见 §2.5）；
- **Final Attribution 不可回溯改写**：一旦确认落定，只能以 *supersede* 方式被新结论取代，原记录保留留审计（见 §2.5）；
- **不做统计显著性断言**：当前量级（个位数/天）下任何 p 值都是自欺欺人。

### 2.2 归因层级（v2 重写：从 L1–L3 改为五级，G3 证明前禁止直连归因）

**核心约束**：米核客户表无来源字段，点击事件与注册事件之间没有同一身份键可 join。所以"有 campaign 点击 + 时间窗口"只能证明**时间相关**，不能证明"点的人就是注册的人"。

因此本阶段（G3 未证明前）**系统里不允许出现 "HIGH / 直连注册归因" 字样**。层级重定义为：

| 层级 | 含义 | 何时出现 | owner 看到 |
|---|---|---|---|
| **VERIFIED / DIRECT** | 注册端真正保留了 campaign/ref，或可连接身份 | **仅当 G3 证明米核注册链保存 campaign/ref 后** | 已验证直连 |
| **CLICK_ASSOCIATED** | 有 campaign 点击 + 时间窗口 → 强时间关联 | 有自有跳转层 + 点击 + 窗口 | 点击关联 |
| **EXPERIMENT_ASSOCIATED** | 实验窗口内全局只有一条 leadgen 内容在跑 → 时间实验关联 | 零基建，立即可用 | 实验关联 |
| **AMBIGUOUS** | 多个候选内容同时满足时间条件 | 重叠窗口 | 存疑（多个候选） |
| **UNATTRIBUTED** | 无任何可关联证据 | 窗口内无实验 / 无点击 / 证据不足 | 未归因 |

> **v2 铁律**：在 §7 G3 证明 AI觅注册链真正保存 `campaign/ref` 之前，注册归因的**最高可达层级**是 `CLICK_ASSOCIATED` 或 `EXPERIMENT_ASSOCIATED`，**绝不**发出 `VERIFIED/DIRECT`。一旦把相关性写成"高置信归因"，后续 Agent 会基于错误证据学习内容方向 —— 这是本稿唯一接近 P1 的风险点，已根除。

**CLICK_ASSOCIATED 与 EXPERIMENT_ASSOCIATED 的区别（D2 重定义的关键）**：

- 自有跳转层（G2 已批准）解决的是**点击归因真值**：`内容 → channel-specific link → ClickEvent 真值 → AI觅`。有了它，"哪条内容真正带来了点击"可以做到确定。
- 但**注册归因**在 G3 证明前仍是**关联推断**：跳转层能证明"有人点了 A 的链接"，却无法证明"10:14 注册的那个人就是 10:03 点 A 的人"（米核不回传身份）。
- 所以：**点击归因 = 可达真值；注册归因 = 仍为关联推断**。这条必须写死，不可模糊。

> **硬合同锚点（D1，权威定义见 §11.1）**：在 G3 证明前，*持久化*归因词汇表不得定义 `VERIFIED_DIRECT` / `DIRECT` / `HIGH` 置信 person-level 注册归因值；`CLICK_ASSOCIATED` 仅表示"点击层关联证据"（证明某 campaign 收到了点击），**不构成** person-level 注册归因结果。按 D1，G3 前的注册归因落库结果**仅限** `EXPERIMENT_ASSOCIATED` / `AMBIGUOUS` / `UNATTRIBUTED`——§2.2 层级表中 `CLICK_ASSOCIATED` 行在 G3 前应理解为点击层事实，不进入注册归因结果集。

### 2.3 归因判定顺序（确定性算法，v2 五级版）

```
对每一条新增注册 r（按 registeredAt 升序处理，排除 UNKNOWN_BATCH_COHORT）：
  1. 若 G3 已证明且注册端带 campaign/ref 或可连接身份 → VERIFIED/DIRECT
  2. 否则，若存在 campaign 点击且落入点击窗口且唯一        → CLICK_ASSOCIATED
  3. 否则，若实验窗口内全局恰好 1 个 leadgen 实验在跑      → EXPERIMENT_ASSOCIATED
  4. 否则，若窗口内有 ≥2 个 leadgen 实验在跑              → AMBIGUOUS（列全部候选，待 owner 裁定）
  5. 否则                                          → UNATTRIBUTED
  结果先写入 AttributionProposal（可重算）；owner 确认/超时后固化为 Final Attribution。
```

- **首次接触归因（first-touch）**，V0 不做多触点分配、不做小数拆分；
- 一条注册**至多**归因给一个内容；
- 窗口默认 48h（实验可配），点击窗口默认 30min，均为显式参数不是魔数；
- `UNKNOWN_BATCH_COHORT` 账户**不进入归因计算**，单独计入审计桶。

### 2.4 owner 只看业务语言

内部存 `campaign_id` / `content_id` / `CLICK_ASSOCIATED|EXPERIMENT_ASSOCIATED|AMBIGUOUS|UNATTRIBUTED`，owner 看到的是：

```
小红书
《一张产品图生成5套电商场景图》

曝光/访问：暂无数据
新增注册：8
归因把握：实验关联（同期只发了这一条）

主题：AI 商品图
核心钩子：省下拍摄成本
```

映射层强制存在，owner 视图里出现任何内部 ID、英文 slug 或 "HIGH/直连归因" 字样视为缺陷。

### 2.5 AttributionProposal 与 Final Attribution 的分离（v2 新增，解决"可重算"与"不可改写"的矛盾）

"可整体重算"与"落定后不可回溯改写"两句话都对，但必须拆成两个概念：

```
AttributionProposal（可确定性重算）
  每次摄入新证据（新快照 / 新 ClickEvent / 新 PublicationEvent）都重新计算
  → 产出当前"最优归因建议"
        ↓ owner 确认 / 超时自动接受
Final Attribution（不可回溯改写）
  一旦确认，旧记录保留，只能被新结论 supersede
```

**示例**：
- 系统最初：注册 A → 内容 1（`EXPERIMENT_ASSOCIATED`）。
- 后来接入点击证据：重新计算 Proposal → 注册 A → 内容 2（`CLICK_ASSOCIATED`）。
- **不允许**偷偷把原 attribution 改成内容 2。
- 必须：旧 Final Attribution 标记为 `superseded`（保留原因 + 时间戳 + 触发证据），新 Final Attribution 标记为 `accepted`。

这条与 AIOS 既有的 immutable / audit 思路一致：派生可重算，结论不可静默改写。

> **硬合同锚点（D2，权威定义见 §11.2）**：`FinalAttribution` 的"同 registration 归属 / 至多一个 ACCEPTED / 历史保留 / 仅 supersede / 原子替换 / 无两 accepted head 瞬态"必须在**数据库边界**（DDL + 约束 + 事务）强制，**不可弱化为仅应用层校验**；mismatch / duplicate-head / concurrent replacement 必须有测试覆盖。

---

## 3. 内容实验分类法（Content Experiment Taxonomy）

### 3.1 独立词表，**不碰** `CANONICAL_KNOWLEDGE_TAGS`

知识标签是架构锁定的受控词表，改动需评审 + 版本升级。内容分类法是**另一套**受控词表，独立版本号 `CONTENT_TAXONOMY_VERSION = 1`，放在 pilot2 模块内，与知识标签零耦合。

### 3.2 六个维度（V0 刻意做小）

| 维度 | 取值 | owner 中文标签 |
|---|---|---|
| **赛道 track** | ip / leadgen | 个人 IP / 引流 |
| **人群 audience** | apparel, accessories, home, food, general_ecom, side_hustle | 服饰 / 饰品 / 家居 / 食品 / 泛电商 / 副业创业 |
| **场景 use_case** | hero_image, model_image, scene_image, detail_page, product_video | 主图 / 模特图 / 场景图 / 详情页 / 商品视频 |
| **价值主张 value_prop** | save_money, save_time, no_design_skill, better_creative, batch_production | 省钱 / 省时间 / 不用会设计 / 出图更好 / 批量生产 |
| **形式 format** | tutorial, real_case, before_after, experiment, pitfall, tool_rec | 教程 / 真实案例 / 对比前后 / 实测 / 避坑 / 工具推荐 |
| **钩子 hook** | cost, efficiency, result, curiosity, pain | 成本 / 效率 / 效果 / 好奇 / 痛点 |

组合空间 6×5×5×6×5 = 4500 格。**永远测不完**，所以：

### 3.3 实验合同（Experiment Contract）

每个实验**必须**预先声明，事后不得改写：

```yaml
实验名:      给 owner 看的一句话
赛道:        leadgen | ip
假设:        "服饰卖家对『省下拍摄成本』这个钩子最敏感"
主变量:      hook            # 有且仅有一个
锁定维度:    audience=apparel, use_case=scene_image, format=before_after, value_prop=save_money
对照:        上一轮同锁定组合的实验（或无对照，记为基准轮）
渠道:        xhs            # v2：首轮严禁多渠道并发
曝光窗口:    48h
决策阈值:    出现 ≥1 个注册关联 → 建议复测同类方向 2~3 轮；连续 2 轮 =0 → 停止；曝光>500 且注册=0 → 调整钩子
```

**一个实验只允许一个主变量。** 其他维度锁死为"当期默认组合"。

> **v2 首轮纪律（来自 owner 裁定 2）**：第一轮真实实验**只跑一个渠道、只跑一条内容、48h 串行**。不允许"同一条内容同时发朋友圈 + 小红书"。原因：若两个渠道同时开跑，出现 1 个注册时你最多知道"这次 campaign 可能有效"，但**不知道是哪个渠道带来的**，可解释性归零。在 0–1 注册/天的量级，速度根本不是瓶颈，**可解释性才是瓶颈**。等自有跳转层上线、各渠道拿到独立链接后，才适合并发。

### 3.4 两条赛道，两套 KPI，禁止混算

| | A. 个人 IP 内容 | B. 引流内容 |
|---|---|---|
| 目标 | 黎叔 / AI 创业实验室的信任与受众 | 让电商用户点击并注册 AI觅 |
| 主 KPI | 关注增长、互动、被转发 | **归因注册数** |
| 归因要求 | 不要求 | 必须可归因 |
| 失败判据 | 长期零互动 | 连续 2 轮零注册 |

同一条内容不得同时挂两条赛道的 KPI；确需两用则拆成两条发布记录。

### 3.5 小样本纪律（必须写进系统行为）

基线：可用于当前实验基线的非批量历史注册观测 3 人 / 近 30 天（量级约 0–1 人/天）。这意味着：

- 系统**只呈现计数与归因状态，不呈现转化率、不做显著性检验、不做趋势外推**；
- 一条内容带来 1 个注册关联是**值得追踪的信号，但不是"这个策略已有效"的证据** —— 因此触发"建议复测同类方向 2~3 轮"，而非直接判定成功；
- "表现最好的主题/钩子"排行在样本 <10 时必须带上"样本极少，仅供参考"的显式提示；
- 决策 = 预设阈值 + owner 判断，**系统不替 owner 下结论**。

---

## 4. 所需数据源

| 数据源 | 内容 | 现状 | 需要做什么 |
|---|---|---|---|
| 米核客户快照 | 注册/登录/充值 | ✅ PILOT-1 已有 | 加分页保护、加差分引擎 |
| 米核收益/流水 | 佣金、资金 | ✅ 已有 | 仅观测，不进归因 |
| AIOS Artifact | 内容成品 | ✅ 已有 | 关联到发布记录 |
| **发布记录** | 何时、发到哪、用了哪个链接 | ❌ 不存在 | V0 手工登记（owner 或我代录，owner 确认） |
| **点击日志** | campaign 级点击 | ❌ 不存在 | 需自有跳转层（G2 已批准） |
| 公众号数据 | 阅读、分享、在看 | ⚠️ 后台可读，无自动化 | V0 人工录入 |
| 小红书数据 | 曝光、阅读、互动、涨粉 | ⚠️ 创作中心可读，无自动化 | V0 人工录入 |
| 实验注册表 | 实验合同与状态 | ❌ 不存在 | 新建（纯内部，零外部依赖） |

### 4.1 数据分层（v2：RAW EVIDENCE LAYER 扩展，解决架构矛盾）

原稿写"②③④ 全部可从 ① 米核快照重建"，但发布记录、点击记录、公众号阅读、小红书曝光**都不可能从米核客户 JSON 重建**。修正为：**所有外部/人工输入的事实首先变成不可变原始证据，上层才能重建**。

```
RAW EVIDENCE LAYER（不可变原始证据，只追加）
├─ MiheSnapshot          米核客户/收益 JSON 快照（PILOT-1 已有）
├─ PublicationEvent      发布记录（渠道 / 时刻 / 用了哪个链接）
├─ ClickEvent            campaign 级点击（需跳转层）
└─ PlatformMetricSnapshot 公众号阅读 / 小红书曝光 等平台指标
        ↓ 规范化
Normalized Observation（注册观测 / 发布 / 点击 / 平台指标实体）
        ↓
Attribution（AttributionProposal → Final Attribution）
        ↓
Analysis（主题榜 / 钩子榜 / 实验结论）
        ↓ （仅"结论"可进）
KnowledgeCandidate → owner 评审 → KnowledgeFact
```

- **Normalized Observation / Attribution / Analysis 三层从 RAW EVIDENCE LAYER 完整重建**，不是"从米核快照重建"；
- 米核快照只是 RAW EVIDENCE LAYER 的四个来源之一，**不是一切真相源**；
- RAW EVIDENCE LAYER 本身不可变、只追加；上三层是纯派生，可整体重算；
- RAW EVIDENCE LAYER → Analysis 不产生 KnowledgeFact；只有 Analysis 之上的"稳定结论"经 owner 批准才升级。

### 4.2 隐私边界

- 米核返回的手机号平台侧**已打码**（`132****3183`），我们**不去解码、不去补全、不与任何外部数据关联**；
- 客户 `id`（CUID）作为主键，非 PII；
- 昵称是平台自动生成的"用户+后四位"，按半标识符处理，**不进 owner 视图**（owner 视图只看聚合数字）；
- 客户名单不导出、不外发、不跨项目使用；
- `UNKNOWN_BATCH_COHORT` 账户单独计入审计桶，不混入任何业务计数。

---

## 5. OOL owner 视图提案

### 5.1 每日引流战报（主卡片）

```
昨天（8月6日）

新增注册        3
  已归因        2
    · 点击关联  1
    · 实验关联  1
  未归因        1        ← 点开可看"当时没有实验在跑"
  批量账户(审计) 0        ← UNKNOWN_BATCH_COHORT 不计入此处

带来注册关联的内容
  1. 《一张产品图生成5套场景图》     小红书    实验关联 2 人
  2. 《我把拍摄外包砍到了0》         公众号    点击关联 0 人   曝光 暂无数据

主题表现（样本极少，仅供参考）
  AI 商品图    2 人 / 2 条
  批量出图     0 人 / 1 条

钩子表现（样本极少，仅供参考）
  省下拍摄成本  2 人
  效率提升      0 人

下游观测（本阶段不优化）
  登录        1 人
  充值        0 人
  佣金        ¥0
```

### 5.2 今天建议做这 3 条（v2：推后至真实数据轮次之后）

> **owner 裁定：建议引擎（2A-5）往后放。** 先产生几轮真实数据，再让 AI 基于**你自己的真实获客数据**建议下一条内容。否则一开始"今天建议做 3 条"大概率只是模型凭常识写，而非基于真实数据决策。

引擎接线后（2A-5），复用现有建议引擎（无匹配证据 → ESCALATE，绝不硬编内容），每条建议给出：标题方向 / 主题 / 钩子 / 渠道 / 依据（引用哪几条已归因证据）。owner 对每条只做四选一：**继续 · 调整 · 停止 · 新增实验**。

### 5.3 待决策收件箱（只在真的需要人时才打扰）

只有三类进 owner inbox：

1. 实验到达决策阈值 → 请 owner 拍继续/调整/停止；
2. 归因冲突（AMBIGUOUS，多个候选）→ 请 owner 裁定或选择保持未归因；
3. 洞察候选待批 → 升级为 KnowledgeFact 前的最后一道人工闸。

**owner 永远不需要自己在表格里算归因。**

### 5.4 语言纪律

owner 视图内：0 个内部 ID、0 个英文 slug、0 个"HIGH/直连归因"字样、0 个技术术语、0 个百分比幻觉。全中文业务标签。

---

## 6. 首个发布渠道建议

三个面各司其职，不要混为一谈：

| 用途 | 渠道 | 理由 | 本阶段做法 |
|---|---|---|---|
| **链路验证首发面** | **朋友圈 / 微信群** | 唯一能在 24–48h 内拿到第一个真实注册的地方；链接完全可控；零平台限制；能立刻验证"点击→注册→快照差分→归因→OOL 显示"整条链路是否真的通 | 手工发，只需一个带 campaign 码的链接 |
| **第一个自动化连接器** | **微信公众号** | 可放外链 → 每篇一个独立归因链接（跳转层建成后能做到 CLICK_ASSOCIATED）；阅读数可读；已有成熟 skill；黎叔已有号 | PILOT-2B 首个接入目标 |
| **主力流量场** | **小红书** | 目标人群（电商卖家）在；视觉即产品，前后对比图天然适配；冷启动分发好 | 手工发 + EXPERIMENT_ASSOCIATED 时间隔离；不建连接器 |

**为什么不是小红书打头阵**：小红书笔记内不能放外链，主页只能挂一个链接。这意味着小红书天然做不到"一条笔记一个链接"的点击归因，只能靠 EXPERIMENT_ASSOCIATED 时间隔离。所以它适合当流量发动机，但**不适合用来验证归因系统本身是否正确**。先用朋友圈把链路跑通，再上公众号做自动化，最后把小红书的量灌进来——这个顺序能让每一步的失败都可定位。

**建议的首个真实动作（v2：单渠道、单内容、48h 串行，且渠道须含目标用户）**：

> **第一轮实验纪律（owner 裁定 2 + 渠道调整）**：
> - **只跑一个渠道、只跑一条内容、48h 串行窗口**，严禁两渠道同时发。
> - 渠道里**必须确实存在目标用户（电商卖家）**。若你现有朋友圈/微信群有电商从业者，可用朋友圈做链路验证首发。
> - **否则第一轮直接：小红书单渠道、单内容、48h 串行**——即使只能得到时间关联（EXPERIMENT_ASSOCIATED），也比拿错误目标人群做一个"技术上很干净"的实验更有商业意义。
> - 等自有跳转层上线、朋友圈与公众号分别拿到独立链接后，才适合并发多渠道。

---

## 7. 埋点缺口清单

| # | 缺口 | 影响 | 处理方式 | 需要 owner？ |
|---|---|---|---|---|
| **G1** | 米核客户表无来源字段（已实测确认） | 注册归因无法靠平台实现 | 自建点击层 + 接受关联推断 | 已定性，无需决策 |
| **G2** | **无自有跳转/短链服务（无域名、无宿主）** | 拿不到点击真值 | **owner 已批准 YES（重定义）**：先解决"点击归因真值"，注册归因仍为关联推断直到 G3 | **R7 必须** |
| **G3** | 贴牌站是否支持带参注册链接（未验证） | **决定能否出现 VERIFIED/DIRECT 层级** | 只读探查站点信息页与注册页 JS，**不做真实注册测试** | 探查无需授权 |
| **G4** | 平台曝光数据无 API | 曝光维度长期缺失 | V0 人工录入；显示"暂无数据" | 否 |
| **G5** | 小红书外链限制 | 无法一笔记一链接 | EXPERIMENT_ASSOCIATED 时间隔离 + 主页单链轮换 | 否 |
| **G6** | 抓取节奏为每天一次 | 注册时间已是毫秒精度，实际不构成瓶颈 | 维持每日；实验期可提至每 6h | 否 |
| **G7** | 米核 token 2026-08-11 过期 | 抓取会断 | 探 refresh 端点；否则约定 owner 重登仪式 | 需 owner 配合重登 |
| **G8** | 客户列表分页（当前 total=12，pageSize=20） | 量大后会漏数据 | 差分引擎必须全量翻页 | 否 |
| **G9** | 无发布记录模型 | 内容与发布行为断链 | 新建，V0 手工登记 | 否 |
| **G10** | 官方客服环节完全黑盒 | 看不到"咨询→成交"过程 | 本阶段接受，只看两端 | 否 |
| **G11** | 那 9 个疑似批量账号性质 | 基线可能被高估 4 倍 | **owner 已裁定：UNKNOWN_BATCH_COHORT，不计入自然基线，不删除、不宣称已证实** | **已裁定（D1）** |

---

## 8. 实施计划（每阶段一个 Issue、一个分支、一个 PR、一道 owner 闸）

| 阶段 | 内容 | 产出 | 外部副作用 | 闸 |
|---|---|---|---|---|
| **2A-0** | 本设计定稿 + 纠偏 | 本文档 v2 + **13 条错误 KnowledgeFact 全部 deactivate**（D3） | 库内停用标记（不物理删） | owner 批准 |
| **2A-1** | 可行性探查 | G3/G7 探查报告；归因层级最终定档 | 只读 | owner 知会 |
| **2A-2** | 数据模型 + 受控词表 | RAW EVIDENCE LAYER / 实验/内容/发布/观测/归因边 的模型与测试 | 无 | PR 评审 |
| **2A-3** | 注册差分引擎 | 基于 PILOT-1 快照，幂等、可复算、UNKNOWN_BATCH_COHORT 排除、未归因一等公民 | 只读 | PR 评审 |
| **2A-4** | 归因求解器 + owner 视图 | 每日战报、内容榜、实验看板（CLICK/EXPERIMENT/ASSOCIATED/AMBIGUOUS/UNATTRIBUTED） | 无 | owner 验收 |
| **真实单渠道实验** | 首轮：单渠道单内容 48h 串行 | 产生真实获客数据轮次 | 真实发布（owner 批准草稿） | owner 批准 |
| **2A-5** | 建议引擎接线（v2：推后） | "今天建议做这 3 条"，**基于真实数据轮次决策** | 无 | owner 验收 |
| **2B-1** | 首个发布连接器（公众号） | 草稿 → owner 逐条批准 → 发布 | **有**（真实发文） | **R7** |
| **2B-2** | 自有跳转层 | 点击真值，升级到 CLICK_ASSOCIATED（G2） | **有**（对外服务） | **R7** |

2A 全程**零外部副作用**（2A-0 的停用标记除外）：只读米核 + 内部计算 + 内部展示。真正会"动外面"的都在 2B，单独授权。

顺序上的关键取舍：**先让归因引擎能吃"手工登记的发布记录"跑起来，再谈自动发布**。因为如果归因是错的，自动发布只会更快地放大错误。

> **v2 顺序调整（owner 裁定）**：建议引擎 2A-5 推后到"真实单渠道实验"之后。先产生几轮真实数据，再让 AI 基于你自己的获客数据建议下一条内容，避免一开始只是模型凭常识写。

> **硬合同锚点（D4，权威定义见 §11.4）**：Pilot-2 模型合同测试是**正常 pytest 门**的一部分——`python -m pytest -q` 必须收集并执行它们，不依赖特殊手工调用路径；优先移入仓库既有 `tests/` 树。若新合同测试未被收集，一次"绿色" CI 视为无效。

---

## 9. 硬性安全与排除边界

**米核侧**
- 只读。禁止任何写操作：不下单、不开卡密、不提现、不改资料、不提交资质、不动客服设置。
- 禁止为了测试归因而真实注册账号（会污染真实客户表）——如确需，必须 owner 单独授权并明确标记。
- 抓取节奏保守，不并发轰炸，不抓与本账号无关的数据。

**数据侧**
- 不存原始手机号，不解码打码号，不做身份关联。
- 客户名单不导出、不外发、不跨项目复用。
- 原始行绝不直接成为 KnowledgeFact（本次纠偏核心，D3 已裁定 13 条停用）。
- **Final Attribution 不可回溯改写**；以 *supersede* 方式留审计，旧记录保留（见 §2.5）。
- `UNKNOWN_BATCH_COHORT` 账户不计入任何业务计数，仅留审计。
- 缺失数据显示"暂无数据"，绝不填 0，绝不估算，绝不把推测值当测量值展示。

**内容侧**
- V0 一律 草稿 → owner 逐条批准 → 才发布，无自动直发。
- 朋友圈/社群有频次上限，不刷屏。
- 不冒充真实使用者、不编造案例数据、不承诺收益。
- 首轮实验单渠道单内容串行，不并发多渠道。

**架构侧**
- 不动 `CANONICAL_KNOWLEDGE_TAGS`；内容分类法是独立词表。
- 不新增任何外部凭证（未走 R7 前）。
- 本阶段**明确不建**：自有客服运行时、销售跟进自动化、12 客户激活 SOP、催充值系统、CRM 销售管线、KYC/提现/支付运营。
- **staging 建表守卫（硬合同 D3，见 §11.3）**：`migrations_create_all` 必须解析并归一化 DB URL，仅允许确切批准的 staging 路径（`uat_ool_v0/staging/human_uat.db`，sqlite）；禁止任何子串识别（"staging" / "human_uat.db" 等）；普通 import / startup 不得触发 `create_all()`；拒绝时 fail closed 非零退出。

---

## 10. 验收指标

### 10.1 设计阶段（本文档）

- owner 对 10 项输出逐项表态（v2 已完成 REQUEST_CHANGES 的 6 点修订）；
- **G11（9 个账号性质）：已裁定 UNKNOWN_BATCH_COHORT（D1）**；
- **G2（跳转层是否上）：已裁定 YES，但重定义为"点击真值 + 注册关联推断"（D2）**；
- D3（13 条 KnowledgeFact）：已裁定全部 deactivate；
- 无遗留阻塞性 R7 事项（G2/G7 已知需 R7，已登记）。

### 10.2 引擎阶段（2A-2 ~ 2A-5）

| # | 指标 | 判据 |
|---|---|---|
| A1 | 归因完整性 | 每条新增注册都有归因状态（含 UNKNOWN_BATCH_COHORT 排除后），空值率 = 0 |
| A2 | 可复算 | 同批快照重跑，AttributionProposal 完全一致 |
| A3 | 幂等 | 重复摄入不产生重复注册/重复归因 |
| A4 | owner 可读 | owner 视图中内部 ID / 英文 slug / "HIGH/直连归因"字样出现次数 = 0 |
| A5 | 时效 | 每日战报在 T+1 上午可用 |
| A6 | 决策闭环 | 实验达阈值即进 inbox，owner 一次点击完成 继续/调整/停止/新增 |
| A7 | 无越界 | 审计显示米核写操作 = 0，原始 PII 落库 = 0，Final Attribution 静默改写 = 0 |

### 10.3 业务阶段（最小可信证据）

> **首个 4 周窗口内，至少出现 1 条"已归因注册关联 ≥ 1"的内容（CLICK_ASSOCIATED 或 EXPERIMENT_ASSOCIATED）。**

达不到，说明不是归因系统的问题，而是**内容或渠道选错了**，应回到 §6 重选主力流量场，而不是继续加功能。

（对照基线：可用于当前实验基线的非批量历史注册观测 3 人 / 近 30 天、付费 0 人、佣金 ¥0；9 个批量账户单独审计。）

---

## 附：本设计明确回答的两个业务问题

1. **"哪些内容真正带来了 AI觅 注册？"**
   → 靠 CLICK_ASSOCIATED（需 R7 建跳转层）或 EXPERIMENT_ASSOCIATED（零成本立即可用）回答；答不出来的部分诚实地进"未归因"桶。**G3 证明前，系统绝不声称"直连/高置信注册归因"**。

2. **"下一轮应该继续生产什么内容？"**
   → 靠主题榜 / 钩子榜 + 预设决策阈值给出建议；样本不足时显式标注"仅供参考"，由 owner 拍板，系统不替 owner 下结论。**v2：建议引擎推后至真实数据轮次之后，避免模型凭常识臆测。**

---

## 11. 评审硬合同补遗（owner 接受 Codex 4 项 blocking findings，2026-08-06）

> **本章节是 Codex 架构评审的硬合同锚点。** owner 已接受 Codex 对实现 PR #136（head `edb157668f50b13985dbb5e119ffc5f01da57a87`）提出的 4 项 blocking findings + 1 项 process-gate finding。PR #136 保持 **BLOCKED**，直到本章节所定义的合同在**设计（本 PR #135）与实现（PR #136）中同时落地**。本章节属于设计门（design gate）的一部分，必须在 PR #135 合入前完成。
>
> **Process 透明记录（process-gate finding）**：实现 PR #136 在 PR #135 完成 owner 设计门之前就已开启；Codex 架构评审正确抓到了"设计门未过即实现"的问题。实现保持 blocked，owner 现要求 **design-first closure**（先冻结设计，再修实现）。不事后声称 PR #136 已通过设计门。

### 11.1 D1 — G3 证明前禁止持久化直接注册归因

**硬要求**：在 G3（见 §7 埋点缺口）证明米核注册链保存 `campaign/ref` 或存在身份保持（identity-preserving）的注册桥之前：

- 系统中**不得存在**可被普通写入方持久化的以下任何值：
  - `VERIFIED_DIRECT`
  - `DIRECT`
  - `HIGH`-置信度 person-level 注册归因
- 当前允许的**注册归因结果**（落库语义）**仅限于**：
  - `EXPERIMENT_ASSOCIATED`
  - `AMBIGUOUS`
  - `UNATTRIBUTED`
  - （`CLICK_ASSOCIATED` 仅表示"点击层关联证据"——证明某 campaign 收到了点击，属于点击事实层，**不构成** person-level 注册归因结果，不得在 G3 前作为注册归因持久化）
- **点击证据的能力边界**：点击证据只能证明 *"该 campaign 收到了一次点击"*；**仅凭点击证据本身不得证明** *"这一个确切注册来自那次点击"*。person-level 注册归属在 G3 证明前不可得。
- 归因词汇表（AttributionLevel 枚举）在 G3 证明前**只定义**上述允许值；不得预定义 `VERIFIED_DIRECT` / `DIRECT` / `HIGH` "以备将来用"。未来若 G3 成功，须通过**单独评审的合同 / 版本变更**引入新归因层级。
- 与 §2.2 一致：§2.2 层级表中 `VERIFIED/DIRECT` 行标注为"G3 证明后才允许出现"，实现中该值**不得进入持久化词汇表**。

### 11.2 D2 — Final Attribution 数据库边界不变量

设计**要求在数据库边界（DDL + 约束 + 事务）强制**以下不变量，**不可弱化为仅应用层校验**：

1. 一条 `FinalAttribution` proposal 必须属于**同一个**被 finalize 的 `RegistrationObservation`；
2. 一个注册**至多有一个**当前 `ACCEPTED` 的 final attribution；
3. 历史前驱记录**保留**（不可物理删除）；
4. 替换只能用 `SUPERSEDE` 表示，**绝不**静默覆盖；
5. 替换必须**原子**；
6. 不允许"两个 accepted head"的无效瞬态——即便在单个原子事务之外也不得短暂存在；
7. `mismatch`（proposal 与 registration 不匹配）、`duplicate-head`（两个 accepted head）、`concurrent replacement`（并发替换）**必须被测试覆盖**。

**意图 DB 形状（实现指导，非强制）**：
- proposal / registration 组合身份强制（外键 + 一致性约束）；
- `accepted` head 唯一 / 部分唯一约束（partial unique：仅当前 accepted 唯一）；
- 原子 supersede + successor 创建在单个事务内完成。

### 11.3 D3 — Staging create-all 守卫（精确拓扑 / 路径，禁止子串识别）

**移除**基于子串的 staging 识别逻辑。以下逻辑一律**禁止**：

- URL 含 `"staging"`
- 文件名含 `"staging"`
- 主机名含 `"staging"`
- 查询串含 `"staging"`
- 用户名 / 密码含 `"staging"`
- 任何位置出现 `"human_uat.db"`

**要求的守卫**：schema helper 必须**解析并归一化 DB URL**，仅允许**确切批准的 staging 拓扑 / 路径**。

**当前确切批准的 Pilot staging 数据库位置**（来源：`uat_ool_v0/human_env.py`）：

- 绝对路径：`D:/workbuddy/2026-07-16-02-53-22/uat_ool_v0/staging/human_uat.db`
- scheme：`sqlite`
- 解析方式：`UAT_DB_PATH = HUMAN_ROOT / "staging" / "human_uat.db"`，其中 `HUMAN_ROOT = Path(__file__).resolve().parent`（即 `uat_ool_v0/` 目录）

**对 SQLite 的守卫步骤**：
- 解析 URL 得到实际文件系统路径；
- 与**归一化 / 绝对化**后的批准路径做**精确字符串比较**；
- **拒绝**名称相像的 lookalike，例如 `prod-staging-backup.db`；
- **拒绝**非批准的 scheme / host / path；
- 拒绝时**以非零退出码退出**（fail closed）；
- 普通运行时 import / startup **不得**触发 `create_all()`。

### 11.4 D4 — 必需的 CI 集合（Pilot-2 合同测试纳入正常 pytest 门）

PILOT-2 合同测试是**必需的正常 pytest 门**的一部分：

- 仓库标准命令 `python -m pytest -q` **必须收集并执行** Pilot-2 模型合同测试；
- **不依赖**特殊手工调用的测试路径；
- **优先**将 Pilot-2 测试移入仓库已有的、被收集的 `tests/` 树，除非有强理由改动仓库级 pytest 配置；
- 设计**显式声明**：若新的 Pilot-2 合同测试未被收集，则一次"绿色"的正常 CI 运行**无效**（视为失败）。
