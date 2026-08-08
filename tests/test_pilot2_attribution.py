"""PILOT-2A-4 attribution solver + owner report contract tests (design §11 / D4).

Collected by the normal ``pytest -q`` gate (testpaths=["tests"]); if these are
not collected, a green CI run is invalid (D4).

Covers:
  A2/A3  solve() is deterministic and idempotent (input_hash stable, no dup rows)
  A1     every non-batch registration gets a status (UNATTRIBUTED is legal)
  D1     only EXPERIMENT_ASSOCIATED / AMBIGUOUS / UNATTRIBUTED are produced or
         persisted as FinalAttribution; CLICK_ASSOCIATED is impossible as a
         final level (type system + DB CHECK boundary, both tested)
  D2     finalize creates exactly one head; re-finalize does not duplicate
  A4     owner report text carries 0 internal ids / 0 English slugs / 0
         "HIGH/直连归因" wording; batch accounts are separated into audit
  §9     missing data renders "暂无数据", never a fabricated 0
  §3.5   small sample (< 10) renders the explicit caveat
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, text

from aios.db import get_engine
from aios.pilot2.attribution_head import (
    AttributionHeadError,
    current_decision,
    finalize_attribution,
)
from aios.pilot2.attribution_solver import (
    DEFAULT_ACTIVE_STATUSES,
    DEFAULT_CONSIDERED_TRACKS,
    AttributionSolverError,
    _decide,
    finalize_solved,
    solve,
)
from aios.pilot2.models import (
    AttributionLevel,
    AttributionProposal,
    Channel,
    CohortTag,
    ExperimentRegistry,
    ExperimentStatus,
    FetchStatus,
    MiheEndpoint,
    MiheSnapshot,
    RegistrationAttributionLevel,
    RegistrationObservation,
)
from aios.pilot2.owner_report import (
    SMALL_SAMPLE_NOTE,
    build_daily_report,
    render_report_text,
)

# --- test fixtures ----------------------------------------------------------
_DATE = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)


def sqlite_url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _make_engine(db_path):
    eng = get_engine(sqlite_url(db_path))
    from aios.pilot2.models import pilot2_metadata

    pilot2_metadata.create_all(eng)  # also installs D2 triggers
    return eng


@pytest.fixture
def engine(tmp_path):
    return _make_engine(tmp_path / "pilot2_attr.db")


def _add_registration(
    session: Session,
    *,
    customer_id: str,
    registered_at: datetime,
    is_batch: bool = False,
    cohort: CohortTag = CohortTag.NATURAL,
    last_login_at: datetime | None = None,
    total_recharge: int = 0,
    reg_id: str | None = None,
) -> RegistrationObservation:
    # source_snapshot_id is a FK to MiheSnapshot (PRAGMA foreign_keys=ON), so we
    # anchor every registration to a real (shared) snapshot row.
    if session.get(MiheSnapshot, "msnap_anchor") is None:
        session.add(
            MiheSnapshot(
                id="msnap_anchor",
                endpoint=MiheEndpoint.CUSTOMERS,
                raw_hash=f"anchor_{customer_id}",
                fetch_status=FetchStatus.OK,
            )
        )
        session.commit()
    kwargs = {} if reg_id is None else {"id": reg_id}
    reg = RegistrationObservation(
        customer_id=customer_id,
        registered_at=registered_at,
        last_login_at=last_login_at,
        total_recharge=total_recharge,
        recharge_count=1 if total_recharge else 0,
        cohort_tag=cohort,
        is_batch=is_batch,
        source_snapshot_id="msnap_anchor",
        **kwargs,
    )
    session.add(reg)
    session.commit()
    return reg


def _add_experiment(
    session: Session,
    *,
    name: str,
    created_at: datetime,
    channel: Channel = Channel.XHS,
    exposure_window_h: int = 48,
    status: ExperimentStatus = ExperimentStatus.RUNNING,
    track: str = "leadgen",
    exp_id: str | None = None,
) -> ExperimentRegistry:
    kwargs = {} if exp_id is None else {"id": exp_id}
    exp = ExperimentRegistry(
        name=name,
        track=track,
        channel=channel,
        exposure_window_h=exposure_window_h,
        status=status,
        created_at=created_at,
        **kwargs,
    )
    session.add(exp)
    session.commit()
    return exp


# --- A2 / A3: determinism + idempotency -------------------------------------
def test_solve_deterministic_and_idempotent(engine):
    with Session(engine) as s:
        _add_experiment(
            s, name="单实验", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
        )
        _add_registration(
            s, customer_id="c_single", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )

    n1 = solve(engine)
    assert n1 == 1

    # Same state -> no change on replay.
    n2 = solve(engine)
    assert n2 == 0

    with Session(engine) as s:
        props = s.exec(select(AttributionProposal)).all()
        assert len(props) == 1
        assert props[0].level == AttributionLevel.EXPERIMENT_ASSOCIATED
        # stable hash
        h1 = props[0].input_hash
    n3 = solve(engine)
    assert n3 == 0
    with Session(engine) as s:
        prop = s.exec(select(AttributionProposal)).one()
        assert prop.input_hash == h1


# --- A1 + D1: the three legal levels ----------------------------------------
def test_solve_three_legal_levels(engine):
    with Session(engine) as s:
        _add_experiment(
            s, name="实验一", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
        )
        _add_experiment(
            s, name="实验二", created_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        )
        # reg A: only exp1 active -> EXPERIMENT_ASSOCIATED
        _add_registration(
            s, customer_id="c_a", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )
        # reg B: both active -> AMBIGUOUS
        _add_registration(
            s, customer_id="c_b", registered_at=datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
        )
        # reg C: outside every window -> UNATTRIBUTED
        _add_registration(
            s, customer_id="c_c", registered_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
        )

    changed = solve(engine)
    assert changed == 3

    with Session(engine) as s:
        props = {
            p.registration_observation_id: p
            for p in s.exec(select(AttributionProposal)).all()
        }
        # D1: the solver never emits anything outside the three legal levels.
        legal = {
            AttributionLevel.EXPERIMENT_ASSOCIATED,
            AttributionLevel.AMBIGUOUS,
            AttributionLevel.UNATTRIBUTED,
        }
        assert props  # non-empty
        for p in props.values():
            assert p.level in legal
        # map by customer via the registration rows
        regs = {r.customer_id: r.id for r in s.exec(select(RegistrationObservation)).all()}
        assert props[regs["c_a"]].level == AttributionLevel.EXPERIMENT_ASSOCIATED
        assert props[regs["c_b"]].level == AttributionLevel.AMBIGUOUS
        assert props[regs["c_c"]].level == AttributionLevel.UNATTRIBUTED
        # AMBIGUOUS evidence lists both candidates
        amb = props[regs["c_b"]]
        assert amb.evidence_json["active_experiment_count"] == 2
        exp_ids = {
            e.id for e in s.exec(select(ExperimentRegistry)).all()
        }
        assert set(amb.evidence_json["active_experiment_ids"]) == exp_ids


# --- D1: batch accounts excluded from attribution ----------------------------
def test_batch_cohort_excluded_from_attribution(engine):
    with Session(engine) as s:
        _add_experiment(s, name="实验", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC))
        _add_registration(
            s,
            customer_id="c_batch",
            registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
            is_batch=True,
            cohort=CohortTag.UNKNOWN_BATCH_COHORT,
        )
    solve(engine)
    with Session(engine) as s:
        # No proposal is ever created for a batch account.
        assert s.exec(select(AttributionProposal)).all() == []

    report = build_daily_report(engine, as_of_date=datetime(2026, 8, 10).date())
    # Batch is in the audit bucket, NOT in business new_registrations.
    assert report.new_registrations == 0
    assert report.batch_audit == 1


# --- D1: CLICK_ASSOCIATED is unrepresentable as a final level ----------------
def test_final_level_enum_excludes_click_associated():
    # The final-attribution enum simply has no CLICK_ASSOCIATED member, so the
    # type system refuses it before any DB write.
    with pytest.raises(ValueError):
        RegistrationAttributionLevel("click_associated")
    assert hasattr(AttributionLevel, "CLICK_ASSOCIATED")
    assert not hasattr(RegistrationAttributionLevel, "CLICK_ASSOCIATED")


def test_d1_db_boundary_rejects_click_associated_final(engine):
    # Direct raw insert of a CLICK_ASSOCIATED final decision must be refused by
    # the DB CHECK constraints (ck_fdec_level_gate + ck_fdec_no_direct_attribution).
    with Session(engine) as s:
        reg = _add_registration(
            s, customer_id="c_bound", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )
        prop = AttributionProposal(
            registration_observation_id=reg.id,
            level=AttributionLevel.UNATTRIBUTED,
            evidence_json={"rule": "test"},
            input_hash="boundary_test_hash",
        )
        s.add(prop)
        s.commit()
        prop_id = prop.id
        reg_id = reg.id

    # Attempt to persist a forbidden final level directly against the DB.
    ts = datetime(2026, 8, 10, tzinfo=UTC).isoformat()
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO finalattributiondecision "
                "(id, proposal_id, registration_observation_id, level, "
                "decided_at, decided_by) "
                "VALUES (:id, :pid, :rid, 'CLICK_ASSOCIATED', :ts, 'test')"
            ),
            {
                "id": "fdec_boundary",
                "pid": prop_id,
                "rid": reg_id,
                "ts": ts,
            },
        )


# --- D2: single head invariant under finalize -------------------------------
def test_finalize_single_head_invariant(engine):
    with Session(engine) as s:
        _add_experiment(
            s, name="实验", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
        )
        _add_registration(
            s, customer_id="c_f", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )

    finalized = finalize_solved(engine, decided_by="owner_lishu")
    assert finalized == 1

    with Session(engine) as s:
        reg = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == "c_f"
            )
        ).one()
        decision = current_decision(engine, reg.id)
        assert decision is not None
        assert decision.level == RegistrationAttributionLevel.EXPERIMENT_ASSOCIATED

    # Re-finalizing must not create a second head (immutable history).
    again = finalize_solved(engine, decided_by="owner_lishu")
    assert again == 0
    with Session(engine) as s:
        from aios.pilot2.models import FinalAttributionHead

        heads = s.exec(select(FinalAttributionHead)).all()
        assert len(heads) == 1

    # And the service refuses a direct second finalize.
    with Session(engine) as s:
        reg = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == "c_f"
            )
        ).one()
        prop = s.exec(
            select(AttributionProposal).where(
                AttributionProposal.registration_observation_id == reg.id
            )
        ).one()
        with pytest.raises(AttributionHeadError):
            finalize_attribution(
                engine,
                registration_observation_id=reg.id,
                proposal_id=prop.id,
                level=RegistrationAttributionLevel.EXPERIMENT_ASSOCIATED,
                decided_by="owner_lishu",
            )


# --- A4 + §9 + §3.5: owner report discipline --------------------------------
def test_report_chinese_discipline_and_separation(engine):
    with Session(engine) as s:
        _add_experiment(
            s, name="测试实验一", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
        )
        _add_experiment(
            s, name="测试实验二", created_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        )
        # reg A (single) with a downstream login + recharge
        _add_registration(
            s,
            customer_id="c_a",
            registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
            last_login_at=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
            total_recharge=60,
        )
        # reg B (ambiguous)
        _add_registration(
            s, customer_id="c_b", registered_at=datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
        )
        # reg batch (audit, NOT in business counts)
        _add_registration(
            s,
            customer_id="c_batch",
            registered_at=datetime(2026, 8, 10, 2, 0, tzinfo=UTC),
            is_batch=True,
            cohort=CohortTag.UNKNOWN_BATCH_COHORT,
        )
    solve(engine)
    report = build_daily_report(engine, as_of_date=datetime(2026, 8, 10).date())
    text = render_report_text(report)

    # Chinese business labels present.
    assert "实验关联" in text
    assert "待裁定" in text
    assert "未归因" in text
    # Batch separated.
    assert report.batch_audit == 1
    assert report.new_registrations == 2  # only the two non-batch regs
    # Internal ids / English slugs / forbidden wording absent.
    for forbidden in (
        "experiment_associated",
        "EXPERIMENT_ASSOCIATED",
        "AMBIGUOUS",
        "HIGH",
        "直连归因",
        "regob_",
        "aprop_",
        "fdec_",
        "exp_",
    ):
        assert forbidden not in text, f"forbidden substring leaked into owner view: {forbidden!r}"
    # Small sample caveat present (2 < 10).
    assert SMALL_SAMPLE_NOTE in text
    # Downstream measured values present, commission honestly "暂无数据".
    assert "登录" in text and "1 人" in text
    assert "¥60" in text
    assert "暂无数据" in text


def test_report_missing_data_shows_no_data_not_zero(engine):
    # A day with no registrations at all -> no fabricated counts.
    report = build_daily_report(engine, as_of_date=datetime(2026, 8, 10).date())
    text = render_report_text(report)
    assert report.new_registrations == 0
    assert "暂无数据" in text
    # Still must not emit a fabricated percentage or 0-as-measurement for the
    # experiment/theme/hook sections.
    assert "实验关联 0 人" not in text
    assert SMALL_SAMPLE_NOTE in text  # 0 < 10 -> caveat, not a clean "0"


# --- A4 (hardened): experiment slug / internal id never leaks to owner ------
def test_report_sanitizes_experiment_slug(engine):
    with Session(engine) as s:
        # An English slug experiment name must NOT appear in the owner view.
        _add_experiment(
            s, name="summer_campaign_2026",
            created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
        )
        _add_registration(
            s, customer_id="c", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )
    solve(engine)
    report = build_daily_report(engine, as_of_date=datetime(2026, 8, 10).date())
    text = render_report_text(report)
    # The raw slug must be sanitized out of the owner view (A4 / Codex P1).
    assert "summer_campaign_2026" not in text
    assert "实验（名称已脱敏）" in text


def test_report_keeps_chinese_experiment_name(engine):
    with Session(engine) as s:
        _add_experiment(
            s, name="朋友圈首发活动",
            created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
        )
        _add_registration(
            s, customer_id="c", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )
    solve(engine)
    report = build_daily_report(engine, as_of_date=datetime(2026, 8, 10).date())
    text = render_report_text(report)
    # A genuine Chinese business name is shown verbatim (not over-sanitized).
    assert "朋友圈首发活动" in text


# --- D2 (hardened): conflict on candidate change even when level unchanged ---
def test_finalize_conflict_on_candidate_change_same_level(engine):
    with Session(engine) as s:
        # A covers the registration initially; B initially does not.
        _add_experiment(
            s, name="A", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
            exposure_window_h=48,
        )
        _add_experiment(
            s, name="B", created_at=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
            exposure_window_h=1,
        )
        _add_registration(
            s, customer_id="c", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )
    finalize_solved(engine, decided_by="owner_lishu")  # EXPERIMENT_ASSOCIATED(A)
    # Shift coverage from A to B: same level, DIFFERENT candidate experiment.
    with Session(engine) as s:
        exps = {e.name: e for e in s.exec(select(ExperimentRegistry)).all()}
        exps["A"].exposure_window_h = 0
        exps["B"].created_at = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
        exps["B"].exposure_window_h = 48
        s.commit()
    # A pure level comparison would miss this (still EXPERIMENT_ASSOCIATED);
    # the full-hash conflict check must still raise (D2).
    with pytest.raises(AttributionSolverError):
        finalize_solved(engine, decided_by="owner_lishu")


# --- A2 / A3 (hardened): an input change MUST trigger a recompute ------------
def test_input_hash_recomputes_on_experiment_window_change(engine):
    with Session(engine) as s:
        _add_experiment(
            s, name="实验", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
            exposure_window_h=48,
        )
        _add_registration(
            s, customer_id="c", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )
    assert solve(engine) == 1
    with Session(engine) as s:
        prop = s.exec(select(AttributionProposal)).one()
        assert prop.level == AttributionLevel.EXPERIMENT_ASSOCIATED
        h1 = prop.input_hash

    # Shrink the window so the registration now falls OUTSIDE -> level must flip.
    with Session(engine) as s:
        exp = s.exec(select(ExperimentRegistry)).one()
        exp.exposure_window_h = 1
        s.commit()
    assert solve(engine) == 1  # recomputed, NOT a stale no-op
    with Session(engine) as s:
        prop = s.exec(select(AttributionProposal)).one()
        assert prop.level == AttributionLevel.UNATTRIBUTED
        assert prop.input_hash != h1  # hash must reflect the changed input


# --- D2 (hardened): solve never rewrites a finalized registration -------------
def test_solve_skips_finalized_registration(engine):
    with Session(engine) as s:
        _add_experiment(
            s, name="实验", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
            exposure_window_h=48,
        )
        _add_registration(
            s, customer_id="c", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )
    finalize_solved(engine, decided_by="owner_lishu")
    with Session(engine) as s:
        prop = s.exec(select(AttributionProposal)).one()
        frozen_level = prop.level
        frozen_hash = prop.input_hash

    # Change experiment so a fresh solve would flip the level...
    with Session(engine) as s:
        exp = s.exec(select(ExperimentRegistry)).one()
        exp.exposure_window_h = 1
        s.commit()
    # ...but solve must NOT rewrite the immutable finalized proposal row.
    assert solve(engine) == 0
    with Session(engine) as s:
        prop = s.exec(select(AttributionProposal)).one()
        assert prop.level == frozen_level
        assert prop.input_hash == frozen_hash


# --- D2 (hardened): finalize_solved raises on immutable-history conflict -----
def test_finalize_solved_rejects_immutable_history_conflict(engine):
    with Session(engine) as s:
        _add_experiment(
            s, name="实验", created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
            exposure_window_h=48,
        )
        _add_registration(
            s, customer_id="c", registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        )
    finalize_solved(engine, decided_by="owner_lishu")

    # Change experiment so current classification diverges from frozen decision.
    with Session(engine) as s:
        exp = s.exec(select(ExperimentRegistry)).one()
        exp.exposure_window_h = 1
        s.commit()

    # A second finalize_solved must surface the conflict, not silently no-op.
    with pytest.raises(AttributionSolverError):
        finalize_solved(engine, decided_by="owner_lishu")


# --- A4 / P2: downstream observations counted even without a proposal --------
def test_report_counts_downstream_without_proposal(engine):
    with Session(engine) as s:
        # No experiment registered, so build_daily_report sees no proposal.
        _add_registration(
            s, customer_id="c",
            registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
            last_login_at=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
            total_recharge=60,
        )
    report = build_daily_report(engine, as_of_date=datetime(2026, 8, 10).date())
    assert report.new_registrations == 1
    assert report.unattributed == 1
    # Codex P2: downstream measured data must not be suppressed by attribution
    # fallback when no proposal exists yet.
    assert report.login_count == 1
    assert report.recharge_people == 1
    assert report.recharge_sum == 60


# ============================================================================
# OWNER P1 (2026-08-08) -- DECISION-SCOPED input_hash / determinism contract
#
# input_hash must cover ONLY the inputs that can change THIS registration's
# attribution decision. Unrelated rows added to the experiment registry after a
# registration is finalized must NOT manufacture an immutable-history conflict,
# because that breaks the normal recurring (round 2+) workflow.
# ============================================================================
_R1_AT = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
_EXP1_AT = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)


def _seed_finalized_round_one(engine) -> str:
    """Round 1: one leadgen experiment covering one registration, finalized.

    Returns the frozen ``input_hash`` of the finalized proposal.
    """
    with Session(engine) as s:
        _add_experiment(
            s, name="轮次一实验", created_at=_EXP1_AT, exposure_window_h=48
        )
        _add_registration(s, customer_id="c_r1", registered_at=_R1_AT)
    assert finalize_solved(engine, decided_by="owner_lishu") == 1
    return _stored_hash(engine)


def _stored_hash(engine, customer_id: str = "c_r1") -> str:
    with Session(engine) as s:
        reg = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == customer_id
            )
        ).one()
        prop = s.exec(
            select(AttributionProposal).where(
                AttributionProposal.registration_observation_id == reg.id
            )
        ).one()
        return prop.input_hash


def _recompute_hash(engine, customer_id: str = "c_r1") -> str:
    """Recompute the decision hash from the CURRENT database state."""
    with Session(engine) as s:
        reg = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == customer_id
            )
        ).one()
        exps = s.exec(select(ExperimentRegistry)).all()
        _, _, digest = _decide(
            reg,
            exps,
            exposure_window_h=None,
            considered=DEFAULT_CONSIDERED_TRACKS,
            active=DEFAULT_ACTIVE_STATUSES,
        )
        return digest


def _decision_fingerprint(engine, reg_id: str) -> tuple:
    d = current_decision(engine, reg_id)
    assert d is not None
    return (d.id, d.proposal_id, d.level, d.decided_at, d.decided_by)


# --- (1) unrelated DRAFT experiment must not conflict ------------------------
def test_replay_ignores_unrelated_draft_experiment(engine):
    frozen = _seed_finalized_round_one(engine)
    with Session(engine) as s:
        # Deliberately WOULD have covered the registration if it were RUNNING.
        _add_experiment(
            s,
            name="新草稿实验",
            created_at=_EXP1_AT,
            exposure_window_h=48,
            status=ExperimentStatus.DRAFT,
        )
    assert finalize_solved(engine, decided_by="owner_lishu") == 0
    assert _stored_hash(engine) == frozen
    assert _recompute_hash(engine) == frozen


# --- (2) RUNNING experiment on an excluded track must not conflict -----------
def test_replay_ignores_excluded_track_experiment(engine):
    frozen = _seed_finalized_round_one(engine)
    with Session(engine) as s:
        # In-window and RUNNING, but track="ip" is not in considered_tracks.
        _add_experiment(
            s,
            name="IP轨道实验",
            created_at=_EXP1_AT,
            exposure_window_h=48,
            status=ExperimentStatus.RUNNING,
            track="ip",
        )
    assert finalize_solved(engine, decided_by="owner_lishu") == 0
    assert _stored_hash(engine) == frozen
    assert _recompute_hash(engine) == frozen


# --- (3) experiment outside the registration's window must not conflict ------
def test_replay_ignores_out_of_window_experiment(engine):
    frozen = _seed_finalized_round_one(engine)
    with Session(engine) as s:
        _add_experiment(
            s,
            name="窗口外实验",
            created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
            exposure_window_h=48,
        )
    assert finalize_solved(engine, decided_by="owner_lishu") == 0
    assert _stored_hash(engine) == frozen
    assert _recompute_hash(engine) == frozen


# --- (4) a genuinely eligible in-window leadgen experiment MUST conflict -----
def test_replay_conflicts_on_genuinely_eligible_experiment(engine):
    frozen = _seed_finalized_round_one(engine)
    with Session(engine) as s:
        _add_experiment(
            s,
            name="真正合格的新实验",
            created_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
            exposure_window_h=48,
            status=ExperimentStatus.RUNNING,
        )
    # The decision inputs for THIS registration genuinely changed -> hash changes.
    assert _recompute_hash(engine) != frozen
    # ...and the fail-closed immutable-history guard fires as designed (D2).
    with pytest.raises(AttributionSolverError):
        finalize_solved(engine, decided_by="owner_lishu")
    # The frozen row itself is still not rewritten.
    assert _stored_hash(engine) == frozen


# --- (5) canonical hash is independent of DB insertion order -----------------
def test_canonical_hash_independent_of_insertion_order(tmp_path):
    def build(db_name: str, order: list[str]) -> tuple:
        eng = _make_engine(tmp_path / db_name)
        specs = {
            "x": dict(
                name="实验X",
                exp_id="exp_x",
                created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
                exposure_window_h=48,
            ),
            "y": dict(
                name="实验Y",
                exp_id="exp_y",
                created_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC),
                exposure_window_h=48,
            ),
            # decoy: never eligible, must not perturb the canonical hash
            "d": dict(
                name="干扰草稿",
                exp_id="exp_decoy",
                created_at=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
                exposure_window_h=48,
                status=ExperimentStatus.DRAFT,
            ),
        }
        with Session(eng) as s:
            for key in order:
                _add_experiment(s, **specs[key])
            _add_registration(
                s,
                customer_id="c_ord",
                reg_id="regob_canonical",
                registered_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
            )
        assert solve(eng) == 1
        with Session(eng) as s:
            prop = s.exec(select(AttributionProposal)).one()
            return prop.level, prop.input_hash

    level_a, hash_a = build("order_a.db", ["x", "y", "d"])
    level_b, hash_b = build("order_b.db", ["d", "y", "x"])
    assert level_a == level_b == AttributionLevel.AMBIGUOUS
    # Same logical candidate set, different insertion sequence -> same hash.
    assert hash_a == hash_b


# --- (6) repeated identical batch replay is a deterministic zero-op ----------
def test_repeated_batch_replay_is_deterministic_zero_op(engine):
    frozen = _seed_finalized_round_one(engine)
    for _ in range(3):
        assert solve(engine) == 0
        assert finalize_solved(engine, decided_by="owner_lishu") == 0
    assert _stored_hash(engine) == frozen
    with Session(engine) as s:
        from aios.pilot2.models import FinalAttributionHead

        assert len(s.exec(select(FinalAttributionHead)).all()) == 1
        assert len(s.exec(select(AttributionProposal)).all()) == 1


# --- (7) finalized rows stay untouched when unrelated experiments appear -----
def test_finalized_rows_untouched_by_unrelated_experiments(engine):
    frozen = _seed_finalized_round_one(engine)
    with Session(engine) as s:
        reg_id = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == "c_r1"
            )
        ).one().id
    before = _decision_fingerprint(engine, reg_id)

    with Session(engine) as s:
        _add_experiment(
            s, name="草稿", created_at=_EXP1_AT, status=ExperimentStatus.DRAFT
        )
        _add_experiment(s, name="IP实验", created_at=_EXP1_AT, track="ip")
        _add_experiment(
            s, name="窗口外", created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        )
    assert finalize_solved(engine, decided_by="owner_lishu") == 0
    assert _decision_fingerprint(engine, reg_id) == before
    assert _stored_hash(engine) == frozen


# --- (8) a candidate merely CONCLUDING must not conflict ---------------------
def test_replay_survives_candidate_conclusion(engine):
    """RUNNING -> CONCLUDED is the normal end-of-experiment event.

    Both statuses are inside ``DEFAULT_ACTIVE_STATUSES`` (owner decision: keep
    CONCLUDED for T+1 retrospective reporting), so the classification is
    unchanged and the hash must be unchanged too -- otherwise every round-1
    attribution would falsely conflict the moment its experiment closes.
    """
    frozen = _seed_finalized_round_one(engine)
    with Session(engine) as s:
        exp = s.exec(select(ExperimentRegistry)).one()
        exp.status = ExperimentStatus.CONCLUDED
        exp.concluded_at = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
        s.commit()
    assert finalize_solved(engine, decided_by="owner_lishu") == 0
    assert _stored_hash(engine) == frozen
    assert _recompute_hash(engine) == frozen


# --- (9) BUSINESS GATE: round 2 must not invalidate round 1 ------------------
def test_round_two_does_not_invalidate_round_one(engine):
    """The recurring-loop milestone: run experiment #1, then experiment #2.

    Round 1's finalized attribution must survive round 2 untouched, round 2's
    own registration must finalize normally, and a further replay must be a
    clean zero-op.
    """
    frozen_r1 = _seed_finalized_round_one(engine)
    with Session(engine) as s:
        reg1_id = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == "c_r1"
            )
        ).one().id
    before_r1 = _decision_fingerprint(engine, reg1_id)

    with Session(engine) as s:
        # Round 1's experiment closes.
        exp1 = s.exec(select(ExperimentRegistry)).one()
        exp1.status = ExperimentStatus.CONCLUDED
        exp1.concluded_at = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
        s.commit()
        # Round 2 launches: a real experiment plus the usual registry noise.
        _add_experiment(
            s,
            name="轮次二实验",
            created_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
            exposure_window_h=48,
        )
        _add_experiment(
            s,
            name="轮次二草稿",
            created_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
            status=ExperimentStatus.DRAFT,
        )
        _add_experiment(
            s,
            name="轮次二IP实验",
            created_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
            track="ip",
        )
        _add_registration(
            s,
            customer_id="c_r2",
            registered_at=datetime(2026, 8, 20, 6, 0, tzinfo=UTC),
        )

    # Exactly the round-2 registration finalizes; round 1 is not disturbed.
    assert finalize_solved(engine, decided_by="owner_lishu") == 1
    assert _stored_hash(engine, customer_id="c_r1") == frozen_r1
    assert _decision_fingerprint(engine, reg1_id) == before_r1
    with Session(engine) as s:
        reg2 = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == "c_r2"
            )
        ).one()
        d2 = current_decision(engine, reg2.id)
        assert d2 is not None
        assert d2.level == RegistrationAttributionLevel.EXPERIMENT_ASSOCIATED

    # Round 3 replay: deterministic zero-op, still no conflict.
    assert finalize_solved(engine, decided_by="owner_lishu") == 0
    assert _stored_hash(engine, customer_id="c_r1") == frozen_r1


# --- (10) configuration sequences are canonicalized as SETS ------------------
def _hash_with_config(engine, *, considered, active, customer_id: str = "c_r1") -> str:
    """Recompute the decision hash under an explicit solver configuration."""
    with Session(engine) as s:
        reg = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == customer_id
            )
        ).one()
        exps = s.exec(select(ExperimentRegistry)).all()
        _, _, digest = _decide(
            reg,
            exps,
            exposure_window_h=None,
            considered=considered,
            active=active,
        )
        return digest


def test_hash_ignores_duplicate_and_reordered_config_entries(engine):
    """Duplicates / ordering in the config describe the SAME decision.

    ``_experiment_active_at`` only membership-tests ``considered_tracks`` and
    ``active_statuses``, so ("leadgen", "leadgen") is eligibility-identical to
    ("leadgen",). Hashing the raw sorted sequence would let a harmless caller
    typo manufacture an immutable-history conflict, which violates the owner
    determinism contract ("two logically identical decisions -> same hash").
    """
    frozen = _seed_finalized_round_one(engine)

    assert (
        _hash_with_config(
            engine,
            considered=("leadgen", "leadgen"),
            active=DEFAULT_ACTIVE_STATUSES,
        )
        == frozen
    )
    assert (
        _hash_with_config(
            engine,
            considered=DEFAULT_CONSIDERED_TRACKS,
            active=(
                ExperimentStatus.CONCLUDED,
                ExperimentStatus.RUNNING,
                ExperimentStatus.CONCLUDED,
            ),
        )
        == frozen
    )
    # Both at once.
    assert (
        _hash_with_config(
            engine,
            considered=("leadgen", "leadgen", "leadgen"),
            active=(
                ExperimentStatus.CONCLUDED,
                ExperimentStatus.RUNNING,
                ExperimentStatus.RUNNING,
            ),
        )
        == frozen
    )

    # Track ORDER alone must not matter either. This needs a genuinely
    # multi-element set to be meaningful, so compare a widened config against
    # its own reversal (both differ from `frozen`, but must equal each other).
    widened = _hash_with_config(
        engine, considered=("leadgen", "ip"), active=DEFAULT_ACTIVE_STATUSES
    )
    assert (
        _hash_with_config(
            engine, considered=("ip", "leadgen"), active=DEFAULT_ACTIVE_STATUSES
        )
        == widened
    )
    assert (
        _hash_with_config(
            engine, considered=("ip", "leadgen", "ip"), active=DEFAULT_ACTIVE_STATUSES
        )
        == widened
    )


def test_hash_still_changes_on_genuinely_different_config(engine):
    """Guard the other side: set-canonicalization must not over-collapse.

    Widening the considered tracks or the active-status contract is a real
    change to the decision inputs, so the hash MUST move (A3).
    """
    frozen = _seed_finalized_round_one(engine)

    assert (
        _hash_with_config(
            engine,
            considered=("leadgen", "ip"),
            active=DEFAULT_ACTIVE_STATUSES,
        )
        != frozen
    )
    assert (
        _hash_with_config(
            engine,
            considered=DEFAULT_CONSIDERED_TRACKS,
            active=(ExperimentStatus.RUNNING,),
        )
        != frozen
    )


def test_finalize_replay_survives_duplicated_config(engine):
    """End-to-end: a duplicated config on round 2 is a clean zero-op, not a raise."""
    frozen = _seed_finalized_round_one(engine)
    assert (
        finalize_solved(
            engine,
            decided_by="owner_lishu",
            considered_tracks=("leadgen", "leadgen"),
            active_statuses=(
                ExperimentStatus.RUNNING,
                ExperimentStatus.CONCLUDED,
                ExperimentStatus.RUNNING,
            ),
        )
        == 0
    )
    assert _stored_hash(engine) == frozen
