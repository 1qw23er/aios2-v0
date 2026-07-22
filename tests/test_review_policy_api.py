from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.api.app import create_app
from aios.api.security import authenticate_owner
from aios.audit import AuditLog
from aios.db import get_database_url, get_engine
from aios.models import (
    AgentTrustLevel,
    Artifact,
    ArtifactType,
    Project,
    ReviewAssignment,
    ReviewDimension,
    ReviewPolicy,
    Task,
)
from aios.review import create_review_policy, dispatch_reviews_for_artifact
from aios.services import ServiceError


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """Owner-authenticated client. POST /review-policies and GET /audit are both
    gated by ``authenticate_owner`` (#74); the fixture installs the trusted-owner
    override (the SAME mechanism production routes resolve to) and never relaxes
    the create route itself."""
    database_path = tmp_path / "review_policy.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    app = create_app()
    app.dependency_overrides[authenticate_owner] = lambda: ActorContext(
        kind="owner", owner_id="owner"
    )
    with TestClient(app) as test_client:
        yield test_client


VALID_POLICY = {
    "name": "editorial-v1",
    "applies_to": "editorial",
    "dimensions": ["fact_correctness", "brand_strategy"],
    "required_reviewer_trust": "verified_external",
    "required_reviewers": 2,
    "max_revisions": 2,
    "enabled": True,
}


def test_create_review_policy_returns_201(client: TestClient) -> None:
    resp = client.post("/review-policies", json=VALID_POLICY)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "editorial-v1"
    assert body["dimensions"] == ["fact_correctness", "brand_strategy"]
    assert body["required_reviewer_trust"] == "verified_external"
    assert body["required_reviewers"] == 2


def test_create_equivalent_is_idempotent_returns_200(client: TestClient) -> None:
    first = client.post("/review-policies", json=VALID_POLICY)
    assert first.status_code == 201
    first_id = first.json()["id"]
    second = client.post("/review-policies", json=VALID_POLICY)
    assert second.status_code == 200
    assert second.json()["id"] == first_id


def test_create_conflicting_config_returns_409(client: TestClient) -> None:
    assert client.post("/review-policies", json=VALID_POLICY).status_code == 201
    conflicting = dict(VALID_POLICY, dimensions=["fact_correctness", "risk"])
    resp = client.post("/review-policies", json=conflicting)
    assert resp.status_code == 409


def test_create_empty_dimensions_returns_400(client: TestClient) -> None:
    resp = client.post("/review-policies", json={**VALID_POLICY, "dimensions": []})
    assert resp.status_code == 400


def test_create_invalid_trust_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/review-policies", json={**VALID_POLICY, "required_reviewer_trust": "bogus"}
    )
    assert resp.status_code == 400


def test_dispatch_unknown_policy_still_404(client: TestClient) -> None:
    # The dispatch contract is unchanged: an explicit, existing policy_id is
    # required; a missing policy keeps returning 404 (no implicit creation).
    resp = client.post(
        "/artifacts/does-not-exist/reviews/dispatch",
        json={"policy_id": "rp_does_not_exist"},
    )
    assert resp.status_code == 404


def test_create_writes_audit_log(client: TestClient) -> None:
    created = client.post("/review-policies", json=VALID_POLICY)
    assert created.status_code == 201
    policy_id = created.json()["id"]

    audit = client.get("/audit", params={"action": "review_policy.created"})
    assert audit.status_code == 200
    items = audit.json()["items"]
    matches = [
        it
        for it in items
        if it["resource_type"] == "review_policy" and it["resource_id"] == policy_id
    ]
    assert matches
    # The audit actor is the authenticated owner (AIOS_OWNER_ID), never a raw
    # actor string leaked from the request.
    assert all(it["actor"] == "owner" for it in matches)
    # No secret / material leakage in the redacted audit projection.
    forbidden = {
        "body",
        "statement",
        "prompt",
        "content",
        "feedback",
        "reason",
        "input",
        "output",
        "api_key",
        "secret",
        "snapshot",
    }
    for it in items:
        assert not (set(it.get("safe_delta", {}).keys()) & forbidden)


# --- name + identity rules (locked design #3) -------------------------------


def test_whitespace_name_is_canonicalized_and_idempotent(client: TestClient) -> None:
    """Leading/trailing whitespace is stripped and stored as the canonical name;
    a re-create with the stripped name returns the SAME policy (200)."""
    padded = dict(VALID_POLICY, name="  editorial-v1  ")
    first = client.post("/review-policies", json=padded)
    assert first.status_code == 201
    first_id = first.json()["id"]
    assert first.json()["name"] == "editorial-v1"  # stored stripped

    reopen = client.post("/review-policies", json=dict(VALID_POLICY, name="editorial-v1"))
    assert reopen.status_code == 200
    assert reopen.json()["id"] == first_id


def test_empty_name_rejected(client: TestClient) -> None:
    resp = client.post("/review-policies", json={**VALID_POLICY, "name": ""})
    assert resp.status_code == 400


def test_case_sensitive_names_are_distinct(client: TestClient) -> None:
    """Case-sensitive: 'editorial-v1' and 'Editorial-v1' are different names."""
    lower = client.post("/review-policies", json=dict(VALID_POLICY, name="editorial-v1"))
    upper = client.post(
        "/review-policies", json=dict(VALID_POLICY, name="Editorial-v1")
    )
    assert lower.status_code == 201
    assert upper.status_code == 201
    assert lower.json()["id"] != upper.json()["id"]


# --- dimensions + reviewers invariants (locked design #4) --------------------


def test_dimensions_order_is_equivalent(client: TestClient) -> None:
    """Request order of dimensions does not affect equivalence."""
    a = client.post(
        "/review-policies",
        json=dict(VALID_POLICY, dimensions=["fact_correctness", "brand_strategy"]),
    )
    b = client.post(
        "/review-policies",
        json=dict(VALID_POLICY, dimensions=["brand_strategy", "fact_correctness"]),
    )
    assert a.status_code == 201
    assert b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_duplicate_dimensions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/review-policies",
        json=dict(VALID_POLICY, dimensions=["fact_correctness", "fact_correctness"]),
    )
    assert resp.status_code == 400


def test_required_reviewers_must_equal_dimension_count(client: TestClient) -> None:
    """One-review-task-one-dimension: required_reviewers must equal len(dimensions)."""
    resp = client.post(
        "/review-policies",
        json=dict(VALID_POLICY, dimensions=["fact_correctness", "brand_strategy", "risk"],
                  required_reviewers=2),
    )
    assert resp.status_code == 400


def test_max_revisions_negative_rejected(client: TestClient) -> None:
    resp = client.post("/review-policies", json={**VALID_POLICY, "max_revisions": -1})
    assert resp.status_code == 400


# --- dispatch fail-closed on illegal stored policy (locked design #5) --------


def test_dispatch_rejects_illegal_policy_fail_closed(client) -> None:
    """dispatch_reviews_for_artifact must fail-closed (422) on a stored/legacy
    policy that violates the invariant (required_reviewers != len(dimensions))."""
    engine = get_engine(get_database_url())
    with Session(engine) as session:
        project = session.exec(select(Project)).first()
        if project is None:
            project = Project(name="dispatch-fc", objective="objective")
            session.add(project)
            session.commit()
            session.refresh(project)
        artifact = Artifact(
            project_id=project.id,
            type=ArtifactType.JSON,
            uri="exec://dispatch-fc",
            checksum="dispatch-fc",
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        # Illegal: 1 dimension but required_reviewers=3.
        policy = ReviewPolicy(
            name="illegal-legacy",
            dimensions=[ReviewDimension.FACT_CORRECTNESS.value],
            required_reviewer_trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            required_reviewers=3,
        )
        session.add(policy)
        session.commit()
        session.refresh(policy)
        with pytest.raises(ServiceError) as exc:
            dispatch_reviews_for_artifact(
                session, target_artifact_id=artifact.id, policy=policy
            )
        assert exc.value.status_code == 422


def test_dispatch_rejects_non_canonical_stored_policy_fail_closed(client) -> None:
    """A stored ReviewPolicy whose name is NOT canonical (leading/trailing
    whitespace) must be rejected fail-closed (422) and must NOT produce any
    ReviewAssignment, Review Task, or ``review.task_created`` dispatch audit --
    the app-layer must not silently strip() a legacy/corrupt DB value."""
    engine = get_engine(get_database_url())
    with Session(engine) as session:
        project = session.exec(select(Project)).first()
        if project is None:
            project = Project(name="dispatch-nc", objective="objective")
            session.add(project)
            session.commit()
            session.refresh(project)
        artifact = Artifact(
            project_id=project.id,
            type=ArtifactType.JSON,
            uri="exec://dispatch-nc",
            checksum="dispatch-nc",
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        # Non-canonical stored name: whitespace the create path would otherwise
        # strip, but a legacy/corrupt DB row must NOT be silently re-canonicalized.
        policy = ReviewPolicy(
            name=" editorial-v1 ",
            dimensions=[ReviewDimension.FACT_CORRECTNESS.value],
            required_reviewer_trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            required_reviewers=1,
        )
        session.add(policy)
        session.commit()
        session.refresh(policy)

        with pytest.raises(ServiceError) as exc:
            dispatch_reviews_for_artifact(
                session, target_artifact_id=artifact.id, policy=policy
            )
        assert exc.value.status_code == 422

        # Nothing was materialized: no bindings, no review tasks, no dispatch audit.
        session.expire_all()
        assert (
            session.exec(
                select(ReviewAssignment).where(
                    ReviewAssignment.target_artifact_id == artifact.id
                )
            ).all()
            == []
        )
        assert (
            session.exec(
                select(Task).where(Task.project_id == project.id)
            ).all()
            == []
        )
        # No ``review.task_created`` (the dispatch success audit) was written.
        assert (
            session.exec(
                select(AuditLog).where(
                    AuditLog.action == "review.task_created",
                    AuditLog.resource_type == "task",
                )
            ).all()
            == []
        )


# --- concurrent idempotency (locked design #8) ------------------------------


def test_concurrent_equivalent_creation_one_row_one_audit(client) -> None:
    """Two racing equivalent creates converge to ONE row, ONE created audit, and
    the responses map to 201 + 200 (created vs idempotent)."""
    import threading

    engine = get_engine(get_database_url())
    barrier = threading.Barrier(2)
    results: dict[str, tuple[str, bool]] = {}

    def worker(key: str) -> None:
        with Session(engine) as s:
            barrier.wait()
            try:
                _, created = create_review_policy(
                    s,
                    actor=ActorContext(kind="owner", owner_id="owner"),
                    name="concurrent-eq",
                    dimensions=["fact_correctness", "brand_strategy"],
                    required_reviewer_trust="verified_external",
                    required_reviewers=2,
                )
                results[key] = ("ok", created)
            except ServiceError as e:
                results[key] = ("err", e.status_code)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["a"][0] == "ok"
    assert results["b"][0] == "ok"
    # Exactly one policy row, exactly one created audit.
    with Session(engine) as s:
        rows = s.exec(
            select(ReviewPolicy).where(ReviewPolicy.name == "concurrent-eq")
        ).all()
        assert len(rows) == 1
        audits = s.exec(
            select(AuditLog).where(AuditLog.action == "review_policy.created")
        ).all()
        assert len(audits) == 1
    # One created=True (201) and one created=False (200).
    created_flags = {results["a"][1], results["b"][1]}
    assert created_flags == {True, False}


def test_concurrent_conflicting_creation_one_row_one_audit(client) -> None:
    """Two racing conflicting creates: one wins (201), the other converges to a
    409 conflict -- still ONE row and ONE created audit."""
    import threading

    engine = get_engine(get_database_url())
    barrier = threading.Barrier(2)
    results: dict[str, tuple[str, object]] = {}

    def worker(key: str, dimensions: list[str]) -> None:
        from aios.actor import ActorContext as _AC
        from aios.review import create_review_policy as _crp

        with Session(engine) as s:
            barrier.wait()
            try:
                _, created = _crp(
                    s,
                    actor=_AC(kind="owner", owner_id="owner"),
                    name="concurrent-conf",
                    dimensions=dimensions,
                    required_reviewer_trust="verified_external",
                    required_reviewers=len(dimensions),
                )
                results[key] = ("ok", created)
            except ServiceError as e:
                results[key] = ("err", e.status_code)

    t1 = threading.Thread(target=worker, args=("a", ["fact_correctness", "brand_strategy"]))
    t2 = threading.Thread(target=worker, args=("b", ["fact_correctness", "risk"]))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    codes = {results["a"][0], results["b"][0]}
    # One created (ok), one conflict (err/409).
    assert "ok" in codes and "err" in codes
    with Session(engine) as s:
        rows = s.exec(
            select(ReviewPolicy).where(ReviewPolicy.name == "concurrent-conf")
        ).all()
        assert len(rows) == 1
        from aios.audit import AuditLog

        audits = s.exec(
            select(AuditLog).where(AuditLog.action == "review_policy.created")
        ).all()
        assert len(audits) == 1


# --- owner-auth gap on POST /review-policies (locked design #1, #9) ---------


def test_create_requires_owner_when_unconfigured(owner_auth_client: TestClient) -> None:
    """No owner auth configured -> 503 owner_auth_not_configured."""
    resp = owner_auth_client.post("/review-policies", json=VALID_POLICY)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "owner_auth_not_configured"


def test_create_requires_credentials_when_configured(
    owner_auth_client: TestClient, monkeypatch
) -> None:
    """Owner configured but no credentials -> 401 with challenge."""
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", "x" * 40)
    resp = owner_auth_client.post("/review-policies", json=VALID_POLICY)
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == 'Basic realm="aios-owner"'


def test_agent_key_cannot_create_policy(
    owner_auth_client: TestClient, monkeypatch
) -> None:
    """The outbound AIOS_AGENT_API_KEY is NOT a valid owner credential."""
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", "y" * 40)
    monkeypatch.setenv("AIOS_AGENT_API_KEY", "agent-secret")
    resp = owner_auth_client.post(
        "/review-policies",
        json=VALID_POLICY,
        auth=("owner", "agent-secret"),  # present the agent key as basic auth
    )
    assert resp.status_code == 401


def test_forged_owner_credentials_rejected(
    owner_auth_client: TestClient, monkeypatch
) -> None:
    """Wrong owner password is rejected (401), indistinguishable from missing."""
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", "z" * 40)
    resp = owner_auth_client.post(
        "/review-policies",
        json=VALID_POLICY,
        auth=("owner", "wrong-password"),
    )
    assert resp.status_code == 401
