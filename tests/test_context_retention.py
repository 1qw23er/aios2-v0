from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.audit import AuditLog
from aios.context_retention import delete_context_for_retention
from aios.db import get_engine, run_migrations
from aios.models import Project, Task, TaskContext
from aios.services import ServiceError


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def add_context(session: Session) -> TaskContext:
    project = Project(name="P", objective="O")
    session.add(project)
    session.flush()
    task = Task(project_id=project.id, title="T", description="I")
    session.add(task)
    session.flush()
    context = TaskContext(
        task_id=task.id,
        project_id=project.id,
        objective="O",
        instructions="I",
        context_hash="b" * 64,
    )
    session.add(context)
    session.commit()
    return context


def test_task_context_update_is_rejected(tmp_path: Path) -> None:
    url = database(tmp_path, "immutable.db")
    with Session(get_engine(url)) as session:
        context = add_context(session)
        with pytest.raises(IntegrityError, match="task_context is append-only"):
            session.exec(
                update(TaskContext)
                .where(TaskContext.id == context.id)
                .values(instructions="changed")
            )
            session.commit()


def test_controlled_retention_deletes_with_minimal_audit(tmp_path: Path) -> None:
    url = database(tmp_path, "retention.db")
    with Session(get_engine(url)) as session:
        context = add_context(session)

        delete_context_for_retention(
            session,
            context.id,
            actor="retention_admin",
            rationale="retention period expired",
        )

        assert session.get(TaskContext, context.id) is None
        audit = session.exec(select(AuditLog).where(AuditLog.action == "context.deleted")).one()
        assert audit.actor == "retention_admin"
        assert audit.after_snapshot == {
            "context_id": context.id,
            "context_hash": "b" * 64,
            "rationale": "retention period expired",
        }
        assert "instructions" not in audit.after_snapshot


@pytest.mark.parametrize("actor,rationale", [("", "reason"), ("admin", "")])
def test_controlled_retention_requires_actor_and_rationale(
    tmp_path: Path, actor: str, rationale: str
) -> None:
    url = database(tmp_path, f"invalid-{len(actor)}-{len(rationale)}.db")
    with Session(get_engine(url)) as session:
        context = add_context(session)
        with pytest.raises(ServiceError, match="required"):
            delete_context_for_retention(
                session,
                context.id,
                actor=actor,
                rationale=rationale,
            )
        assert session.get(TaskContext, context.id) is not None


def test_audit_failure_rolls_back_controlled_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = database(tmp_path, "rollback.db")
    with Session(get_engine(url)) as session:
        context = add_context(session)

        def fail_audit(*args: object, **kwargs: object) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr("aios.context_retention.append_audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            delete_context_for_retention(
                session,
                context.id,
                actor="admin",
                rationale="expired",
            )
        assert session.get(TaskContext, context.id) is not None
