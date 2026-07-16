"""Alpha-3.1 operational validation: end-to-end deterministic workflow (integration test).

This test imports the *single source of truth* ``run_validation`` from
``scripts/run_alpha_3_1_validation.py`` and asserts on its structured result.
It exercises only the existing AIOS architecture (no new services, models,
migrations, routing logic, or LLM use). It follows Issue #5 steps 1-11.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from aios.audit import AuditLog
from aios.db import get_engine
from aios.models import (
    Approval,
    ApprovalStatus,
    ExecutionAssignment,
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    Project,
    Task,
    TaskContext,
    TaskStatus,
)
from aios.orchestrator import Orchestrator
from scripts.run_alpha_3_1_validation import (
    APPROVAL_ID,
    FACT_SERIES,
    PLANNING_ID,
    PROJECT_ID,
    RESEARCH_ID,
    WRITING_ID,
    run_validation,
)


def test_alpha_3_1_end_to_end_workflow(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha-3-1.db"
    database_url = f"sqlite:///{db_path.as_posix()}"

    report = run_validation(database_url)

    # Step 2: four tasks exist.
    with Session(get_engine(database_url)) as session:
        tasks = list(session.exec(select(Task).where(Task.project_id == PROJECT_ID)))
        assert {t.id for t in tasks} == {RESEARCH_ID, PLANNING_ID, WRITING_ID, APPROVAL_ID}

        # Step 3: research/planning/writing capability-routed; L4 manual.
        routed = {d["task"]: d for d in report.routing_decisions}
        assert routed[RESEARCH_ID]["selected_agent"] == "agt_research"
        assert routed[PLANNING_ID]["selected_agent"] == "agt_planning"
        assert routed[WRITING_ID]["selected_agent"] == "agt_writing"
        assert all(not d["fallback_used"] for d in report.routing_decisions)

        # Step 4: L4 approval task was activated to READY by the orchestrator but
        # is MANUAL => never auto-assigned an agent (route_task returns None).
        assert session.get(Task, APPROVAL_ID).status == TaskStatus.READY
        l4_assignment = list(
            session.exec(
                select(ExecutionAssignment).where(ExecutionAssignment.task_id == APPROVAL_ID)
            )
        )
        assert l4_assignment == []

        # Step 5: a TaskContext was generated for research/planning/writing.
        assert RESEARCH_ID in report.context_hashes
        assert PLANNING_ID in report.context_hashes
        assert WRITING_ID in report.context_hashes
        contexts = list(
            session.exec(select(TaskContext).where(TaskContext.project_id == PROJECT_ID))
        )
        assert len(contexts) >= 3

        # Step 7: research artifact explicitly approved by a human.
        assert any(
            "Explicit manual approval of research Artifact" in line
            for line in report.manual_interventions
        )

        # Step 8: two candidates submitted; one approved, one rejected.
        assert len(report.knowledge_provenance) == 2
        approved = [p for p in report.knowledge_provenance if p["decision"] == "approve"]
        rejected = [p for p in report.knowledge_provenance if p["decision"] == "reject"]
        assert len(approved) == 1 and len(rejected) == 1
        assert approved[0]["series_id"] == FACT_SERIES

        # Step 9: only the approved KnowledgeFact exists; rejected produced none.
        facts = list(session.exec(select(KnowledgeFact)))
        assert len(facts) == 1
        assert facts[0].status == KnowledgeFactStatus.APPROVED
        assert facts[0].id == approved[0]["fact_id"]
        candidates = list(session.exec(select(KnowledgeCandidate)))
        assert len(candidates) == 2
        assert any(
            c.status == KnowledgeCandidateStatus.REJECTED for c in candidates
        )

        # Step 9: approved fact flows into planning + writing TaskContexts.
        for task_id in (PLANNING_ID, WRITING_ID):
            ctx = next(c for c in contexts if c.task_id == task_id)
            fact_ids = {f["fact_id"] for f in ctx.approved_facts}
            assert facts[0].id in fact_ids

        # Step 10: workflow stopped at L4 PENDING.
        assert report.workflow_stopped_at.startswith("L4 approval PENDING")
        approvals = list(
            session.exec(
                select(Approval).where(
                    Approval.project_id == PROJECT_ID,
                    Approval.status == ApprovalStatus.PENDING,
                )
            )
        )
        assert len(approvals) == 1

        # Coordination conclusion is evidence-based and neutral: the manual L4
        # gate was preserved (no auto-assigned agent, approval left PENDING).
        assert report.manual_gates_preserved is True

        # Step 6: external export/import paths recorded.
        assert any("task_packet.json" in p for p in report.exported_packages)
        assert any("res_research" in p for p in report.imported_packages)

        # Counts are sane.
        assert report.counts["tasks"] == 4
        assert report.counts["execution_assignments"] == 3
        assert report.counts["knowledge_facts"] == 1
        assert report.counts["knowledge_candidates"] == 2
        assert report.counts["approvals_pending"] == 1
        assert report.counts["audit_logs"] >= 10
        assert len(list(session.exec(select(AuditLog)))) >= 10

        # Step 11: persistence already re-verified inside run_validation via a new
        # session; assert the reopened session sees the same entities.
        assert session.get(Project, PROJECT_ID) is not None
        ctx_count = len(
            list(session.exec(select(TaskContext).where(TaskContext.project_id == PROJECT_ID)))
        )
        assert ctx_count >= 3

    # Orchestrator did not create spurious ready events for the manual task.
    with Session(get_engine(database_url)) as session:
        orchestrator = Orchestrator(session)
        pending = orchestrator.process_pending()
        # Manual L4 task remains READY (activated) with no auto-assignment.
        assert session.get(Task, APPROVAL_ID).status == TaskStatus.READY
        assert pending == []
