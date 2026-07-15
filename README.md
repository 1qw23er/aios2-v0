# AIOS V0 — Codex Handoff

目标：让多个异构 AI/Agent 围绕同一项目共享状态、领取任务、提交成果，并由系统自动触发下一步；人只处理审批、异常和无 API 的外部工位。

## V0 边界

- 支持项目、任务、事件、上下文、成果物、审批。
- 支持 API Agent 自动执行。
- 支持闭源 Agent 通过“任务包导出 / 结果回传”半自动接入。
- 不承诺闭源 Agent 的主动调用、后台运行或无授权浏览器自动化。
- 默认所有外部写操作需要审批。

## 技术栈

- Python 3.12+
- FastAPI
- SQLite（V0）；后续可切 PostgreSQL
- SQLModel / Pydantic
- Alembic（应用启动时自动执行 `upgrade head`）
- 文件系统 artifact store（V0）

## 安装与启动

Linux/macOS：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
uvicorn aios.api.app:app --reload
```

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn aios.api.app:app --reload
```

应用在接受请求前自动执行 Alembic 迁移；迁移失败时启动失败。默认数据库为 `data/aios.db`。可通过 `AIOS_DATABASE_URL` 覆盖，例如 `sqlite:///data/test.db`。

打开 `http://127.0.0.1:8000/docs`，健康检查为 `GET /health`。

## 持久化模型

SQLite 是 V0 的状态事实源，聊天记录不作为状态。初始迁移建立 `Project`、`Task`、`Event`、`Artifact`、`Agent` 和 `Approval`。SQLite 连接默认启用外键约束。

## P0 REST API

- `POST /projects`：创建项目；ID、状态和时间戳由服务端生成。
- `POST /tasks`：创建任务；校验项目、Agent、依赖存在且依赖属于同一项目。
- `GET /projects/{project_id}/tasks`：查询项目任务。
- `GET /projects/{project_id}/board`：按全部任务状态分组，并返回待审批项。
- `POST /approvals`：提交审批请求。

L4 发布、发信、删除、付款和承诺类动作只会生成 `pending` 审批记录，本 API 不执行外部动作。

## Transactional outbox 与幂等

项目、任务和审批创建会在同一 SQLite 事务中写入业务记录与 Event；任一写入失败时整体回滚。写接口接受可选的 `Idempotency-Key` 请求头：

- 相同 key 和相同请求返回第一次创建的资源，不重复写入 Event。
- 相同 key 但不同请求返回 HTTP 409。
- 未提供 key 时服务端生成唯一 key。

`SQLiteEventBus` 按创建时间读取 pending Event，可标记 processed；失败会增加尝试次数、保存错误并保持 pending 供后续重试。P0-4 不消费事件，也不创建依赖任务；`task.completed -> create dependent task` 属于 P0-5。

## 验证

```bash
python -m pytest -q
python -m ruff check src tests alembic
```

## Codex 首轮任务

请先阅读 `docs/`、`CODEX_TASKS.md`，然后按 P0 顺序实现，保持测试先行。
## Orchestrator

`complete_task` 将任务置为 `done` 并在同一事务写入 `task.completed`。Orchestrator 只处理该类 pending Event；当下游任务的全部 `depends_on` 均为 `done` 时，将其从 `backlog` 原子更新为 `ready` 并写入唯一的 `task.ready` Event。重复消费完成事件不会重复激活任务或产生重复事件。