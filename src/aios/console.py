from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import quote

from sqlmodel import Session, or_, select

from aios.campaign import V1_TASKS
from aios.distribution import (
    get_package_artifact,
    resolve_package_task,
    resolve_publish_gate_task,
)
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
    Artifact,
    ArtifactReviewStatus,
    DelegationMode,
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    RoutingMode,
)
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

    # V1-I4 (#35): resolve the packaging task (T7) and publish-gate task (T8) by the
    # dependency graph, and the single distribution package artifact + its readiness.
    package_task = resolve_package_task(session, project_id)
    gate_task = resolve_publish_gate_task(session, project_id)
    package_task_id = package_task.id if package_task else None
    gate_task_id = gate_task.id if gate_task else None
    package_artifact = get_package_artifact(session, project_id)
    package_exists = package_artifact is not None
    package_ready = (
        package_artifact is not None
        and package_artifact.review_status == ArtifactReviewStatus.APPROVED
    )

    # V1-I5 (#38): knowledge-preservation support. The T9 (knowledge_capture) task
    # can be used to preserve a candidate from any APPROVED source artifact in this
    # project; draft candidates in this project surface in a review area.
    knowledge_task = next(
        (d for d in V1_TASKS if d.get("key") == "T9"), None
    )
    knowledge_task_key = knowledge_task["key"] if knowledge_task else "T9"
    approved_artifacts = list(
        session.exec(
            select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.review_status == ArtifactReviewStatus.APPROVED,
            )
        ).all()
    )
    pending_candidates = list(
        session.exec(
            select(KnowledgeCandidate).where(
                KnowledgeCandidate.project_id == project_id,
                KnowledgeCandidate.status == KnowledgeCandidateStatus.DRAFT,
            )
        ).all()
    )

    # Current knowledge series heads in this project's effective scope
    # (project-local + company-wide), so the owner can SEE the latest version of
    # each series when reviewing (and never has to track versions manually).
    facts = session.exec(
        select(KnowledgeFact).where(
            or_(
                KnowledgeFact.project_id == project_id,
                KnowledgeFact.project_id.is_(None),
            )
        )
    ).all()
    _seen: dict[tuple[str, str | None], int] = {}
    for f in facts:
        if f.status != KnowledgeFactStatus.APPROVED:
            continue
        key = (f.series_id, f.project_id)
        if _seen.get(key) is None or f.version > _seen[key]:
            _seen[key] = f.version
    series_heads = [
        {"series_id": sid, "version": ver, "scope": "company" if pid is None else "project"}
        for (sid, pid), ver in _seen.items()
    ]
    series_heads.sort(key=lambda s: (s["scope"], s["series_id"]))

    # Latest artifact summary per task, so the owner can see execution output.
    artifact_summary_by_task: dict[str, str] = {}
    for artifact in session.exec(select(Artifact).where(Artifact.task_id.isnot(None))).all():
        if artifact.task_id and artifact.metadata_json.get("summary"):
            # Keep the first (earliest) summary per task; iteration is by id order.
            artifact_summary_by_task.setdefault(
                artifact.task_id, str(artifact.metadata_json["summary"])
            )

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
        task_id = board_task["id"]
        is_package = task_id == package_task_id
        is_publish_gate = task_id == gate_task_id
        is_knowledge = task_def.get("key") == knowledge_task_key
        # The packaging task (T7) is assembled deterministically, NOT run via the LLM
        # execute path -- so it is executable only through "生成分发包".
        can_package = is_package and status == "ready" and not package_exists
        # The knowledge-capture task (T9) offers "沉淀知识" when a source exists.
        can_preserve = is_knowledge and bool(approved_artifacts)
        # The publish gate (T8) is actionable once the package + its L3 approval exist.
        publish_actionable = (
            is_publish_gate and package_exists and pending is not None and status == "ready"
        )
        ordered.append(
            {
                "key": task_def["key"],
                "title": task_def["title"],
                "task_id": task_id,
                "department": department,
                "is_gate": is_gate,
                "is_package": is_package,
                "is_publish_gate": is_publish_gate,
                "is_knowledge": is_knowledge,
                "can_package": can_package,
                "can_preserve": can_preserve,
                "package_exists": is_package and package_exists,
                "package_ready": is_package and package_ready,
                "publish_actionable": publish_actionable,
                "pending_approval_id": pending["id"] if pending else None,
                "assigned_agent": assigned,
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "depends_on": list(task_def.get("depends_on", [])),
                "artifact_summary": artifact_summary_by_task.get(task_id),
                "executable": (
                    status == "ready"
                    and not is_gate
                    and not is_package
                    and bool(assigned_agent_id)
                ),
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
        "pending_candidates": [
            {
                "id": c.id,
                "statement": c.statement,
                "artifact_id": c.artifact_id,
                "project_id": c.project_id,
                "source_project_id": c.source_project_id,
                "scope": "company" if c.project_id is None else "project",
            }
            for c in pending_candidates
        ],
        "series_heads": series_heads,
        "approved_artifacts": [
            {"id": a.id, "summary": (a.metadata_json or {}).get("summary", "")}
            for a in approved_artifacts
        ],
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
        extra: list[str] = []
        if item.get("artifact_summary"):
            extra.append(
                f'<div class="artifact-summary">执行产物：{escape(item["artifact_summary"])}</div>'
            )
        if item.get("executable"):
            tid = escape(item["task_id"])
            extra.append(
                f'<form method="post" action="/owner/tasks/{tid}/execute" class="run-form">'
                f'<button type="submit" class="btn-run">运行部门任务</button></form>'
            )
        if item.get("can_package"):
            tid = escape(item["task_id"])
            extra.append(
                f'<form method="post" action="/owner/tasks/{tid}/package" class="run-form">'
                f'<button type="submit" class="btn-package">生成分发包</button></form>'
            )
        elif item.get("package_ready"):
            extra.append('<div class="artifact-summary">分发包已就绪，可复制内容手动发布。</div>')
        elif item.get("package_exists"):
            extra.append(
                '<div class="artifact-summary">分发包已生成，等待发布审批（见下方）。</div>'
            )
        if item.get("can_preserve"):
            tid = escape(item["task_id"])
            options = "".join(
                f'<option value="{escape(a["id"])}">{escape(a["summary"] or a["id"])}</option>'
                for a in view.get("approved_artifacts", [])
            )
            extra.append(
                f'<form method="post" action="/owner/tasks/{tid}/preserve" class="run-form">'
                f'<select name="artifact_id" class="preserve-select">{options}</select>'
                f'<input type="text" name="statement" class="preserve-input" '
                f'placeholder="提炼这条已批准产出的可复用知识">'
                f'<fieldset class="scope-fieldset">'
                f'<legend>知识范围</legend>'
                f'<label class="scope-opt">'
                f'<input type="radio" name="scope" value="project" checked> '
                f"本项目（仅本 campaign 复用）</label>"
                f'<label class="scope-opt">'
                f'<input type="radio" name="scope" value="company"> '
                f"全公司（所有 campaign 可复用）</label>"
                f'<p class="scope-warn">⚠ 选择「全公司」会把来源 campaign 的知识提升为公司级，'
                f"所有 campaign 都能自动复用。请确认你确实希望开放给全公司。</p>"
                f"</fieldset>"
                f'<button type="submit" class="btn-preserve">沉淀知识</button>'
                f"</form>"
            )
        extra_html = "\n".join(extra)
        rows.append(
            f'<tr class="{row_class}">'
            f'<td><b>{escape(item["key"])}</b><br>{escape(item["title"])}'
            f"{extra_html}</td>"
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
        head = (
            f'<div class="gate-head"><b>{ttitle}</b> '
            f'<span class="badge badge-gate">需你处理 / 审批</span> '
            f'<span class="badge">{tstatus}</span></div>'
        )
        if item.get("is_publish_gate"):
            # V1-I4 (#35): the publish gate is a single approve/reject on the L3
            # publish approval; approving marks the distribution package ready. The
            # owner then copies the content and posts by hand -- nothing auto-posts.
            if item.get("publish_actionable"):
                body = (
                    f'<form method="post" action="/owner/tasks/{tid}/publish" class="gate-form">'
                    f'<input type="hidden" name="decision" value="approve">'
                    f'<button type="submit" class="btn-approve">'
                    f"批准发布闸门（标记分发包就绪）</button>"
                    f"</form>"
                    f'<form method="post" action="/owner/tasks/{tid}/publish" class="gate-form">'
                    f'<input type="hidden" name="decision" value="reject">'
                    f'<input type="text" name="rationale" placeholder="驳回理由（可选）">'
                    f'<button type="submit" class="btn-reject">驳回</button>'
                    f"</form>"
                    '<div class="meta">批准后系统只标记分发包「就绪」，'
                    "由你复制内容手动发布，系统不会自动发到任何平台。</div>"
                )
            else:
                body = (
                    '<div class="meta">请先在上方任务 T7 点击「生成分发包」，'
                    "生成后这里会出现发布审批按钮。</div>"
                )
            gate_cards.append(f'<div class="gate-card">{head}{body}</div>')
            continue
        gate_cards.append(
            f'<div class="gate-card">'
            f"{head}"
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

    # V1-I5 (#38): review area for knowledge candidates awaiting the owner's decision.
    knowledge_cards: list[str] = []
    series_options = "".join(
        f'<option value="{escape(s["series_id"])}">'
        f'{escape(s["series_id"])}（当前 v{s["version"]}，{escape(s["scope"])}）</option>'
        for s in view.get("series_heads", [])
    )
    for cand in view.get("pending_candidates", []):
        cid = escape(cand["id"])
        stmt = escape(cand["statement"])
        scope_label = "全公司" if cand["scope"] == "company" else "本项目"
        knowledge_cards.append(
            f'<div class="gate-card">'
            f'<div class="gate-head"><b>待审阅知识</b></div>'
            f'<div class="meta">陈述：{stmt}</div>'
            f'<div class="meta">范围（创建时锁定，评审不可更改）：<b>{scope_label}</b></div>'
            f'<form method="post" action="/owner/knowledge/{cid}/review" class="gate-form">'
            f'<input type="hidden" name="decision" value="approve">'
            f'<input type="text" name="series_id" class="series-input" list="series-heads" '
            f'placeholder="系列（如 positioning，留空=新建系列）">'
            f'<input type="hidden" name="version" value="">'
            f'<input type="text" name="rationale" placeholder="审阅理由" required>'
            f'<button type="submit" class="btn-approve">批准为知识事实</button>'
            f'<p class="hint">版本号由系统自动分配：新系列为 v1，选择已有系列则续接其最新版本。</p>'
            f"</form>"
            f'<form method="post" action="/owner/knowledge/{cid}/review" class="gate-form">'
            f'<input type="hidden" name="decision" value="reject">'
            f'<input type="text" name="rationale" placeholder="驳回理由" required>'
            f'<button type="submit" class="btn-reject">驳回</button>'
            f"</form>"
            f"</div>"
        )
    series_datalist = (
        f'<datalist id="series-heads">{"".join(series_options)}</datalist>'
        if series_options
        else ""
    )
    knowledge_html = (
        '<div class="card"><h2>待审阅知识</h2>' + "\n".join(knowledge_cards) + "</div>"
        if knowledge_cards
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
  .artifact-summary {{ margin-top: 8px; padding: 8px 10px; background: #f3f8f4;
              border: 1px solid #cfe9d6; border-radius: 6px; font-size: 13px;
              color: #1f6b3a; }}
  .run-form {{ margin-top: 8px; }}
  .btn-run {{ background: #6d28d9; color: #fff; border: 0; border-radius: 6px;
              padding: 6px 14px; font-size: 13px; cursor: pointer; }}
  .btn-package {{ background: #b54708; color: #fff; border: 0; border-radius: 6px;
              padding: 6px 14px; font-size: 13px; cursor: pointer; }}
  .btn-preserve {{ background: #0e7490; color: #fff; border: 0; border-radius: 6px;
              padding: 6px 14px; font-size: 13px; cursor: pointer; }}
  .preserve-select {{ padding: 6px 8px; border: 1px solid #d0d3d9; border-radius: 6px;
              font-size: 13px; max-width: 220px; }}
  .preserve-input {{ flex: 1; padding: 6px 8px; border: 1px solid #d0d3d9; border-radius: 6px;
              font-size: 13px; min-width: 180px; }}
  .scope-fieldset {{ border: 1px solid #e3e6ea; border-radius: 8px; margin: 8px 0 4px;
              padding: 8px 10px; }}
  .scope-fieldset legend {{ font-size: 12px; color: #6b7280; padding: 0 4px; }}
  .scope-opt {{ display: block; font-size: 13px; margin: 4px 0; font-weight: 400; }}
  .scope-warn {{ color: #b45309; background: #fffbeb; border: 1px solid #fde68a;
              border-radius: 6px; padding: 6px 8px; font-size: 12px; margin: 6px 0 0; }}
  .series-input {{ flex: 1; padding: 8px; border: 1px solid #d0d3d9; border-radius: 6px;
              font-size: 13px; }}
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
  {knowledge_html}
  {series_datalist}
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


def owner_measurement_html(report: dict[str, Any]) -> str:
    """Read-only V1-I6 measurement report (Issue #40). No writes, no inputs."""
    generated = escape(str(report.get("generated_at", "")))
    total = report.get("total_campaigns", 0)
    comp_rate = round(report.get("campaign_completion_rate", 0.0) * 100, 1)
    human = report.get("total_human_interventions", 0)
    avg_prod = report.get("avg_content_production_seconds")
    avg_prod = f"{avg_prod:.0f}s" if avg_prod is not None else "—"
    revisions = report.get("total_revisions", 0)
    pub_rate = round(report.get("publishable_rate", 0.0) * 100, 1)
    reuse = report.get("knowledge_reuse_campaigns", 0)
    dev = report.get("developer_assisted_failures", 0)
    notes = "".join(f"<li>{escape(n)}</li>" for n in report.get("notes", []))

    campaigns = report.get("campaigns", [])
    cards: list[str] = []
    for c in campaigns:
        name = escape(c.get("name", ""))
        objective = escape(c.get("objective", ""))
        status = escape(c.get("status", ""))
        ts = c.get("task_statuses", {})
        task_rows = "".join(
            f"<tr><td>{escape(k)}</td><td>{escape(ts.get(k, '?'))}</td></tr>"
            for k in (t["key"] for t in V1_TASKS)
        )
        arts = "".join(
            f"<li>[{escape(a.get('task_key',''))}] {escape(a.get('type',''))}: "
            f"{escape(str(a.get('summary','')))} "
            f"({escape(a.get('review_status',''))})</li>"
            for a in c.get("artifacts", [])
        ) or "<li>无</li>"
        rating = escape(str(c.get("quality_rating") or "—（owner 评分待填）"))
        completion_pct = round(c.get("completion_rate", 0.0) * 100, 1)
        metrics_block = "\n".join([
            "<ul class=\"metrics\">",
            f"<li>成功 Agent 执行：{c.get('successful_executions',0)} · "
            f"失败：{c.get('execution_failures',0)} · 重试：{c.get('retries',0)}</li>",
            f"<li>owner 批准：{c.get('owner_approvals',0)} · "
            f"驳回：{c.get('owner_rejections',0)} · 修订：{c.get('owner_revisions',0)} · "
            f"控制台外干预：{c.get('manual_interventions',0)}</li>",
            f"<li>分发包可发布：{'是' if c.get('publish_ready_package') else '否'} · "
            f"内容生产时长：{c.get('content_production_seconds') or '—'}s · "
            f"owner 操作时长：{round(c.get('owner_operating_seconds',0.0))}s</li>",
            f"<li>沉淀候选：{c.get('knowledge_candidates',0)} · "
            f"批准事实：{c.get('approved_knowledge_facts',0)} · "
            f"公司级事实：{c.get('company_scoped_facts',0)} · "
            f"复用公司知识：{'是' if c.get('reused_company_knowledge') else '否'}</li>",
            f"<li>owner 质量评级：{rating}</li>",
            "</ul>",
        ])
        cards.append(f"""
        <div class="card">
          <h2>{name}</h2>
          <div class="meta">目标：{objective}</div>
          <div class="meta">状态：{status} · 完成率 {completion_pct}%</div>
          <table>
            <tr><th>任务</th><th>状态</th></tr>
            {task_rows}
          </table>
          {metrics_block}
          <details><summary>产物清单</summary><ul>{arts}</ul></details>
        </div>""")

    cards_html = "\n".join(cards) or '<div class="card">尚无 campaign 数据。</div>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V1 测量报告 · AIOS</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; }}
  .wrap {{ max-width: 980px; margin: 32px auto; padding: 0 20px; }}
  h1 {{ font-size: 22px; }}
  h2 {{ font-size: 18px; margin: 0 0 8px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 22px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 20px; }}
  .meta {{ color: #6b7280; font-size: 14px; margin: 4px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #eef0f3;
           font-size: 13px; }}
  th {{ background: #f0f3f8; }}
  .metrics {{ margin: 10px 0; padding-left: 18px; font-size: 13px; color: #374151; }}
  .metrics li {{ margin: 3px 0; }}
  .back {{ display: inline-block; margin-bottom: 14px; color: #2f6fed; text-decoration: none; }}
  ul {{ font-size: 13px; }}
  details {{ margin-top: 10px; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/owner">&larr; 返回控制台</a>
  <h1>V1 测量报告（Issue #40）</h1>
  <div class="meta">生成时间：{generated} · 共 {total} 个 campaign</div>
  <div class="card">
    <h2>聚合指标（Epic 9 项）</h2>
    <ul class="metrics">
      <li>campaign 完成率：{comp_rate}%</li>
      <li>人工干预总次数：{human} · 修订总次数：{revisions}</li>
      <li>平均内容生产时长：{avg_prod}</li>
      <li>可发布产出率：{pub_rate}%</li>
      <li>复用公司级知识的 campaign 数：{reuse}</li>
      <li>需开发者协助的失败 campaign 数：{dev}</li>
    </ul>
    <details><summary>系统未捕获的指标（需 owner 手动填）</summary>
      <ul>{notes}</ul></details>
  </div>
  {cards_html}
</div>
</body>
</html>"""


def owner_agents_html(agents: list[Agent], *, error: str | None = None) -> str:
    """Render the agent registry: a list of registered agents + a registration form.

    Mirrors ``owner_home_html`` styling (server-rendered, no separate frontend). The
    owner can register a new agent or enable/disable an existing one without touching
    code or the database directly (#57, #61). Secret values are NEVER entered here —
    only an opaque ``secret_ref`` handle.
    """
    error_block = ""
    if error:
        error_block = f'<div class="msg msg-error">{escape(error)}</div>'

    rows: list[str] = []
    for a in agents:
        dm = a.delegation_mode.value if a.delegation_mode else "—（本地适配器）"
        enabled_badge = (
            '<span class="badge badge-on">启用</span>'
            if a.enabled
            else '<span class="badge badge-off">停用</span>'
        )
        toggle_label = "停用" if a.enabled else "启用"
        toggle_value = "false" if a.enabled else "true"
        rows.append(
            f"""<tr>
              <td>{escape(a.name)}</td>
              <td>{escape(a.role)}</td>
              <td>{escape(a.adapter_type.value)}</td>
              <td>{escape(dm)}</td>
              <td>{escape(a.trust_level.value)}</td>
              <td>{enabled_badge}</td>
              <td>{escape(a.endpoint or "—")}</td>
              <td>
                <form method="post" action="/owner/agents/{escape(a.id)}/toggle" class="run-form">
                  <input type="hidden" name="enabled" value="{toggle_value}">
                  <button type="submit" class="btn-run">{toggle_label}</button>
                </form>
              </td>
            </tr>"""
        )
    rows_html = "\n".join(rows) if rows else '<tr><td colspan="8">暂无已注册 agent。</td></tr>'

    adapter_opts = "".join(
        f'<option value="{at.value}">{at.value}</option>' for at in AdapterType
    )
    mode_opts = "".join(
        f'<option value="{dm.value}">{dm.value}</option>' for dm in DelegationMode
    )
    trust_opts = "".join(
        f'<option value="{tl.value}"{" selected" if tl == AgentTrustLevel.INTERNAL else ""}>'
        f"{tl.value}</option>"
        for tl in AgentTrustLevel
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent 注册表 · 控制台</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; }}
  .wrap {{ max-width: 920px; margin: 40px auto; padding: 0 20px; }}
  h1 {{ font-size: 22px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 24px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 20px; }}
  label {{ display: block; font-weight: 600; margin: 14px 0 6px; }}
  input, select, textarea {{ width: 100%; box-sizing: border-box; padding: 10px;
                     border: 1px solid #d0d3d9; border-radius: 6px; font-size: 15px; }}
  textarea {{ min-height: 60px; resize: vertical; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }}
  button {{ margin-top: 18px; width: 100%; padding: 12px; font-size: 16px;
           background: #2f6fed; color: #fff; border: 0; border-radius: 6px;
           cursor: pointer; }}
  button:hover {{ background: #2559c9; }}
  .msg {{ padding: 10px 12px; border-radius: 6px; margin-bottom: 12px; font-size: 14px; }}
  .msg-error {{ background: #fdecea; color: #b42318; border: 1px solid #f5c2bd; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #eceef1; }}
  th {{ color: #6b7280; font-weight: 600; }}
  .badge {{ padding: 2px 8px; border-radius: 10px; font-size: 12px; }}
  .badge-on {{ background: #e7f7ec; color: #1a7f37; }}
  .badge-off {{ background: #f1f2f4; color: #6b7280; }}
  .btn-run {{ width: auto; margin: 0; padding: 6px 12px; font-size: 13px; background: #2f6fed; }}
  .hint {{ color: #6b7280; font-size: 13px; margin-top: 8px; }}
  a {{ color: #2f6fed; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/owner">&larr; 返回控制台</a>
  <h1>Agent 注册表</h1>
  <div class="card">
    <h2>已注册 Agent</h2>
    <table>
      <tr><th>名称</th><th>角色</th><th>适配器</th><th>委托模式</th>
          <th>信任等级</th><th>状态</th><th>端点</th><th>操作</th></tr>
      {rows_html}
    </table>
  </div>
  <div class="card">
    <h2>注册新 Agent</h2>
    {error_block}
    <form method="post" action="/owner/agents/register">
      <div class="grid">
        <div><label for="name">名称</label>
          <input id="name" name="name" type="text" required placeholder="例如：Hermes"></div>
        <div><label for="role">角色</label>
          <input id="role" name="role" type="text" required placeholder="例如：内容岗"></div>
        <div><label for="adapter_type">适配器类型</label>
          <select id="adapter_type" name="adapter_type" required>{adapter_opts}</select></div>
        <div><label for="delegation_mode">委托模式（外部 agent 必填）</label>
          <select id="delegation_mode" name="delegation_mode">{mode_opts}</select></div>
        <div><label for="trust_level">信任等级</label>
          <select id="trust_level" name="trust_level">{trust_opts}</select></div>
        <div><label for="endpoint">端点 URL</label>
          <input id="endpoint" name="endpoint" type="text" placeholder="https://...（可选）"></div>
        <div><label for="callback_url">回调 URL</label>
          <input id="callback_url" name="callback_url" type="text" placeholder="https://.../callback（可选）"></div>
        <div><label for="secret_ref">凭证引用（仅不透明句柄，如 secret://hermes-key）</label>
          <input id="secret_ref" name="secret_ref" type="text" placeholder="secret://...（可选）"></div>
        <div><label for="timeout_s">超时（秒）</label>
          <input id="timeout_s" name="timeout_s" type="number" value="300" step="1"></div>
        <div><label for="max_retries">最大重试次数</label>
          <input id="max_retries" name="max_retries" type="number" value="3" step="1"></div>
      </div>
      <label for="capabilities">能力（逗号分隔，可选）</label>
      <input id="capabilities" name="capabilities" type="text" placeholder="写作, 排版, 审核">
      <label for="limitations">限制（逗号分隔，可选）</label>
      <input id="limitations" name="limitations" type="text" placeholder="不支持视频生成">
      <label style="display:inline; font-weight:400;">
        <input type="checkbox" name="enabled" value="true" checked
               style="width:auto; display:inline;"> 注册后启用
      </label>
      <button type="submit">注册 Agent</button>
    </form>
    <p class="hint">仅填写不透明凭证引用（secret://...），切勿粘贴真实密钥——密钥只在外部分发执行边界解析，绝不落库。</p>
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Owner Operating Layer V0 (#121) -- server-rendered inbox surfaces.
#
# HARD RULE (plan §1.3 / §4.1 / T-D1): these pages MUST NOT render any internal
# identity. No project id, no artifact / conversation / suggestion / candidate /
# fact id, no checksum, no revision, no enum value, no canonical tag id, no
# series·version, no ``display_binding``. The ONLY opaque strings on the wire are
# the sealed ``OwnerSealedToken`` values, whose payload is AES-256-GCM encrypted.
# ---------------------------------------------------------------------------

OOL_STYLE = """
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; }
  .wrap { max-width: 820px; margin: 40px auto; padding: 0 20px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  h2 { font-size: 17px; margin: 0 0 10px; }
  .sub { color: #6b7280; font-size: 13px; margin: 0 0 20px; }
  .card { background: #fff; border-radius: 10px; padding: 20px 22px; margin-bottom: 14px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  .tag { font-size: 12px; color: #1d4ed8; background: #eef4ff; border: 1px solid #c9dbff;
         border-radius: 999px; padding: 2px 10px; white-space: nowrap; }
  .ref { color: #6b7280; font-size: 12px; }
  .preview { color: #4b5563; font-size: 14px; margin: 8px 0 0; line-height: 1.6; }
  .sec { border-top: 1px solid #eef0f3; padding-top: 12px; margin-top: 12px; }
  .sec-k { font-weight: 600; font-size: 13px; color: #374151; }
  .sec-v { font-size: 14px; color: #1f2329; margin-top: 4px; line-height: 1.6;
           white-space: pre-wrap; }
  .msg { padding: 10px 12px; border-radius: 6px; margin-bottom: 14px; font-size: 14px; }
  .msg-error { background: #fdecea; color: #b42318; border: 1px solid #f5c2bd; }
  .msg-ok { background: #ecfdf3; color: #067647; border: 1px solid #bbf0d2; }
  .msg-info { background: #eef4ff; color: #1d4ed8; border: 1px solid #c9dbff; }
  .empty { color: #6b7280; font-size: 14px; }
  button { padding: 10px 16px; font-size: 15px; background: #2f6fed; color: #fff;
           border: 0; border-radius: 6px; cursor: pointer; }
  button:hover { background: #2559c9; }
  button.ghost { background: #fff; color: #2f6fed; border: 1px solid #c9dbff; }
  textarea, select, input[type=text] { width: 100%; box-sizing: border-box; padding: 9px;
           border: 1px solid #d0d3d9; border-radius: 6px; font-size: 14px; }
  textarea { min-height: 84px; resize: vertical; }
  .acts { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
  .act { border: 1px solid #eef0f3; border-radius: 8px; padding: 12px; margin-top: 10px; }
  .act-h { font-weight: 600; font-size: 14px; margin-bottom: 8px; }
  .nav { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 6px; }
  .nav a { display: inline-block; padding: 10px 14px; background: #fff; border-radius: 8px;
           border: 1px solid #dfe3e8; text-decoration: none; color: #1f2329; font-size: 15px; }
  .nav a:hover { border-color: #2f6fed; color: #2f6fed; }
  a { color: #2f6fed; }
  .foot { margin-top: 18px; font-size: 13px; }
"""

OOL_INBOX_NAV: list[tuple[str, str]] = [
    ("content", "内容决策"),
    ("cs", "客服决策"),
    ("feedback", "反馈决策"),
    ("knowledge", "知识决策"),
]


def _ool_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{OOL_STYLE}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>"""


def _ool_msg(message: str | None, kind: str) -> str:
    if not message:
        return ""
    return f'<div class="msg msg-{kind}">{escape(message)}</div>'


def _ool_hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'


def owner_project_picker_html(
    options: list[Any], *, error: str | None = None, message: str | None = None
) -> str:
    """``GET /owner/project-picker`` -- business labels only, one sealed token each."""
    if options:
        cards = "\n".join(
            f"""  <div class="card">
    <div class="row">
      <h2>{escape(option.business_label)}</h2>
      <span class="tag">{escape(option.status_label)}</span>
    </div>
    <form method="post" action="/owner/project-pick">
      {_ool_hidden("select_token", option.select_token)}
      <div class="acts"><button type="submit">进入这个业务</button></div>
    </form>
  </div>"""
            for option in options
        )
    else:
        cards = '  <div class="card"><p class="empty">目前没有在进行中的业务。</p></div>'
    body = f"""  <h1>选择你要处理的业务</h1>
  <p class="sub">先选业务，再看待办。系统只会显示这个业务下需要你决定的事。</p>
{_ool_msg(error, "error")}{_ool_msg(message, "ok")}
{cards}
"""
    return _ool_page("选择业务 · 待办中心", body)


def owner_inbox_hub_html(
    project_label: str, context_token: str, *, message: str | None = None
) -> str:
    """``POST /owner/project-pick`` result -- the four-inbox hub for one project."""
    ctx = quote(context_token, safe="")
    links = "\n".join(
        f'      <a href="/owner/inboxes/{kind}?ctx={ctx}">{escape(title)}</a>'
        for kind, title in OOL_INBOX_NAV
    )
    body = f"""  <h1>{escape(project_label)} · 待办中心</h1>
  <p class="sub">这里只列出需要你拍板的事。点进任意一类查看。</p>
{_ool_msg(message, "ok")}
  <div class="card">
    <h2>四类待办</h2>
    <div class="nav">
{links}
    </div>
  </div>
  <p class="foot"><a href="/owner/project-picker">切换业务</a></p>
"""
    return _ool_page(f"{project_label} · 待办中心", body)


def owner_inbox_page_html(
    page: Any, *, message: str | None = None, error: str | None = None
) -> str:
    """``GET /owner/inboxes/{kind}?ctx=...`` -- one inbox list."""
    ctx = quote(page.context_token, safe="")
    if page.items:
        cards = "\n".join(
            f"""  <div class="card">
    <div class="row">
      <h2>{escape(item.business_label)}</h2>
      <span class="tag">{escape(item.status_label)}</span>
    </div>
    <div class="ref">{escape(item.detail_ref)}</div>
    <p class="preview">{escape(item.preview)}</p>
    <form method="post" action="/owner/inboxes/{escape(page.inbox)}/detail">
      {_ool_hidden("detail_token", item.token)}
      {_ool_hidden("ctx", page.context_token)}
      <div class="acts"><button type="submit" class="ghost">查看并处理</button></div>
    </form>
  </div>"""
            for item in page.items
        )
    else:
        cards = '  <div class="card"><p class="empty">这一类目前没有待办。</p></div>'
    links = "\n".join(
        f'      <a href="/owner/inboxes/{kind}?ctx={ctx}">{escape(title)}</a>'
        for kind, title in OOL_INBOX_NAV
        if kind != page.inbox
    )
    # Pagination (contract #125): only the *current page* size is shown -- the
    # total count is never exposed (§8 no-leakage) -- and "下一页" carries the
    # opaque sealed cursor from this page, never a raw internal id.
    if page.has_more and page.next_token:
        next_href = (
            f"/owner/inboxes/{escape(page.inbox)}?ctx={ctx}"
            f"&cursor={quote(page.next_token, safe='')}"
        )
        next_link = f'    <p class="pager"><a href="{next_href}">下一页</a></p>'
    else:
        next_link = ""
    body = f"""  <h1>{escape(page.project_label)} · {escape(page.title)}</h1>
  <p class="sub">本页 {len(page.items)} 条待办。</p>
{_ool_msg(error, "error")}{_ool_msg(message, "ok")}
{cards}
{next_link}
  <div class="card">
    <h2>其他待办</h2>
    <div class="nav">
{links}
    </div>
  </div>
  <p class="foot"><a href="/owner/project-picker">切换业务</a></p>
"""
    return _ool_page(f"{page.project_label} · {page.title}", body)


_OOL_INPUT_FIELDS: dict[str, str] = {
    "stage": "stage_label",
    "canonical": "canonical_choice",
    "series": "series_choice",
}


def _ool_input_control(option: Any) -> str:
    """Render the owner's *business* input for one decision (never an internal id)."""
    kind = option.input_kind
    if kind == "reason":
        return '<textarea name="reason" placeholder="填写处理理由（会记入操作记录）"></textarea>'
    if kind == "body":
        return '<textarea name="body" placeholder="修改后的正文"></textarea>'
    if kind == "text":
        return '<textarea name="text" placeholder="可直接发送，也可以改写后再发送"></textarea>'
    if kind == "title":
        return '<input type="text" name="title" placeholder="跟进任务标题">'
    if kind in _OOL_INPUT_FIELDS:
        field_name = _OOL_INPUT_FIELDS[kind]
        opts = "\n".join(
            f'        <option value="{escape(choice)}">{escape(choice)}</option>'
            for choice in option.choices
        )
        return f'<select name="{field_name}">\n{opts}\n      </select>'
    if kind == "tags":
        return "\n".join(
            '        <label style="font-weight:400; display:block;">'
            f'<input type="checkbox" name="tag_labels" value="{escape(choice)}"'
            f' style="width:auto;"> {escape(choice)}</label>'
            for choice in option.choices
        )
    return ""


def owner_inbox_detail_html(detail: Any, *, error: str | None = None) -> str:
    """``POST /owner/inboxes/{kind}/detail`` -- one item plus its valid decisions."""
    sections = "\n".join(
        f"""  <div class="sec">
    <div class="sec-k">{escape(key)}</div>
    <div class="sec-v">{escape(value)}</div>
  </div>"""
        for key, value in detail.sections
    )
    if detail.options:
        actions = "\n".join(
            f"""  <div class="act">
    <div class="act-h">{escape(option.label)}</div>
    <form method="post" action="/owner/inboxes/{escape(detail.inbox)}/decide">
      {_ool_hidden("action_token", option.token)}
      {_ool_hidden("ctx", detail.context_token)}
      {_ool_input_control(option)}
      <div class="acts"><button type="submit">{escape(option.label)}</button></div>
    </form>
  </div>"""
            for option in detail.options
        )
    else:
        actions = '  <p class="empty">这条目前没有你可以执行的操作。</p>'
    if detail.audit_entries:
        audit = "\n".join(
            f'    <div class="sec-v">{escape(entry)}</div>' for entry in detail.audit_entries
        )
    else:
        audit = '    <p class="empty">暂无操作记录。</p>'
    ctx = quote(detail.context_token, safe="")
    body = f"""  <h1>{escape(detail.project_label)} · {escape(detail.title)}</h1>
  <p class="sub">{escape(detail.detail_ref)}</p>
{_ool_msg(error, "error")}
  <div class="card">
    <div class="row">
      <h2>{escape(detail.business_label)}</h2>
      <span class="tag">{escape(detail.status_label)}</span>
    </div>
{sections}
  </div>
  <div class="card">
    <h2>你可以做的决定</h2>
{actions}
  </div>
  <div class="card">
    <h2>操作记录</h2>
{audit}
  </div>
  <p class="foot"><a href="/owner/inboxes/{escape(detail.inbox)}?ctx={ctx}">返回该类待办</a>
     · <a href="/owner/project-picker">切换业务</a></p>
"""
    return _ool_page(f"{detail.project_label} · {detail.title}", body)


def owner_inbox_error_html(message: str) -> str:
    """Uniform owner-facing failure page (§8) -- one business sentence, no internals.

    Deliberately takes nothing but the sentence:

    * **No token is echoed back.** A failure page carrying the submitted context
      token would differ between "the context was refused" and "the action was
      refused", re-opening the enumeration channel that the uniform failure
      class exists to close -- and it would embed a sealed token in a page
      rendered for a request we just declined to trust.
    * **One safe recovery action.** The owner returns to the business list and
      re-enters the inbox with a freshly minted context, so recovery never
      depends on a token whose validity could not be confirmed.
    """
    back = '  <p class="foot"><a href="/owner/project-picker">返回业务列表</a></p>'
    hint = "收件箱里的内容可能已经变化。刷新后按最新状态重新处理即可，系统不会重复执行。"
    body = f"""  <h1>这一步没能完成</h1>
{_ool_msg(message, "error")}
  <div class="card">
    <p class="empty">{escape(hint)}</p>
  </div>
{back}
"""
    return _ool_page("操作未完成", body)
