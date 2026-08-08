"""PILOT-2A-4 minimal attribution solver (design §2.3 / D1).

Deterministic, recomputable attribution for every non-batch registration.

Hard contracts (design §11 / Issue #134, PILOT-2A-4):
  D1  G3 证明前，注册归因落库结果**仅限** ``EXPERIMENT_ASSOCIATED`` /
      ``AMBIGUOUS`` / ``UNATTRIBUTED``。``VERIFIED_DIRECT`` / ``DIRECT`` /
      ``HIGH`` 不存在于持久化词汇表；``CLICK_ASSOCIATED`` 只作点击层证据，
      绝不进终态（见 ``RegistrationAttributionLevel`` 与 D1 DB 边界 CHECK）。
      本求解器**只实现** §2.3 判定顺序中的规则 ③/④/⑤：
        * 规则 ①（VERIFIED/DIRECT）需 G3 证明 → 当前阶段不实现；
        * 规则 ②（CLICK_ASSOCIATED）需自有跳转层（2B-2）→ 当前无 ClickEvent
          数据，且 D1 禁止其作为终态，故不产出。
      因此求解器产出的 ``AttributionProposal.level`` 永远是上述三合法值之一，
      ``finalize_solved`` 经 ``attribution_head.finalize_attribution`` 落
      ``FinalAttributionDecision`` 时类型系统即拒绝任何越界值。
  A1  每条新增注册都有归因状态（UNKNOWN_BATCH_COHORT 排除后仍计），空值率 = 0。
  A2  可复算 + **决策域哈希**（owner 裁定 2026-08-08，P1 NORMAL MULTI-ROUND
      WORKFLOW CORRECTNESS）：``input_hash`` 只编码**能够改变这条注册归因结论
      的输入**——注册身份与观测时间、归因窗口参数、``considered_tracks``、
      ``active_statuses`` 契约、求解器契约版本，以及**该注册实际命中的候选实验
      集合**（每个候选的决策相关不可变字段，规范化排序）。
      它**绝不**对全局实验表求哈希。因此：
        * 新增无关 DRAFT 实验、非 ``considered_tracks`` 轨道（如 ip）实验、
          落在该注册归因窗口之外的实验、其他时段/其他注册的行 —— 哈希不变，
          第二轮 replay 是确定性零操作，不产生伪冲突；
        * 反之，任一决策相关输入变化（新的合格实验进入该注册窗口、候选时序
          变化、轨道集合变化、窗口参数变化）—— 哈希必变并触发重算。
        * ``considered_tracks`` / ``active_statuses`` 按**集合**规范化（去重后
          排序）：判定只做成员测试，故 ("leadgen","leadgen") 与 ("leadgen",)
          是同一决策，不得因调用方书写差异产生伪冲突（Codex R5 P2）。
  A3  幂等：同一提案被重复摄入不产生重复行。
  D2  不可变历史：已被 ``FinalAttribution`` 指向的注册，``solve`` 永不改写其
      提案行；``finalize_solved`` 在输入变更导致分歧时抛错，须经 owner 的
      ``replace_attribution`` 流程解决，而非静默覆盖。
  Fail-closed：实验窗口外、无活动实验 → ``UNATTRIBUTED``（合法状态，非错误）。

The engine is a PURE function over (RegistrationObservation set, ExperimentRegistry
set) for a given window configuration. No wall-clock time enters the level
decision; ``input_hash`` is derived solely from the inputs so replays are
byte-stable.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlmodel import Session, select

from aios.pilot2.attribution_head import finalize_attribution
from aios.pilot2.models import (
    AttributionLevel,
    AttributionProposal,
    CohortTag,
    ExperimentRegistry,
    ExperimentStatus,
    RegistrationAttributionLevel,
    RegistrationObservation,
    now_utc,
)

# Default considered track + the experiment statuses that count as "in its
# running window" for attribution. ``DRAFT`` never counts (it was never
# executed); ``CONCLUDED`` counts because the experiment was genuinely active
# during its exposure window, so a registration inside that window is still
# attributable after the experiment has been closed (post-hoc daily report).
DEFAULT_CONSIDERED_TRACKS: tuple[str, ...] = ("leadgen",)
DEFAULT_ACTIVE_STATUSES: tuple[ExperimentStatus, ...] = (
    ExperimentStatus.RUNNING,
    ExperimentStatus.CONCLUDED,
)

# Bumped whenever the *decision semantics* or the hash payload shape changes, so
# a stored ``input_hash`` computed by an older contract is never mistaken for a
# match.
#   v2 = decision-scoped hash (owner P1, 2026-08-08): the payload covers the
#        eligible-candidate set for one registration instead of the global
#        experiment registry.
#   v3 = configuration sequences canonicalized as *sets* (Codex R5 P2): the
#        payload's ``considered_tracks`` / ``active_statuses`` are deduplicated,
#        because eligibility only ever membership-tests them.
#        Note the precise claim: the set-canonicalization ITSELF is a no-op for
#        any duplicate-free config, but the digest still changes for every caller
#        because ``contract`` is part of the hashed payload -- that is exactly
#        what a version bump is for. It is free here because this solver does not
#        exist on ``main``, so no v2 ``input_hash`` can have been persisted.
SOLVER_CONTRACT_VERSION = "pilot2a4.decision-scoped-hash.v3"


class AttributionSolverError(RuntimeError):
    """Raised for solver-level misuse (e.g. immutable-history conflict)."""


def _experiment_active_at(
    exp: ExperimentRegistry,
    t: datetime,
    *,
    exposure_window_h: int | None,
    considered_tracks: Sequence[str],
    active_statuses: Sequence[ExperimentStatus],
) -> bool:
    """Whether ``exp`` was in its active exposure window at registration time ``t``.

    An experiment contributes to attribution for a registration at time ``t`` iff:
      * its track is in ``considered_tracks`` (default: leadgen),
      * its status is one of ``active_statuses`` (default: RUNNING or CONCLUDED;
        DRAFT never counts),
      * ``exp.created_at <= t <= exp.created_at + window`` where ``window`` is the
        per-experiment ``exposure_window_h`` (or the caller override).
    """
    if exp.track not in considered_tracks:
        return False
    if exp.status not in active_statuses:
        return False
    if exp.created_at is None or t < exp.created_at:
        return False
    window = exposure_window_h if exposure_window_h is not None else exp.exposure_window_h
    window_end = exp.created_at + timedelta(hours=window)
    return t <= window_end


def _classify(
    reg: RegistrationObservation,
    experiments: Sequence[ExperimentRegistry],
    *,
    exposure_window_h: int | None,
    considered: Sequence[str],
    active: Sequence[ExperimentStatus],
) -> tuple[AttributionLevel, dict, list[ExperimentRegistry]]:
    """Classify one registration into its D1-legal level (design §2.3 rules ③/④/⑤).

    Returns ``(level, evidence_dict, eligible_candidates)``. The candidate list is
    the ONLY part of the experiment registry that can influence this
    registration's decision -- everything else in the table is irrelevant and must
    never reach ``input_hash`` (owner P1: decision-scoped hash).

    The result is a PURE function of the registration and its eligible candidate
    set, and the candidate list is canonically ordered so DB insertion/query order
    can never leak into the decision or its evidence.
    """
    t = reg.registered_at
    active_exps = [
        exp
        for exp in experiments
        if _experiment_active_at(
            exp,
            t,
            exposure_window_h=exposure_window_h,
            considered_tracks=considered,
            active_statuses=active,
        )
    ]
    # Canonical order: (window_start, id). Insertion order must not be observable.
    active_exps.sort(
        key=lambda e: (
            e.created_at.isoformat() if e.created_at is not None else "",
            e.id,
        )
    )
    active_ids = [exp.id for exp in active_exps]
    if len(active_exps) == 1:
        level = AttributionLevel.EXPERIMENT_ASSOCIATED
        rule = "experiment_window_single"
    elif len(active_exps) >= 2:
        level = AttributionLevel.AMBIGUOUS
        rule = "experiment_window_overlap"
    else:
        level = AttributionLevel.UNATTRIBUTED
        rule = "no_active_experiment"
    evidence = {
        "rule": rule,
        "level": level.value,
        "registered_at": t.isoformat() if t is not None else None,
        "exposure_window_h": (
            exposure_window_h
            if exposure_window_h is not None
            else (active_exps[0].exposure_window_h if active_exps else None)
        ),
        # Canonical set form, matching what ``_input_hash`` actually hashes, so
        # the stored audit trail lets a reviewer recompute the digest verbatim.
        "considered_tracks": sorted(set(considered)),
        "active_experiment_ids": active_ids,
        "active_experiment_count": len(active_exps),
    }
    return level, evidence, active_exps


def _candidate_fingerprint(
    exp: ExperimentRegistry, *, exposure_window_h: int | None
) -> dict:
    """Decision-relevant, immutable projection of ONE eligible candidate.

    Includes exactly the fields ``_experiment_active_at`` consults to decide
    whether this experiment covers the registration: identity, track, window
    start, and the *effective* window length actually applied to this decision
    (caller override wins, so editing an experiment's own ``exposure_window_h``
    while an override is in force is correctly a no-op).

    Deliberately EXCLUDES:
      * ``status`` -- its only decision-relevant projection is *membership in*
        ``active_statuses``, which is invariantly true for a candidate. A
        ``RUNNING -> CONCLUDED`` transition is the normal end-of-experiment event
        and does not change the classification, so it must not manufacture a
        conflict on the round-2 replay. A transition *out* of ``active_statuses``
        does change the decision -- and is captured because the experiment then
        drops out of the candidate set entirely. The status contract itself is
        hashed once, at the top level, as ``active_statuses``.
      * ``concluded_at`` / ``name`` / ``channel`` / ``hypothesis`` / ... -- never
        consulted by the current window contract (see P3-B, owner: non-blocking).
    """
    window_h = exposure_window_h if exposure_window_h is not None else exp.exposure_window_h
    return {
        "id": exp.id,
        "track": exp.track,
        "window_start": exp.created_at.isoformat() if exp.created_at is not None else None,
        "window_h": window_h,
    }


def _canonical_candidates(
    candidates: Sequence[ExperimentRegistry], *, exposure_window_h: int | None
) -> list[dict]:
    """Canonically ordered fingerprints of a registration's eligible candidates.

    Sorting on the serialized fingerprint gives a total, content-derived order,
    so the same logical candidate set produces byte-identical bytes regardless of
    DB insertion order or query plan.
    """
    fingerprints = [
        _candidate_fingerprint(exp, exposure_window_h=exposure_window_h)
        for exp in candidates
    ]
    return sorted(
        fingerprints, key=lambda d: json.dumps(d, separators=(",", ":"), sort_keys=True)
    )


def _input_hash(
    *,
    registration_observation_id: str,
    registered_at: datetime | None,
    candidate_fingerprints: Sequence[dict],
    level: AttributionLevel,
    considered_tracks: Sequence[str],
    exposure_window_h: int | None,
    active_statuses: Sequence[ExperimentStatus],
) -> str:
    """Deterministic, DECISION-SCOPED idempotency token (design §2.5 / A2 / A3).

    Owner determinism contract (2026-08-08):
      * two logically identical decisions for this registration MUST produce the
        same hash even if unrelated rows were added to the database;
      * any change to a decision-relevant input for this registration MUST
        produce a different hash.

    The payload therefore carries only: the solver contract version, the
    registration identity + observed-at time, the *eligible candidate set* for
    this registration (canonical fingerprints), the derived level, and the
    solver configuration (considered tracks, window override, active-status
    contract). The global experiment registry is NOT hashed.

    ``considered_tracks`` and ``active_statuses`` are canonicalized as *sets*,
    not merely sorted: eligibility consults them through membership tests only
    (``_experiment_active_at``), so ``("leadgen", "leadgen")`` and
    ``("leadgen",)`` describe the very same decision and must not diverge on the
    hash (Codex R5 P2).
    """
    payload = {
        "contract": SOLVER_CONTRACT_VERSION,
        "rid": registration_observation_id,
        "registered_at": registered_at.isoformat() if registered_at is not None else None,
        "candidates": list(candidate_fingerprints),
        "level": level.value,
        "considered_tracks": sorted(set(considered_tracks)),
        "exposure_window_h_override": exposure_window_h,
        "active_statuses": sorted({s.value for s in active_statuses}),
    }
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest


def _decide(
    reg: RegistrationObservation,
    experiments: Sequence[ExperimentRegistry],
    *,
    exposure_window_h: int | None,
    considered: Sequence[str],
    active: Sequence[ExperimentStatus],
) -> tuple[AttributionLevel, dict, str]:
    """SINGLE source of truth for one registration's decision record.

    Returns ``(level, evidence, input_hash)``. Both ``solve`` and
    ``finalize_solved`` go through this function, so the proposal that is written
    and the hash that is later re-verified can never drift apart (the previous
    duplicated snapshot blocks are what let the global-registry scope leak in).
    """
    level, evidence, candidates = _classify(
        reg,
        experiments,
        exposure_window_h=exposure_window_h,
        considered=considered,
        active=active,
    )
    fingerprints = _canonical_candidates(candidates, exposure_window_h=exposure_window_h)
    # Audit trail: lets a reviewer recompute the hash from the stored proposal.
    evidence["candidate_fingerprints"] = fingerprints
    evidence["solver_contract"] = SOLVER_CONTRACT_VERSION
    digest = _input_hash(
        registration_observation_id=reg.id,
        registered_at=reg.registered_at,
        candidate_fingerprints=fingerprints,
        level=level,
        considered_tracks=considered,
        exposure_window_h=exposure_window_h,
        active_statuses=active,
    )
    return level, evidence, digest


def solve(
    engine,
    *,
    exposure_window_h: int | None = None,
    considered_tracks: Sequence[str] = DEFAULT_CONSIDERED_TRACKS,
    active_statuses: Sequence[ExperimentStatus] = DEFAULT_ACTIVE_STATUSES,
) -> int:
    """Compute and persist ``AttributionProposal`` for every eligible registration.

    Eligible = not ``UNKNOWN_BATCH_COHORT`` (``is_batch`` / cohort tag), ordered
    by ``registered_at`` ascending (design §2.3). For each, count leadgen
    experiments whose exposure window covers the registration time:

      * exactly 1 active  -> ``EXPERIMENT_ASSOCIATED`` (rule ③)
      * >= 2 active        -> ``AMBIGUOUS`` (rule ④, candidates listed in evidence)
      * 0 active           -> ``UNATTRIBUTED`` (rule ⑤)

    Rules ①/② are intentionally NOT produced (see module docstring / D1).

    Idempotent + recomputable: ``input_hash`` covers every input that can change
    THIS registration's conclusion -- its observed-at time, its eligible candidate
    set, and the solver config -- and nothing else. Unrelated experiments (DRAFT,
    non-considered tracks, out-of-window) never perturb it, so a normal recurring
    batch is a deterministic zero-op; a genuine change always recomputes (A2/A3).

    Immutable-history safe: a registration that already carries a
    ``FinalAttribution`` decision is never rewritten by ``solve`` -- its proposal
    row stays frozen and any divergence must go through the owner
    ``replace_attribution`` flow (design §2.5 / D2).

    Returns the number of proposals inserted or refreshed.
    """
    from aios.pilot2.attribution_head import current_decision

    considered = tuple(considered_tracks)
    active = tuple(active_statuses)
    with Session(engine) as session:
        # The full registry is SCANNED to find each registration's candidates, but
        # only the candidates that survive the eligibility filter are ever hashed.
        experiments = session.exec(select(ExperimentRegistry)).all()
        registrations = session.exec(
            select(RegistrationObservation)
            .where(RegistrationObservation.cohort_tag != CohortTag.UNKNOWN_BATCH_COHORT)
            .order_by(RegistrationObservation.registered_at.asc())
        ).all()

        changed = 0
        for reg in registrations:
            # D2: never rewrite a proposal that a FinalAttribution already points at.
            if current_decision(engine, reg.id) is not None:
                continue
            level, evidence, new_hash = _decide(
                reg,
                experiments,
                exposure_window_h=exposure_window_h,
                considered=considered,
                active=active,
            )
            existing = session.exec(
                select(AttributionProposal).where(
                    AttributionProposal.registration_observation_id == reg.id
                )
            ).one_or_none()
            if existing is not None and existing.input_hash == new_hash:
                continue  # unchanged -> no-op (A2 idempotency)
            if existing is None:
                session.add(
                    AttributionProposal(
                        registration_observation_id=reg.id,
                        content_id=None,
                        level=level,
                        evidence_json=evidence,
                        input_hash=new_hash,
                        computed_at=now_utc(),
                    )
                )
            else:
                existing.level = level
                existing.evidence_json = evidence
                existing.input_hash = new_hash
                existing.computed_at = now_utc()
            changed += 1
        session.commit()
    return changed


def finalize_solved(
    engine,
    *,
    decided_by: str,
    exposure_window_h: int | None = None,
    considered_tracks: Sequence[str] = DEFAULT_CONSIDERED_TRACKS,
    active_statuses: Sequence[ExperimentStatus] = DEFAULT_ACTIVE_STATUSES,
    reason: str | None = None,
) -> int:
    """Promote current proposals to ``FinalAttribution`` (gated by owner).

    Runs ``solve`` first (idempotent) so every eligible registration has a
    current proposal, then finalizes each registration that is NOT yet finalized.

    Immutable-history guarantees (design §2.5 / D2):
      * A registration that already has a ``FinalAttribution`` is never silently
        overwritten. If its experiment inputs have since changed such that the
        *current* classification disagrees with the frozen decision, this raises
        ``AttributionSolverError`` -- the owner must resolve it via the explicit
        ``replace_attribution`` flow, never via a blind re-finalize.
      * A registration whose DECISION inputs are unchanged re-finalizes as a safe
        no-op (idempotent batch replay), returning 0. Per the owner determinism
        contract this explicitly includes the recurring-workflow cases where the
        experiment registry has grown since round 1: a new DRAFT experiment, a
        RUNNING experiment on a non-considered track (e.g. ``ip``), an experiment
        whose window does not cover this registration, or a candidate that merely
        transitioned ``RUNNING -> CONCLUDED``. None of these may manufacture a
        conflict -- round 2 must never invalidate round 1.
      * The single-row head invariant is enforced by ``finalize_attribution``
        itself (a direct second ``finalize_attribution`` raises
        ``AttributionHeadError``).

    The solver only emits the three D1-legal proposal levels, so the conversion
    ``RegistrationAttributionLevel(proposal.level.value)`` always succeeds and
    the DB boundary CHECK (``ck_fdec_no_direct_attribution``) can never be hit.

    Returns the number of newly finalized registrations.
    """
    from aios.pilot2.attribution_head import current_decision

    considered = tuple(considered_tracks)
    active = tuple(active_statuses)
    solve(
        engine,
        exposure_window_h=exposure_window_h,
        considered_tracks=considered,
        active_statuses=active,
    )

    finalized = 0
    with Session(engine) as session:
        experiments = session.exec(select(ExperimentRegistry)).all()
        regs = session.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.cohort_tag != CohortTag.UNKNOWN_BATCH_COHORT
            )
        ).all()
        regs_by_id = {r.id: r for r in regs}

        proposals = session.exec(
            select(AttributionProposal)
            .join(
                RegistrationObservation,
                AttributionProposal.registration_observation_id
                == RegistrationObservation.id,
            )
            .where(RegistrationObservation.cohort_tag != CohortTag.UNKNOWN_BATCH_COHORT)
        ).all()
        for prop in proposals:
            decision = current_decision(engine, prop.registration_observation_id)
            if decision is not None:
                # Already finalized -> verify immutability (D2) by recomputing the
                # DECISION-SCOPED hash and comparing it with the frozen proposal.
                # Because the hash covers only this registration's own decision
                # inputs, unrelated experiments created later (DRAFT, excluded
                # track, out-of-window) leave it byte-identical and the replay is a
                # clean no-op; a genuine change to THIS registration's candidate
                # set still raises, even when the level happens to be unchanged.
                reg = regs_by_id.get(prop.registration_observation_id)
                if reg is not None:
                    recomputed_level, recomputed_evidence, recomputed_hash = _decide(
                        reg,
                        experiments,
                        exposure_window_h=exposure_window_h,
                        considered=considered,
                        active=active,
                    )
                    if recomputed_hash != prop.input_hash:
                        raise AttributionSolverError(
                            f"immutable-history conflict for registration "
                            f"{prop.registration_observation_id!r}: the stored final "
                            f"decision (proposal {prop.id}, level "
                            f"{prop.level.value!r}) was computed from different "
                            f"decision inputs than the current state implies "
                            f"(now {recomputed_level.value!r} over candidates "
                            f"{[c['id'] for c in recomputed_evidence['candidate_fingerprints']]}); "
                            f"do not re-finalize silently -- use the owner "
                            f"replace_attribution flow"
                        )
                continue  # unchanged -> safe idempotent no-op
            try:
                finalize_attribution(
                    engine,
                    registration_observation_id=prop.registration_observation_id,
                    proposal_id=prop.id,
                    level=RegistrationAttributionLevel(prop.level.value),
                    decided_by=decided_by,
                    reason=reason or "auto-finalized by PILOT-2A-4 minimal solver",
                )
            except Exception as exc:  # pragma: no cover - defensive, should not happen
                raise AttributionSolverError(
                    f"failed to finalize registration "
                    f"{prop.registration_observation_id!r}: {exc}"
                ) from exc
            finalized += 1
    return finalized


def _build_cli() -> None:
    """Internal CLI entry point (dev/ops; staging runs go through the
    authorized staging refresh on the staging host)."""
    import argparse

    from aios.db import get_engine
    from aios.pilot2.models import pilot2_metadata

    parser = argparse.ArgumentParser(prog="attribution_solver")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_solve = sub.add_parser("solve", help="compute & persist attribution proposals")
    p_solve.add_argument("--db", required=True, help="sqlite DB path")
    p_solve.add_argument("--exposure-window-h", type=int, default=None)

    p_final = sub.add_parser("finalize", help="promote proposals to FinalAttribution")
    p_final.add_argument("--db", required=True)
    p_final.add_argument("--decided-by", required=True)
    p_final.add_argument("--exposure-window-h", type=int, default=None)

    p_report = sub.add_parser("report", help="render owner daily report (Chinese)")
    p_report.add_argument("--db", required=True)
    p_report.add_argument("--as-of", default=None, help="YYYY-MM-DD")

    args = parser.parse_args()
    engine = get_engine(f"sqlite:///{args.db}")
    pilot2_metadata.create_all(engine)  # also installs D2 triggers

    if args.cmd == "solve":
        n = solve(engine, exposure_window_h=args.exposure_window_h)
        print(f"solve: {n} proposal(s) inserted/refreshed")
    elif args.cmd == "finalize":
        n = finalize_solved(
            engine,
            decided_by=args.decided_by,
            exposure_window_h=args.exposure_window_h,
        )
        print(f"finalize: {n} registration(s) newly finalized")
    elif args.cmd == "report":
        from aios.pilot2.owner_report import build_daily_report, render_report_text

        as_of = None
        if args.as_of:
            from datetime import date

            as_of = date.fromisoformat(args.as_of)
        report = build_daily_report(engine, as_of_date=as_of)
        print(render_report_text(report))


if __name__ == "__main__":
    _build_cli()
