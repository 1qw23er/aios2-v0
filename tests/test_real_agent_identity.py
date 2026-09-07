"""Tests for Gap #3: real-agent-identity (registry-backed WorkBuddy / GPT).

Gap #3 upgrades the previously free-text ``"workbuddy"`` actor used by
``scripts/wb_draft_to_aios.py`` into a *registry-validated* identity:

* ``src/aios/known_agents.py`` declares two stable ``Agent`` rows
  (``workbuddy`` = 蟹将 / ``gpt`` = 总编) and seeds them idempotently.
* ``scripts/wb_draft_to_aios.py`` now calls ``agent_registry.get_agent``
  before minting the actor; ``get_agent`` raises ``ServiceError(404)`` when
  the row is absent, so an unseeded / tampered registry fails closed -- no
  draft is ever produced from an unregistered identity.

These tests pin that contract:
  1. ``KNOWN_AGENTS`` declares exactly the two expected agents with the
     correct adapter / delegation / trust invariants.
  2. ``seed_known_agents`` is idempotent (re-run -> already_present, no dupes).
  3. Without seeding, ``create_draft`` fails closed with ``ServiceError``
     (``status_code == 404``) and produces no artifact.
  4. After seeding, ``create_draft`` succeeds and the producer is the
     registry-derived ``agent:workbuddy``.

ZERO migration: the harness builds tables via ``SQLModel.metadata.create_all``
(equivalent to applied migrations for the tables used here). A file-backed
sqlite URL lets the script's own ``make_session()`` share the data.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select

# Load the producer script as a module (absolute aios.* imports). Lives in
# ../scripts relative to this tests/ file.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wb_draft_to_aios.py"
_spec = importlib.util.spec_from_file_location("wb_draft_to_aios", _SCRIPT)
wb_draft_to_aios = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb_draft_to_aios)

from aios.db import get_engine  # noqa: E402
from aios.known_agents import (  # noqa: E402
    GPT_AGENT_ID,
    KNOWN_AGENTS,
    WORKBUDDY_AGENT_ID,
    seed_known_agents,
)
from aios.models import (  # noqa: E402
    AdapterType,
    Agent,
    AgentTrustLevel,
    Artifact,
    Project,
    ProjectStatus,
)
from aios.services import ServiceError  # noqa: E402

PROJECT_ID = "proj_test_real_agent_identity"


@pytest.fixture()
def db_url(tmp_path: Path, monkeypatch) -> str:
    """File-backed sqlite so make_session() (separate connection) shares data.

    Uses monkeypatch.setenv so AIOS_DATABASE_URL is auto-restored after the test
    (prevents a stale DB url leaking into later tests). Teardown disposes the
    engine and clears the get_engine cache to avoid cross-test sqlite file I/O
    collisions on the same machine.
    """
    url = f"sqlite:///{tmp_path / 'test_real_agent_identity.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    engine = get_engine(url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            Project(
                id=PROJECT_ID,
                name="test",
                objective="test objective",
                status=ProjectStatus.PROPOSED,
            )
        )
        s.commit()
    yield url
    engine.dispose()
    get_engine.cache_clear()


def test_known_agents_structure():
    """KNOWN_AGENTS declares exactly workbuddy + gpt with correct invariants."""
    assert set(KNOWN_AGENTS) == {WORKBUDDY_AGENT_ID, GPT_AGENT_ID}

    wb = KNOWN_AGENTS[WORKBUDDY_AGENT_ID]
    gpt = KNOWN_AGENTS[GPT_AGENT_ID]

    # Identity + role text (the 2026-08-24 content-workflow spec).
    assert wb["name"] == "WorkBuddy (蟹将)"
    assert wb["role"] == "业务研究员 + 初稿执行 Agent"
    assert gpt["name"] == "GPT (总编)"
    assert gpt["role"] == "内容总编 + 视觉总监"

    # Both are LOCAL CLI adapters -> delegation_mode MUST be omitted (None);
    # only external adapters (API/EXTERNAL) specify a delegation mode.
    assert wb["adapter_type"] is AdapterType.CLI
    assert wb["delegation_mode"] is None
    assert gpt["adapter_type"] is AdapterType.CLI
    assert gpt["delegation_mode"] is None

    # Both are trusted internal agents of the one-person company toolchain.
    assert wb["trust_level"] is AgentTrustLevel.INTERNAL
    assert gpt["trust_level"] is AgentTrustLevel.INTERNAL

    # List-typed fields are present and are genuine lists.
    for spec in (wb, gpt):
        assert isinstance(spec["capabilities"], list) and spec["capabilities"]
        assert isinstance(spec["limitations"], list)
        assert isinstance(spec["permissions"], list) and spec["permissions"]


def test_seed_known_agents_is_idempotent(db_url):
    """Re-running seed_known_agents never duplicates rows or fails."""
    engine = get_engine(db_url)
    with Session(engine) as s:
        first = seed_known_agents(s)
    with Session(engine) as s:
        second = seed_known_agents(s)

    assert first == {
        WORKBUDDY_AGENT_ID: "inserted",
        GPT_AGENT_ID: "inserted",
    }
    assert second == {
        WORKBUDDY_AGENT_ID: "already_present",
        GPT_AGENT_ID: "already_present",
    }

    # Exactly two agent rows exist -- no duplication on the second seed.
    with Session(engine) as s:
        rows = s.exec(select(Agent)).all()
        assert len(rows) == 2
        ids = {r.id for r in rows}
        assert ids == {WORKBUDDY_AGENT_ID, GPT_AGENT_ID}


def test_create_draft_fails_closed_without_seed(db_url):
    """Unseeded / tampered registry must refuse to produce a draft.

    The script calls ``get_agent`` before minting the actor; when the
    ``workbuddy`` row is absent, ``get_agent`` raises ``ServiceError(404)``.
    No artifact may be created from an unregistered identity.
    """
    with pytest.raises(ServiceError) as exc:
        wb_draft_to_aios.create_draft(
            project_id=PROJECT_ID,
            topic="未注册身份不应产生草稿",
            body="正文……",
            idempotency_key="kc-noseed",
        )
    assert exc.value.status_code == 404

    # Fail-closed: nothing was persisted.
    engine = get_engine(db_url)
    with Session(engine) as s:
        assert s.exec(select(Artifact)).all() == []


def test_seed_then_create_draft_produces_registry_identity(db_url):
    """After seeding, create_draft succeeds with producer == 'agent:workbuddy'."""
    engine = get_engine(db_url)
    with Session(engine) as s:
        seed_known_agents(s)

    result = wb_draft_to_aios.create_draft(
        project_id=PROJECT_ID,
        topic="电商卖家如何用 AI 做内容",
        body="就是一段普通正文，没有分节标记。",
        idempotency_key="kc-seeded",
    )
    assert result["artifact_id"]
    assert result["producer"] == "agent:workbuddy"
