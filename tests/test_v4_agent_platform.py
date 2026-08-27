"""V4 unified Agent platform — self-registration, capability discovery, Agent Relay (#99/#101).

Covers the six gates (A–F) and the §8 TDD test checklist from
``docs/issue-99-v4-plan.md``:

  * Gate A — identity: agent cannot impersonate owner / another agent; bootstrap
    token scope is token-bound (not body-bound); single-use; no secret leak.
  * Gate B — idempotency: bootstrap strict single-CREATE (collision -> 401, no
    row); self-update upsert (same id, last-writer-wins); relay idempotency +
    per-actor scoping.
  * Gate C — capability catalog single source of truth (unknown slug -> 422).
  * Gate D — relay provenance derived from auth; never attests; scoped namespace.
  * Gate E — every successful self-registration + relay ingest is audited;
    collision / scope-violation 401s are NOT audited.
  * Gate F — single controlled migration (no new tables); attest path untouched.

The secret store is an in-process singleton; every fixture resets it so agent
credentials never leak across tests.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import ActorContext, resolve_agent_actor
from aios.agent_registry import (
    create_agent_via_bootstrap,
    list_agents,
    list_capabilities,
    rotate_credential,
    upsert_agent,
)
from aios.api.app import create_app
from aios.api.security import authenticate_owner, mint_bootstrap_token
from aios.audit import AuditLog
from aios.db import get_database_url, get_engine, run_migrations
from aios.models import (
    Agent,
    AgentCapability,
    AgentStatus,
    Artifact,
    ArtifactReviewStatus,
    Capability,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
)
from aios.scheduler import route_task
from aios.secrets_store import get_secret_store, reset_secret_store
from aios.services import ServiceError

# Must be >= 32 chars (mirrors the production owner-API-key length floor) and is
# used both as the env signing key and the mint key so token verification and
# issuance share the same secret.
OWNER_API_KEY = "test-owner-api-key-for-v4-unit-tests-00000001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def v4_session(tmp_path, monkeypatch) -> Session:
    """Fresh migrated DB + clean secret store for service-layer tests."""
    reset_secret_store()
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'v4.db'}")
    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()
    reset_secret_store()


@pytest.fixture
def v4_client(authenticated_client, monkeypatch):
    """Trusted-owner app + real bootstrap/agent auth deps; clean secret store.

    ``authenticated_client`` already overrides ``authenticate_owner`` with a
    trusted owner, so owner-only endpoints (e.g. rotate-credential) work without
    real credentials. The bootstrap-token and agent-bearer deps stay REAL, so we
    set the owner signing key in the environment and reset the secret store.
    """
    reset_secret_store()
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", OWNER_API_KEY)
    yield authenticated_client
    reset_secret_store()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_capability(session: Session, cap_id: str, name: str) -> Capability:
    cap = session.get(Capability, cap_id)
    if cap is None:
        cap = Capability(id=cap_id, name=name)
        session.add(cap)
        session.commit()
    return cap


def _make_project(session: Session) -> Project:
    p = Project(name="P", objective="O")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _audit_actions(session: Session) -> list[str]:
    session.expire_all()
    return [a.action for a in session.exec(select(AuditLog)).all()]


def _mint_token(platform: str, external_ref: str, jti: str | None = None) -> str:
    return mint_bootstrap_token(platform, external_ref, key=OWNER_API_KEY, jti=jti)


def _bootstrap_via_service(
    session: Session,
    platform: str,
    external_ref: str,
    jti: str,
    *,
    capabilities: list[str] | None = None,
) -> tuple[Agent, str]:
    return create_agent_via_bootstrap(
        session,
        platform=platform,
        external_ref=external_ref,
        jti=jti,
        name="agent",
        role="worker",
        adapter_type="model",
        capabilities=capabilities,
    )


# ===========================================================================
# Gate A — identity
# ===========================================================================


def test_authenticate_agent_resolves_actor(v4_client) -> None:
    """A valid agent bearer credential authenticates; a wrong one is 401."""
    token = _mint_token("p1", "r1")
    resp = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    credential = resp.json()["credential"]

    # Wrong credential on a protected agent endpoint -> 401.
    bad = v4_client.put(
        "/agents/self",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "x",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": "Bearer not-a-real-credential"},
    )
    assert bad.status_code == 401

    # The real credential works on the protected endpoint.
    good = v4_client.put(
        "/agents/self",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent-renamed",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert good.status_code == 200
    assert good.json()["name"] == "agent-renamed"


def test_bootstrap_token_registers_scoped_identity(v4_client) -> None:
    """A valid scoped token creates exactly one (platform,external_ref) agent."""
    token = _mint_token("p1", "r1")
    resp = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["platform"] == "p1"
    assert body["external_ref"] == "r1"
    assert body["credential"]  # one-time credential returned exactly once


def test_bootstrap_token_rejects_wrong_tuple(v4_client) -> None:
    """Scope is decided by the token, not the body -- a mismatched tuple is 401."""
    token = _mint_token("p1", "r1")  # token authorizes (p1, r1)
    resp = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p2",  # body claims a different identity
            "external_ref": "r2",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


def test_bootstrap_token_single_use(v4_client) -> None:
    """The same token replayed after a successful bootstrap is rejected (401)."""
    token = _mint_token("p1", "r1", jti="jti-single")
    first = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 201
    assert "credential" in first.json()

    second = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 401
    # The replay never re-issues a credential.
    assert "credential" not in second.json()


def test_bootstrap_token_no_secret_leak(v4_client) -> None:
    """The bootstrap response carries ``credential`` but never ``secret_ref``/keys."""
    token = _mint_token("p1", "r1")
    resp = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "secret_ref" not in body
    assert "nvapi-" not in body["credential"]
    assert "sk-" not in body["credential"]

    # The public agent read omits the credential / secret entirely.
    agent_id = body["id"]
    got = v4_client.get(f"/agents/{agent_id}")
    assert got.status_code == 200
    assert "credential" not in got.json()
    assert "secret_ref" not in got.json()


def test_owner_rotate_credential(v4_client) -> None:
    """Owner rotates a lost credential out of band; the old one stops working."""
    token = _mint_token("p1", "r1")
    boot = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert boot.status_code == 201
    agent_id = boot.json()["id"]
    old_credential = boot.json()["credential"]

    rot = v4_client.post(f"/agents/{agent_id}/rotate-credential")
    assert rot.status_code == 200
    new_credential = rot.json()["credential"]
    assert new_credential != old_credential

    # The old credential no longer resolves to the agent.
    old_self = v4_client.put(
        "/agents/self",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "stale",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {old_credential}"},
    )
    assert old_self.status_code == 401

    # The new credential works.
    new_self = v4_client.put(
        "/agents/self",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "fresh",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {new_credential}"},
    )
    assert new_self.status_code == 200
    assert new_self.json()["name"] == "fresh"


def test_bootstrap_credential_failure_rolls_back(v4_session, monkeypatch) -> None:
    """If the external credential store fails, the agent row is NOT committed."""
    _seed_capability(v4_session, "cap_x", "cap_x")

    def _boom(self, agent_id: str, session=None) -> str:  # pragma: no cover - injected failure
        raise RuntimeError("secret store unavailable")

    monkeypatch.setattr(
        "aios.secrets_store.AgentSecretStore.issue", _boom
    )
    with pytest.raises(RuntimeError):
        _bootstrap_via_service(
            v4_session, "p1", "r1", "jti-rollback", capabilities=["cap_x"]
        )
    # Roll back the open (uncommitted) transaction and assert no agent persisted.
    v4_session.rollback()
    v4_session.expire_all()
    assert (
        v4_session.exec(
            select(Agent).where(Agent.external_ref == "r1")
        ).first()
        is None
    )


def test_bootstrap_commit_failure_revokes_credential(
    v4_session, monkeypatch
) -> None:
    """If the final commit fails AFTER credential issuance, the already-issued
    plaintext credential is revoked -- no orphaned active bearer secret."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    store = get_secret_store()
    captured: dict[str, str] = {}

    real_issue = store.issue

    def _spy_issue(agent_id: str, session=None) -> str:
        tok = real_issue(agent_id)
        captured["token"] = tok
        return tok

    monkeypatch.setattr(store, "issue", _spy_issue)

    def _boom_commit(*_args, **_kwargs) -> None:  # pragma: no cover - injected failure
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(v4_session, "commit", _boom_commit)

    with pytest.raises(RuntimeError):
        _bootstrap_via_service(
            v4_session, "p1", "r1", "jti-commit-fail", capabilities=["cap_x"]
        )
    assert captured, "credential was never issued"
    # The plaintext credential must have been revoked on rollback.
    assert store.resolve(captured["token"]) is None


def test_bootstrap_audit_failure_revokes_credential(
    v4_session, monkeypatch
) -> None:
    """If the audit write fails after credential issuance, the plaintext
    credential is still revoked (every post-issuance failure is compensated)."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    store = get_secret_store()
    captured: dict[str, str] = {}

    real_issue = store.issue

    def _spy_issue(agent_id: str, session=None) -> str:
        tok = real_issue(agent_id)
        captured["token"] = tok
        return tok

    monkeypatch.setattr(store, "issue", _spy_issue)
    monkeypatch.setattr(
        "aios.agent_registry.append_audit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )

    with pytest.raises(RuntimeError):
        _bootstrap_via_service(
            v4_session, "p1", "r1", "jti-audit-fail", capabilities=["cap_x"]
        )
    assert captured, "credential was never issued"
    assert store.resolve(captured["token"]) is None


def test_rotate_credential_audit_failure_revokes_credential(
    v4_session, monkeypatch
) -> None:
    """If the audit write fails after credential issuance during rotation, the
    plaintext credential is still revoked (every post-issuance failure is
    compensated, mirroring the bootstrap path)."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-rotate-audit")
    store = get_secret_store()
    captured: dict[str, str] = {}

    real_issue = store.issue

    def _spy_issue(agent_id: str, session=None) -> str:
        tok = real_issue(agent_id)
        captured["token"] = tok
        return tok

    monkeypatch.setattr(store, "issue", _spy_issue)
    monkeypatch.setattr(
        "aios.agent_registry.append_audit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )

    with pytest.raises(RuntimeError):
        rotate_credential(v4_session, agent.id)
    assert captured, "credential was never issued"
    # The freshly-issued plaintext credential must be revoked on audit failure.
    assert store.resolve(captured["token"]) is None


def test_self_update_preserves_owner_disabled_state(v4_session) -> None:
    """A self-update must NOT re-enable an agent the owner previously disabled;
    the owner-controlled ``enabled`` flag (and derived ``status``) is preserved."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-disable")

    # Simulate an owner disabling the agent out of band.
    v4_session.expire_all()
    reloaded = v4_session.get(Agent, agent.id)
    reloaded.enabled = False
    v4_session.add(reloaded)
    v4_session.commit()
    v4_session.refresh(reloaded)
    assert reloaded.enabled is False

    # The agent self-updates its profile legitimately.
    upserted = upsert_agent(
        v4_session,
        actor=resolve_agent_actor(agent.id),
        platform="p1",
        external_ref="r1",
        name="still-disabled",
        role="worker",
        adapter_type="model",
        capabilities=["cap_x"],
    )
    # Owner authority over enable/disable is preserved by a self-update.
    assert upserted.enabled is False, "owner-disabled state must be preserved"
    assert upserted.status == AgentStatus.UNAVAILABLE


def test_self_register_scope_locked(v4_session) -> None:
    """Self-update must be scope-locked to the authenticated agent's identity."""
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-a")

    # Body claims a different (platform, external_ref) -> 422 (scope mismatch).
    with pytest.raises(ServiceError) as exc:
        upsert_agent(
            v4_session,
            actor=resolve_agent_actor(agent.id),
            platform="pX",
            external_ref="rX",
            name="hijack",
            role="worker",
            adapter_type="model",
        )
    assert exc.value.status_code == 422

    # A non-agent (owner) actor cannot self-update -> 403.
    with pytest.raises(ServiceError) as exc2:
        upsert_agent(
            v4_session,
            actor=ActorContext(kind="owner", owner_id="owner"),
            platform="p1",
            external_ref="r1",
            name="x",
            role="worker",
            adapter_type="model",
        )
    assert exc2.value.status_code == 403

    # The original agent is untouched.
    v4_session.expire_all()
    reloaded = v4_session.get(Agent, agent.id)
    assert reloaded.platform == "p1"
    assert reloaded.external_ref == "r1"


# ===========================================================================
# Gate B — idempotency
# ===========================================================================


def test_bootstrap_strict_single_create_and_collision(v4_session) -> None:
    """Bootstrap is strict CREATE: same jti replay -> 401; same tuple w/ new jti -> 401."""
    _seed_capability(v4_session, "cap_x", "cap_x")

    # First bootstrap with jti J1 succeeds.
    a1, _ = _bootstrap_via_service(v4_session, "p1", "r1", "J1", capabilities=["cap_x"])
    # Replay the SAME jti -> consumed -> 401.
    with pytest.raises(ServiceError) as exc:
        _bootstrap_via_service(v4_session, "p1", "r1", "J1", capabilities=["cap_x"])
    assert exc.value.status_code == 401
    # A different jti but the SAME (platform, external_ref) tuple -> collision 401.
    with pytest.raises(ServiceError) as exc2:
        _bootstrap_via_service(v4_session, "p1", "r1", "J2", capabilities=["cap_x"])
    assert exc2.value.status_code == 401

    # Exactly one agent row exists for the tuple.
    v4_session.expire_all()
    rows = v4_session.exec(select(Agent).where(Agent.external_ref == "r1")).all()
    assert len(rows) == 1
    assert rows[0].id == a1.id


def test_bootstrap_concurrent_collision_rejected(v4_session) -> None:
    """Two concurrent same-tuple bootstraps yield at most one agent, no 500s.

    The partial unique index arbitrates; the loser hits it and is rejected with
    401 (zero side effects). Real concurrency relies on the same DB-level
    guarantee exercised here with a thread barrier.
    """
    _seed_capability(v4_session, "cap_x", "cap_x")
    eng = get_engine(get_database_url())
    barrier = threading.Barrier(2)
    outcomes: dict[int, str] = {}

    def worker(wid: int, jti: str) -> None:
        s = Session(eng)
        try:
            barrier.wait()
            _bootstrap_via_service(s, "p1", "r1", jti, capabilities=["cap_x"])
            outcomes[wid] = "ok"
        except ServiceError:
            outcomes[wid] = "err"
        finally:
            s.close()

    threads = [
        threading.Thread(target=worker, args=(i, f"jti-{i}")) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    v4_session.expire_all()
    rows = v4_session.exec(select(Agent).where(Agent.external_ref == "r1")).all()
    assert len(rows) <= 1  # never two agents for one identity
    assert "ok" in outcomes.values()
    assert "err" in outcomes.values()


def test_self_update_keeps_same_id(v4_session) -> None:
    """Self-update is an idempotent upsert: same id, updated fields, no row blow-up."""
    _seed_capability(v4_session, "positioning", "positioning")
    _seed_capability(v4_session, "xhs_adaptation", "xhs_adaptation")
    agent, _ = _bootstrap_via_service(
        v4_session, "p1", "r1", "jti-u", capabilities=["positioning"]
    )
    original_id = agent.id

    actor = resolve_agent_actor(agent.id)
    # First update: switch capability to xhs_adaptation.
    upsert_agent(
        v4_session,
        actor=actor,
        platform="p1",
        external_ref="r1",
        name="agent",
        role="writer",
        adapter_type="model",
        capabilities=["xhs_adaptation"],
    )
    # Second update: back to positioning.
    upsert_agent(
        v4_session,
        actor=actor,
        platform="p1",
        external_ref="r1",
        name="agent",
        role="writer",
        adapter_type="model",
        capabilities=["positioning"],
    )

    v4_session.expire_all()
    reloaded = v4_session.get(Agent, original_id)
    assert reloaded.id == original_id  # never created a duplicate entity
    caps = v4_session.exec(
        select(AgentCapability).where(AgentCapability.agent_id == original_id)
    ).all()
    # Only the last-declared capability remains (relation reconciled, not appended).
    assert {c.capability_id for c in caps} == {"positioning"}


def test_self_update_concurrent_same_agent(v4_session) -> None:
    """Concurrent self-updates serialize (BEGIN IMMEDIATE); final state is consistent.

    Exactly one agent exists; its (name, capabilities) reflect exactly one of
    the competing payloads -- never a silent drop, never a mixed aggregate.
    """
    _seed_capability(v4_session, "positioning", "positioning")
    _seed_capability(v4_session, "xhs_adaptation", "xhs_adaptation")
    agent, _ = _bootstrap_via_service(
        v4_session, "p1", "r1", "jti-cu", capabilities=["positioning"]
    )
    eng = get_engine(get_database_url())
    barrier = threading.Barrier(2)
    payloads = [
        {"name": "alpha", "capabilities": ["positioning"]},
        {"name": "beta", "capabilities": ["xhs_adaptation"]},
    ]

    errors: list[Exception] = []

    def worker(wid: int) -> None:
        s = Session(eng)
        try:
            barrier.wait()
            upsert_agent(
                s,
                actor=resolve_agent_actor(agent.id),
                platform="p1",
                external_ref="r1",
                name=payloads[wid]["name"],
                role="writer",
                adapter_type="model",
                capabilities=payloads[wid]["capabilities"],
            )
        except Exception as exc:  # noqa: BLE001 - surface thread failures as test errors
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Gate A: both concurrent self-updates must complete (a silent "database is
    # locked" / thread exception must not let the test pass) before we inspect
    # the final serialized state.
    assert not errors, f"concurrent self-update workers failed: {errors}"
    v4_session.expire_all()
    rows = v4_session.exec(select(Agent)).all()
    assert len(rows) == 1
    final = rows[0]
    caps = v4_session.exec(
        select(AgentCapability).where(AgentCapability.agent_id == final.id)
    ).all()
    # Final state must be one of the two submitted payloads exactly.
    if final.name == "alpha":
        assert {c.capability_id for c in caps} == {"positioning"}
    else:
        assert final.name == "beta"
        assert {c.capability_id for c in caps} == {"xhs_adaptation"}


def test_relay_idempotent_replay(v4_session) -> None:
    """Same key + same payload relay -> 200 returning the existing log (one row)."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-rl")
    project = _make_project(v4_session)
    actor = resolve_agent_actor(agent.id)
    from aios.relay import relay_work_log
    from aios.schemas import WorkLogSubmit

    payload = WorkLogSubmit(
        project_id=project.id,
        report_type="daily",
        what_done="did",
        why="because",
        problem="none",
        solution="fixed",
        new_knowledge="learned",
        source_platform="chatgpt",
    )
    a1, created1 = relay_work_log(
        v4_session, payload=payload, idempotency_key="k1", actor=actor, agent=agent
    )
    assert created1 is True
    a2, created2 = relay_work_log(
        v4_session, payload=payload, idempotency_key="k1", actor=actor, agent=agent
    )
    assert created2 is False
    assert a1.id == a2.id
    v4_session.expire_all()
    assert (
        len(v4_session.exec(select(Artifact).where(Artifact.type == "work_log")).all())
        == 1
    )


def test_relay_idempotent_conflict(v4_session) -> None:
    """Same key + DIFFERENT payload -> 409 (fail-closed, never silently overwritten)."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-rc")
    project = _make_project(v4_session)
    actor = resolve_agent_actor(agent.id)
    from aios.relay import relay_work_log
    from aios.schemas import WorkLogSubmit

    base = dict(
        project_id=project.id,
        report_type="daily",
        why="because",
        problem="none",
        solution="fixed",
        new_knowledge="learned",
        source_platform="chatgpt",
    )
    p1 = WorkLogSubmit(**base, what_done="version-one")
    p2 = WorkLogSubmit(**base, what_done="version-two")
    relay_work_log(
        v4_session, payload=p1, idempotency_key="k-conflict", actor=actor, agent=agent
    )
    with pytest.raises(ServiceError) as exc:
        relay_work_log(
            v4_session,
            payload=p2,
            idempotency_key="k-conflict",
            actor=actor,
            agent=agent,
        )
    assert exc.value.status_code == 409


def test_relay_idempotency_scoped_per_actor(v4_session) -> None:
    """Two agents relaying the same key+content each get their OWN independent log."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent_a, _ = _bootstrap_via_service(v4_session, "pa", "ra", "jti-a")
    agent_b, _ = _bootstrap_via_service(v4_session, "pb", "rb", "jti-b")
    project = _make_project(v4_session)
    from aios.relay import relay_work_log
    from aios.schemas import WorkLogSubmit

    payload = WorkLogSubmit(
        project_id=project.id,
        report_type="daily",
        what_done="did",
        why="because",
        problem="none",
        solution="fixed",
        new_knowledge="learned",
        source_platform="chatgpt",
    )
    a_art, _ = relay_work_log(
        v4_session,
        payload=payload,
        idempotency_key="same-key",
        actor=resolve_agent_actor(agent_a.id),
        agent=agent_a,
    )
    b_art, _ = relay_work_log(
        v4_session,
        payload=payload,
        idempotency_key="same-key",
        actor=resolve_agent_actor(agent_b.id),
        agent=agent_b,
    )
    assert a_art.id != b_art.id
    v4_session.expire_all()
    a_row = v4_session.get(Artifact, a_art.id)
    b_row = v4_session.get(Artifact, b_art.id)
    assert a_row.metadata_json["produced_by_agent_id"] == agent_a.id
    assert b_row.metadata_json["produced_by_agent_id"] == agent_b.id


def test_relay_concurrent_idempotent(v4_session) -> None:
    """Concurrent same-key same-payload relays converge to exactly one log."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-cr")
    agent_id = agent.id  # capture before cross-session use
    project = _make_project(v4_session)
    eng = get_engine(get_database_url())
    barrier = threading.Barrier(3)
    errors: list[Exception] = []

    def worker() -> None:
        s = Session(eng)
        try:
            from aios.relay import relay_work_log
            from aios.schemas import WorkLogSubmit

            worker_agent = s.get(Agent, agent_id)  # re-fetch in worker session
            assert worker_agent is not None, "agent must exist in worker session"
            payload = WorkLogSubmit(
                project_id=project.id,
                report_type="daily",
                what_done="did",
                why="because",
                problem="none",
                solution="fixed",
                new_knowledge="learned",
                source_platform="chatgpt",
            )
            barrier.wait()
            relay_work_log(
                s,
                payload=payload,
                idempotency_key="k-concurrent",
                actor=resolve_agent_actor(agent_id),
                agent=worker_agent,
            )
        except Exception as exc:  # noqa: BLE001 - surface thread failures as test errors
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Gate B: every concurrent worker must complete successfully (a silent
    # "database is locked" / thread exception must NOT let the test pass) and
    # the duplicate submissions must collapse to exactly one log.
    assert not errors, f"concurrent relay workers failed: {errors}"
    v4_session.expire_all()
    rows = v4_session.exec(select(Artifact).where(Artifact.type == "work_log")).all()
    assert len(rows) == 1  # concurrent duplicates collapse to exactly one


# ===========================================================================
# Gate C — capability catalog
# ===========================================================================


def test_self_register_unknown_capability_422(v4_session) -> None:
    """A capability slug not in the catalog is rejected (fail-closed 422)."""
    with pytest.raises(ServiceError) as exc:
        _bootstrap_via_service(
            v4_session, "p1", "r1", "jti-unknown", capabilities=["ghost-cap"]
        )
    assert exc.value.status_code == 422


def test_self_registered_agent_routable(v4_session) -> None:
    """A self-registered agent with a capability is routable via BEST_AVAILABLE."""
    cap = _seed_capability(v4_session, "writing", "writing")
    agent, _ = _bootstrap_via_service(
        v4_session, "p1", "r1", "jti-route", capabilities=["writing"]
    )
    project = _make_project(v4_session)
    task = Task(
        project_id=project.id,
        title="Draft",
        description="Draft",
        status=TaskStatus.READY,
        required_capabilities=[cap.id],
        routing_mode=RoutingMode.BEST_AVAILABLE,
    )
    v4_session.add(task)
    v4_session.commit()
    v4_session.refresh(task)

    assignment = route_task(v4_session, task.id, "route-1")
    assert assignment is not None
    assert assignment.selected_agent_id == agent.id


# ===========================================================================
# Gate D — relay provenance / never attests
# ===========================================================================


def test_relay_requires_auth(v4_client) -> None:
    """Missing / wrong agent credential on the relay endpoint -> 401."""
    project = _make_project_via_client(v4_client)
    body = {
        "project_id": project,
        "report_type": "daily",
        "what_done": "did",
        "why": "because",
        "problem": "none",
        "solution": "fixed",
        "new_knowledge": "learned",
        "source_platform": "chatgpt",
    }
    no_auth = v4_client.post(
        "/relay/work-logs", json=body, headers={"Idempotency-Key": "k1"}
    )
    assert no_auth.status_code == 401
    bad_auth = v4_client.post(
        "/relay/work-logs",
        json=body,
        headers={"Idempotency-Key": "k1", "Authorization": "Bearer bogus"},
    )
    assert bad_auth.status_code == 401


def test_relay_ingests_unverified(v4_client) -> None:
    """A valid agent relay creates an UNVERIFIED log with auth-derived provenance."""
    token = _mint_token("p1", "r1")
    boot = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert boot.status_code == 201
    credential = boot.json()["credential"]
    project = _make_project_via_client(v4_client)

    resp = v4_client.post(
        "/relay/work-logs",
        json={
            "project_id": project,
            "report_type": "daily",
            "what_done": "did",
            "why": "because",
            "problem": "none",
            "solution": "fixed",
            "new_knowledge": "learned",
            "source_platform": "chatgpt",
        },
        headers={"Idempotency-Key": "relay-k1", "Authorization": f"Bearer {credential}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["review_status"] == "unverified"
    # Provenance is the authenticated agent, never the owner or a foreign id.
    assert body["id"]


def test_relay_provenance_derived_from_auth(v4_client) -> None:
    """A conflicting body produced_by_agent_id is rejected (422)."""
    token = _mint_token("p1", "r1")
    boot = v4_client.post(
        "/agents/bootstrap",
        json={
            "platform": "p1",
            "external_ref": "r1",
            "name": "agent",
            "role": "worker",
            "adapter_type": "model",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    credential = boot.json()["credential"]
    project = _make_project_via_client(v4_client)

    resp = v4_client.post(
        "/relay/work-logs",
        json={
            "project_id": project,
            "report_type": "daily",
            "what_done": "did",
            "why": "because",
            "problem": "none",
            "solution": "fixed",
            "new_knowledge": "learned",
            "produced_by_agent_id": "some-other-agent-id",  # conflicts with auth
            "source_platform": "chatgpt",
        },
        headers={"Idempotency-Key": "relay-k2", "Authorization": f"Bearer {credential}"},
    )
    assert resp.status_code == 422


def test_relay_never_attests(v4_session) -> None:
    """Relay logs are UNVERIFIED and never flip to APPROVED / KB-eligible."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-na")
    project = _make_project(v4_session)
    from aios.relay import relay_work_log
    from aios.schemas import WorkLogSubmit

    payload = WorkLogSubmit(
        project_id=project.id,
        report_type="daily",
        what_done="did",
        why="because",
        problem="none",
        solution="fixed",
        new_knowledge="learned",
        source_platform="chatgpt",
    )
    artifact, _ = relay_work_log(
        v4_session,
        payload=payload,
        idempotency_key="k-na",
        actor=resolve_agent_actor(agent.id),
        agent=agent,
    )
    v4_session.expire_all()
    row = v4_session.get(Artifact, artifact.id)
    assert row.review_status == ArtifactReviewStatus.UNVERIFIED
    assert row.metadata_json.get("should_enter_kb") is False


def test_relay_forces_kb_ineligible(v4_session) -> None:
    """A relay caller cannot self-promote logs into the KB: a supplied
    should_enter_kb=true is forced to False at the relay trust boundary."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-kb")
    project = _make_project(v4_session)
    from aios.relay import relay_work_log
    from aios.schemas import WorkLogSubmit

    payload = WorkLogSubmit(
        project_id=project.id,
        report_type="daily",
        what_done="did",
        why="because",
        problem="none",
        solution="fixed",
        new_knowledge="learned",
        source_platform="chatgpt",
        should_enter_kb=True,  # attacker attempts to mark KB-eligible
    )
    artifact, _ = relay_work_log(
        v4_session,
        payload=payload,
        idempotency_key="k-kb",
        actor=resolve_agent_actor(agent.id),
        agent=agent,
    )
    v4_session.expire_all()
    row = v4_session.get(Artifact, artifact.id)
    assert row.metadata_json.get("should_enter_kb") is False


# ===========================================================================
# Gate E — auditability
# ===========================================================================


def test_bootstrap_creates_audited(v4_session) -> None:
    """Successful bootstrap is audited with upserted=False and no secret leak."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, credential = _bootstrap_via_service(
        v4_session, "p1", "r1", "jti-audit", capabilities=["cap_x"]
    )
    actions = _audit_actions(v4_session)
    assert "agent.self_registered" in actions
    audit = v4_session.exec(
        select(AuditLog).where(AuditLog.action == "agent.self_registered")
    ).one()
    assert audit.after_snapshot.get("upserted") is False
    serialized = str(audit.after_snapshot)
    # No secret material may appear in the audit trail -- these must ALL hold
    # (conjunctive). The opaque handle and the plaintext credential are never
    # written to AuditLog.
    assert "secret_ref" not in serialized
    assert "credential" not in serialized
    assert "nvapi-" not in serialized
    assert "sk-" not in serialized


def test_self_register_audit_logged(v4_session) -> None:
    """Self-update upsert is audited with upserted=True."""
    _seed_capability(v4_session, "positioning", "positioning")
    agent, _ = _bootstrap_via_service(
        v4_session, "p1", "r1", "jti-aud2", capabilities=["positioning"]
    )
    upsert_agent(
        v4_session,
        actor=resolve_agent_actor(agent.id),
        platform="p1",
        external_ref="r1",
        name="agent",
        role="writer",
        adapter_type="model",
        capabilities=["positioning"],
    )
    audits = v4_session.exec(
        select(AuditLog).where(AuditLog.action == "agent.self_registered")
    ).all()
    # Both the bootstrap CREATE and the self-update upsert are audited; the
    # upsert is the one flagged upserted=True.
    assert len(audits) == 2
    upsert_audit = next(a for a in audits if a.after_snapshot.get("upserted") is True)
    assert upsert_audit is not None


def test_bootstrap_collision_not_audited(v4_session) -> None:
    """A rejected (collision) bootstrap writes NO self_registered audit."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    _bootstrap_via_service(v4_session, "p1", "r1", "J1", capabilities=["cap_x"])
    with pytest.raises(ServiceError):
        _bootstrap_via_service(v4_session, "p1", "r1", "J2", capabilities=["cap_x"])
    actions = _audit_actions(v4_session)
    # Only the successful registration is audited; the collision is not.
    assert actions.count("agent.self_registered") == 1


def test_relay_ingest_audit_logged(v4_session) -> None:
    """Relay ingest is audited with agent_id / source_platform and no secret."""
    _seed_capability(v4_session, "cap_x", "cap_x")
    agent, _ = _bootstrap_via_service(v4_session, "p1", "r1", "jti-ra")
    project = _make_project(v4_session)
    from aios.relay import relay_work_log
    from aios.schemas import WorkLogSubmit

    payload = WorkLogSubmit(
        project_id=project.id,
        report_type="daily",
        what_done="did",
        why="because",
        problem="none",
        solution="fixed",
        new_knowledge="learned",
        source_platform="chatgpt",
    )
    relay_work_log(
        v4_session,
        payload=payload,
        idempotency_key="k-ra",
        actor=resolve_agent_actor(agent.id),
        agent=agent,
    )
    audit = v4_session.exec(
        select(AuditLog).where(AuditLog.action == "relay.work_log_ingested")
    ).one()
    assert audit.after_snapshot.get("agent_id") == agent.id
    assert audit.after_snapshot.get("source_platform") == "chatgpt"


# ===========================================================================
# Gate F — single controlled migration / no new tables / attest untouched
# ===========================================================================


def test_alembic_single_new_head() -> None:
    """Alembic head is the current single leaf: past the V4 secret-store #103
    slice (20260730_0001), the #109 customer-service workflow slice
    (20260731_0001) and the SalesPlaybook V0 slice (20260812_0001)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head == "20260827_0001_workforce_capreq_hardening"


def test_migration_adds_bootstrap_token_ref_no_second_index(v4_session) -> None:
    """The controlled migration adds only the partial unique index + one column."""
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(v4_session.get_bind())
    cols = {c["name"] for c in inspector.get_columns("agent")}
    assert "bootstrap_token_ref" in cols

    indexes = inspector.get_indexes("agent")
    unique_names = [ix["name"] for ix in indexes if ix.get("unique")]
    assert "uq_agent_platform_external_ref" in unique_names

    # The claim column must NOT carry a second (standalone) index.
    indexed_cols = set()
    for ix in indexes:
        indexed_cols.update(ix.get("column_names", []))
    assert "bootstrap_token_ref" not in indexed_cols


def test_no_new_tables(v4_session) -> None:
    """V4 reuses Agent/Capability/AgentCapability; #103 adds ONLY agent_secret.

    No *other* secret-store / token table (bootstrap_token, secret_store,
    agent_credential) was created -- agent_secret is the single, deliberate
    addition carrying only KEK-derived HMAC tags (issue #103 §4.2).
    """
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(v4_session.get_bind())
    tables = set(inspector.get_table_names())
    # No *extra* secret-store / token table was created beyond the one #103 adds.
    for forbidden in ("bootstrap_token", "secret_store", "agent_credential"):
        assert forbidden not in tables
    # The pre-existing registry tables are still present (reused, not forked).
    for expected in ("agent", "capability", "agent_capability", "agent_secret"):
        assert expected in tables


def test_attest_path_untouched(v4_session) -> None:
    """The owner-only submit + attest path still flips a log to APPROVED (Gate F)."""
    from aios.work_log import WorkLogService

    project = _make_project(v4_session)
    artifact, _ = WorkLogService(v4_session).submit_work_log(
        project_id=project.id,
        report_type="daily",
        what_done="did",
        why="because",
        problem="none",
        solution="fixed",
        new_knowledge="learned",
        idempotency_key="attest-k",
        actor=ActorContext(kind="owner", owner_id="owner"),
    )
    assert artifact.review_status == ArtifactReviewStatus.UNVERIFIED
    attested = WorkLogService(v4_session).attest_work_log(
        artifact_id=artifact.id,
        actor=ActorContext(kind="owner", owner_id="owner"),
    )
    assert attested.review_status == ArtifactReviewStatus.APPROVED


# ===========================================================================
# Capability discovery endpoints
# ===========================================================================


def test_list_capabilities_returns_catalog(v4_session) -> None:
    _seed_capability(v4_session, "cap_a", "cap_a")
    _seed_capability(v4_session, "cap_b", "cap_b")
    catalog = list_capabilities(v4_session)
    names = {c.name for c in catalog}
    assert "cap_a" in names
    assert "cap_b" in names


def test_list_agents_by_capability_filters(v4_session) -> None:
    """list_agents(capability=) returns only enabled agents with that capability."""
    _seed_capability(v4_session, "positioning", "positioning")
    _seed_capability(v4_session, "xhs_adaptation", "xhs_adaptation")
    agent, _ = _bootstrap_via_service(
        v4_session, "p1", "r1", "jti-filt", capabilities=["positioning"]
    )
    matched = list_agents(v4_session, capability="positioning")
    assert any(a.id == agent.id for a in matched)
    # Unknown slug -> 422.
    with pytest.raises(ServiceError) as exc:
        list_agents(v4_session, capability="ghost")
    assert exc.value.status_code == 422


def test_capabilities_endpoint(v4_client) -> None:
    resp = v4_client.get("/capabilities")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_agents_by_capability_endpoint(tmp_path, monkeypatch) -> None:
    """GET /agents?capability=<known-but-unclaimed> -> 200 empty; unknown -> 422.

    Self-contained: builds its own app + DB so the capability catalog can be
    seeded into the exact DB the endpoint reads from (the ``v4_client`` fixture
    shares no DB with ``v4_session``).
    """
    db_url = f"sqlite:///{(tmp_path / 'cap_endpoint.db').as_posix()}"
    monkeypatch.setenv("AIOS_DATABASE_URL", db_url)
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", OWNER_API_KEY)
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)

    run_migrations(db_url)
    # Seed a KNOWN capability so filtering by it is a 200 (empty), not a 422.
    eng = get_engine(db_url)
    with Session(eng) as s:
        s.add(Capability(id="positioning", name="positioning"))
        s.commit()

    app = create_app()
    app.dependency_overrides[authenticate_owner] = (
        lambda: ActorContext(kind="owner", owner_id="owner")
    )
    with TestClient(app) as client:
        ok = client.get("/agents", params={"capability": "positioning"})
        assert ok.status_code == 200  # known-but-unclaimed -> empty list, not error
        assert ok.json() == []
        bad = client.get("/agents", params={"capability": "ghost"})
        assert bad.status_code == 422  # unknown slug -> fail-closed


# ---------------------------------------------------------------------------
# Client helper (endpoint tests need a project created through the app)
# ---------------------------------------------------------------------------


def _make_project_via_client(client) -> str:
    resp = client.post(
        "/projects",
        json={"name": "P", "objective": "O"},
        headers={"Idempotency-Key": "proj-key"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]
