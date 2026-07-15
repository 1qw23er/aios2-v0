from pathlib import Path

from sqlmodel import Session, select

from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.schemas import ProjectCreate
from aios.services import create_project


def test_project_creation_writes_audit_in_same_transaction(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'project-audit.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        project = create_project(
            session, ProjectCreate(name="Launch", objective="Ship"), "project-create-1"
        )
        audit = session.exec(select(AuditLog)).one()
        assert audit.action == "project.created"
        assert audit.resource_id == project.id
        assert audit.idempotency_key == "audit:project-create-1"
