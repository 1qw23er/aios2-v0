from __future__ import annotations

from sqlmodel import Session, select

from aios.models import Event, EventStatus, Task, TaskStatus, now_utc
from aios.services import ServiceError, append_event


def complete_task(session: Session, task_id: str, idempotency_key: str) -> Event:
    existing = session.exec(
        select(Event).where(Event.idempotency_key == idempotency_key)
    ).first()
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
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(event)
    return event


class Orchestrator:
    def __init__(self, session: Session) -> None:
        self.session = session

    def process_pending(self, limit: int = 100) -> list[Event]:
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
        for event in events:
            self.process_event(event.id)
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
                if self.session.exec(
                    select(Event).where(Event.idempotency_key == ready_key)
                ).first() is None:
                    append_event(
                        self.session,
                        project_id=task.project_id,
                        task_id=task.id,
                        event_type="task.ready",
                        idempotency_key=ready_key,
                        payload={"source_event_id": source_event.id},
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
