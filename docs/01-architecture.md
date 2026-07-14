# AIOS V0 架构

```mermaid
flowchart TB
    CEO[人类 CEO\n目标/审批/异常处理]
    UI[控制台/API]
    ORCH[Orchestrator\n规则 + 状态机]
    BUS[Event Bus\nSQLite Outbox]
    DB[(State Store\nProject/Task/Event/Approval)]
    CTX[(Context Store\n事实/决策/SOP)]
    ART[(Artifact Store\nMarkdown/JSON/文件)]
    REG[Agent Registry\n能力/成本/权限/在线状态]

    APIA[API Agent Adapter\nGPT/可调用模型]
    CLI[CLI Agent Adapter\nCodex/OpenClaw]
    EXT[External Workstation Adapter\nHermes/WorkBuddy/Marvis/扣子]

    CEO --> UI
    UI --> ORCH
    ORCH <--> DB
    ORCH <--> CTX
    ORCH <--> REG
    ORCH --> BUS
    BUS --> APIA
    BUS --> CLI
    BUS --> EXT
    APIA --> ART
    CLI --> ART
    EXT -->|导出任务包| HUMANRELAY[最小人工转交]
    HUMANRELAY -->|回传结果包| ART
    ART --> ORCH
    ORCH -->|需审批| CEO
```

## 五个中心

1. **Project Center**：目标、阶段、预算、风险。
2. **Task Center**：任务依赖、负责人、验收标准、截止时间。
3. **Context Center**：经过确认的事实、决策、品牌规则、客户资料。
4. **Event Center**：任务完成、失败、审批、上下文更新等事件。
5. **Agent Registry**：每个 AI 的能力、接入方式、成本和权限。

## 关键设计原则

- 单一事实源：状态只在 AIOS 中有效，聊天记录不是事实库。
- 事件驱动：完成任务产生事件，由规则生成下一任务。
- 适配器隔离：每个 Agent 的接入差异封装在 Adapter。
- 证据优先：成果物必须附来源、假设和置信度。
- 人类闸门：外部发布、删除、付款、客户承诺必须审批。
- 幂等：相同事件重复消费不得重复创建任务或外部操作。
