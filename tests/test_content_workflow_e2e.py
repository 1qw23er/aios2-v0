import json
from pathlib import Path

from sqlmodel import Session, select

from aios.adapters.external import ExternalWorkstationAdapter, TaskPacket
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.external_service import import_external_result
from aios.mock_agent import MockApiAgent
from aios.models import Approval, Artifact, Event, Project, RiskLevel, Task, TaskStatus
from aios.orchestrator import Orchestrator
from aios.schemas import ApprovalCreate
from aios.services import create_approval


def result_schema(task_id: str) -> dict:
    return {
        "type": "object",
        "required": ["result_id", "task_id", "summary", "claims", "artifacts"],
        "properties": {
            "result_id": {"type": "string"},
            "task_id": {"const": task_id},
            "summary": {"type": "string", "minLength": 1},
            "claims": {"type": "array"},
            "artifacts": {"type": "array"},
        },
    }


def import_external(
    session: Session,
    adapter: ExternalWorkstationAdapter,
    task: Task,
    project: Project,
    result_id: str,
) -> Artifact:
    adapter.export_task(
        TaskPacket(
            task_id=task.id,
            project={"id": project.id, "objective": project.objective},
            role="researcher" if task.title == "Research" else "writer",
            instructions=task.description,
            output_schema=result_schema(task.id),
        ),
        f"# {project.name}\n\n{project.objective}",
    )
    path = adapter.inbox / f"{result_id}.json"
    path.write_text(
        json.dumps(
            {
                "result_id": result_id,
                "task_id": task.id,
                "summary": f"Completed {task.title}",
                "claims": [],
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    return import_external_result(session, adapter, path)


def test_content_workflow_external_api_external_approval(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}"
    run_migrations(url)
    adapter = ExternalWorkstationAdapter(tmp_path / "outbox", tmp_path / "inbox")
    with Session(get_engine(url)) as session:
        project = Project(name="Campaign", objective="Publish article")
        session.add(project)
        session.flush()
        research = Task(
            project_id=project.id,
            title="Research",
            description="Research market",
            status=TaskStatus.WAITING_EXTERNAL,
        )
        session.add(research)
        session.flush()
        planning = Task(
            project_id=project.id,
            title="Planning",
            description="Create outline",
            depends_on=[research.id],
        )
        session.add(planning)
        session.flush()
        writing = Task(
            project_id=project.id,
            title="Writing",
            description="Write article",
            depends_on=[planning.id],
        )
        session.add(writing)
        session.flush()
        approval_task = Task(
            project_id=project.id,
            title="Approval",
            description="Approve publication",
            depends_on=[writing.id],
        )
        session.add(approval_task)
        session.commit()

        research_artifact = import_external(session, adapter, research, project, "res_research")
        assert (
            import_external(session, adapter, research, project, "res_research").id
            == research_artifact.id
        )
        Orchestrator(session).process_pending()
        assert session.get(Task, planning.id).status == TaskStatus.READY

        MockApiAgent(session).complete(planning.id, {"outline": ["Intro", "Body"]}, "mock-planning")
        Orchestrator(session).process_pending()
        assert session.get(Task, writing.id).status == TaskStatus.READY

        writing.status = TaskStatus.WAITING_EXTERNAL
        session.add(writing)
        session.commit()
        writing_artifact = import_external(session, adapter, writing, project, "res_writing")
        assert (
            import_external(session, adapter, writing, project, "res_writing").id
            == writing_artifact.id
        )
        Orchestrator(session).process_pending()
        assert session.get(Task, approval_task.id).status == TaskStatus.READY

        create_approval(
            session,
            ApprovalCreate(
                project_id=project.id,
                task_id=approval_task.id,
                action_type="publish",
                risk_level=RiskLevel.L4,
                rationale="CEO review required",
            ),
            "approval-publish",
        )

        task_ids = [research.id, planning.id, writing.id, approval_task.id]
    with Session(get_engine(url)) as session:
        statuses = [session.get(Task, task_id).status for task_id in task_ids]
        assert statuses == [TaskStatus.DONE, TaskStatus.DONE, TaskStatus.DONE, TaskStatus.READY]
        assert len(list(session.exec(select(Artifact)))) == 3
        assert len(list(session.exec(select(Approval)))) == 1
        audits = list(session.exec(select(AuditLog)))
        assert len(audits) == 7
        assert {audit.action for audit in audits} >= {
            "external_result.imported",
            "task.ready",
            "task.completed",
            "approval.requested",
        }
        events = list(session.exec(select(Event)))
        assert not any(event.type.startswith("external.publish") for event in events)
