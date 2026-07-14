from __future__ import annotations

from sqlmodel import Session, select

from aios.models import Event, EventStatus, now_utc


class SQLiteEventBus:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_pending(self, limit: int = 100) -> list[Event]:
        statement = (
            select(Event)
            .where(Event.status == EventStatus.PENDING)
            .order_by(Event.created_at, Event.id)
            .limit(limit)
        )
        return list(self.session.exec(statement))

    def mark_processed(self, event_id: str) -> Event:
        event = self._get(event_id)
        event.status = EventStatus.PROCESSED
        event.processed_at = now_utc()
        event.last_error = None
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event

    def mark_failed(self, event_id: str, error: str) -> Event:
        event = self._get(event_id)
        event.status = EventStatus.PENDING
        event.attempt_count += 1
        event.last_error = error
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event

    def _get(self, event_id: str) -> Event:
        event = self.session.get(Event, event_id)
        if event is None:
            raise KeyError(event_id)
        return event
