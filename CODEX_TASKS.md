# Codex 开发任务

## P0 — 可运行骨架

1. 初始化 FastAPI、SQLModel、Alembic、pytest。
2. 实现 Project/Task/Event/Artifact/Agent/Approval 模型与迁移。
3. 实现 REST API：创建项目、创建任务、查询看板、提交审批。
4. 实现 SQLite transactional outbox Event Bus。
5. 实现 Orchestrator 规则：`task.completed -> create dependent task`。
6. 实现 ExternalWorkstationAdapter：导出任务包、导入结果包、schema 校验。
7. 实现审计日志和幂等键。
8. 写一个端到端测试：研究（external）→ 策划（mock API）→ 写作（external）→ 审批。

## P1 — 可用控制台

1. 简单 Web 控制台：项目、任务、事件、审批、成本。
2. Agent Registry 页面与能力匹配。
3. 文件 Artifact 上传/下载及 checksum。
4. 失败重试、超时、dead-letter queue。

## P2 — 自动 Agent

1. 接入 OpenAI 模型 Adapter。
2. 接入 Codex CLI/OpenClaw Adapter（仅允许沙箱目录）。
3. 用 LangGraph 表达内容工作流，持久化 checkpoint。
4. 添加 evaluator：格式、事实、品牌、成本四类评分。

## 验收条件

- `pytest` 全绿。
- API 文档可访问。
- 重启服务后任务和事件不丢失。
- 重复提交同一结果包不会重复触发后续任务。
- 所有 L4 动作无审批时不能执行。
- README 提供 Windows 与 Linux 启动方式。
