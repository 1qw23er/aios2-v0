from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from aios.audit import append_audit
from aios.models import Event, EventStatus, Task, TaskStatus, now_utc
from aios.services import ServiceError, append_event


@dataclass
class ProcessResult:
    """Detailed result of ``Orchestrator.process_pending``.

    ``activated_task_ids`` contains ONLY the tasks this invocation actually
    moved to READY (committed inside ``process_event``). It is strict against
    concurrency: a caller that loses the activation race for a shared source
    event reports an empty list, because its ``process_event`` call is a no-op
    (the source event is already PROCESSED).
    """

    events: list[Event]
    activated_task_ids: list[str]


def complete_task(session: Session, task_id: str, idempotency_key: str) -> Event:
    existing = session.exec(select(Event).where(Event.idempotency_key == idempotency_key)).first()
    if existing is not None:
        if existing.type != "task.completed" or existing.task_id != task_id:
            raise ServiceError(409, "Idempotency key conflicts with another operation")
        return existing
    task = session.get(Task, task_id)
    if task is None:
        raise ServiceError(404, "Task not found")
    before = task.status
    task.status = TaskStatus.DONE
    task.updated_at = now_utc()
    try:
        session.add(task)
        event = append_event(
            session,
            project_id=task.project_id,
            task_id=task.id,
            event_type="task.completed",
            idempotency_key=idempotency_key,
            payload={"before": before.value, "after": TaskStatus.DONE.value},
        )
        append_audit(
            session,
            actor="orchestrator",
            action="task.completed",
            resource_type="task",
            resource_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            before={"status": before.value},
            after={"status": TaskStatus.DONE.value},
            idempotency_key=f"audit:{idempotency_key}",
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(event)
    return event


class Orchestrator:
    def __init__(self, session: Session) -> None:
        self.session = session

    def process_pending(
        self, limit: int = 100, return_detailed: bool = False
    ) -> list[Event] | ProcessResult:
        events = list(
            self.session.exec(
                select(Event)
                .where(
                    Event.status == EventStatus.PENDING,
                    Event.type == "task.completed",
                )
                .order_by(Event.created_at, Event.id)
                .limit(limit)
            )
        )
        activated_task_ids: list[str] = []
        for event in events:
            activated = self.process_event(event.id)
            activated_task_ids.extend(task.id for task in activated)
        if return_detailed:
            return ProcessResult(events=events, activated_task_ids=activated_task_ids)
        return events

    def process_event(self, event_id: str) -> list[Task]:
        source_event = self.session.get(Event, event_id)
        if source_event is None:
            raise KeyError(event_id)
        if source_event.status == EventStatus.PROCESSED:
            return []
        if source_event.type != "task.completed":
            raise ValueError(f"Unsupported event type: {source_event.type}")

        tasks = list(
            self.session.exec(
                select(Task).where(
                    Task.project_id == source_event.project_id,
                    Task.status == TaskStatus.BACKLOG,
                )
            )
        )
        activated: list[Task] = []
        try:
            for task in tasks:
                if source_event.task_id not in task.depends_on:
                    continue
                dependencies = [self.session.get(Task, task_id) for task_id in task.depends_on]
                if not dependencies or any(
                    dependency is None or dependency.status != TaskStatus.DONE
                    for dependency in dependencies
                ):
                    continue
                task.status = TaskStatus.READY
                task.updated_at = now_utc()
                self.session.add(task)
                ready_key = f"orchestrator:{source_event.id}:ready:{task.id}"
                if (
                    self.session.exec(
                        select(Event).where(Event.idempotency_key == ready_key)
                    ).first()
                    is None
                ):
                    append_event(
                        self.session,
                        project_id=task.project_id,
                        task_id=task.id,
                        event_type="task.ready",
                        idempotency_key=ready_key,
                        payload={"source_event_id": source_event.id},
                    )
                append_audit(
                    self.session,
                    actor="orchestrator",
                    action="task.ready",
                    resource_type="task",
                    resource_id=task.id,
                    project_id=task.project_id,
                    task_id=task.id,
                    before={"status": TaskStatus.BACKLOG.value},
                    after={"status": TaskStatus.READY.value},
                    idempotency_key=f"audit:{ready_key}",
                )
                activated.append(task)
            source_event.status = EventStatus.PROCESSED
            source_event.processed_at = now_utc()
            self.session.add(source_event)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return activated
