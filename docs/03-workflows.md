# V0 工作流

## 工作流 A：内容生产

1. CEO 创建项目与内容目标。
2. Orchestrator 创建研究任务，分配 WorkBuddy（external）。
3. 系统导出 `task_packet.json + context.md` 到 outbox。
4. 人工将任务包交给 WorkBuddy；结果按模板回传 inbox。
5. Importer 校验结果，保存 Artifact，发出 `task.completed`。
6. 规则自动创建策划任务，分配 ChatGPT/API Agent。
7. 策划完成后自动创建写作任务，分配 Hermes（external）。
8. Hermes 回传初稿；系统创建事实核查任务。
9. 核查通过后进入 CEO 审批。
10. CEO 批准后才允许发布任务进入队列。

## 工作流 B：AI 客服部署

诊断 → FAQ 构建 → 风险审查 → 沙箱测试 → 客户验收 → 上线审批。

## 外部任务包格式

```json
{
  "task_id": "tsk_123",
  "project": {"id": "prj_001", "objective": "..."},
  "role": "researcher",
  "instructions": "...",
  "inputs": [{"type": "context", "uri": "..."}],
  "acceptance_criteria": ["..."],
  "output_schema": {
    "summary": "string",
    "claims": [{"claim": "string", "source": "string", "confidence": 0.0}],
    "artifacts": []
  }
}
```

## 回传原则

- 必须包含 task_id。
- 必须符合 output_schema。
- 不符合时进入 rejected，并生成修订任务。
- 所有未经验证的事实标注为 claim，不直接写入 Context Store。
