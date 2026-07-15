from pathlib import Path

from sqlmodel import Session, select

from aios.db import get_engine, run_migrations
from aios.models import Event, Project, Task, TaskStatus
from aios.orchestrator import Orchestrator, complete_task


def test_completion_waits_for_all_dependencies_and_then_activates(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'orchestrator.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        project = Project(name="Content", objective="Publish")
        session.add(project)
        session.flush()
        research = Task(project_id=project.id, title="Research", description="Research")
        compare = Task(project_id=project.id, title="Compare", description="Compare")
        session.add_all([research, compare])
        session.flush()
        planning = Task(
            project_id=project.id,
            title="Plan",
            description="Plan",
            depends_on=[research.id, compare.id],
        )
        session.add(planning)
        session.commit()

        complete_task(session, research.id, "complete-research")
        Orchestrator(session).process_pending()
        assert session.get(Task, planning.id).status == TaskStatus.BACKLOG

        complete_task(session, compare.id, "complete-compare")
        Orchestrator(session).process_pending()
        assert session.get(Task, planning.id).status == TaskStatus.READY


def test_reprocessing_completion_is_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'idempotent.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        project = Project(name="Content", objective="Publish")
        session.add(project)
        session.flush()
        source = Task(project_id=project.id, title="Source", description="Source")
        session.add(source)
        session.flush()
        downstream = Task(
            project_id=project.id,
            title="Next",
            description="Next",
            depends_on=[source.id],
        )
        session.add(downstream)
        session.commit()

        event = complete_task(session, source.id, "complete-source")
        orchestrator = Orchestrator(session)
        orchestrator.process_event(event.id)
        orchestrator.process_event(event.id)

        ready_events = list(session.exec(select(Event).where(Event.type == "task.ready")))
        assert len(ready_events) == 1
        assert session.get(Task, downstream.id).status == TaskStatus.READY
