"""Issue #50: propagate structured upstream Artifact data into downstream TaskContext.

These tests prove that a dependent task (T2) receives BOTH the existing
artifact summary information AND the structured ``metadata_json["artifacts"]``
payload produced by its upstream dependency (T1). They also prove the
fail-safe behavior for missing / malformed ``artifacts`` values and that
context construction is deterministic.

Scope (per Issue #50):
- adds the ``artifacts`` field to each dependency output;
- preserves ``artifact_id`` / ``type`` / ``summary`` unchanged;
- no migration, no routing / approval / execution-protocol changes;
- no real model calls, no credentials or cost gate.
"""

import json
from pathlib import Path

from sqlmodel import Session, select

from aios.context_service import ContextService, _structured_artifacts
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
)


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def seed_chain(session: Session, t1_artifact_metadatas: list[dict], prefix: str = "p50") -> tuple:
    """Create project -> T1(DONE, with given artifacts) -> T2(depends_on T1).

    ``prefix`` uniquifies entity ids so the helper can be called multiple times
    within the same session (e.g. the malformed-values test).
    """
    project = Project(name=f"{prefix}-P", objective="O50", description="desc")
    agent = Agent(
        id=f"agt_{prefix}",
        name=f"A{prefix}",
        role="writer",
        adapter_type=AdapterType.EXTERNAL,
        permissions=[],
        limitations=[],
    )
    session.add_all([project, agent])
    session.flush()
    t1 = Task(project_id=project.id, title="T1", description="produce", status=TaskStatus.DONE)
    session.add(t1)
    session.flush()
    t2 = Task(
        project_id=project.id,
        title="T2",
        description="consume",
        status=TaskStatus.READY,
        assigned_agent_id=agent.id,
        routing_mode=RoutingMode.BEST_AVAILABLE,
        acceptance_criteria=["x"],
        depends_on=[t1.id],
    )
    session.add(t2)
    session.flush()
    for idx, meta in enumerate(t1_artifact_metadatas):
        session.add(
            Artifact(
                project_id=project.id,
                task_id=t1.id,
                type=ArtifactType.JSON,
                uri=f"t1-{idx}.json",
                checksum=f"cs-{idx}",
                review_status=ArtifactReviewStatus.APPROVED,
                metadata_json=meta,
            )
        )
    session.commit()
    return project, t1, t2, agent


# --------------------------------------------------------------------------- #
# Happy path: structured nested field reaches the downstream task             #
# --------------------------------------------------------------------------- #
def test_structured_artifacts_propagated_to_downstream(tmp_path: Path) -> None:
    url = database(tmp_path, "p50_happy.db")
    with Session(get_engine(url)) as s:
        meta = {
            "summary": "T1 summary text",
            "artifacts": [
                {
                    "type": "json",
                    "uri": "exec://tsk_t1/output-001",
                    "data": {
                        "headline": "Concrete headline from T1",
                        "metrics": {"score": 42, "pass": True},
                    },
                }
            ],
        }
        _, _, t2, _ = seed_chain(s, [meta])
        ctx = ContextService(s).build_context(t2.id)
        outs = ctx.dependency_outputs
        assert len(outs) == 1
        out = outs[0]

        # Backward-compatible fields remain unchanged.
        assert out["artifact_id"]
        assert out["type"] == "json"
        assert out["summary"] == "T1 summary text"

        # The concrete nested structured field produced by T1 is readable.
        assert out["artifacts"][0]["type"] == "json"
        assert out["artifacts"][0]["uri"] == "exec://tsk_t1/output-001"
        assert out["artifacts"][0]["data"]["headline"] == "Concrete headline from T1"
        assert out["artifacts"][0]["data"]["metrics"]["score"] == 42
        assert out["artifacts"][0]["data"]["metrics"]["pass"] is True


# --------------------------------------------------------------------------- #
# Missing / malformed -> fail-safe empty list                                  #
# --------------------------------------------------------------------------- #
def test_missing_artifacts_falls_back_to_empty_list(tmp_path: Path) -> None:
    url = database(tmp_path, "p50_missing.db")
    with Session(get_engine(url)) as s:
        _, _, t2, _ = seed_chain(s, [{"summary": "no artifacts key"}])
        ctx = ContextService(s).build_context(t2.id)
        assert ctx.dependency_outputs[0]["artifacts"] == []


def test_malformed_artifacts_falls_back_to_empty_list(tmp_path: Path) -> None:
    url = database(tmp_path, "p50_malformed.db")
    with Session(get_engine(url)) as s:
        # string instead of list
        bad_str = {"summary": "bad", "artifacts": "not-a-list"}
        _, _, t2a, _ = seed_chain(s, [bad_str], prefix="mal_a")
        ctx_a = ContextService(s).build_context(t2a.id)
        assert ctx_a.dependency_outputs[0]["artifacts"] == []

        # dict instead of list
        _, _, t2b, _ = seed_chain(s, [{"summary": "bad2", "artifacts": {"k": 1}}], prefix="mal_b")
        ctx_b = ContextService(s).build_context(t2b.id)
        assert ctx_b.dependency_outputs[0]["artifacts"] == []

        # explicit null
        _, _, t2c, _ = seed_chain(s, [{"summary": "bad3", "artifacts": None}], prefix="mal_c")
        ctx_c = ContextService(s).build_context(t2c.id)
        assert ctx_c.dependency_outputs[0]["artifacts"] == []


# --------------------------------------------------------------------------- #
# Determinism: identical upstream artifacts -> identical context / hash        #
# --------------------------------------------------------------------------- #
def test_context_construction_is_deterministic_with_artifacts(tmp_path: Path) -> None:
    url = database(tmp_path, "p50_det.db")
    with Session(get_engine(url)) as s:
        meta = {
            "summary": "S",
            "artifacts": [
                {
                    "type": "json",
                    "uri": "u",
                    "data": {"k": "v", "n": [1, 2, 3]},
                }
            ],
        }
        _, _, t2, _ = seed_chain(s, [meta])
        first = ContextService(s).build_context(t2.id)
        second = ContextService(s).build_context(t2.id)

        assert first.id == second.id
        assert first.context_hash == second.context_hash
        assert first.dependency_outputs == second.dependency_outputs
        # list ordering is stable (no nondeterministic transformation)
        assert first.dependency_outputs[0]["artifacts"][0]["data"]["n"] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Unit test of the fail-safe helper                                            #
# --------------------------------------------------------------------------- #
def test_structured_artifacts_helper_is_safe() -> None:
    assert _structured_artifacts({}) == []
    assert _structured_artifacts({"artifacts": None}) == []
    assert _structured_artifacts({"artifacts": "x"}) == []
    assert _structured_artifacts({"artifacts": {"a": 1}}) == []
    assert _structured_artifacts({"artifacts": []}) == []
    payload = [{"type": "json", "data": {"k": "v"}}]
    # valid list is propagated unchanged (content identical, no coercion)
    assert _structured_artifacts({"artifacts": payload}) == payload


# --------------------------------------------------------------------------- #
# Codex (changes-requested) regression: structured payload must obey the       #
# ContextService redaction boundary (SENSITIVE_KEYS -> [REDACTED]).            #
# --------------------------------------------------------------------------- #
def test_sensitive_keys_redacted_in_structured_artifacts(tmp_path: Path) -> None:
    url = database(tmp_path, "p50_redact.db")
    with Session(get_engine(url)) as s:
        meta = {
            "summary": "has secret",
            "artifacts": [
                {
                    "type": "json",
                    "uri": "exec://tsk_t1/secret-001",
                    "data": {
                        "api_key": "sk-secret-value",
                        "token": "tok-value",
                        "title": "Public title",
                        "nested": {"password": "pw-value", "ok": "keep"},
                    },
                }
            ],
        }
        _, t1, t2, _ = seed_chain(s, [meta], prefix="red")

        stored = s.exec(select(Artifact).where(Artifact.task_id == t1.id)).one()
        stored_before = stored.metadata_json

        ctx = ContextService(s).build_context(t2.id)

        # sensitive values are redacted; non-sensitive values are preserved
        data = ctx.dependency_outputs[0]["artifacts"][0]["data"]
        assert data["api_key"] == "[REDACTED]"
        assert data["token"] == "[REDACTED]"
        assert data["nested"]["password"] == "[REDACTED]"
        assert data["nested"]["ok"] == "keep"
        assert data["title"] == "Public title"

        # original secret values never enter the persisted context
        serialized = json.dumps(ctx.dependency_outputs, default=str)
        assert "sk-secret-value" not in serialized
        assert "tok-value" not in serialized
        assert "pw-value" not in serialized

        # stored Artifact metadata_json remains unchanged (immutable snapshot)
        s.refresh(stored)
        assert stored.metadata_json == stored_before
        assert stored.metadata_json["artifacts"][0]["data"]["api_key"] == "sk-secret-value"

        # deterministic / idempotent construction remains intact
        ctx2 = ContextService(s).build_context(t2.id)
        assert ctx.context_hash == ctx2.context_hash
        assert ctx.dependency_outputs == ctx2.dependency_outputs
