import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from aios.adapters.external import ExternalWorkstationAdapter, TaskPacket
from aios.audit import AuditLog, append_audit
from aios.db import get_engine, run_migrations
from aios.external_service import import_external_result
from aios.models import Artifact, Event, Project, Task, TaskStatus


def test_audit_sanitizes_secrets(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'audit.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        append_audit(
            session,
            actor="system",
            action="agent.updated",
            resource_type="agent",
            resource_id="agt_1",
            project_id=None,
            task_id=None,
            before={},
            after={"endpoint": "https://example", "api_key": "secret"},
            idempotency_key="audit-agent-1",
        )
        session.commit()
        audit = session.exec(select(AuditLog)).one()
        assert "secret" not in json.dumps(audit.after_snapshot)
        assert audit.after_snapshot["api_key"] == "[REDACTED]"


def test_external_import_is_atomic_and_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'external.db').as_posix()}"
    run_migrations(url)
    adapter = ExternalWorkstationAdapter(tmp_path / "outbox", tmp_path / "inbox")
    with Session(get_engine(url)) as session:
        project = Project(name="Content", objective="Publish")
        session.add(project)
        session.flush()
        task = Task(
            project_id=project.id,
            title="Research",
            description="Research",
            status=TaskStatus.WAITING_EXTERNAL,
        )
        session.add(task)
        session.commit()
        schema = {
            "type": "object",
            "required": ["result_id", "task_id", "summary", "claims", "artifacts"],
            "properties": {"task_id": {"const": task.id}, "summary": {"minLength": 1}},
        }
        adapter.export_task(
            TaskPacket(
                task_id=task.id,
                project={"id": project.id, "objective": project.objective},
                role="researcher",
                instructions="Research",
                output_schema=schema,
            ),
            "context",
        )
        result_path = tmp_path / "inbox" / "research.result.json"
        result_path.write_text(
            json.dumps(
                {
                    "result_id": "res_research_1",
                    "task_id": task.id,
                    "summary": "Findings",
                    "claims": [],
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )

        first = import_external_result(session, adapter, result_path)
        second = import_external_result(session, adapter, result_path)

        assert second.id == first.id
        assert session.get(Task, task.id).status == TaskStatus.DONE
        assert len(list(session.exec(select(Artifact)))) == 1
        assert len(list(session.exec(select(Event).where(Event.type == "task.completed")))) == 1
        assert len(list(session.exec(select(AuditLog)))) >= 1


def test_audit_failure_rolls_back_external_import(tmp_path: Path, monkeypatch) -> None:
    url = f"sqlite:///{(tmp_path / 'rollback.db').as_posix()}"
    run_migrations(url)
    adapter = ExternalWorkstationAdapter(tmp_path / "outbox", tmp_path / "inbox")
    with Session(get_engine(url)) as session:
        project = Project(name="Content", objective="Publish")
        session.add(project)
        session.flush()
        task = Task(project_id=project.id, title="Research", description="Research")
        session.add(task)
        session.commit()
        adapter.export_task(
            TaskPacket(
                task_id=task.id,
                project={"id": project.id},
                role="researcher",
                instructions="Research",
                output_schema={"type": "object"},
            )
        )
        path = tmp_path / "inbox" / "result.json"
        path.write_text(
            json.dumps(
                {
                    "result_id": "res_1",
                    "task_id": task.id,
                    "summary": "ok",
                    "claims": [],
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "aios.external_service.append_audit",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit failed")),
        )
        with pytest.raises(RuntimeError, match="audit failed"):
            import_external_result(session, adapter, path)
        assert list(session.exec(select(Artifact))) == []
