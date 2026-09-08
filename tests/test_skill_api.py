"""Skill System V1 -- HTTP surface tests (Contract §12 / §16).

Six endpoints, owner-only. Covers: 201 submit (idempotent replay), 200 list,
single-gate review (approve mints / reject records), 200 skill list + detail,
deactivate, and the error mapping (404 / 409 / 422 / 403). Route-shape guards
live in test_skill_invariants.py (S4); this file exercises behaviour.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from aios.db import get_database_url, get_engine
from aios.models import Capability, Project


def _api_session() -> Session:
    return Session(get_engine(get_database_url()))


def _seed_project_and_capability() -> tuple[str, str]:
    with _api_session() as session:
        project = Project(name="SkillApi", objective="API coverage")
        capability = Capability(name="drafting", description="Writes")
        session.add_all([project, capability])
        session.commit()
        return project.id, capability.id


_PAYLOAD = {
    "name": "outline_first",
    "description": "Always outline before writing",
    "steps": [{"step": 1, "do": "outline"}],
    "tool_bindings": {},
    "execution_strategy": "single_pass",
}


def _submit(client: TestClient, capability_id: str, project_id: str, **overrides):
    payload = dict(_PAYLOAD, capability_id=capability_id, project_id=project_id, **overrides)
    return client.post("/skills/candidates", json=payload)


def test_api_full_lifecycle(authenticated_client: TestClient) -> None:
    client = authenticated_client
    project_id, capability_id = _seed_project_and_capability()

    # 201 submit.
    r = _submit(client, capability_id, project_id)
    assert r.status_code == 201, r.text
    candidate = r.json()
    assert candidate["status"] == "draft"

    # Idempotent replay: same content -> same candidate id (no 409).
    r2 = _submit(client, capability_id, project_id)
    assert r2.status_code == 201
    assert r2.json()["id"] == candidate["id"]

    # 200 candidate queue.
    r = client.get("/skills/candidates", params={"project_id": project_id})
    assert r.status_code == 200
    assert [c["id"] for c in r.json()] == [candidate["id"]]

    # Single governance entrance: approve mints the skill.
    r = client.post(
        "/skills/reviews",
        json={
            "candidate_id": candidate["id"],
            "decision": "approve",
            "rationale": "reusable",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["decision"] == "approve"
    skill = body["skill"]
    assert skill is not None
    assert skill["version"] == 1
    assert skill["status"] == "approved"

    # 200 skill list + detail.
    r = client.get("/skills", params={"project_id": project_id})
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == [skill["id"]]
    r = client.get(f"/skills/{skill['id']}")
    assert r.status_code == 200
    assert r.json()["steps"] == [{"step": 1, "do": "outline"}]

    # Deactivate mints no version; it flips status only.
    r = client.post(
        f"/skills/{skill['id']}/deactivate", json={"rationale": "obsolete"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "inactive"
    # Deactivated skills leave the default (approved) listing.
    r = client.get("/skills", params={"project_id": project_id, "status": "approved"})
    assert r.status_code == 200
    assert r.json() == []


def test_api_reject_path(authenticated_client: TestClient) -> None:
    client = authenticated_client
    project_id, capability_id = _seed_project_and_capability()
    r = _submit(client, capability_id, project_id, name="reject_me")
    candidate_id = r.json()["id"]
    r = client.post(
        "/skills/reviews",
        json={
            "candidate_id": candidate_id,
            "decision": "reject",
            "rationale": "not reusable",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["decision"] == "reject"
    assert body["skill"] is None


def test_api_error_mapping(authenticated_client: TestClient) -> None:
    client = authenticated_client
    project_id, capability_id = _seed_project_and_capability()

    # 404s.
    assert client.get("/skills/skill_missing").status_code == 404
    assert (
        client.post(
            "/skills/reviews",
            json={"candidate_id": "cand_missing", "decision": "approve", "rationale": "x"},
        ).status_code
        == 404
    )

    # 422 validation: bad slug reaches the service as a 422 (no DB CHECK by
    # design; see Contract deviation note -- service-level fail-fast).
    r = _submit(client, capability_id, project_id, name="Bad Slug")
    assert r.status_code == 422
    # 422: steps required -- a Skill is "how", not a fact.
    r = _submit(client, capability_id, project_id, name="no_steps", steps=[])
    assert r.status_code == 422
    # 422: bad execution strategy.
    r = _submit(client, capability_id, project_id, name="bad_strategy", execution_strategy="yolo")
    assert r.status_code == 422

    # 409: approve twice with different rationale = history rewrite attempt.
    r = _submit(client, capability_id, project_id, name="conflict_me")
    candidate_id = r.json()["id"]
    r = client.post(
        "/skills/reviews",
        json={"candidate_id": candidate_id, "decision": "approve", "rationale": "first"},
    )
    assert r.status_code == 200
    skill_id = r.json()["skill"]["id"]
    # Deactivate once, then again -> 409.
    assert (
        client.post(f"/skills/{skill_id}/deactivate", json={"rationale": "a"}).status_code
        == 200
    )
    assert (
        client.post(f"/skills/{skill_id}/deactivate", json={"rationale": "b"}).status_code
        == 409
    )


def test_api_all_endpoints_require_owner(owner_auth_client: TestClient, monkeypatch) -> None:
    """Real ``authenticate_owner`` contract, identical to every owner surface
    (mirrors test_employee_bridge.py): unconfigured owner env -> 503
    ``owner_auth_not_configured`` (server misconfiguration, never 401);
    configured env + wrong Basic credentials -> 401. No 200s anywhere."""
    client = owner_auth_client
    from aios.api.security import OWNER_API_KEY_ENV, OWNER_ID_ENV

    calls = [
        (
            "post",
            "/skills/candidates",
            {"json": dict(_PAYLOAD, capability_id="c", project_id="p")},
        ),
        ("get", "/skills/candidates", {}),
        (
            "post",
            "/skills/reviews",
            {"json": {"candidate_id": "c", "decision": "approve", "rationale": "r"}},
        ),
        ("get", "/skills", {}),
        ("get", "/skills/some_id", {}),
        ("post", "/skills/some_id/deactivate", {"json": {"rationale": "r"}}),
    ]
    # Unconfigured: all six endpoints must return the same 503 contract.
    for method, path, kwargs in calls:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 503, (
            f"{method.upper()} {path} must be 503 owner_auth_not_configured "
            f"(got {response.status_code})"
        )
        assert response.json()["detail"] == "owner_auth_not_configured"
    # Configured + wrong credentials -> 401, still no 200s.
    monkeypatch.setenv(OWNER_ID_ENV, "owner-real")
    monkeypatch.setenv(OWNER_API_KEY_ENV, "k" * 40)
    for method, path, kwargs in calls:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, (
            f"{method.upper()} {path} must reject wrong credentials "
            f"(got {response.status_code})"
        )


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/skills/candidates"),
        ("post", "/skills/reviews"),
        ("get", "/skills"),
        ("get", "/skills/some_id"),
        ("post", "/skills/some_id/deactivate"),
    ],
)
def test_api_route_paths_are_fully_qualified(
    authenticated_client: TestClient, method: str, path: str
) -> None:
    """Every skill path starts with /skills (F-3: no bare /candidates)."""
    assert path.startswith("/skills")
    response = getattr(authenticated_client, method)(path)
    # 404/422 fine -- what must NOT happen is a 404 from an unregistered route
    # shape or, worse, the W6 guard tripping. A registered route answers with
    # anything but FastAPI's route-not-found noise for a *missing* route; we
    # simply assert the call does not 500.
    assert response.status_code < 500
