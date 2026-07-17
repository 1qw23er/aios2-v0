from __future__ import annotations

from html import escape
from typing import Any

from sqlmodel import Session, select

from aios.campaign import V1_TASKS
from aios.models import Agent, RoutingMode
from aios.services import get_board

STATUS_LABELS: dict[str, str] = {
    "backlog": "待开始",
    "ready": "已就绪",
    "running": "进行中",
    "waiting_external": "等待外部",
    "review": "审阅中",
    "approved": "已批准",
    "rejected": "已退回",
    "done": "已完成",
    "failed": "失败",
}

PROJECT_STATUS_LABELS: dict[str, str] = {
    "backlog": "待启动",
    "active": "进行中",
    "paused": "已暂停",
    "completed": "已完成",
    "archived": "已归档",
}


def build_board_view(session: Session, project_id: str) -> dict[str, Any]:
    """Enrich ``get_board`` output with the V1 playbook's human-readable metadata.

    Reuses the exact ``get_board`` service used by ``GET /owner/campaigns/{id}``.
    Live status, assignment and the owner-gate flag come from the persisted board
    (``routing_mode == MANUAL`` marks T6 / T8 as owner gates); the department name
    is presentation metadata from ``V1_TASKS``.
    """
    board = get_board(session, project_id)  # raises ServiceError(404) if missing
    project = board["project"]

    agent_name = {agent.id: agent.name for agent in session.exec(select(Agent)).all()}

    board_task_by_id: dict[str, dict[str, Any]] = {}
    for tasks in board["tasks_by_status"].values():
        for task in tasks:
            board_task_by_id[task["id"]] = task
    pending_by_task: dict[str, dict[str, Any]] = {
        a.get("task_id"): a for a in board.get("pending_approvals", []) if a.get("task_id")
    }

    ordered: list[dict[str, Any]] = []
    for task_def in V1_TASKS:
        board_task = next(
            (task for task in board_task_by_id.values() if task["title"] == task_def["title"]),
            None,
        )
        if board_task is None:
            continue
        # The owner-gate is derived from the PERSISTED routing decision
        # (routing_mode == MANUAL), not from the presentation constant in V1_TASKS.
        # T6 / T8 are launched with RoutingMode.MANUAL and never assigned to an agent.
        is_gate = board_task.get("routing_mode") == RoutingMode.MANUAL.value
        department = (
            "需要你来处理（不自动分配）" if is_gate else task_def.get("department") or "—"
        )
        assigned_agent_id = board_task.get("assigned_agent_id")
        assigned = agent_name.get(assigned_agent_id) if assigned_agent_id else None
        status = board_task["status"]
        pending = pending_by_task.get(board_task["id"])
        ordered.append(
            {
                "key": task_def["key"],
                "title": task_def["title"],
                "task_id": board_task["id"],
                "department": department,
                "is_gate": is_gate,
                "pending_approval_id": pending["id"] if pending else None,
                "assigned_agent": assigned,
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "depends_on": list(task_def.get("depends_on", [])),
            }
        )

    done_count = sum(1 for item in ordered if item["status"] in ("done", "approved"))
    current = next(
        (item for item in ordered if item["status"] not in ("done", "approved")), None
    )
    stage = current["title"] if current is not None else "全部任务已完成"

    return {
        "project": project,
        "ordered": ordered,
        "done_count": done_count,
        "total": len(ordered),
        "stage": stage,
    }


def owner_home_html(
    *,
    idem: str,
    error: str | None = None,
    last_campaign_id: str | None = None,
) -> str:
    """Render the launch form. ``idem`` is a hidden, server-generated idempotency key."""
    error_block = ""
    if error:
        error_block = f'<div class="msg msg-error">{escape(error)}</div>'
    last_block = ""
    if last_campaign_id:
        last_block = (
            '<div class="msg msg-info">你上次的 campaign：'
            f'<a href="/owner/board/{escape(last_campaign_id)}">查看看板</a>'
            "（数据已保存在本地数据库中）</div>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>个人 AI 公司 V1 · 控制台</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; }}
  .wrap {{ max-width: 720px; margin: 40px auto; padding: 0 20px; }}
  h1 {{ font-size: 22px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 24px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  label {{ display: block; font-weight: 600; margin: 16px 0 6px; }}
  input, textarea {{ width: 100%; box-sizing: border-box; padding: 10px;
                     border: 1px solid #d0d3d9; border-radius: 6px; font-size: 15px; }}
  textarea {{ min-height: 88px; resize: vertical; }}
  button {{ margin-top: 20px; width: 100%; padding: 12px; font-size: 16px;
           background: #2f6fed; color: #fff; border: 0; border-radius: 6px;
           cursor: pointer; }}
  button:hover {{ background: #2559c9; }}
  .msg {{ padding: 10px 12px; border-radius: 6px; margin-bottom: 12px; font-size: 14px; }}
  .msg-error {{ background: #fdecea; color: #b42318; border: 1px solid #f5c2bd; }}
  .msg-info {{ background: #eef4ff; color: #1d4ed8; border: 1px solid #c9dbff; }}
  .hint {{ color: #6b7280; font-size: 13px; margin-top: 8px; }}
  a {{ color: #2f6fed; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>个人 AI 公司 V1 · 控制台</h1>
  <div class="card">
    {last_block}
    {error_block}
    <form method="post" action="/owner/launch">
      <label for="name">Campaign 名称</label>
      <input id="name" name="name" type="text" required
             placeholder="例如：把失败的 AI 系统重建成可用的小 AI 公司">
      <label for="objective">Campaign 目标</label>
      <textarea id="objective" name="objective" required
                placeholder="这次 campaign 要达成的业务结果。"></textarea>
      <input type="hidden" name="idem" value="{escape(idem)}">
      <button type="submit">启动工作流</button>
    </form>
    <p class="hint">只需填写名称与目标，点击「启动工作流」即可。无需写代码、无需数据库操作。
       如果重复点击，系统会自动识别为同一次提交，不会创建重复 campaign。</p>
  </div>
</div>
</body>
</html>"""


def owner_board_html(view: dict[str, Any]) -> str:
    project = view["project"]
    name = escape(project.get("name", ""))
    objective = escape(project.get("objective", ""))
    project_status = PROJECT_STATUS_LABELS.get(
        project.get("status", ""), escape(str(project.get("status", "")))
    )
    stage = escape(view["stage"])
    progress = f'{view["done_count"]}/{view["total"]}'

    rows: list[str] = []
    for item in view["ordered"]:
        gate_badge = ""
        row_class = "task-row"
        if item["is_gate"]:
            gate_badge = '<span class="badge badge-gate">需你处理 / 审批</span>'
            row_class = "task-row task-gate"
        assigned = escape(item["assigned_agent"]) if item["assigned_agent"] else "—"
        deps = "、".join(item["depends_on"]) if item["depends_on"] else "—"
        rows.append(
            f'<tr class="{row_class}">'
            f'<td><b>{escape(item["key"])}</b><br>{escape(item["title"])}</td>'
            f'<td>{escape(item["department"])}</td>'
            f'<td><span class="badge">{escape(item["status_label"])}</span>{gate_badge}</td>'
            f'<td>{assigned}</td>'
            f'<td>{escape(deps)}</td>'
            f"</tr>"
        )
    rows_html = "\n".join(rows)

    gate_items = [item for item in view["ordered"] if item["is_gate"]]
    gate_cards: list[str] = []
    for item in gate_items:
        tid = escape(item["task_id"])
        ttitle = escape(item["title"])
        tstatus = escape(item["status_label"])
        gate_cards.append(
            f'<div class="gate-card">'
            f'<div class="gate-head"><b>{ttitle}</b> '
            f'<span class="badge badge-gate">需你处理 / 审批</span> '
            f'<span class="badge">{tstatus}</span></div>'
            f'<form method="post" action="/owner/tasks/{tid}/decide" class="gate-form">'
            f'<input type="hidden" name="decision" value="approve">'
            f'<button type="submit" class="btn-approve">批准并继续</button>'
            f"</form>"
            f'<form method="post" action="/owner/tasks/{tid}/decide" class="gate-form">'
            f'<input type="hidden" name="decision" value="reject">'
            f'<input type="text" name="rationale" placeholder="驳回理由（可选）">'
            f'<button type="submit" class="btn-reject">驳回</button>'
            f"</form>"
            f'<form method="post" action="/owner/tasks/{tid}/revision" class="gate-form">'
            f'<input type="text" name="feedback" placeholder="需要修订什么？">'
            f'<button type="submit" class="btn-revise">要求修订</button>'
            f"</form>"
            f"</div>"
        )
    gate_html = (
        '<div class="card"><h2>需要你处理的任务</h2>' + "\n".join(gate_cards) + "</div>"
        if gate_cards
        else ""
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>看板 · {name}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; }}
  .wrap {{ max-width: 980px; margin: 32px auto; padding: 0 20px; }}
  h1 {{ font-size: 22px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 22px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 20px; }}
  .meta {{ color: #6b7280; font-size: 14px; margin: 4px 0; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
         border-radius: 10px; overflow: hidden;
         box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th, td {{ text-align: left; padding: 12px 14px; border-bottom: 1px solid #eef0f3;
           font-size: 14px; vertical-align: top; }}
  th {{ background: #f0f3f8; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
           background: #eef4ff; color: #1d4ed8; font-size: 12px; }}
  .badge-gate {{ background: #fff4e5; color: #b54708; margin-left: 6px; }}
  .task-gate {{ background: #fffaf2; }}
  a {{ color: #2f6fed; text-decoration: none; }}
  .back {{ display: inline-block; margin-bottom: 14px; }}
  .gate-card {{ background: #fffaf2; border: 1px solid #ffe0b3; border-radius: 10px;
              padding: 16px 18px; margin-bottom: 14px; }}
  .gate-head {{ margin-bottom: 10px; }}
  .gate-form {{ display: flex; gap: 8px; align-items: center; margin-top: 8px; }}
  .gate-form input[type="text"] {{ flex: 1; padding: 8px; border: 1px solid #d0d3d9;
              border-radius: 6px; font-size: 14px; }}
  .btn-approve {{ background: #1f9d55; color: #fff; border: 0; border-radius: 6px;
              padding: 8px 16px; font-size: 14px; cursor: pointer; }}
  .btn-reject {{ background: #b42318; color: #fff; border: 0; border-radius: 6px;
              padding: 8px 16px; font-size: 14px; cursor: pointer; }}
  .btn-revise {{ background: #2f6fed; color: #fff; border: 0; border-radius: 6px;
              padding: 8px 16px; font-size: 14px; cursor: pointer; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/owner">← 返回控制台</a>
  <div class="card">
    <h1>{name}</h1>
    <div class="meta">目标：{objective}</div>
    <div class="meta">项目状态：{escape(project_status)}</div>
    <div class="meta">当前阶段：{stage}</div>
    <div class="meta">进度：{progress} 个任务已完成</div>
  </div>
  <table>
    <thead>
      <tr><th>任务</th><th>负责部门</th><th>状态</th><th>负责 agent</th><th>依赖</th></tr>
    </thead>
    <tbody>
    {rows_html}
    </tbody>
  </table>
  {gate_html}
  <p class="meta">标注「需你处理 / 审批」的任务（T6 人工审阅、T8 发布闸门）不会自动分配，
     需要你在系统中做出明确决定后才会继续。</p>
</div>
</body>
</html>"""


def owner_error_html(
    *,
    message: str,
    idem: str | None = None,
    last_campaign_id: str | None = None,
) -> str:
    """Readable page for an unexpected server error (HTTP 500). No stack traces.

    Preserves the idempotency key in a hidden retry field so a retry stays part of
    the same submission lifecycle (one key for the full form lifecycle).
    """
    error_block = f'<div class="msg msg-error">{escape(message)}</div>'
    idem_field = f'<input type="hidden" name="idem" value="{escape(idem)}">' if idem else ""
    last_block = ""
    if last_campaign_id:
        last_block = (
            '<div class="msg msg-info">你上次的 campaign：'
            f'<a href="/owner/board/{escape(last_campaign_id)}">查看看板</a></div>'
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>提交出错</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; }}
  .wrap {{ max-width: 640px; margin: 60px auto; padding: 0 20px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 28px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  h1 {{ font-size: 20px; }}
  .msg {{ padding: 10px 12px; border-radius: 6px; margin-bottom: 12px; font-size: 14px; }}
  .msg-error {{ background: #fdecea; color: #b42318; border: 1px solid #f5c2bd; }}
  .msg-info {{ background: #eef4ff; color: #1d4ed8; border: 1px solid #c9dbff; }}
  button {{ margin-top: 16px; padding: 12px 18px; font-size: 15px;
           background: #2f6fed; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }}
  a {{ color: #2f6fed; text-decoration: none; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>提交时出现问题</h1>
    {last_block}
    {error_block}
    <p>系统在处理这次提交时遇到意外错误。你可以重试一次；</p>
    <p>你的提交标识保持不变，重复提交不会创建重复 campaign。</p>
    <form method="post" action="/owner/launch">
      {idem_field}
      <button type="submit">重试提交</button>
    </form>
    <p><a href="/owner">返回控制台</a></p>
  </div>
</div>
</body>
</html>"""


def owner_not_found_html(project_id: str) -> str:
    pid = escape(project_id)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>未找到 campaign</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; }}
  .wrap {{ max-width: 640px; margin: 60px auto; padding: 0 20px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 28px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  h1 {{ font-size: 20px; }}
  a {{ color: #2f6fed; text-decoration: none; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>未找到该 campaign</h1>
    <p>系统里没有 id 为 <code>{pid}</code> 的 campaign。</p>
    <p>请确认链接是否正确，或返回 <a href="/owner">控制台</a> 重新启动一个工作流。</p>
  </div>
</div>
</body>
</html>"""
