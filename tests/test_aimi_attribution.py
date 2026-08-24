"""Tests for aios/attribution.py + content_draft attribution_key (Gap #2).

Closes the "publish -> signup" attribution loop WITHOUT a new table: the
attribution key is minted into draft metadata, and signup observations land in
the existing immutable AuditLog (action ``content.aimi_attribution``). These
tests verify key format / uniqueness / slug degradacy, URL building (default +
env override), the record/sum aggregation with fail-closed semantics, and the
end-to-end ``create_draft`` integration.

Zero-migration: tables come from ``SQLModel.metadata.create_all`` -- equivalent
to the applied migrations for the tables we touch. The file-backed sqlite url is
shared with the script's own ``make_session()`` via ``monkeypatch.setenv``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel

from aios.actor import resolve_agent_actor
from aios.attribution import (
    AIMI_ATTRIBUTION_AUDIT,
    build_aimi_signup_url,
    generate_attribution_key,
    record_aimi_attribution,
    sum_aimi_attributions,
)
from aios.audit import AuditLog
from aios.db import get_engine
from aios.models import Artifact, ArtifactType, Project, ProjectStatus

PROJECT_ID = "proj_test_aimi_attr"

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wb_draft_to_aios.py"
_spec = importlib.util.spec_from_file_location("wb_draft_to_aios", _SCRIPT)
wb_draft_to_aios = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb_draft_to_aios)


@pytest.fixture()
def db_url(tmp_path: Path, monkeypatch) -> str:
    """File-backed sqlite so make_session() (separate connection) shares data.

    The teardown clears ``get_engine``'s ``lru_cache`` so the per-test temp DB
    engines (one per unique URL) do not accumulate across the whole module and
    exhaust file handles / trigger Windows sqlite lock contention. This is a
    test-harness concern only; no production code is touched.
    """
    url = f"sqlite:///{tmp_path / 'test_aimi_attr.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    engine = get_engine(url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            Project(
                id=PROJECT_ID,
                name="test",
                objective="test",
                status=ProjectStatus.PROPOSED,
            )
        )
        s.commit()
    yield url
    # Dispose the pooled connections so Windows releases the sqlite file locks
    # (WAL/SHM siblings) and pytest's tmp_path cleanup can delete the temp DB.
    # Without this, accumulated locked handles hang the run after ~12 tests.
    engine.dispose()
    get_engine.cache_clear()


def _actor() -> str:
    return resolve_agent_actor("workbuddy", PROJECT_ID).derive_submitted_by()


# --- key generation -------------------------------------------------------


def test_attribution_key_format_and_prefix():
    key = generate_attribution_key("电商卖家如何用 AI 做内容")
    assert key.startswith("aios_")
    parts = key.split("_")
    assert len(parts) == 3  # aios_<slug>_<hex8>
    assert parts[0] == "aios"
    assert len(parts[2]) == 8  # secrets.token_hex(4) -> 8 hex chars


def test_attribution_key_unique_per_call():
    k1 = generate_attribution_key("same topic")
    k2 = generate_attribution_key("same topic")
    assert k1 != k2  # random suffix guarantees per-article uniqueness


def test_attribution_key_ascii_slug_is_readable():
    key = generate_attribution_key("How To Use AI For Ecommerce")
    slug = key.split("_")[1]
    assert slug == "how-to-use-ai-for-ecommerce"
    assert not slug.startswith("t")  # no hash fallback for ASCII topics


def test_attribution_key_cjk_only_slug_degrades_to_hash():
    # Pure-CJK topic (no ASCII letters) has no ASCII representation, so it MUST
    # fall back to the stable ``t<hex6>`` slug rather than emit mojibake.
    key = generate_attribution_key("电商卖家如何用内容")
    slug = key.split("_")[1]
    assert slug.startswith("t") and len(slug) == 7  # t + 6 hex digest


def test_attribution_key_mixed_cjk_ascii_keeps_ascii_slug():
    # A mixed topic keeps only the ASCII letters; "AI" survives, CJK dropped.
    # The random suffix still guarantees per-article uniqueness.
    key = generate_attribution_key("电商卖家如何用AI做内容")
    slug = key.split("_")[1]
    assert slug == "ai"


# --- URL building ---------------------------------------------------------


def test_build_signup_url_default():
    key = generate_attribution_key("topic")
    assert build_aimi_signup_url(key) == f"https://aimi.quantv.com/register?ref={key}"


def test_build_signup_url_env_override(monkeypatch):
    monkeypatch.setenv("AIMI_SIGNUP_BASE_URL", "https://example.test/reg")
    key = generate_attribution_key("topic")
    assert build_aimi_signup_url(key) == "https://example.test/reg?ref=" + key
    # explicit base arg wins over env
    assert (
        build_aimi_signup_url(key, base_url="https://x.test/join")
        == "https://x.test/join?ref=" + key
    )


# --- record / sum aggregation --------------------------------------------


def _new_draft(engine, metadata: dict | None = None) -> str:
    with Session(engine) as s:
        art = Artifact(
            project_id=PROJECT_ID,
            type=ArtifactType.CONTENT_DRAFT,
            uri="body",
            checksum="sha256:x",
            metadata_json=metadata or {},
        )
        s.add(art)
        s.commit()
        s.refresh(art)
        return art.id


def test_record_and_sum_attribution(db_url):
    engine = get_engine(db_url)
    aid = _new_draft(engine)
    with Session(engine) as s:
        record_aimi_attribution(
            s,
            project_id=PROJECT_ID,
            actor=_actor(),
            artifact_id=aid,
            registrations=3,
            attribution_key="aios-k1-abc",
        )
        record_aimi_attribution(
            s,
            project_id=PROJECT_ID,
            actor=_actor(),
            artifact_id=aid,
            registrations=5,
            attribution_key="aios-k1-abc",
        )
        s.commit()
    with Session(engine) as s:  # sum from a fresh session
        assert sum_aimi_attributions(s, aid) == 8


def test_record_recovers_attribution_key_from_metadata(db_url):
    engine = get_engine(db_url)
    aid = _new_draft(engine, metadata={"attribution_key": "aios-meta-deadbeef"})
    with Session(engine) as s:
        # attribution_key omitted -> recovered from the draft metadata
        record_aimi_attribution(
            s,
            project_id=PROJECT_ID,
            actor=_actor(),
            artifact_id=aid,
            registrations=2,
        )
        s.commit()
        rows = s.exec(
            AuditLog.__table__.select().where(AuditLog.resource_id == aid)
        ).all()
    assert any(r.after_snapshot.get("attribution_key") == "aios-meta-deadbeef" for r in rows)


def test_sum_zero_for_unknown_artifact(db_url):
    engine = get_engine(db_url)
    with Session(engine) as s:
        assert sum_aimi_attributions(s, "does-not-exist") == 0


def test_sum_fail_closed_on_malformed_rows(db_url):
    engine = get_engine(db_url)
    aid = _new_draft(engine)
    with Session(engine) as s:
        record_aimi_attribution(
            s,
            project_id=PROJECT_ID,
            actor=_actor(),
            artifact_id=aid,
            registrations=4,
            attribution_key="k",
        )
        # Inject a malformed audit row (non-int registrations) directly to prove
        # sum() tolerates it instead of raising or inflating the total.
        s.add(
            AuditLog(
                actor=_actor(),
                action=AIMI_ATTRIBUTION_AUDIT,
                resource_type="artifact",
                resource_id=aid,
                project_id=PROJECT_ID,
                before_snapshot={},
                after_snapshot={"registrations": "oops", "attribution_key": "k"},
                idempotency_key="audit:aimi_attribution:bad:1",
            )
        )
        s.commit()
    with Session(engine) as s:
        assert sum_aimi_attributions(s, aid) == 4  # bad row counted as 0


def test_record_rejects_non_int_registrations(db_url):
    engine = get_engine(db_url)
    aid = _new_draft(engine)
    with Session(engine) as s, pytest.raises(TypeError):
        record_aimi_attribution(
            s,
            project_id=PROJECT_ID,
            actor=_actor(),
            artifact_id=aid,
            registrations="5",  # type mismatch must fail closed
        )


# --- create_draft integration --------------------------------------------


def test_create_draft_lands_attribution_key_and_url(db_url):
    result = wb_draft_to_aios.create_draft(
        project_id=PROJECT_ID,
        topic="电商卖家如何用 AI 做内容",
        body="# 初稿\n\n### C. 文章结构\n- CTA：注册 AI觅\n",
        idempotency_key="attr-key-1",
    )
    assert result["attribution_key"]
    assert result["attribution_key"].startswith("aios_")
    assert result["aimi_signup_url"].startswith("https://aimi.quantv.com/register?ref=")
    assert result["aimi_signup_url"].endswith(result["attribution_key"])

    engine = get_engine(db_url)
    with Session(engine) as s:
        art = s.get(Artifact, result["artifact_id"])
        assert (art.metadata_json or {}).get("attribution_key") == result["attribution_key"]


def test_create_draft_preserves_supplied_attribution_key(db_url):
    result = wb_draft_to_aios.create_draft(
        project_id=PROJECT_ID,
        topic="指定 key 的文章",
        body="正文",
        idempotency_key="attr-key-2",
        attribution_key="aios-custom-12345678",
    )
    assert result["attribution_key"] == "aios-custom-12345678"
    assert (
        result["aimi_signup_url"]
        == "https://aimi.quantv.com/register?ref=aios-custom-12345678"
    )
