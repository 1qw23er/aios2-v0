"""Contract tests for the debt & hygiene slice (W1-W7 checkpoint, direction 1).

Baseline: ``docs/workforce/Workforce_Architecture_Checkpoint_W1-W7.md``
(merged as PR#17, ``55501ab``). This slice resolves four register items:

* DR-W7-6 -- a global ``IntegrityError`` -> 409 translator in the API layer.
  (W7-I13 pinned the ABSENCE of this handler; the checkpoint reclassified
  DR-W7-6 as an ARCHITECTURE decision and this slice implements it, so I13 was
  amended on purpose. These tests pin the NEW contract behaviorally.)
* G-E -- candidate quality signalling consults the agent trust axis, as
  ADVISORY TEXT ONLY (never a gate, never a score component), surfaced on the
  Recommendation as ``trust_advisory`` (mirrors F-R5 ``cost_advisory``).
* FILLED / ``Task.actual_cost`` are comment/doc-only fixes (no behavior), so
  they have no tests here; the pre-existing D-3 lock test still guards FILLED.

Helpers mirror ``tests/test_workforce_recommendation_w3c.py`` so this file is
self-contained.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from aios.api.app import create_app, integrity_error_handler
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentTrustLevel,
    Candidate,
    JobVersion,
    RecommendationStatus,
)
from aios.workforce import (
    compute_match,
    discover_candidates,
    evaluate_candidate,
)
from aios.workforce_recommendation import (
    _DELEGATION_CLEARING,
    _build_trust_advisory,
    recommend_candidate,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_workforce_recommendation_w3c.py)
# ---------------------------------------------------------------------------


def _db(url: str) -> Session:
    run_migrations(url)
    return Session(get_engine(url))


def _seed_capability(session: Session, name: str) -> None:
    from aios.models import Capability

    session.add(Capability(name=name, description=f"{name} capability"))
    session.commit()


def _seed_agent(
    session: Session,
    name: str,
    capabilities: dict[str, int],
    *,
    trust_level: AgentTrustLevel = AgentTrustLevel.INTERNAL,
) -> Agent:
    from sqlmodel import select

    from aios.models import Capability

    agent = Agent(
        name=name, role=name, adapter_type=AdapterType.EXTERNAL
    )
    agent.trust_level = trust_level
    session.add(agent)
    session.flush()
    for cap_name, priority in capabilities.items():
        cap = session.exec(
            select(Capability).where(Capability.name == cap_name)
        ).first()
        assert cap is not None, f"capability must be seeded first: {cap_name}"
        session.add(
            AgentCapability(
                agent_id=agent.id,
                capability_id=cap.id,
                priority=priority,
            )
        )
    session.commit()
    return agent


def _prepared(
    session: Session,
    *,
    trust_level: AgentTrustLevel,
    cap_name: str = "writing",
) -> Candidate:
    """Discovered + EVALUATED candidate with a COMPUTED Match (W3-C input)."""
    from aios.models import CapabilityRequirement  # noqa: F401 (chain via spec)
    from aios.workforce import (
        create_business_goal,
        create_job,
        create_required_work,
    )

    _seed_capability(session, cap_name)
    _seed_agent(session, "A", {cap_name: 80}, trust_level=trust_level)
    goal = create_business_goal(session, "增长北极星", target_outcome="新增注册 +20%")
    rw = create_required_work(
        session, goal.id, "公众号内容生产", rationale="内容带来自然注册"
    )
    job = create_job(
        session,
        rw.id,
        "内容初稿研究员",
        role_summary="把选题做成初稿",
        capability_names=[cap_name],
    )
    session.commit()
    head = session.get(JobVersion, job.head_version_id)
    assert head is not None
    cands = discover_candidates(session, head.id)
    session.commit()
    assert len(cands) == 1
    cand = cands[0]
    evaluate_candidate(session, cand.id)
    session.commit()
    compute_match(session, cand.id, head.id)
    session.commit()
    return cand


# ---------------------------------------------------------------------------
# DR-W7-6: IntegrityError -> 409 (never 500), generic detail
# ---------------------------------------------------------------------------


def test_integrity_error_handler_returns_409_with_generic_detail() -> None:
    """Unit: the handler maps IntegrityError to exactly 409 and never leaks
    DB internals (statement text / constraint names) in the detail."""
    exc = IntegrityError(
        "INSERT INTO whatever ... UNIQUE constraint failed: x.y",
        None,
        Exception("UNIQUE constraint failed: x.y"),
    )
    resp = integrity_error_handler(request=None, exc=exc)  # type: ignore[arg-type]
    assert resp.status_code == 409
    import json

    payload = json.loads(bytes(resp.body))
    detail = payload["detail"]
    assert isinstance(detail, str) and detail
    assert "UNIQUE" not in detail
    assert "whatever" not in detail
    assert "x.y" not in detail


def test_create_app_registers_integrity_handler() -> None:
    """create_app registers the DR-W7-6 handler (no silent regression)."""
    app = create_app()
    assert IntegrityError in app.exception_handlers
    assert app.exception_handlers[IntegrityError] is integrity_error_handler


def test_integrity_error_end_to_end_409_via_test_client() -> None:
    """End-to-end: a route raising IntegrityError yields HTTP 409, not 500."""
    probe = FastAPI()

    @probe.get("/boom")
    def boom() -> None:
        raise IntegrityError("INSERT ...", None, Exception("UNIQUE ..."))

    probe.add_exception_handler(IntegrityError, integrity_error_handler)
    client = TestClient(probe, raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "resource conflict: integrity constraint violated"
    }


# ---------------------------------------------------------------------------
# G-E: trust advisory (advisory text only -- never a gate, never a score)
# ---------------------------------------------------------------------------


def test_trust_advisory_emitted_for_experimental_agent(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'trust1.db').as_posix()}"
    with _db(url) as session:
        cand = _prepared(session, trust_level=AgentTrustLevel.EXPERIMENTAL)
        rec = recommend_candidate(session, cand.id)
        session.commit()

        assert rec.status == RecommendationStatus.PROPOSED  # advisory never gates
        assert rec.trust_advisory is not None
        assert "experimental" in rec.trust_advisory
        assert "advisory only" in rec.trust_advisory
        # Text-only discipline (mirrors cost_advisory): no digits, no score.
        assert not any(ch.isdigit() for ch in rec.trust_advisory)
        # The recommendation itself is untouched by the advisory; the candidate
        # advanced EVALUATED -> RECOMMENDED exactly as the F-R1a gate dictates.
        session.refresh(cand)
        assert cand.status.value == "recommended"


def test_trust_advisory_none_for_delegation_clearing_agents(tmp_path: Path) -> None:
    for i, level in enumerate(
        (AgentTrustLevel.INTERNAL, AgentTrustLevel.VERIFIED_EXTERNAL)
    ):
        url = f"sqlite:///{(tmp_path / f'trust2_{i}.db').as_posix()}"
        with _db(url) as session:
            cand = _prepared(session, trust_level=level)
            rec = recommend_candidate(session, cand.id)
            session.commit()
            assert rec.trust_advisory is None, level
            assert rec.status == RecommendationStatus.PROPOSED


def test_trust_advisory_missing_agent_is_none_not_fabricated(tmp_path: Path) -> None:
    """No agent row -> None (fail-silent advisory, never a fabricated text).

    Uses an unattached Candidate probe (never added to the session) so the
    probe's bogus agent_id cannot trip the FK via autoflush."""
    url = f"sqlite:///{(tmp_path / 'trust3.db').as_posix()}"
    with _db(url) as session:
        cand = _prepared(session, trust_level=AgentTrustLevel.EXPERIMENTAL)
        probe = Candidate(
            agent_id="agent-does-not-exist",
            job_id=cand.job_id,
            job_version_id=cand.job_version_id,
        )
        assert _build_trust_advisory(session, probe) is None


def test_trust_advisory_reads_registry_live_not_snapshot(tmp_path: Path) -> None:
    """SSoT + snapshot semantics: no trust data is copied onto Candidate; the
    advisory is computed by LIVE registry read at recommend time, and the
    Recommendation row keeps the advisory text captured at that moment
    (mirrors cost_advisory: a replay/§8 never rewrites the historical row)."""
    url = f"sqlite:///{(tmp_path / 'trust4.db').as_posix()}"
    with _db(url) as session:
        cand = _prepared(session, trust_level=AgentTrustLevel.EXPERIMENTAL)
        # No trust_level column was snapshotted onto the Candidate row.
        cols = {c.name for c in Candidate.__table__.columns}
        assert "trust_level" not in cols
        rec = recommend_candidate(session, cand.id)
        session.commit()
        assert rec.trust_advisory is not None
        # Promote the agent's trust: the LIVE advisory read flips to None...
        agent = session.get(Agent, cand.agent_id)
        assert agent is not None
        agent.trust_level = AgentTrustLevel.VERIFIED_EXTERNAL
        session.commit()
        assert _build_trust_advisory(session, cand) is None
        # ...while the historical Recommendation row keeps its snapshot text.
        session.expire(rec)
        session.refresh(rec)
        assert rec.trust_advisory is not None


def test_delegation_clearing_set_matches_delegation_boundary() -> None:
    """Drift pin: workforce's re-declared clearing set must equal delegation's
    ``_TRUST_DELEGABLE`` byte-for-byte (string values). The src side must NOT
    import delegation (W7-I1), so the comparison lives on the test side only."""
    from aios.delegation import _TRUST_DELEGABLE

    assert {level.value for level in _DELEGATION_CLEARING} == {
        level.value for level in _TRUST_DELEGABLE
    }


def test_recommendation_has_trust_advisory_column_after_migration() -> None:
    """The additive column exists on the mapped model and in the migration
    chain head (single head: 20260906_0001_recommendation_trust_advisory)."""
    from aios.models import Recommendation

    cols = {c.name for c in Recommendation.__table__.columns}
    assert "trust_advisory" in cols
    versions = ROOT / "alembic" / "versions"
    assert (versions / "20260906_0001_recommendation_trust_advisory.py").exists()
