from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session

from aios.db import get_engine, run_migrations
from aios.event_bus import SQLiteEventBus
from aios.models import Event, EventStatus, Project


@pytest.fixture
def session(tmp_path: Path) -> Session:
    url = f"sqlite:///{(tmp_path / 'events.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as database_session:
        yield database_session


def seed_events(session: Session) -> list[Event]:
    project = Project(name="Launch", objective="Ship V0")
    session.add(project)
    session.flush()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        Event(
            project_id=project.id,
            type="first",
            idempotency_key="event-1",
            created_at=start,
        ),
        Event(
            project_id=project.id,
            type="second",
            idempotency_key="event-2",
            created_at=start + timedelta(seconds=1),
        ),
    ]
    session.add_all(events)
    session.commit()
    return events


def test_pending_events_are_ordered_and_transition_to_processed(session: Session) -> None:
    events = seed_events(session)
    bus = SQLiteEventBus(session)

    pending = bus.list_pending(limit=10)
    processed = bus.mark_processed(events[0].id)

    assert [event.id for event in pending] == [events[0].id, events[1].id]
    assert processed.status == EventStatus.PROCESSED
    assert processed.processed_at is not None
    assert [event.id for event in bus.list_pending()] == [events[1].id]


def test_failed_event_records_attempt_and_error(session: Session) -> None:
    event = seed_events(session)[0]

    failed = SQLiteEventBus(session).mark_failed(event.id, "adapter unavailable")

    assert failed.status == EventStatus.PENDING
    assert failed.attempt_count == 1
    assert failed.last_error == "adapter unavailable"


def test_missing_event_transition_is_reported(session: Session) -> None:
    with pytest.raises(KeyError, match="evt_missing"):
        SQLiteEventBus(session).mark_processed("evt_missing")
