from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from aios.audit import append_audit
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentStatus,
    Capability,
    RoutingMode,
    Task,
    TaskStatus,
    now_utc,
)
from aios.scheduler import route_task
from aios.schemas import CampaignLaunchResult, ProjectCreate, TaskCreate
from aios.services import ServiceError, create_project, create_task

# --- V1 seed data (plain Python data; NO new DB models / migrations) ---

V1_CAPABILITIES: list[dict[str, str]] = [
    {
        "name": "user_research",
        "description": (
            "Collect target-user problems, search intent, benchmark accounts, "
            "competitor and community signals."
        ),
    },
    {
        "name": "positioning",
        "description": (
            "Define personal-IP positioning, audiences, content pillars, "
            "differentiation and conversion paths."
        ),
    },
    {
        "name": "wechat_writing",
        "description": "Produce WeChat official-account long-form articles.",
    },
    {
        "name": "xhs_adaptation",
        "description": "Adapt a core topic into a Xiaohongshu post.",
    },
    {
        "name": "video_script",
        "description": "Write short-video scripts for Douyin / Video Accounts.",
    },
    {
        "name": "packaging",
        "description": "Assemble platform outputs into one distribution package.",
    },
    {
        "name": "knowledge_capture",
        "description": "Capture an approved output as a reusable knowledge candidate/fact.",
    },
]

V1_DEPARTMENTS: list[dict[str, Any]] = [
    {"name": "User Research Agent", "role": "user_research", "capabilities": ["user_research"]},
    {
        "name": "Positioning and Strategy Agent",
        "role": "positioning",
        "capabilities": ["positioning"],
    },
    {
        "name": "Content Strategy Agent",
        "role": "content_strategy",
        "capabilities": ["xhs_adaptation", "packaging"],
    },
    {
        "name": "Content Production Agent",
        "role": "content_production",
        "capabilities": ["wechat_writing", "video_script"],
    },
    {"name": "Visual and Design Agent", "role": "visual_design", "capabilities": []},
    {
        "name": "Growth and Conversion Agent",
        "role": "growth",
        "capabilities": ["knowledge_capture"],
    },
]

# Ordered T1..T9. `department` is a V1_DEPARTMENTS name, or None for owner gates.
# `required_capabilities` / `depends_on` use names / T-keys here; they are resolved to
# capability IDs / task IDs at launch time.
V1_TASKS: list[dict[str, Any]] = [
    {
        "key": "T1",
        "title": "T1 用户与竞品调研",
        "description": (
            "收集目标用户痛点、搜索意图、对标账号、社区问题与竞品信号，"
            "产出证据化用户洞察。"
        ),
        "department": "User Research Agent",
        "required_capabilities": ["user_research"],
        "depends_on": [],
        "acceptance_criteria": [
            "形成不少于 3 条证据化用户洞察",
            "明确首篇内容要解决的用户问题",
        ],
    },
    {
        "key": "T2",
        "title": "T2 定位与策略",
        "description": "基于调研定义个人 IP 定位、目标人群、内容支柱、差异化与转化路径。",
        "department": "Positioning and Strategy Agent",
        "required_capabilities": ["positioning"],
        "depends_on": ["T1"],
        "acceptance_criteria": ["给出清晰定位陈述", "列出 3 个内容支柱"],
    },
    {
        "key": "T3",
        "title": "T3 核心微信文章",
        "description": "基于定位产出一篇微信官方号长文（核心资产）。",
        "department": "Content Production Agent",
        "required_capabilities": ["wechat_writing"],
        "depends_on": ["T2"],
        "acceptance_criteria": ["产出一篇可直接发布的微信长文", "紧扣定位与用户问题"],
    },
    {
        "key": "T4",
        "title": "T4 小红书改编",
        "description": "将核心文章改编为一篇小红书笔记。",
        "department": "Content Strategy Agent",
        "required_capabilities": ["xhs_adaptation"],
        "depends_on": ["T3"],
        "acceptance_criteria": ["产出一篇小红书笔记", "适配搜索型发现语境"],
    },
    {
        "key": "T5",
        "title": "T5 短视频脚本",
        "description": "基于核心文章产出一条短视频脚本。",
        "department": "Content Production Agent",
        "required_capabilities": ["video_script"],
        "depends_on": ["T3"],
        "acceptance_criteria": ["产出一条短视频脚本", "时长与平台节奏匹配"],
    },
    {
        "key": "T6",
        "title": "T6 人工审阅",
        "description": "所有者审阅核心文章与平台改编，决定通过 / 退回 / 要求修改。",
        "department": None,
        "required_capabilities": [],
        "depends_on": ["T3", "T4", "T5"],
        "acceptance_criteria": ["所有者做出明确审批决定"],
    },
    {
        "key": "T7",
        "title": "T7 分发包",
        "description": "将三个平台产出打包为一个分发包（Artifact + metadata_json）。",
        "department": "Content Strategy Agent",
        "required_capabilities": ["packaging"],
        "depends_on": ["T6"],
        "acceptance_criteria": ["分发包列出 T3/T4/T5 产出", "关联发布审批"],
    },
    {
        "key": "T8",
        "title": "T8 发布闸门",
        "description": "所有者审批发布闸门（L3），系统标记包就绪，不自动发布。",
        "department": None,
        "required_capabilities": [],
        "depends_on": ["T7"],
        "acceptance_criteria": ["所有者批准发布闸门"],
    },
    {
        "key": "T9",
        "title": "T9 知识沉淀",
        "description": "从已批准文章提炼知识候选，经所有者审阅成为可复用 KnowledgeFact。",
        "department": "Growth and Conversion Agent",
        "required_capabilities": ["knowledge_capture"],
        "depends_on": ["T6"],
        "acceptance_criteria": ["形成可复用知识事实", "可被下一 campaign 复用"],
    },
]


def _upsert_capability(
    session: Session, name: str, description: str, probe: Callable[[], None] | None = None
) -> Capability:
    """Idempotent AND race-safe capability upsert.

    Uses a deterministic id (``cap:<name>``) and ``INSERT ... ON CONFLICT DO NOTHING``
    so two concurrent launches cannot crash on the ``Capability.name`` unique index
    nor create duplicate rows. ``probe`` (test-only) runs just before the INSERT to
    force a check-then-insert race window.
    """
    cap_id = f"cap:{name}"
    if probe is not None:
        probe()
    session.execute(
        sqlite_insert(Capability)
        .values(id=cap_id, name=name, description=description)
        .on_conflict_do_nothing(index_elements=["name"])
    )
    session.flush()
    return session.exec(select(Capability).where(Capability.name == name)).first()


def _upsert_agent(
    session: Session, agent_id: str, name: str, role: str, capabilities: list[str]
) -> Agent:
    """Idempotent AND race-safe agent upsert (deterministic id ``agt:<role>``).

    All columns are supplied explicitly because a core ``INSERT`` does not apply the
    model's Python-side ``default_factory`` defaults (which ``Session.add`` would).
    """
    session.execute(
        sqlite_insert(Agent)
        .values(
            id=agent_id,
            name=name,
            role=role,
            adapter_type=AdapterType.EXTERNAL,
            capabilities=capabilities,
            permissions=[],
            cost_policy={},
            endpoint=None,
            config_ref=None,
            enabled=True,
            limitations=[],
            status=AgentStatus.AVAILABLE,
        )
        .on_conflict_do_nothing()
    )
    session.flush()
    return session.exec(select(Agent).where(Agent.id == agent_id)).first()


def _upsert_agent_capability(
    session: Session, agent_id: str, capability_id: str, priority: int = 50, enabled: bool = True
) -> None:
    """Idempotent AND race-safe AgentCapability link upsert (composite PK)."""
    session.execute(
        sqlite_insert(AgentCapability)
        .values(agent_id=agent_id, capability_id=capability_id, priority=priority, enabled=enabled)
        .on_conflict_do_nothing()
    )


def seed_v1_agents(
    session: Session,
    commit: bool = True,
    *,
    _probe: Callable[[], None] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Idempotently seed V1 capabilities + 6 department agents.

    Returns (agent_by_name, capability_by_name) mapping the seed names to their DB ids,
    so callers can wire Task.required_capabilities (which stores capability ids) correctly.

    The upserts are race-safe (deterministic ids + ON CONFLICT DO NOTHING) so concurrent
    campaign launches never crash on the Capability.name unique index nor duplicate the
    6 department agents.
    """
    capability_by_name: dict[str, str] = {}
    for index, cap in enumerate(V1_CAPABILITIES):
        # Force the race window only on the first capability (test seam).
        probe = _probe if index == 0 else None
        existing = _upsert_capability(session, cap["name"], cap["description"], probe=probe)
        capability_by_name[cap["name"]] = existing.id

    agent_by_name: dict[str, str] = {}
    for dept in V1_DEPARTMENTS:
        agent_id = f"agt:{dept['role']}"
        existing = _upsert_agent(
            session,
            agent_id,
            dept["name"],
            dept["role"],
            [capability_by_name[name] for name in dept["capabilities"]],
        )
        agent_by_name[dept["name"]] = existing.id
        for cap_name in dept["capabilities"]:
            _upsert_agent_capability(session, existing.id, capability_by_name[cap_name])

    if commit:
        session.commit()
    else:
        session.flush()
    return agent_by_name, capability_by_name


def launch_campaign(
    session: Session, request: ProjectCreate, idempotency_key: str
) -> CampaignLaunchResult:
    """Create a V1 campaign Project + the T1-T9 task graph and kick off the first task.

    Reuses existing Project/Task/depends_on, capability routing (scheduler.route_task in
    FIXED mode), AuditLog, the Event outbox and idempotency (request_fingerprint/_replay via
    create_project/create_task). No new production models or migrations are introduced.
    """
    if not request.name or not request.name.strip():
        raise ServiceError(
            400,
            "campaign 目标标题不能为空。请填写一个真实的业务目标，"
            "例如：把失败的 AI 系统重建成可用的小 AI 公司。",
        )
    if not request.objective or not request.objective.strip():
        raise ServiceError(
            400,
            "campaign 目标描述不能为空。请说明这次 campaign 要达成的业务结果。",
        )

    agent_by_name, capability_by_name = seed_v1_agents(session, commit=False)
    try:
        try:
            project = create_project(session, request, idempotency_key, commit=False)
        except ServiceError as error:
            if error.status_code == 409:
                # A repeated Idempotency-Key with a *different* body is a conflict, not a
                # silent replay. Surface it in owner-readable Chinese instead of the
                # generic English message from the idempotency layer.
                raise ServiceError(
                    409,
                    "你刚才用同一个提交标识（Idempotency-Key）提交过一次，但这一次的标题或目标描述"
                    "和上一次不一样，系统为避免重复创建已拒绝。"
                    "若确实要新建 campaign，请换一个提交标识；"
                    "若只是想重试，请保持标题和目标描述完全一致。",
                ) from error
            raise

        created_ids: dict[str, str] = {}
        for task_def in V1_TASKS:
            dept = task_def.get("department")
            assigned_agent_id = agent_by_name.get(dept) if dept else None
            depends_on = [created_ids[key] for key in task_def["depends_on"]]
            task_request = TaskCreate(
                project_id=project.id,
                title=task_def["title"],
                description=task_def["description"],
                assigned_agent_id=assigned_agent_id,
                required_capabilities=[
                    capability_by_name[name] for name in task_def.get("required_capabilities", [])
                ],
                routing_mode=RoutingMode.FIXED if dept else RoutingMode.MANUAL,
                acceptance_criteria=list(task_def.get("acceptance_criteria", [])),
                depends_on=depends_on,
            )
            task = create_task(
                session, task_request, f"{idempotency_key}:task:{task_def['key']}", commit=False
            )
            created_ids[task_def["key"]] = task.id

        # Kick off: tasks with no dependencies are ready to start now.
        kicked_off: list[str] = []
        for task_def in V1_TASKS:
            if not task_def["depends_on"]:
                task = session.get(Task, created_ids[task_def["key"]])
                if task is not None and task.status == TaskStatus.BACKLOG:
                    task.status = TaskStatus.READY
                    task.updated_at = now_utc()
                    session.add(task)
                    append_audit(
                        session,
                        actor="owner",
                        action="task.ready",
                        resource_type="task",
                        resource_id=task.id,
                        project_id=project.id,
                        task_id=task.id,
                        before={"status": TaskStatus.BACKLOG.value},
                        after={"status": TaskStatus.READY.value},
                        idempotency_key=f"kickoff:{idempotency_key}:{task_def['key']}",
                    )
                    kicked_off.append(task_def["key"])

        # Reuse capability routing: assign each kicked-off task to its department agent.
        # Done inside the same transaction so routing T1 is part of the atomic launch.
        for key in kicked_off:
            route_task(session, created_ids[key], f"{idempotency_key}:route:{key}", commit=False)

        # Single atomic commit: Project + T1-T9 + dependency wiring + seeding +
        # AuditLog + Event outbox + idempotency records + routing T1 all land together,
        # or none of it does.
        session.commit()
    except Exception:
        session.rollback()
        raise

    tasks_out: list[dict[str, Any]] = []
    for task_def in V1_TASKS:
        task = session.get(Task, created_ids[task_def["key"]])
        tasks_out.append(
            {
                "key": task_def["key"],
                "title": task_def["title"],
                "task_id": task.id,
                "status": task.status.value,
                "assigned_agent_id": task.assigned_agent_id,
                "depends_on": [created_ids[key] for key in task_def["depends_on"]],
            }
        )

    message = (
        "✅ 你的「个人 AI 公司 V1」工作流已经启动。"
        f"已创建项目 {project.id} 与 {len(V1_TASKS)} 个任务（T1 调研 → T9 知识沉淀）。"
        "首个任务「T1 用户与竞品调研」已就绪并已分配给对应部门，可在看板查看进度。"
    )
    return CampaignLaunchResult(
        project_id=project.id,
        project_status=project.status.value,
        task_count=len(V1_TASKS),
        tasks=tasks_out,
        message=message,
    )
