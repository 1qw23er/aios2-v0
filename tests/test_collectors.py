"""V2 (#92) collector + attest-override + structured-feed tests.

Covers docs/issue-92-v2-plan.md §8. Pure-function ``normalize`` is exhaustively
unit-tested; the service-layer ingestion path (collector -> ``submit_work_log``
-> ``attest_work_log`` override -> ``KnowledgeHarvester``) is exercised through
``WorkLogService`` directly (not the HTTP endpoint), matching the CLI script
path (plan §5/§8). API/endpoint tests live in ``test_api_work_log.py``.
"""

import sys
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlmodel import Session, select

from aios.actor import resolve_owner_actor
from aios.audit import AuditLog
from aios.collectors import (
    CodexAdapter,
    CollectorError,
    CozeAdapter,
    HermesAdapter,
    RawLog,
    WorkBuddyAdapter,
)
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.models import (
    Agent,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    KnowledgeFact,
    KnowledgeReviewDecisionValue,
    Project,
)
from aios.services import ServiceError
from aios.work_log import (
    ContentFeed,
    KnowledgeHarvester,
    WorkLogService,
    _sha256,
    canonical_json,
)

# The collection scripts live in ../scripts; add them to the import path so we
# can exercise run_one / collect_all.main directly (subprocess-free).
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from _collect_common import run_one  # noqa: E402

OWNER = resolve_owner_actor()


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'collectors.db').as_posix()}"
    run_migrations(url)
    return url


@pytest.fixture()
def session(db_url: str):
    with Session(get_engine(db_url)) as s:
        yield s

CODEX_RAW = {
    "goal": "g",
    "background": "b",
    "blocker": "p",
    "action": "s",
    "conclusion": "n",
}
HERMES_RAW = {
    "topic": "g",
    "need": "b",
    "pain": "p",
    "draft": "s",
    "methodology": "n",
}
WORKBUDDY_RAW = {
    "title": "g",
    "context": "b",
    "problem": "p",
    "deliverable": "s",
    "insight": "n",
}
COZE_RAW = {
    "workflow": "g",
    "trigger_reason": "b",
    "blocker": "p",
    "output": "s",
    "experience": "n",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_project(session: Session, name: str = "P") -> Project:
    project = Project(name=name, objective="O")
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _make_agent(session: Session, platform: str) -> Agent:
    agent = Agent(
        name=f"{platform}-bot",
        role="writer",
        adapter_type="external",
        platform=platform,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


def _submit_draft(
    session: Session,
    project: Project,
    adapter: object,
    external_id: str,
    *,
    raw: dict,
    agent_id: str | None = None,
) -> Artifact:
    rl = RawLog(
        external_id=external_id,
        captured_at="2026-01-01T00:00:00Z",
        raw=raw,
        source_platform=adapter.platform,  # type: ignore[attr-defined]
    )
    submit = adapter.normalize(rl, project_id=project.id)  # type: ignore[attr-defined]
    artifact, _ = WorkLogService(session).submit_work_log(
        project_id=project.id,
        report_type=submit.report_type,
        what_done=submit.what_done,
        why=submit.why,
        problem=submit.problem,
        solution=submit.solution,
        new_knowledge=submit.new_knowledge,
        idempotency_key=f"collector:{agent_id or 'agn'}:{external_id}",
        actor=OWNER,
        content_value=submit.content_value,
        should_enter_kb=submit.should_enter_kb,
        content_angle=submit.content_angle,
        source_platform=adapter.platform,  # type: ignore[attr-defined]
    )
    return artifact


def _make_fact(
    session: Session, artifact: Artifact, *, statement: str, series_id: str
) -> KnowledgeFact:
    service = KnowledgeService(session)
    candidate = service.submit_candidate(
        artifact.id, statement, project_id=artifact.project_id,
        tags=["knowledge_capture"], actor=OWNER,
    )
    result = service.review_candidate(
        candidate.id, KnowledgeReviewDecisionValue.APPROVE, "rationale",
        actor=OWNER, series_id=series_id, version=1,
    )
    assert result.fact is not None
    return result.fact


# ---------------------------------------------------------------------------
# normalize (pure function) -- per platform + invariants
# ---------------------------------------------------------------------------


def test_codex_normalize_basic() -> None:
    out = CodexAdapter().normalize(
        RawLog("s1", "t", CODEX_RAW, "codex"), project_id="prj"
    )
    assert out.project_id == "prj"
    assert out.what_done == "g"
    assert out.why == "b"
    assert out.problem == "p"
    assert out.solution == "s"
    assert out.new_knowledge == "n"
    assert out.source_platform == "codex"


def test_hermes_normalize_basic() -> None:
    out = HermesAdapter().normalize(
        RawLog("s1", "t", HERMES_RAW, "hermes"), project_id="prj"
    )
    assert out.what_done == "g" and out.new_knowledge == "n"
    assert out.source_platform == "hermes"


def test_workbuddy_normalize_basic() -> None:
    out = WorkBuddyAdapter().normalize(
        RawLog("s1", "t", WORKBUDDY_RAW, "workbuddy"), project_id="prj"
    )
    assert out.what_done == "g" and out.new_knowledge == "n"
    assert out.source_platform == "workbuddy"


def test_coze_normalize_basic() -> None:
    out = CozeAdapter().normalize(
        RawLog("s1", "t", COZE_RAW, "coze"), project_id="prj"
    )
    assert out.what_done == "g" and out.new_knowledge == "n"
    assert out.source_platform == "coze"


def test_normalize_injects_source_platform() -> None:
    raw_by_platform = {
        "codex": CODEX_RAW,
        "hermes": HERMES_RAW,
        "workbuddy": WORKBUDDY_RAW,
        "coze": COZE_RAW,
    }
    for adapter in (CodexAdapter(), HermesAdapter(), WorkBuddyAdapter(), CozeAdapter()):
        out = adapter.normalize(
            RawLog("x", "t", raw_by_platform[adapter.platform], adapter.platform),
            project_id="prj",
        )
        assert out.source_platform == adapter.platform


def test_normalize_does_not_set_agent_provenance() -> None:
    out = CodexAdapter().normalize(
        RawLog("x", "t", CODEX_RAW, "codex"), project_id="prj"
    )
    assert out.produced_by_agent_id is None
    assert out.task_ref is None


def test_normalize_drops_untrusted_identity() -> None:
    raw = dict(CODEX_RAW, agent="evil", owner="x", author="someone")
    out = CodexAdapter().normalize(RawLog("x", "t", raw, "codex"), project_id="prj")
    # The schema has no identity field; the draft must not carry any claim.
    assert out.produced_by_agent_id is None
    assert out.task_ref is None


def test_normalize_content_value_default() -> None:
    # Long, signal-rich text must STILL default to "low" (never auto-medium),
    # so a collected draft is not auto-harvestable (plan §4 / v18).
    raw = dict(CODEX_RAW, conclusion="实验结论，踩坑明显，决策数据对比，值得沉淀为固定套路长文本")
    out = CodexAdapter().normalize(RawLog("x", "t", raw, "codex"), project_id="prj")
    assert out.content_value == "low"


def test_normalize_should_enter_kb_default_false() -> None:
    out = CodexAdapter().normalize(
        RawLog("x", "t", CODEX_RAW, "codex"), project_id="prj"
    )
    assert out.should_enter_kb is False


def test_normalize_content_angle_truncated() -> None:
    long_text = "x" * 200
    out = CodexAdapter().normalize(
        RawLog("x", "t", {"conclusion": long_text}, "codex"), project_id="prj"
    )
    assert out.content_angle == long_text[:80]


# ---------------------------------------------------------------------------
# fetch_raw failure path (fail-closed)
# ---------------------------------------------------------------------------


def test_codex_fetch_missing_dir_raises(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_RAW_DIR", raising=False)
    with pytest.raises(CollectorError):
        CodexAdapter().fetch_raw(agent=None)


def test_codex_fetch_bad_schema_raises(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    d = tmp_path / "raw"
    d.mkdir()
    (d / "bad.json").write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, not object
    monkeypatch.setenv("CODEX_RAW_DIR", str(d))
    with pytest.raises(CollectorError):
        CodexAdapter().fetch_raw(agent=None)


def test_codex_fetch_invalid_utf8_raises(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    d = tmp_path / "raw"
    d.mkdir()
    # Invalid UTF-8 must be wrapped as CollectorError (not escape fetch_raw as
    # UnicodeDecodeError and crash the multi-platform run). Codex P1.
    (d / "bad.json").write_bytes(b"\xff\xfe\x00\x80not-utf8")
    monkeypatch.setenv("CODEX_RAW_DIR", str(d))
    with pytest.raises(CollectorError):
        CodexAdapter().fetch_raw(agent=None)


# ---------------------------------------------------------------------------
# integration: collector -> submit_work_log (service layer) -> attest
# ---------------------------------------------------------------------------


def test_collector_to_service_layer_full_chain(session: Session) -> None:
    project = _seed_project(session)
    agent = _make_agent(session, "codex")
    artifact = _submit_draft(
        session, project, CodexAdapter(), "ext-1", raw=CODEX_RAW, agent_id=agent.id
    )
    assert artifact.type == ArtifactType.WORK_LOG
    assert artifact.review_status == ArtifactReviewStatus.UNVERIFIED
    assert artifact.metadata_json["source_platform"] == "codex"
    # No agent provenance: collection logs are project-scoped, no agent claim.
    assert artifact.provenance_json.get("produced_by_agent_id") is None
    # Idempotency key namespace is agent-scoped (plan §1 / v8).
    expected_key = f"work_log:{project.id}:{_sha256('collector:' + agent.id + ':ext-1')[:32]}"
    assert artifact.idempotency_key == expected_key


def test_collector_idempotent_replay(session: Session) -> None:
    project = _seed_project(session)
    agent = _make_agent(session, "codex")
    a1 = _submit_draft(session, project, CodexAdapter(), "ext-1", raw=CODEX_RAW, agent_id=agent.id)
    # Identical resubmission (same key + same normalized payload) -> replay.
    a2, created = WorkLogService(session).submit_work_log(
        project_id=project.id,
        report_type="daily",
        what_done="g",
        why="b",
        problem="p",
        solution="s",
        new_knowledge="n",
        idempotency_key=f"collector:{agent.id}:ext-1",
        actor=OWNER,
        content_value="low",
        should_enter_kb=False,
        content_angle="n",
        source_platform="codex",
    )
    assert a2.id == a1.id
    assert created is False


def test_collector_draft_not_harvestable(session: Session) -> None:
    project = _seed_project(session)
    _submit_draft(session, project, CodexAdapter(), "ext-1", raw=CODEX_RAW)
    assert KnowledgeHarvester(session).harvest_candidates(actor=OWNER) == []


def test_collector_attest_then_harvestable(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit_draft(
        session, project, CodexAdapter(), "ext-1",
        raw=dict(CODEX_RAW, conclusion="可复用套路：数字标题打开率更高，值得沉淀"),
    )
    # Default draft is low / should_enter_kb=False -> not harvestable.
    assert KnowledgeHarvester(session).harvest_candidates(actor=OWNER) == []
    # Owner opts it into the KB at attest time.
    WorkLogService(session).attest_work_log(
        artifact_id=artifact.id, actor=OWNER, should_enter_kb=True
    )
    created = KnowledgeHarvester(session).harvest_candidates(actor=OWNER)
    assert len(created) == 1
    assert created[0].artifact_id == artifact.id


# ---------------------------------------------------------------------------
# attest override: checksum + audit trail + fail-closed
# ---------------------------------------------------------------------------


def test_attest_override_recomputes_checksum(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit_draft(session, project, CodexAdapter(), "ext-1", raw=CODEX_RAW)
    WorkLogService(session).attest_work_log(
        artifact_id=artifact.id, actor=OWNER, should_enter_kb=True, content_value="high"
    )
    session.refresh(artifact)
    assert artifact.metadata_json["should_enter_kb"] is True
    assert artifact.metadata_json["content_value"] == "high"
    expected = f"sha256:{_sha256(canonical_json(artifact.metadata_json))}"
    assert artifact.checksum == expected


def test_attest_override_audit_trail(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit_draft(session, project, CodexAdapter(), "ext-1", raw=CODEX_RAW)
    WorkLogService(session).attest_work_log(
        artifact_id=artifact.id, actor=OWNER, should_enter_kb=True
    )
    audit = session.exec(
        select(AuditLog).where(
            AuditLog.idempotency_key == f"audit:work_log:attest:{artifact.id}"
        )
    ).one()
    before = audit.before_snapshot
    after = audit.after_snapshot
    # Unified 4-field snapshot, regardless of whether an override was supplied.
    assert before["prev_should_enter_kb"] is False
    assert after["next_should_enter_kb"] is True
    assert "prev_content_value" in before
    assert "next_content_value" in after


def test_attest_rejects_invalid_content_value(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit_draft(session, project, CodexAdapter(), "ext-1", raw=CODEX_RAW)
    with pytest.raises(ServiceError) as excinfo:
        WorkLogService(session).attest_work_log(
            artifact_id=artifact.id, actor=OWNER, content_value="urgent"
        )
    assert excinfo.value.status_code == 422


def test_attest_conflicting_override_409(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit_draft(session, project, CodexAdapter(), "ext-1", raw=CODEX_RAW)
    # First attest (no override): UNVERIFIED -> APPROVED, default low/False.
    WorkLogService(session).attest_work_log(artifact_id=artifact.id, actor=OWNER)
    # Conflicting override on an already-APPROVED log -> 409 fail-closed.
    with pytest.raises(ServiceError) as excinfo:
        WorkLogService(session).attest_work_log(
            artifact_id=artifact.id, actor=OWNER, should_enter_kb=True
        )
    assert excinfo.value.status_code == 409
    # Matching override (no change) is an idempotent no-op, not an error.
    again = WorkLogService(session).attest_work_log(
        artifact_id=artifact.id, actor=OWNER, should_enter_kb=False
    )
    assert again.review_status == ArtifactReviewStatus.APPROVED


# ---------------------------------------------------------------------------
# content feed: source_platform field + structured platform filter
# ---------------------------------------------------------------------------


def _approve_with_source(
    session: Session, project: Project, adapter: object, external_id: str, *, raw: dict
) -> Artifact:
    artifact = _submit_draft(session, project, adapter, external_id, raw=raw)
    return WorkLogService(session).attest_work_log(artifact_id=artifact.id, actor=OWNER)


def test_feed_log_entries_include_source_platform(session: Session) -> None:
    project = _seed_project(session)
    _approve_with_source(session, project, CodexAdapter(), "c1", raw=CODEX_RAW)
    # Collector drafts default to content_value="low"; lower the threshold so
    # they are visible in the feed.
    feed = ContentFeed(session).get_content_feed(
        actor=OWNER, project_id=project.id, min_value="low"
    )
    logs = [e for e in feed if e["kind"] == "work_log"]
    assert logs
    assert all(e["source_platform"] == "codex" for e in logs)


def test_feed_filter_by_source_platform(session: Session) -> None:
    project = _seed_project(session)
    codex_log = _approve_with_source(session, project, CodexAdapter(), "c1", raw=CODEX_RAW)
    hermes_log = _approve_with_source(session, project, HermesAdapter(), "h1", raw=HERMES_RAW)
    f1 = _make_fact(session, codex_log, statement="fact-1", series_id="s1")
    f2 = _make_fact(session, codex_log, statement="fact-2", series_id="s2")
    f3 = _make_fact(session, codex_log, statement="fact-3", series_id="s3")

    resp = ContentFeed(session).get_content_feed(
        actor=OWNER,
        project_id=project.id,
        min_value="low",
        source_platform="codex",
        log_limit=10,
        fact_limit=2,
        log_offset=0,
        fact_offset=0,
    )
    # Structured split view.
    assert isinstance(resp, dict)
    assert {k for k in resp} == {"work_logs", "facts"}
    # Platform filter applies ONLY to logs.
    assert all(e["source_platform"] == "codex" for e in resp["work_logs"])
    assert len(resp["work_logs"]) == 1
    assert resp["work_logs"][0]["id"] == codex_log.id
    # The non-matching (hermes) log never appears.
    assert all(e["id"] != hermes_log.id for e in resp["work_logs"])
    # Facts are independent of the log filter: top 2 by (created_at, id) DESC.
    expected_fact_ids = sorted(
        (f1.id, f2.id, f3.id),
        key=lambda fid: (
            session.get(KnowledgeFact, fid).created_at,
            fid,
        ),
        reverse=True,
    )[:2]
    assert [e["id"] for e in resp["facts"]] == expected_fact_ids


# ---------------------------------------------------------------------------
# collection script logic (run_one / collect_all.main) -- fail-closed contract
# ---------------------------------------------------------------------------


def test_feed_structured_offset_fallback(session: Session) -> None:
    project = _seed_project(session)
    logs = [
        _approve_with_source(session, project, CodexAdapter(), f"c{i}", raw=CODEX_RAW)
        for i in range(3)
    ]
    f1 = _make_fact(session, logs[0], statement="fact-1", series_id="s1")
    f2 = _make_fact(session, logs[0], statement="fact-2", series_id="s2")
    f3 = _make_fact(session, logs[0], statement="fact-3", series_id="s3")

    resp = ContentFeed(session).get_content_feed(
        actor=OWNER,
        project_id=project.id,
        min_value="low",
        source_platform="codex",
        offset=1,
        log_limit=10,
        fact_limit=10,
    )
    assert isinstance(resp, dict)
    # Omitted split offsets fall back to the legacy ``offset`` (Codex P1): the
    # same offset must window BOTH slices, not reset facts to page 0.
    logs_sorted = sorted(logs, key=lambda a: (a.created_at, a.id), reverse=True)
    facts_sorted = sorted(
        (f1.id, f2.id, f3.id),
        key=lambda fid: (session.get(KnowledgeFact, fid).created_at, fid),
        reverse=True,
    )
    assert [e["id"] for e in resp["work_logs"]] == [a.id for a in logs_sorted[1:]]
    assert [e["id"] for e in resp["facts"]] == facts_sorted[1:]


def _seed_logs_table_count(session: Session) -> int:
    return len(
        session.exec(select(Artifact).where(Artifact.type == ArtifactType.WORK_LOG)).all()
    )


def test_collect_script_dry_run_no_db_write(session: Session, monkeypatch: MonkeyPatch) -> None:
    project = _seed_project(session)
    agent = _make_agent(session, "codex")
    monkeypatch.setattr(
        CodexAdapter, "fetch_raw",
        lambda self, *, agent=None, since=None: [
            RawLog("e1", "t", dict(CODEX_RAW), "codex")
        ],
    )
    pe, re = run_one(
        CodexAdapter(), session=session, actor=OWNER, project_id=project.id,
        agent_ref=agent.id, since=None, dry_run=True,
    )
    assert (pe, re) == (0, 0)
    assert _seed_logs_table_count(session) == 0


def test_collect_script_dry_run_normalize_failure_continues(
    session: Session, monkeypatch: MonkeyPatch
) -> None:
    project = _seed_project(session)
    agent = _make_agent(session, "codex")
    monkeypatch.setattr(
        CodexAdapter, "fetch_raw",
        lambda self, *, agent=None, since=None: [
            RawLog("e1", "t", dict(CODEX_RAW), "codex")
        ],
    )
    # A malformed raw makes normalize raise. In dry-run this must NOT crash the
    # whole run -- the per-record boundary catches it and counts a record error
    # (Codex P1: normalize must live inside the exception boundary).
    monkeypatch.setattr(
        CodexAdapter, "normalize",
        lambda self, raw, *, project_id: (_ for _ in ()).throw(ValueError("bad raw")),
    )
    pe, re = run_one(
        CodexAdapter(), session=session, actor=OWNER, project_id=project.id,
        agent_ref=agent.id, since=None, dry_run=True,
    )
    assert (pe, re) == (0, 1)
    assert _seed_logs_table_count(session) == 0


def test_collect_script_rejects_mismatched_agent_platform(session: Session) -> None:
    project = _seed_project(session)
    # Agent is registered for "hermes" but we run the Codex collector.
    agent = _make_agent(session, "hermes")
    pe, re = run_one(
        CodexAdapter(), session=session, actor=OWNER, project_id=project.id,
        agent_ref=agent.id, since=None, dry_run=False,
    )
    # Config error -> platform error, NO log ingested under the wrong namespace.
    assert (pe, re) == (1, 0)
    assert _seed_logs_table_count(session) == 0
    audit = session.exec(
        select(AuditLog).where(AuditLog.action == "collector.config_error")
    ).one()
    error = audit.after_snapshot.get("error") or ""
    assert "platform" in error.lower()
    assert "hermes" in error and "codex" in error


def test_collect_script_one_platform_error_continues(
    session: Session, monkeypatch: MonkeyPatch
) -> None:
    project = _seed_project(session)
    agent = _make_agent(session, "codex")
    monkeypatch.setattr(
        CodexAdapter, "fetch_raw",
        lambda self, *, agent=None, since=None: (_ for _ in ()).throw(
            CollectorError("transport down")
        ),
    )
    pe, re = run_one(
        CodexAdapter(), session=session, actor=OWNER, project_id=project.id,
        agent_ref=agent.id, since=None, dry_run=False,
    )
    assert (pe, re) == (1, 0)
    assert _seed_logs_table_count(session) == 0
    assert session.exec(
        select(AuditLog).where(AuditLog.action == "collector.error")
    ).one()


def test_collect_script_one_record_error_continues(
    session: Session, monkeypatch: MonkeyPatch
) -> None:
    project = _seed_project(session)
    agent = _make_agent(session, "codex")
    monkeypatch.setattr(
        CodexAdapter, "fetch_raw",
        lambda self, *, agent=None, since=None: [
            RawLog("e1", "t", dict(CODEX_RAW), "codex")
        ],
    )
    # Force the per-record submit to fail -> skip + audit, but do NOT abort.
    monkeypatch.setattr(
        WorkLogService, "submit_work_log",
        classmethod(lambda cls, **kwargs: (_ for _ in ()).throw(ServiceError(422, "bad"))),
    )
    pe, re = run_one(
        CodexAdapter(), session=session, actor=OWNER, project_id=project.id,
        agent_ref=agent.id, since=None, dry_run=False,
    )
    assert (pe, re) == (0, 1)
    assert _seed_logs_table_count(session) == 0
    assert session.exec(
        select(AuditLog).where(AuditLog.action == "collector.record_error")
    ).one()


def test_collect_script_rejects_non_owner_actor(session: Session, monkeypatch: MonkeyPatch) -> None:
    from aios.actor import ActorContext

    project = _seed_project(session)
    agent = _make_agent(session, "codex")
    monkeypatch.setattr(
        CodexAdapter, "fetch_raw",
        lambda self, *, agent=None, since=None: [
            RawLog("e1", "t", dict(CODEX_RAW), "codex")
        ],
    )
    # A non-owner actor must never be able to ingest (the CLI boundary enforces
    # this; here we prove the service still rejects it -> record error, no row).
    non_owner = ActorContext(kind="agent", agent_id="agt_x")
    pe, re = run_one(
        CodexAdapter(), session=session, actor=non_owner, project_id=project.id,
        agent_ref=agent.id, since=None, dry_run=False,
    )
    assert re == 1
    assert _seed_logs_table_count(session) == 0


def test_collect_all_aggregates_exit_status(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    import collect_all as collect_all_module

    db = tmp_path / "collect_all.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    run_migrations(f"sqlite:///{db.as_posix()}")
    with Session(get_engine(f"sqlite:///{db.as_posix()}")) as seed:
        project = _seed_project(seed)
        codex_agent = _make_agent(seed, "codex")
        coze_agent = _make_agent(seed, "coze")
        # Capture IDs while still attached to the seed session; otherwise the
        # ``with`` block exit detaches the instances and lazy attribute access
        # (e.g. ``.id``) below would raise DetachedInstanceError.
        project_id = project.id
        codex_agent_id = codex_agent.id
        coze_agent_id = coze_agent.id

    # Codex: platform-level fetch failure (platform error).
    monkeypatch.setattr(
        CodexAdapter, "fetch_raw",
        lambda self, *, agent=None, since=None: (_ for _ in ()).throw(
            CollectorError("codex transport down")
        ),
    )
    # Coze: fetches fine but every record fails on submit (record error).
    monkeypatch.setattr(
        CozeAdapter, "fetch_raw",
        lambda self, *, agent=None, since=None: [
            RawLog("e1", "t", dict(COZE_RAW), "coze")
        ],
    )
    monkeypatch.setattr(
        WorkLogService, "submit_work_log",
        classmethod(lambda cls, **kwargs: (_ for _ in ()).throw(ServiceError(422, "bad"))),
    )
    # Avoid real owner auth in the script entry point.
    monkeypatch.setattr(
        collect_all_module, "authenticate_owner_cli", lambda args: OWNER
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "collect_all",
            "--project-id", project_id,
            "--codex-agent", codex_agent_id,
            "--coze-agent", coze_agent_id,
        ],
    )
    assert collect_all_module.main() == 1
