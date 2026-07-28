"""Endpoint smoke tests for the work-log system (#88 plan §9 / §12).

Covers the three routes -- ``POST /work-logs`` (mandatory ``Idempotency-Key``
header, 201 created / 200 replay / 409 key reuse with a changed payload),
``POST /work-logs/{id}/attest`` (owner attestation, idempotent), and
``GET /content-feed`` (read-only, exact scope) -- plus the owner-auth guard on
all three. The full service-level contract matrix lives in
``tests/test_work_log.py``; this module only proves the HTTP wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.api.app import create_app
from aios.api.security import authenticate_owner
from aios.db import get_database_url, get_engine
from aios.models import (
    Approval,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Project,
    RiskLevel,
)
from aios.work_log import WORK_LOG_ATTESTATION_ACTION


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """Owner-authenticated client (trusted-owner override on this app only)."""
    database_path = tmp_path / "api_work_log.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    app = create_app()
    app.dependency_overrides[authenticate_owner] = lambda: ActorContext(
        kind="owner", owner_id="owner"
    )
    with TestClient(app) as test_client:
        yield test_client


def _seed_project() -> Project:
    with Session(get_engine(get_database_url())) as session:
        project = Project(name="P", objective="O")
        session.add(project)
        session.commit()
        session.refresh(project)
        return project


def _payload(project_id: str, **overrides) -> dict:
    body = {
        "project_id": project_id,
        "report_type": "daily",
        "what_done": "写了工作日志 API",
        "why": "issue #88",
        "problem": "无",
        "solution": "无",
        "new_knowledge": "端点冒烟测试要点",
    }
    body.update(overrides)
    return body


# --- POST /work-logs ---------------------------------------------------------


def test_post_work_log_201_unverified(client: TestClient) -> None:
    project = _seed_project()
    resp = client.post(
        "/work-logs",
        json=_payload(project.id),
        headers={"Idempotency-Key": "k-1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["review_status"] == ArtifactReviewStatus.UNVERIFIED.value
    assert body["project_id"] == project.id
    assert body["report_type"] == "daily"


def test_post_work_log_missing_idempotency_key_422(client: TestClient) -> None:
    project = _seed_project()
    resp = client.post("/work-logs", json=_payload(project.id))
    assert resp.status_code == 422


def test_post_work_log_replay_200_same_artifact(client: TestClient) -> None:
    project = _seed_project()
    headers = {"Idempotency-Key": "k-replay"}
    first = client.post("/work-logs", json=_payload(project.id), headers=headers)
    assert first.status_code == 201
    second = client.post("/work-logs", json=_payload(project.id), headers=headers)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_post_work_log_replay_with_whitespace_key_200(client: TestClient) -> None:
    # Regression for Codex P2: the service trims the Idempotency-Key before
    # hashing, so a key with surrounding whitespace must still replay (200),
    # not be misreported as a new resource (201).
    project = _seed_project()
    headers = {"Idempotency-Key": "  k-ws  "}
    first = client.post("/work-logs", json=_payload(project.id), headers=headers)
    assert first.status_code == 201
    second = client.post("/work-logs", json=_payload(project.id), headers=headers)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_post_work_log_key_reuse_different_payload_409(client: TestClient) -> None:
    project = _seed_project()
    headers = {"Idempotency-Key": "k-conflict"}
    assert (
        client.post("/work-logs", json=_payload(project.id), headers=headers).status_code
        == 201
    )
    changed = _payload(project.id, what_done="改了内容")
    resp = client.post("/work-logs", json=changed, headers=headers)
    assert resp.status_code == 409


def test_post_work_log_unknown_project_422(client: TestClient) -> None:
    resp = client.post(
        "/work-logs",
        json=_payload("prj_missing"),
        headers={"Idempotency-Key": "k-x"},
    )
    assert resp.status_code == 422


# --- POST /work-logs/{id}/attest ----------------------------------------------


def test_attest_endpoint_approves_then_idempotent(client: TestClient) -> None:
    project = _seed_project()
    created = client.post(
        "/work-logs", json=_payload(project.id), headers={"Idempotency-Key": "k-a"}
    ).json()

    first = client.post(f"/work-logs/{created['id']}/attest")
    assert first.status_code == 200
    assert first.json()["review_status"] == ArtifactReviewStatus.APPROVED.value

    second = client.post(f"/work-logs/{created['id']}/attest")
    assert second.status_code == 200
    assert second.json()["review_status"] == ArtifactReviewStatus.APPROVED.value

    with Session(get_engine(get_database_url())) as session:
        approvals = session.exec(
            select(Approval).where(
                Approval.action_type == WORK_LOG_ATTESTATION_ACTION,
            )
        ).all()
        assert len(approvals) == 1
        assert approvals[0].risk_level == RiskLevel.L1
        artifact = session.get(Artifact, created["id"])
        assert artifact is not None
        assert artifact.review_status == ArtifactReviewStatus.APPROVED
        assert artifact.type == ArtifactType.WORK_LOG


def test_attest_endpoint_missing_artifact_404(client: TestClient) -> None:
    resp = client.post("/work-logs/art_missing/attest")
    assert resp.status_code == 404


# --- GET /content-feed ---------------------------------------------------------


def test_content_feed_returns_attested_logs_only(client: TestClient) -> None:
    project = _seed_project()
    attested = client.post(
        "/work-logs",
        json=_payload(project.id, content_value="high", content_angle="角度A"),
        headers={"Idempotency-Key": "k-f1"},
    ).json()
    client.post(f"/work-logs/{attested['id']}/attest")
    # Second log stays UNVERIFIED -> must NOT appear in the feed.
    client.post(
        "/work-logs",
        json=_payload(project.id, content_value="high", what_done="未认证日志"),
        headers={"Idempotency-Key": "k-f2"},
    )

    resp = client.get("/content-feed", params={"project_id": project.id})
    assert resp.status_code == 200
    entries = resp.json()
    assert [entry["id"] for entry in entries if entry["kind"] == "work_log"] == [
        attested["id"]
    ]
    log_entry = entries[0]
    assert log_entry["content_value"] == "high"
    assert "tags" not in log_entry  # log entries never carry tags (plan §8.3)


def test_content_feed_min_value_threshold(client: TestClient) -> None:
    project = _seed_project()
    created = client.post(
        "/work-logs",
        json=_payload(project.id, content_value="medium"),
        headers={"Idempotency-Key": "k-m"},
    ).json()
    client.post(f"/work-logs/{created['id']}/attest")

    medium = client.get("/content-feed", params={"project_id": project.id})
    assert [e["id"] for e in medium.json()] == [created["id"]]
    high = client.get(
        "/content-feed", params={"project_id": project.id, "min_value": "high"}
    )
    assert high.json() == []


def test_content_feed_invalid_min_value_422(client: TestClient) -> None:
    resp = client.get("/content-feed", params={"min_value": "extreme"})
    assert resp.status_code == 422


# --- owner-auth guard -----------------------------------------------------------


def test_work_log_routes_require_owner_auth(tmp_path: Path, monkeypatch) -> None:
    """Without the trusted-owner override the routes hit the REAL fail-closed
    authentication boundary: unconfigured env -> 503 (never an open door)."""
    database_path = tmp_path / "api_work_log_auth.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.delenv("AIOS_OWNER_ID", raising=False)
    monkeypatch.delenv("AIOS_OWNER_API_KEY", raising=False)
    app = create_app()
    app.dependency_overrides.pop(authenticate_owner, None)
    with TestClient(app) as anonymous:
        assert (
            anonymous.post(
                "/work-logs",
                json=_payload("prj_x"),
                headers={"Idempotency-Key": "k"},
            ).status_code
            == 503
        )
        assert anonymous.post("/work-logs/art_x/attest").status_code == 503
        assert anonymous.get("/content-feed").status_code == 503


# --- V2 (#92): attest override body + structured platform feed ---------------


def test_attest_endpoint_accepts_override(client: TestClient) -> None:
    project = _seed_project()
    created = client.post(
        "/work-logs", json=_payload(project.id), headers={"Idempotency-Key": "k-o"}
    ).json()
    # Owner opts the draft into the KB at attest time.
    resp = client.post(
        f"/work-logs/{created['id']}/attest", json={"should_enter_kb": True}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == ArtifactReviewStatus.APPROVED.value
    assert body["should_enter_kb"] is True
    # Backward compatible: no body keeps the submitted default (False).
    created2 = client.post(
        "/work-logs", json=_payload(project.id), headers={"Idempotency-Key": "k-o2"}
    ).json()
    resp2 = client.post(f"/work-logs/{created2['id']}/attest")
    assert resp2.json()["should_enter_kb"] is False


def test_attest_endpoint_invalid_content_value_422(client: TestClient) -> None:
    project = _seed_project()
    created = client.post(
        "/work-logs", json=_payload(project.id), headers={"Idempotency-Key": "k-b"}
    ).json()
    resp = client.post(
        f"/work-logs/{created['id']}/attest", json={"content_value": "urgent"}
    )
    assert resp.status_code == 422


def test_feed_structured_by_source_platform(client: TestClient) -> None:
    project = _seed_project()
    codex = client.post(
        "/work-logs",
        json=_payload(project.id, source_platform="codex"),
        headers={"Idempotency-Key": "k-c"},
    ).json()
    hermes = client.post(
        "/work-logs",
        json=_payload(project.id, source_platform="hermes"),
        headers={"Idempotency-Key": "k-h"},
    ).json()
    client.post(f"/work-logs/{codex['id']}/attest")
    client.post(f"/work-logs/{hermes['id']}/attest")

    resp = client.get(
        "/content-feed",
        params={
            "project_id": project.id,
            "source_platform": "codex",
            "log_limit": 10,
            "fact_limit": 10,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Structured split view (plan §1 / v16).
    assert isinstance(data, dict)
    assert {k for k in data} == {"work_logs", "facts"}
    # Platform filter applies ONLY to logs.
    assert all(e["source_platform"] == "codex" for e in data["work_logs"])
    assert all(e["id"] != hermes["id"] for e in data["work_logs"])
    # Facts are never platform-filtered.
    assert data["facts"] == []


def test_feed_structured_offset_fallback_via_api(client: TestClient) -> None:
    project = _seed_project()
    # Three codex logs; created ascending so newest is last in codex_ids.
    codex_ids = []
    for i in range(3):
        log = client.post(
            "/work-logs",
            json=_payload(project.id, source_platform="codex"),
            headers={"Idempotency-Key": f"k-c{i}"},
        ).json()
        client.post(f"/work-logs/{log['id']}/attest")
        codex_ids.append(log["id"])
    # Omitting the split offsets but setting only `offset=1` must shift BOTH
    # slices by 1 (the endpoint now passes None so the service falls back to
    # `offset`); the most-recent log must be skipped, not reset to page 0.
    resp = client.get(
        "/content-feed",
        params={
            "project_id": project.id,
            "source_platform": "codex",
            "min_value": "low",
            "offset": 1,
            "log_limit": 10,
            "fact_limit": 10,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert codex_ids[-1] not in [e["id"] for e in data["work_logs"]]
    assert len(data["work_logs"]) == 2
    assert data["facts"] == []
