"""HTTP smoke + authorization tests for the #109 customer-service API.

Mirrors ``tests/test_feedback.py``'s ``feedback_app`` / ``_client_for`` fixtures:
a single ``create_app()`` bound to a migrated temp DB, with the
``authenticate_owner_or_agent`` dependency overridden per test actor. Exercises
the eleven CS endpoints, the per-project 403 model, and the auto-send guards at
the HTTP boundary.
"""

from __future__ import annotations

import os

import pytest
from sqlmodel import Session

from aios.actor import resolve_agent_actor, resolve_owner_actor
from aios.api.app import create_app
from aios.content_draft import authenticate_owner_or_agent
from aios.db import get_engine
from aios.knowledge_service import KnowledgeService
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecisionValue,
)

OWNER = resolve_owner_actor()
AGENT = resolve_agent_actor("agent-cs-api")


@pytest.fixture
def cs_app(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'csapi.db').as_posix()}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)
    from fastapi.testclient import TestClient

    app = create_app()
    app.dependency_overrides[authenticate_owner_or_agent] = lambda: OWNER
    client = TestClient(app)
    client.__enter__()
    try:
        yield app
    finally:
        app.dependency_overrides.pop(authenticate_owner_or_agent, None)
        client.__exit__(None, None, None)


def _client_for(app, actor):
    from fastapi.testclient import TestClient

    app.dependency_overrides[authenticate_owner_or_agent] = lambda: actor
    return TestClient(app)


def _create_project(client, name="cs-proj"):
    resp = client.post("/projects", json={"name": name, "objective": "obj"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_fact(project_id: str, statement: str) -> None:
    url = os.environ["AIOS_DATABASE_URL"]
    with Session(get_engine(url)) as s:
        artifact = Artifact(
            project_id=project_id,
            type=ArtifactType.JSON,
            uri="s.json",
            checksum="sha256:s",
            review_status=ArtifactReviewStatus.APPROVED,
            metadata_json={},
        )
        s.add(artifact)
        s.commit()
        s.refresh(artifact)
        cand = KnowledgeService(s).submit_candidate(
            artifact.id,
            statement,
            project_id=project_id,
            tags=["wechat_writing"],
            actor=OWNER,
        )
        KnowledgeService(s).review_candidate(
            cand.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "r",
            actor=OWNER,
            series_id="cs-api-series",
            version=1,
        )
        s.commit()


# ---------------------------------------------------------------------------
# Happy path: owner drives the full loop
# ---------------------------------------------------------------------------


def test_owner_full_loop(cs_app):
    client = _client_for(cs_app, OWNER)
    pid = _create_project(client)
    # create conversation
    r = client.post("/conversations", json={"project_id": pid, "channel": "mock"})
    assert r.status_code == 201, r.text
    conv = r.json()
    cid = conv["id"]
    assert conv["channel"] == "mock"
    assert conv["lead_stage"] == "visitor"
    # inbound message
    r = client.post(f"/conversations/{cid}/messages", json={"text": "hello there"})
    assert r.status_code == 201, r.text
    # suggest (no facts -> escalate)
    r = client.post(f"/conversations/{cid}/suggest", json={"text": "anything unrelated"})
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "escalate"
    # human send
    r = client.post(
        f"/conversations/{cid}/send",
        json={"text": "您好，客服为您服务", "auto_send": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_auto_sent"] is False
    assert r.json()["sender_type"] == "owner"
    # advance funnel
    r = client.patch(f"/conversations/{cid}/stage", json={"stage": "lead", "reason": "r"})
    assert r.status_code == 200, r.text
    assert r.json()["lead_stage"] == "lead"
    # list conversations (owner)
    r = client.get("/conversations", params={"project_id": pid})
    assert r.status_code == 200, r.text
    assert any(c["id"] == cid for c in r.json())


# ---------------------------------------------------------------------------
# AUTO_SEND path end-to-end through HTTP
# ---------------------------------------------------------------------------


def test_auto_send_end_to_end(cs_app):
    client = _client_for(cs_app, OWNER)
    pid = _create_project(client)
    _seed_fact(pid, "the warranty lasts two full years")
    cid = client.post("/conversations", json={"project_id": pid}).json()["id"]
    payload = {"text": "our the warranty lasts two full years and more"}
    r = client.post(f"/conversations/{cid}/suggest", json=payload)
    assert r.status_code == 200, r.text
    sug = r.json()
    assert sug["decision"] == "auto_send"
    # auto-send with bound suggestion + verbatim text
    r = client.post(
        f"/conversations/{cid}/send",
        json={"text": sug["text"], "auto_send": True, "suggestion_id": sug["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_auto_sent"] is True
    # replay -> 409
    r = client.post(
        f"/conversations/{cid}/send",
        json={"text": sug["text"], "auto_send": True, "suggestion_id": sug["id"]},
    )
    assert r.status_code == 409


def test_auto_send_text_mismatch_409(cs_app):
    client = _client_for(cs_app, OWNER)
    pid = _create_project(client)
    _seed_fact(pid, "the warranty lasts two full years")
    cid = client.post("/conversations", json={"project_id": pid}).json()["id"]
    sug = client.post(
        f"/conversations/{cid}/suggest",
        json={"text": "our the warranty lasts two full years and more"},
    ).json()
    r = client.post(
        f"/conversations/{cid}/send",
        json={"text": sug["text"] + " injected", "auto_send": True, "suggestion_id": sug["id"]},
    )
    assert r.status_code == 409


def test_auto_send_stale_fact_409(cs_app):
    client = _client_for(cs_app, OWNER)
    pid = _create_project(client)
    _seed_fact(pid, "the warranty lasts two full years")
    cid = client.post("/conversations", json={"project_id": pid}).json()["id"]
    sug = client.post(
        f"/conversations/{cid}/suggest",
        json={"text": "our the warranty lasts two full years and more"},
    ).json()
    # Revoke the underlying fact directly in the DB.
    url = os.environ["AIOS_DATABASE_URL"]
    with Session(get_engine(url)) as s:
        fact = s.get(KnowledgeFact, sug["knowledge_fact_refs"][0])
        fact.status = KnowledgeFactStatus.SUPERSEDED
        s.add(fact)
        s.commit()
    r = client.post(
        f"/conversations/{cid}/send",
        json={"text": sug["text"], "auto_send": True, "suggestion_id": sug["id"]},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Authorization: agent cannot browse / cannot use owner-only endpoints
# ---------------------------------------------------------------------------


def test_agent_cannot_view_conversation(cs_app):
    owner_client = _client_for(cs_app, OWNER)
    pid = _create_project(owner_client)
    cid = owner_client.post("/conversations", json={"project_id": pid}).json()["id"]
    agent_client = _client_for(cs_app, AGENT)
    assert agent_client.get(f"/conversations/{cid}").status_code == 403
    assert agent_client.get("/conversations", params={"project_id": pid}).status_code == 403


def test_owner_only_endpoints_forbid_agent(cs_app):
    owner_client = _client_for(cs_app, OWNER)
    pid = _create_project(owner_client)
    cid = owner_client.post("/conversations", json={"project_id": pid}).json()["id"]
    agent_client = _client_for(cs_app, AGENT)
    assert (
        agent_client.patch(f"/conversations/{cid}/stage", json={"stage": "lead"}).status_code
        == 403
    )
    assert agent_client.post(f"/conversations/{cid}/assign").status_code == 403
    assert (
        agent_client.post(f"/conversations/{cid}/followup-task", json={"title": "t"}).status_code
        == 403
    )
    assert (
        agent_client.post(
            f"/conversations/{cid}/send", json={"text": "x", "auto_send": False}
        ).status_code
        == 403
    )
    assert (
        agent_client.post(
            f"/conversations/{cid}/escalate", json={"categories": ["complaint"]}
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# Agent MAY suggest + auto-send (the AI reply path), but only via a bound
# AUTO_SEND suggestion (guards still apply).
# ---------------------------------------------------------------------------


def test_agent_can_suggest_and_auto_send(cs_app):
    # An agent explicitly scoped to the conversation's project MAY drive the
    # suggest + auto-send pipeline (the AI reply path). An agent WITHOUT a
    # matching scope is rejected -- see the regression tests below.
    owner_client = _client_for(cs_app, OWNER)
    pid = _create_project(owner_client)
    _seed_fact(pid, "the warranty lasts two full years")
    cid = owner_client.post("/conversations", json={"project_id": pid}).json()["id"]
    scoped_agent = resolve_agent_actor("agent-cs-api", project_id=pid)
    agent_client = _client_for(cs_app, scoped_agent)
    sug = agent_client.post(
        f"/conversations/{cid}/suggest",
        json={"text": "our the warranty lasts two full years and more"},
    ).json()
    assert sug["decision"] == "auto_send"
    r = agent_client.post(
        f"/conversations/{cid}/send",
        json={"text": sug["text"], "auto_send": True, "suggestion_id": sug["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_auto_sent"] is True


def test_unscoped_agent_cannot_suggest(cs_app):
    # Contract B: an agent without an explicit project scope is fail-closed on
    # compute endpoints (the current gateway supplies no scope).
    owner_client = _client_for(cs_app, OWNER)
    pid = _create_project(owner_client)
    cid = owner_client.post("/conversations", json={"project_id": pid}).json()["id"]
    unscoped = resolve_agent_actor("agent-cs-api")
    agent_client = _client_for(cs_app, unscoped)
    assert (
        agent_client.post(
            f"/conversations/{cid}/suggest", json={"text": "what is the price"}
        ).status_code
        == 403
    )


def test_agent_outside_project_cannot_suggest_or_send(cs_app):
    # Contract B: an agent scoped to a DIFFERENT project cannot act on this
    # project's conversation (cross-project act is closed) -- on BOTH the
    # suggest and the auto-send paths (the latter is the one guarded by
    # _assert_can_act inside _auto_send, not the owner-only _human_send).
    owner_client = _client_for(cs_app, OWNER)
    pid = _create_project(owner_client)
    _seed_fact(pid, "the warranty lasts two full years")
    cid = owner_client.post("/conversations", json={"project_id": pid}).json()["id"]
    # Build an eligible AUTO_SEND suggestion in this project as the owner
    # FIRST (while the override is still OWNER) -- the shared dependency
    # override is clobbered once we switch to the agent below.
    sug = owner_client.post(
        f"/conversations/{cid}/suggest",
        json={"text": "our the warranty lasts two full years and more"},
    ).json()
    assert sug["decision"] == "auto_send"
    # Now switch to the wrong-scope agent and assert both paths reject it.
    other = resolve_agent_actor("agent-cs-api", project_id="other-project")
    agent_client = _client_for(cs_app, other)
    assert (
        agent_client.post(
            f"/conversations/{cid}/suggest", json={"text": "what is the price"}
        ).status_code
        == 403
    )
    assert (
        agent_client.post(
            f"/conversations/{cid}/send",
            json={"text": sug["text"], "auto_send": True, "suggestion_id": sug["id"]},
        ).status_code
        == 403
    )


def test_followup_task_created_for_owner(cs_app):
    client = _client_for(cs_app, OWNER)
    pid = _create_project(client)
    cid = client.post("/conversations", json={"project_id": pid}).json()["id"]
    r = client.post(f"/conversations/{cid}/followup-task", json={"title": "call back"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "call back"
    assert r.json()["project_id"] == pid


def test_invalid_lead_stage_422(cs_app):
    client = _client_for(cs_app, OWNER)
    pid = _create_project(client)
    cid = client.post("/conversations", json={"project_id": pid}).json()["id"]
    r = client.patch(f"/conversations/{cid}/stage", json={"stage": "not_a_stage"})
    assert r.status_code == 422
