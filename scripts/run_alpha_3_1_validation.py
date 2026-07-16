#!/usr/bin/env python3
"""Alpha-3.1 operational validation runner.

Deterministic end-to-end validation of the *existing* AIOS architecture.

Usage
-----
    python scripts/run_alpha_3_1_validation.py
    python scripts/run_alpha_3_1_validation.py --reset
    python scripts/run_alpha_3_1_validation.py --db /path/to/custom.db

The runner:
* defaults to ``data/alpha-3-1-validation.db``
* REFUSES to overwrite an existing database (use ``--reset`` to recreate)
* NEVER commits the generated SQLite database to git
* verifies persistence in a brand-new database session

No LLM is used. No new production service, API, model, migration, or routing
logic is added. Only the existing capability scheduler, ContextService,
external workstation adapter, orchestrator, KnowledgeService, Approval,
AuditLog, and transactional outbox are exercised.

This module is the single source of truth for the validation scenario; the
integration test ``tests/test_alpha_3_1_validation.py`` imports ``run_validation``
and asserts on its structured result.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from aios.adapters.external import ExternalWorkstationAdapter, TaskPacket
from aios.audit import AuditLog
from aios.context_service import ContextService
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    Capability,
    Event,
    ExecutionAssignment,
    KnowledgeCandidate,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecision,
    KnowledgeReviewDecisionValue,
    Project,
    ProjectStatus,
    RiskLevel,
    RoutingMode,
    Task,
    TaskContext,
    TaskStatus,
)
from aios.orchestrator import Orchestrator
from aios.schemas import ApprovalCreate
from aios.services import create_approval

# --- Deterministic constants -------------------------------------------------

CAP_RESEARCH = "cap_research"
CAP_PLANNING = "cap_planning"
CAP_WRITING = "cap_writing"

AGT_RESEARCH = "agt_research"
AGT_PLANNING = "agt_planning"
AGT_WRITING = "agt_writing"

PROJECT_ID = "prj_alpha_3_1"
RESEARCH_ID = "tsk_research"
PLANNING_ID = "tsk_planning"
WRITING_ID = "tsk_writing"
APPROVAL_ID = "tsk_approval"

FACT_SERIES = "market_analysis_series"

DEFAULT_DB = Path("data/alpha-3-1-validation.db")


@dataclass
class ValidationReport:
    database_url: str
    manual_interventions: list[str] = field(default_factory=list)
    exported_packages: list[str] = field(default_factory=list)
    imported_packages: list[str] = field(default_factory=list)
    context_hashes: dict[str, str] = field(default_factory=dict)
    routing_decisions: list[dict] = field(default_factory=list)
    knowledge_provenance: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    friction: list[str] = field(default_factory=list)
    workflow_stopped_at: str = ""
    manual_gates_preserved: bool | None = None


def _seed_deterministic_world(session: Session, report: ValidationReport) -> None:
    capabilities = [
        Capability(id=CAP_RESEARCH, name="research", description="Market research"),
        Capability(id=CAP_PLANNING, name="planning", description="Outline planning"),
        Capability(id=CAP_WRITING, name="writing", description="Content writing"),
    ]
    agents = [
        Agent(
            id=AGT_RESEARCH,
            name="Researcher",
            role="researcher",
            adapter_type=AdapterType.EXTERNAL,
            permissions=["read_market_data"],
            limitations=["no_publish"],
        ),
        Agent(
            id=AGT_PLANNING,
            name="Planner",
            role="planner",
            adapter_type=AdapterType.EXTERNAL,
            permissions=["read_artifacts"],
            limitations=["no_publish"],
        ),
        Agent(
            id=AGT_WRITING,
            name="Writer",
            role="writer",
            adapter_type=AdapterType.EXTERNAL,
            permissions=["read_artifacts"],
            limitations=["no_publish"],
        ),
    ]
    session.add_all(capabilities)
    session.add_all(agents)
    session.flush()
    session.add_all(
        [
            AgentCapability(agent_id=AGT_RESEARCH, capability_id=CAP_RESEARCH, priority=90),
            AgentCapability(agent_id=AGT_PLANNING, capability_id=CAP_PLANNING, priority=88),
            AgentCapability(agent_id=AGT_WRITING, capability_id=CAP_WRITING, priority=85),
        ]
    )

    project = Project(
        id=PROJECT_ID,
        name="AI E-Commerce Market Analysis",
        objective="Analyze the AI e-commerce market and produce a go-to-market brief",
        description="Deterministic Alpha-3.1 validation project",
        status=ProjectStatus.ACTIVE,
    )
    session.add(project)
    session.flush()

    research = Task(
        id=RESEARCH_ID,
        project_id=project.id,
        title="Research",
        description="Research the AI e-commerce market landscape",
        status=TaskStatus.READY,
        routing_mode=RoutingMode.BEST_AVAILABLE,
        adapter_type=AdapterType.EXTERNAL,
        required_capabilities=[CAP_RESEARCH],
        acceptance_criteria=["Covers top 5 vendors", "Cites sources"],
    )
    planning = Task(
        id=PLANNING_ID,
        project_id=project.id,
        title="Planning",
        description="Create the go-to-market outline",
        status=TaskStatus.BACKLOG,
        routing_mode=RoutingMode.BEST_AVAILABLE,
        adapter_type=AdapterType.EXTERNAL,
        required_capabilities=[CAP_PLANNING],
        depends_on=[RESEARCH_ID],
        acceptance_criteria=["Outline has 3 sections"],
    )
    writing = Task(
        id=WRITING_ID,
        project_id=project.id,
        title="Writing",
        description="Write the go-to-market brief",
        status=TaskStatus.BACKLOG,
        routing_mode=RoutingMode.BEST_AVAILABLE,
        adapter_type=AdapterType.EXTERNAL,
        required_capabilities=[CAP_WRITING],
        depends_on=[PLANNING_ID],
        acceptance_criteria=["Brief is under 500 words"],
    )
    approval = Task(
        id=APPROVAL_ID,
        project_id=project.id,
        title="L4 Human Approval",
        description="Executive approval before publication",
        status=TaskStatus.BACKLOG,
        routing_mode=RoutingMode.MANUAL,
        depends_on=[WRITING_ID],
    )
    session.add_all([research, planning, writing, approval])
    session.commit()
    report.manual_interventions.append(
        "Seeded deterministic world: 3 agents, 3 capabilities, 1 project, 4 tasks "
        "(research/planning/writing=L4-routed, approval=MANUAL)."
    )


def _route_export_research(
    session: Session, adapter: ExternalWorkstationAdapter, report: ValidationReport
) -> Artifact:
    from aios.external_service import import_external_result
    from aios.scheduler import route_task

    assignment = route_task(session, RESEARCH_ID, "route-research")
    report.routing_decisions.append(
        {
            "task": RESEARCH_ID,
            "mode": "best_available",
            "selected_agent": assignment.selected_agent_id,
            "reason": assignment.routing_reason,
            "fallback_used": assignment.fallback_used,
        }
    )
    context = ContextService(session).build_context(RESEARCH_ID, assignment.id)
    report.context_hashes[RESEARCH_ID] = context.context_hash

    exported = adapter.export_task(
        TaskPacket(
            task_id=RESEARCH_ID,
            project={"id": PROJECT_ID, "objective": "Analyze the AI e-commerce market"},
            role="researcher",
            instructions="Research the AI e-commerce market landscape",
            output_schema={
                "type": "object",
                "required": ["result_id", "task_id", "summary", "claims", "artifacts"],
                "properties": {
                    "result_id": {"type": "string"},
                    "task_id": {"const": RESEARCH_ID},
                    "summary": {"type": "string", "minLength": 1},
                    "claims": {"type": "array"},
                    "artifacts": {"type": "array"},
                },
            },
        ),
        task_context=context,
    )
    report.exported_packages.append(str(exported.packet_path))
    report.exported_packages.append(str(exported.context_path))

    result_path = adapter.inbox / "res_research.json"
    result_path.write_text(
        json.dumps(
            {
                "result_id": "res_research",
                "task_id": RESEARCH_ID,
                "summary": "Top vendors identified",
                "claims": [
                    {"statement": "Vendor A leads the market"},
                    {"statement": "Market grows 20% yearly"},
                ],
                "artifacts": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact = import_external_result(session, adapter, result_path)
    report.imported_packages.append(str(result_path))
    report.manual_interventions.append(
        "External workstation exported research packet+context; imported result "
        f"'res_research' produced artifact {artifact.id}."
    )
    return artifact


def _manual_approve_and_review(
    session: Session, artifact: Artifact, report: ValidationReport
) -> None:
    artifact.review_status = ArtifactReviewStatus.APPROVED
    session.add(artifact)
    session.commit()
    report.manual_interventions.append(
        f"Explicit manual approval of research Artifact {artifact.id} "
        "(review_status -> approved)."
    )

    service = KnowledgeService(session)
    approve_candidate = service.submit_candidate(
        artifact.id, "Vendor A leads the AI e-commerce market", PROJECT_ID, "human_ceo"
    )
    reject_candidate = service.submit_candidate(
        artifact.id, "Market grows 20% yearly without caveat", PROJECT_ID, "human_ceo"
    )
    approved = service.review_candidate(
        approve_candidate.id,
        KnowledgeReviewDecisionValue.APPROVE,
        "human_ceo",
        "Verified against two independent sources",
        series_id=FACT_SERIES,
        version=1,
    )
    service.review_candidate(
        reject_candidate.id,
        KnowledgeReviewDecisionValue.REJECT,
        "human_ceo",
        "Overstates growth, missing caveats",
    )
    report.manual_interventions.append(
        "Knowledge review: approved 1 candidate (series "
        f"{FACT_SERIES} v1 -> fact {approved.fact.id if approved.fact else None}), "
        f"rejected 1 candidate ({reject_candidate.id})."
    )
    report.knowledge_provenance.append(
        {
            "candidate_id": approve_candidate.id,
            "decision": "approve",
            "fact_id": approved.fact.id if approved.fact else None,
            "series_id": FACT_SERIES,
            "version": 1,
        }
    )
    report.knowledge_provenance.append(
        {
            "candidate_id": reject_candidate.id,
            "decision": "reject",
            "fact_id": None,
            "series_id": None,
            "version": None,
        }
    )


def _route_task(
    session: Session, task_id: str, key: str, report: ValidationReport
) -> ExecutionAssignment:
    from aios.scheduler import route_task

    assignment = route_task(session, task_id, key)
    report.routing_decisions.append(
        {
            "task": task_id,
            "mode": "best_available",
            "selected_agent": assignment.selected_agent_id,
            "reason": assignment.routing_reason,
            "fallback_used": assignment.fallback_used,
        }
    )
    return assignment


def run_validation(database_url: str) -> ValidationReport:
    """Run the full Alpha-3.1 deterministic validation against ``database_url``.

    Returns a structured :class:`ValidationReport`. Persistence is re-verified in a
    fresh session before returning. Overwrite/reset handling lives exclusively in
    the CLI layer (``_prepare_db`` / ``main``), not here.
    """
    from aios.mock_agent import MockApiAgent

    report = ValidationReport(database_url=database_url)

    # Build the schema once.
    run_migrations(database_url)

    tmp_root = Path(database_url.replace("sqlite:///", "")).parent / ".alpha_3_1_work"
    adapter = ExternalWorkstationAdapter(tmp_root / "outbox", tmp_root / "inbox")

    with Session(get_engine(database_url)) as session:
        _seed_deterministic_world(session, report)

        research_artifact = _route_export_research(session, adapter, report)
        assert session.get(Task, RESEARCH_ID).status == TaskStatus.DONE

        _manual_approve_and_review(session, research_artifact, report)

        facts = list(session.exec(select(KnowledgeFact)))
        assert len(facts) == 1 and facts[0].status == KnowledgeFactStatus.APPROVED

        Orchestrator(session).process_pending()
        assert session.get(Task, PLANNING_ID).status == TaskStatus.READY

        plan_assignment = _route_task(session, PLANNING_ID, "route-planning", report)
        plan_context = ContextService(session).build_context(PLANNING_ID, plan_assignment.id)
        report.context_hashes[PLANNING_ID] = plan_context.context_hash
        assert facts[0].id in {f["fact_id"] for f in plan_context.approved_facts}

        MockApiAgent(session).complete(
            PLANNING_ID, {"outline": ["Market", "Strategy", "Risks"]}, "mock-planning"
        )
        Orchestrator(session).process_pending()
        assert session.get(Task, WRITING_ID).status == TaskStatus.READY

        write_assignment = _route_task(session, WRITING_ID, "route-writing", report)
        write_context = ContextService(session).build_context(WRITING_ID, write_assignment.id)
        report.context_hashes[WRITING_ID] = write_context.context_hash
        assert facts[0].id in {f["fact_id"] for f in write_context.approved_facts}

        MockApiAgent(session).complete(
            WRITING_ID, {"brief": "Go to market with vendor A"}, "mock-writing"
        )
        Orchestrator(session).process_pending()
        # MANUAL routing: L4 approval is activated to READY by the orchestrator but
        # is NEVER auto-assigned an agent (route_task returns None for MANUAL mode).
        assert session.get(Task, APPROVAL_ID).status == TaskStatus.READY

        create_approval(
            session,
            ApprovalCreate(
                project_id=PROJECT_ID,
                task_id=APPROVAL_ID,
                action_type="publish",
                risk_level=RiskLevel.L4,
                rationale="Executive sign-off required before publication",
            ),
            "approval-l4-publish",
        )
        approval = session.exec(select(Approval)).first()
        assert approval.status == ApprovalStatus.PENDING
        report.workflow_stopped_at = "L4 approval PENDING (manual human decision required)"
        report.manual_interventions.append(
            "L4 Approval requested (risk L4) and left PENDING; no automatic "
            "publication or external L4 action taken."
        )

        # Counts within the active session.
        report.counts = {
            "projects": len(list(session.exec(select(Project).where(Project.id == PROJECT_ID)))),
            "tasks": len(list(session.exec(select(Task).where(Task.project_id == PROJECT_ID)))),
            "execution_assignments": len(list(session.exec(select(ExecutionAssignment)))),
            "task_contexts": len(
                list(session.exec(select(TaskContext).where(TaskContext.project_id == PROJECT_ID)))
            ),
            "artifacts": len(
                list(session.exec(select(Artifact).where(Artifact.project_id == PROJECT_ID)))
            ),
            "knowledge_candidates": len(list(session.exec(select(KnowledgeCandidate)))),
            "knowledge_facts": len(list(session.exec(select(KnowledgeFact)))),
            "knowledge_review_decisions": len(
                list(session.exec(select(KnowledgeReviewDecision)))
            ),
            "events": len(list(session.exec(select(Event)))),
            "approvals_pending": len(
                list(
                    session.exec(
                        select(Approval).where(
                            Approval.project_id == PROJECT_ID,
                            Approval.status == ApprovalStatus.PENDING,
                        )
                    )
                )
            ),
            "audit_logs": len(list(session.exec(select(AuditLog)))),
        }

    # Step 11: brand-new session, verify persistence.
    with Session(get_engine(database_url)) as session:
        assert session.get(Project, PROJECT_ID) is not None
        tasks = list(session.exec(select(Task).where(Task.project_id == PROJECT_ID)))
        assert {t.id for t in tasks} == {RESEARCH_ID, PLANNING_ID, WRITING_ID, APPROVAL_ID}
        assignments = list(session.exec(select(ExecutionAssignment)))
        assert len(assignments) == 3
        contexts = list(
            session.exec(select(TaskContext).where(TaskContext.project_id == PROJECT_ID))
        )
        assert len(contexts) >= 3
        artifacts = list(session.exec(select(Artifact).where(Artifact.project_id == PROJECT_ID)))
        assert len(artifacts) >= 3
        facts = list(session.exec(select(KnowledgeFact)))
        assert len(facts) == 1 and facts[0].status == KnowledgeFactStatus.APPROVED
        candidates = list(session.exec(select(KnowledgeCandidate)))
        assert len(candidates) == 2
        assert len(list(session.exec(select(KnowledgeReviewDecision)))) == 2
        assert len(list(session.exec(select(Event)))) >= 4
        approvals = list(
            session.exec(
                select(Approval).where(
                    Approval.project_id == PROJECT_ID,
                    Approval.status == ApprovalStatus.PENDING,
                )
            )
        )
        assert len(approvals) == 1
        assert len(list(session.exec(select(AuditLog)))) >= 10
        l4_assignment = list(
            session.exec(
                select(ExecutionAssignment).where(ExecutionAssignment.task_id == APPROVAL_ID)
            )
        )
        assert l4_assignment == []
        report.friction.append(
            "L4 approval requires a human in the loop; AIOS correctly refused to "
            "auto-route or auto-decide a manual task."
        )
        # Evidence-based, neutral conclusion: the human gate was preserved.
        # Derived from observed state in this session, not hard-coded.
        report.manual_gates_preserved = len(l4_assignment) == 0 and len(approvals) == 1

    return report


def _prepare_db(db_path: Path, *, reset: bool) -> str:
    if db_path.exists():
        if not reset:
            raise SystemExit(
                f"REFUSING to overwrite existing database: {db_path}\n"
                "Use --reset to recreate it."
            )
        db_path.unlink()
        for sibling in ("-wal", "-shm"):
            candidate = db_path.with_name(db_path.name + sibling)
            if candidate.exists():
                candidate.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path.as_posix()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alpha-3.1 operational validation runner")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to the validation SQLite DB")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Recreate the database if it already exists (otherwise refuse to overwrite)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    database_url = _prepare_db(db_path, reset=args.reset)

    report = run_validation(database_url)

    print(json.dumps(report.__dict__, indent=2, ensure_ascii=False, default=str))
    print(
        "\nValidation complete. Database left on disk (NEVER committed to git): "
        f"{db_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
