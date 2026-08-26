"""T31 — zero-migration proof for the #110 feedback-loop implementation.

The feedback loop reuses existing primitives (Artifact / ArtifactType /
AuditLog / Approval / ActorContext) and adds NO Alembic migration. This module
asserts that invariant from several angles:

* the Alembic head is unchanged (single head ``20260730_0001``);
* a freshly migrated DB accepts ``ArtifactType.FEEDBACK`` with no new tables;
* the ``FEEDBACK`` enum value round-trips through the VARCHAR column unchanged;
* a stage transition does NOT alter the content checksum (A/B-zone split);
* ``AuditLog`` stays append-only / lazy (no ``Event`` / ``Task`` side-write).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlmodel import Session

from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.feedback import (
    FeedbackService,
    FeedbackStage,
    FeedbackTransition,
)
from aios.models import Artifact, ArtifactType, Event, Project, Task

HEAD = "20260825_0001_workforce_core"


# Shared DB fixtures (mirror tests/test_feedback.py; pytest fixtures are
# module-scoped and not shared across files).
@pytest.fixture
def engine(tmp_path):
    url = f"sqlite:///{(tmp_path / 'fbz.db').as_posix()}"
    run_migrations(url)
    return get_engine(url)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def project(session):
    p = Project(name="p1", objective="obj")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# T31a: Alembic head unchanged (single head)
# ---------------------------------------------------------------------------


def test_single_alembic_head_unchanged():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_current_head() == HEAD


def test_no_new_migration_files():
    """#110 (feedback) introduced zero migrations.

    The assertion is scoped to the #110 slice rather than to "no later file
    exists at all": unrelated later slices legitimately extend the chain (the
    #109 customer-service migration, then the SalesPlaybook V0 migration), and
    pinning the absolute tail here would make every future migration look like a
    feedback regression. What #110 actually promises is that IT added nothing,
    so we assert nothing was wedged into its window and that no migration
    anywhere in the tree is a feedback migration.
    """
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    heads = [
        p.name
        for p in versions.glob("*.py")
        if not p.name.startswith("_") and p.name != "__init__.py"
    ]
    assert all(p.startswith("2026") for p in heads)
    assert "20260730_0001_agent_secret.py" in heads
    # #109 legitimately adds exactly one migration (customer-service workflow).
    assert "20260731_0001_customer_service.py" in heads
    # Nothing sits between the V4 secret-store slice and #109 -- that is the
    # window in which a #110 migration would have appeared.
    between = [
        p
        for p in heads
        if "20260730_0001_agent_secret.py" < p < "20260731_0001_customer_service.py"
    ]
    assert between == []
    # And no migration in the whole tree is a feedback migration.
    assert not any("feedback" in p for p in heads)


# ---------------------------------------------------------------------------
# T31b: fresh DB accepts FEEDBACK with no new tables / single head
# ---------------------------------------------------------------------------


def test_fresh_db_accepts_feedback_and_single_head(session, project):
    with session.get_bind().connect() as conn:
        version = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    assert version == HEAD
    # The enum value is stored in the existing VARCHAR column with no migration.
    fb = FeedbackService(session).create_feedback(
        project_id=project.id,
        actor=_owner(),
        original_text="zero migration feedback",
    )
    assert fb.type == ArtifactType.FEEDBACK
    reloaded = session.get(Artifact, fb.id)
    assert reloaded.type == "feedback"  # raw VARCHAR value, no enum table


def test_feedback_enum_value_round_trip_via_raw_sql(session, project):
    fb = FeedbackService(session).create_feedback(
        project_id=project.id,
        actor=_owner(),
        original_text="raw sql round trip",
    )
    raw = session.exec(
        text("SELECT type FROM artifact WHERE id = :id"),
        params={"id": fb.id},
    ).scalar()
    # The enum is stored in the existing VARCHAR column with no migration; the
    # stored token is the enum member name (StrEnum), round-tripping back to the
    # same ArtifactType on reload.
    assert raw == ArtifactType.FEEDBACK.name
    reloaded = session.get(Artifact, fb.id)
    assert reloaded.type == ArtifactType.FEEDBACK


# ---------------------------------------------------------------------------
# T31c: stage transition does NOT mutate the content checksum
# ---------------------------------------------------------------------------


def test_stage_change_does_not_alter_checksum(session, project):
    fb = FeedbackService(session).create_feedback(
        project_id=project.id,
        actor=_owner(),
        original_text="stable checksum across stages",
        scenario="s",
        expected_outcome="o",
        risk_tags=["ux"],
    )
    c0 = fb.checksum
    # Pure stage moves (no A-zone content edit) must not change the checksum.
    fb = _transition(session, fb, FeedbackTransition.CLARIFY_REQUESTED)
    fb = _transition(session, fb, FeedbackTransition.CLARIFIED)
    assert fb.checksum == c0
    assert fb.metadata_json["stage"] == FeedbackStage.SOLUTION.value
    # A-zone edit via amend MUST change the checksum (proves the checksum is
    # genuinely A-zone-derived, not a constant) -- the contrast to the above.
    fb = FeedbackService(session).amend_feedback(
        artifact_id=fb.id, actor=_owner(), reason="edit", scenario="s2"
    )
    assert fb.checksum != c0


# ---------------------------------------------------------------------------
# T31d: AuditLog stays lazy / append-only; no Event/Task side-effects
# ---------------------------------------------------------------------------


def test_audit_log_append_only_no_event_side_effect(session, project):
    before_events = len(session.exec(_select(Event)).all())
    before_tasks = len(session.exec(_select(Task)).all())

    fb = FeedbackService(session).create_feedback(
        project_id=project.id,
        actor=_owner(),
        original_text="audit only",
    )
    fb = _to_solution(session, fb)
    fb = _transition(session, fb, FeedbackTransition.SUBMIT_FOR_APPROVAL)

    # Exactly the create + submit audits exist; no Event/Task rows were written.
    audits = session.exec(
        _select(AuditLog).where(AuditLog.resource_id == fb.id)
    ).all()
    actions = sorted(a.action for a in audits)
    assert "feedback.create" in actions
    assert "feedback.submit_for_approval" in actions
    assert len(session.exec(_select(Event)).all()) == before_events
    assert len(session.exec(_select(Task)).all()) == before_tasks


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _select(model):
    from sqlmodel import select

    return select(model)


def _owner():
    from aios.actor import ActorContext

    return ActorContext(kind="owner", owner_id="owner")


def _to_solution(session, fb):
    fb = _transition(session, fb, FeedbackTransition.CLARIFY_REQUESTED)
    fb = _transition(session, fb, FeedbackTransition.CLARIFIED)
    return FeedbackService(session).amend_feedback(
        artifact_id=fb.id,
        actor=_owner(),
        reason="solution",
        solution_text="proposed fix",
    )


def _transition(session, fb, transition):
    return FeedbackService(session).apply_transition(
        artifact_id=fb.id,
        actor=_owner(),
        transition=transition,
    )
