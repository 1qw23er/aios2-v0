"""Work-log & knowledge-capture system (#88) -- service layer.

Implements plan docs/issue-88-implementation-plan.md (v3, approved via PR #90):

- ``WorkLogService.submit_work_log`` -- submission entry point. Creates an
  ``Artifact(type=WORK_LOG, review_status=UNVERIFIED)`` under the single
  idempotency contract (plan §5) and the identity/ownership validation rules
  (plan §6). Submission NEVER creates an APPROVED artifact.
- ``WorkLogService.attest_work_log`` -- the ONLY path that promotes a work log
  to APPROVED. Atomic owner-attestation contract (plan §7.2): one transaction
  writes the Approval + status flip + AuditLog evidence triple, serialized via
  ``BEGIN IMMEDIATE`` so concurrent attestors converge to exactly one evidence
  set (winner=updated, loser=idempotent no-op).
- ``ContentValueJudge`` -- pure-function content value heuristics (no LLM).
- ``KnowledgeHarvester`` -- scans APPROVED work logs and submits DRAFT
  ``KnowledgeCandidate`` rows through the existing ``KnowledgeService``
  (owner still reviews manually; nothing is auto-approved). Tags come from the
  deterministic canonical mapping (plan §8.2 option A) -- never from callers.
- ``ContentFeed`` -- read-only feed combining eligible work logs and APPROVED
  knowledge facts with precise scope rules (plan §8.3).

Identity rule (plan §6): every method takes a trusted ``actor: ActorContext``
injected by the authentication boundary (``authenticate_owner`` / scripts that
authenticate first). This module NEVER calls ``resolve_owner_actor()`` itself.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor
from aios.audit import AuditLog, append_audit
from aios.knowledge_service import KnowledgeService
from aios.knowledge_tags import CANONICAL_KNOWLEDGE_TAGS
from aios.models import (
    Agent,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    ExecutionAssignment,
    KnowledgeCandidate,
    KnowledgeFact,
    KnowledgeFactStatus,
    Project,
    RiskLevel,
    Task,
    now_utc,
)
from aios.services import ServiceError

# --- Contract constants (plan §5 / §7.2 / §8.3) ---

WORK_LOG_ATTESTATION_ACTION = "work_log_attestation"
WORK_LOG_ATTEST_AUDIT_ACTION = "work_log.owner_attested"
WORK_LOG_SUBMIT_AUDIT_ACTION = "work_log.submitted"

REPORT_TYPES = frozenset({"daily", "retro"})
CONTENT_VALUES = ("none", "low", "medium", "high")  # ascending order
_CONTENT_VALUE_RANK = {value: rank for rank, value in enumerate(CONTENT_VALUES)}

FEED_MAX_LIMIT = 500

# Deterministic keyword -> canonical tag mapping (plan §8.2 option A). Pure
# data: every target tag MUST be a member of CANONICAL_KNOWLEDGE_TAGS; the
# registry itself is never extended here.
_TAG_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("公众号", "微信", "wechat"), "wechat_writing"),
    (("小红书", "xhs"), "xhs_adaptation"),
    (("视频脚本", "video script"), "video_script"),
    (("定位", "positioning"), "positioning"),
    (("用户调研", "访谈", "user research"), "user_research"),
    (("包装", "封面", "排版", "packaging"), "packaging"),
)


def canonical_json(payload: dict[str, Any]) -> str:
    """Canonical JSON for fingerprints: sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def storage_idempotency_key(
    project_id: str, client_key: str, scope: str | None = None
) -> str:
    """Namespaced storage key (plan §5.3): endpoint + verified project_id.

    Deliberately date-free (retries across UTC midnight replay cleanly) and
    free of any untrusted agent id.

    V4 (#99/#101): an optional ``scope`` segment isolates the idempotency
    namespace per authenticated identity so that an agent's relay ingest and the
    owner's direct submission (and two different agents) never converge on the
    same storage row even with an identical ``Idempotency-Key``. ``scope=None``
    preserves the legacy owner-key format ``work_log:{project_id}:{hash}``; a
    relay scope of ``"agent:<agent_id>"`` yields
    ``work_log:{project_id}:agent:<agent_id>:{hash}`` -- the literal
    ``:agent:`` segment makes the two formats structurally disjoint.
    """
    if scope is None:
        return f"work_log:{project_id}:{_sha256(client_key)[:32]}"
    return f"work_log:{project_id}:{scope}:{_sha256(client_key)[:32]}"


def _request_fingerprint(business_fields: dict[str, Any]) -> str:
    return _sha256(canonical_json(business_fields))


def _work_log_evidence_intact(session: Session, artifact_id: str) -> bool:
    """True iff this work log carries the complete attestation evidence.

    The central trust boundary (plan §7.2): a work log is publishable only
    when it has EXACTLY one approved ``work_log_attestation`` Approval, its
    matching audit trail, and no conflicting (non-approved) row. Zero,
    two-plus, or duplicated/conflicting rows all mean the evidence is broken.
    Used both to fail-closed re-attestation and to gate the consumers
    (harvest + content feed) so a log approved via the wrong path is never
    consumed. (#88 Codex P1)
    """
    approvals = list(
        session.exec(
            select(Approval).where(
                Approval.target_artifact_id == artifact_id,
                Approval.action_type == WORK_LOG_ATTESTATION_ACTION,
            )
        )
    )
    approved = [a for a in approvals if a.status == ApprovalStatus.APPROVED]
    conflicting = [a for a in approvals if a.status != ApprovalStatus.APPROVED]
    audit = session.exec(
        select(AuditLog).where(
            AuditLog.idempotency_key == f"audit:work_log:attest:{artifact_id}"
        )
    ).first()
    return len(approved) == 1 and audit is not None and not conflicting


class ContentValueJudge:
    """Pure-function content value judgement (plan §8.1). No LLM, no I/O."""

    _SIGNAL_KEYWORDS = ("实验", "踩坑", "决策", "数据", "对比", "结论")

    @classmethod
    def judge(cls, metadata: dict[str, Any]) -> tuple[str, bool, str]:
        """Return ``(content_value, should_enter_kb, content_angle)``.

        - ``should_enter_kb``: the explicit flag wins; default False.
        - ``content_value``: explicit caller value wins; otherwise heuristics
          on ``new_knowledge`` (signal keyword hit AND length > 50 -> medium,
          else low).
        - ``content_angle``: explicit value wins; otherwise the first 80 chars
          of ``new_knowledge``.
        """
        new_knowledge = str(metadata.get("new_knowledge") or "")

        should_enter_kb = bool(metadata.get("should_enter_kb", False))

        explicit_value = metadata.get("content_value")
        if isinstance(explicit_value, str) and explicit_value in _CONTENT_VALUE_RANK:
            content_value = explicit_value
        elif (
            len(new_knowledge) > 50
            and any(keyword in new_knowledge for keyword in cls._SIGNAL_KEYWORDS)
        ):
            content_value = "medium"
        else:
            content_value = "low"

        explicit_angle = metadata.get("content_angle")
        if isinstance(explicit_angle, str) and explicit_angle.strip():
            content_angle = explicit_angle.strip()
        else:
            content_angle = new_knowledge[:80]

        return content_value, should_enter_kb, content_angle


def map_work_log_tags(metadata: dict[str, Any]) -> list[str]:
    """Deterministic canonical-tag mapping (plan §8.2, option A).

    Always includes ``knowledge_capture``; other canonical tags are appended
    only when substantiated by case-insensitive keyword hits on the persisted
    ``new_knowledge + content_angle`` text. The output is always a subset of
    ``CANONICAL_KNOWLEDGE_TAGS`` so ``normalize_tags()`` can never 422.
    """
    text = (
        f"{metadata.get('new_knowledge') or ''} {metadata.get('content_angle') or ''}"
    ).lower()
    tags = {"knowledge_capture"}
    for keywords, tag in _TAG_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            tags.add(tag)
    # Defensive invariant: never emit anything outside the canonical registry.
    assert tags <= CANONICAL_KNOWLEDGE_TAGS
    return sorted(tags)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(422, f"{field} must be a non-empty string")
    return value.strip()


class WorkLogService:
    """Submission + attestation for work-log artifacts (plan §5-§7)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- submission (plan §6 validation + §5 idempotency) -------------------

    def submit_work_log(
        self,
        *,
        project_id: str,
        report_type: str,
        what_done: str,
        why: str,
        problem: str,
        solution: str,
        new_knowledge: str,
        idempotency_key: str,
        actor: ActorContext,
        task_ref: str | None = None,
        produced_by_agent_id: str | None = None,
        execution_assignment_id: str | None = None,
        content_value: str | None = None,
        should_enter_kb: bool = False,
        content_angle: str | None = None,
        source_platform: str | None = None,
    ) -> tuple[Artifact, bool]:
        """Create an UNVERIFIED work-log Artifact (plan §7.1).

        Returns ``(artifact, created)`` where ``created`` is ``True`` only when
        this call actually inserted a brand-new row. Replays (same key +
        same fingerprint) and concurrent-duplicate losers return the existing
        artifact with ``created=False``. The HTTP layer uses ``created`` to
        decide 201 vs 200 atomically -- never a preliminary query, which would
        be racy (TOCTOU) and would mis-handle key whitespace (the service
        trims the key before deriving the storage hash).


        Validation order (plan §6, any failure -> 422 except the owner guard
        which is 403): trusted owner actor -> project exists -> task belongs to
        project -> agent provenance bound to an exact ExecutionAssignment.
        Idempotency (plan §5): namespaced storage key + canonical-JSON request
        fingerprint; replay returns the existing artifact, key reuse with a
        different payload is 409, concurrent duplicates are re-adjudicated
        after the partial unique index raises IntegrityError.
        """
        # 1. Trusted owner actor (injected by the boundary; never minted here).
        _assert_owner_actor(actor)

        if report_type not in REPORT_TYPES:
            raise ServiceError(422, "report_type must be 'daily' or 'retro'")
        what_done = _required_text(what_done, "what_done")
        why = _required_text(why, "why")
        problem = _required_text(problem, "problem")
        solution = _required_text(solution, "solution")
        new_knowledge = _required_text(new_knowledge, "new_knowledge")
        client_key = _required_text(idempotency_key, "idempotency_key")
        if content_value is not None and content_value not in _CONTENT_VALUE_RANK:
            raise ServiceError(
                422, "content_value must be one of high|medium|low|none"
            )

        # 2. Project must exist.
        project = self.session.get(Project, project_id)
        if project is None:
            raise ServiceError(422, "unknown project_id")

        # 3. Optional task_ref: the task must exist and belong to the project.
        task: Task | None = None
        if task_ref is not None:
            task = self.session.get(Task, task_ref)
            if task is None:
                raise ServiceError(422, "unknown task_ref")
            if task.project_id != project_id:
                raise ServiceError(422, "task_ref does not belong to project_id")

        # 4. Agent provenance: bind to one exact ExecutionAssignment (plan §6.4).
        provenance_assignment_id: str | None = None
        legacy_assigned_agent = False
        agent: Agent | None = None
        if produced_by_agent_id is not None:
            if task is None:
                raise ServiceError(
                    422,
                    "produced_by_agent_id requires task_ref: an agent claim "
                    "without a task anchor is never ownership proof",
                )
            agent = self.session.get(Agent, produced_by_agent_id)
            if agent is None:
                raise ServiceError(422, "unknown produced_by_agent_id")
            assignments = list(
                self.session.exec(
                    select(ExecutionAssignment).where(
                        ExecutionAssignment.task_id == task.id
                    )
                )
            )
            if assignments:
                # Routed task: the exact assignment id is mandatory (task_id is
                # NOT unique on execution_assignment -- retries/fallbacks leave
                # multiple rows, so selecting by task alone is ambiguous).
                if execution_assignment_id is None:
                    raise ServiceError(
                        422, "ambiguous assignment: assignment id required"
                    )
                assignment = self.session.get(
                    ExecutionAssignment, execution_assignment_id
                )
                if assignment is None:
                    raise ServiceError(422, "unknown execution_assignment_id")
                if assignment.task_id != task.id:
                    raise ServiceError(
                        422, "execution_assignment_id belongs to a different task"
                    )
                if assignment.selected_agent_id != produced_by_agent_id:
                    raise ServiceError(
                        422,
                        "execution_assignment_id does not prove this agent "
                        "executed the task (selected_agent_id mismatch)",
                    )
                if task.project_id != project_id:  # re-check (§6.4.4)
                    raise ServiceError(422, "task_ref does not belong to project_id")
                provenance_assignment_id = assignment.id
            else:
                # Legacy compatibility ONLY: zero assignments AND no assignment
                # id supplied AND the task's fixed assignment matches.
                if execution_assignment_id is not None:
                    raise ServiceError(
                        422,
                        "execution_assignment_id supplied but the task has no "
                        "execution assignments",
                    )
                if task.assigned_agent_id != produced_by_agent_id:
                    raise ServiceError(
                        422,
                        "agent is not the task's assigned agent (preferred_agent_id "
                        "is never accepted as execution proof)",
                    )
                legacy_assigned_agent = True
        elif execution_assignment_id is not None:
            raise ServiceError(
                422, "execution_assignment_id requires produced_by_agent_id"
            )

        # -- Idempotency (plan §5): storage key + request fingerprint --
        # (content-value judgement is performed inside the shared helper)
        business_fields = {
            "project_id": project_id,
            "report_type": report_type,
            "task_ref": task_ref,
            "produced_by_agent_id": produced_by_agent_id,
            "execution_assignment_id": execution_assignment_id,
            "what_done": what_done,
            "why": why,
            "problem": problem,
            "solution": solution,
            "new_knowledge": new_knowledge,
            "content_value": content_value,
            "should_enter_kb": should_enter_kb,
            "content_angle": content_angle,
            "source_platform": source_platform,
        }
        fingerprint = _request_fingerprint(business_fields)
        storage_key = storage_idempotency_key(project_id, client_key, scope=None)

        artifact, created = self._create_unverified_work_log(
            project_id=project_id,
            report_type=report_type,
            what_done=what_done,
            why=why,
            problem=problem,
            solution=solution,
            new_knowledge=new_knowledge,
            task_ref=task_ref,
            produced_by_agent_id=produced_by_agent_id,
            source_platform=source_platform,
            content_value=content_value,
            should_enter_kb=should_enter_kb,
            content_angle=content_angle,
            task=task,
            agent=agent,
            provenance_assignment_id=provenance_assignment_id,
            legacy_assigned_agent=legacy_assigned_agent,
            fingerprint=fingerprint,
            storage_key=storage_key,
            actor=actor,
            scope=None,
        )
        if created:
            metadata = artifact.metadata_json or {}
            append_audit(
                self.session,
                actor=actor.derive_submitted_by(),
                action=WORK_LOG_SUBMIT_AUDIT_ACTION,
                resource_type="artifact",
                resource_id=artifact.id,
                project_id=project_id,
                task_id=artifact.task_id,
                before={},
                after={
                    "review_status": ArtifactReviewStatus.UNVERIFIED.value,
                    "report_type": report_type,
                    "content_value": metadata.get("content_value"),
                    "should_enter_kb": metadata.get("should_enter_kb"),
                    "source_platform": source_platform,
                },
                idempotency_key=f"audit:work_log:submit:{artifact.id}",
            )
            self.session.commit()
        return artifact, created

    def _find_by_storage_key(self, storage_key: str) -> Artifact | None:
        return self.session.exec(
            select(Artifact).where(Artifact.idempotency_key == storage_key)
        ).first()

    def _adjudicate_replay(self, existing: Artifact, fingerprint: str) -> Artifact:
        stored = (existing.metadata_json or {}).get("_request_fingerprint")
        if stored == fingerprint:
            return existing  # replay no-op (plan §5.5 case 1)
        raise ServiceError(409, "idempotency key reuse with different payload")

    def _create_unverified_work_log(
        self,
        *,
        project_id: str,
        report_type: str,
        what_done: str,
        why: str,
        problem: str,
        solution: str,
        new_knowledge: str,
        task_ref: str | None,
        produced_by_agent_id: str | None,
        source_platform: str | None,
        content_value: str | None,
        should_enter_kb: bool,
        content_angle: str | None,
        task: Task | None,
        agent: Agent | None,
        provenance_assignment_id: str | None,
        legacy_assigned_agent: bool,
        fingerprint: str,
        storage_key: str,
        actor: ActorContext,
        scope: str | None,
    ) -> tuple[Artifact, bool]:
        """Shared UNVERIFIED-artifact creation used by BOTH ``submit_work_log``
        (owner, ``scope=None``) and ``relay_work_log`` (agent, scoped).

        Builds the work-log ``Artifact`` (UNVERIFIED) and performs the
        idempotency / concurrent-duplicate adjudication, but does NOT write the
        audit row and does NOT commit -- the caller decides which audit action
        applies (``work_log.submitted`` for owner, ``relay.work_log_ingested``
        for relay) and commits. This keeps the trust chain identical across both
        entry points while letting each own its audit identity (V4, #99/#101).
        """
        raw_metadata = {
            "new_knowledge": new_knowledge,
            "content_value": content_value,
            "should_enter_kb": should_enter_kb,
            "content_angle": content_angle,
        }
        judged_value, judged_should_enter, judged_angle = ContentValueJudge.judge(
            raw_metadata
        )

        existing = self._find_by_storage_key(storage_key)
        if existing is not None:
            return self._adjudicate_replay(existing, fingerprint), False

        metadata_json = {
            "report_type": report_type,
            "produced_by_agent_id": produced_by_agent_id,
            "task_ref": task_ref,
            "what_done": what_done,
            "why": why,
            "problem": problem,
            "solution": solution,
            "new_knowledge": new_knowledge,
            "content_value": judged_value,
            "should_enter_kb": judged_should_enter,
            "content_angle": judged_angle,
            "source_platform": source_platform,
            "_request_fingerprint": fingerprint,
        }
        provenance_json = {
            "submitted_by": actor.derive_submitted_by(),
            "submitted_at": now_utc().isoformat(),
            "produced_by_agent_id": produced_by_agent_id,
            "produced_by_platform": agent.platform if agent is not None else None,
            "task_id": task.id if task is not None else None,
            "execution_assignment_id": provenance_assignment_id,
            "legacy_assigned_agent": legacy_assigned_agent,
        }
        artifact = Artifact(
            project_id=project_id,
            task_id=task.id if task is not None else None,
            type=ArtifactType.WORK_LOG,
            review_status=ArtifactReviewStatus.UNVERIFIED,
            provenance_json=provenance_json,
            metadata_json=metadata_json,
            uri="",  # patched to work_log:<id> below (id exists after flush)
            checksum=f"sha256:{_sha256(canonical_json(metadata_json))}",
            idempotency_key=storage_key,
        )
        try:
            self.session.add(artifact)
            self.session.flush()
            artifact.uri = f"work_log:{artifact.id}"
            self.session.add(artifact)
            self.session.flush()
        except IntegrityError:
            # Concurrent duplicate submit/relay: the partial unique index
            # uq_artifact_idempotency is the arbiter. Re-read the winner and
            # re-adjudicate by fingerprint (plan §5.5 case 3).
            self.session.rollback()
            winner = self._find_by_storage_key(storage_key)
            if winner is None:  # pragma: no cover - unrelated integrity error
                raise
            return self._adjudicate_replay(winner, fingerprint), False
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(artifact)
        return artifact, True

    # -- attestation (plan §7.2) --------------------------------------------

    def attest_work_log(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        should_enter_kb: bool | None = None,
        content_value: str | None = None,
    ) -> Artifact:
        """Owner attestation: the ONLY path that flips a work log to APPROVED.

        Serialized via ``BEGIN IMMEDIATE`` (SQLite RESERVED write lock) so two
        concurrent attestors produce exactly one Approval + one AuditLog + one
        status flip; the loser re-reads committed state under the lock and
        returns through the idempotent no-op branch. An IntegrityError (e.g.
        AuditLog idempotency collision) is caught, rolled back and
        re-adjudicated -- it never leaks to the caller.

        V2 (#92): optional ``should_enter_kb`` / ``content_value`` override lets
        the owner decide KB eligibility at attest time (the ONLY place it can be
        set). The override only mutates ``metadata_json``'s two judgement fields
        and atomically recomputes ``checksum`` -- never the evidence trio or
        ``provenance``. A conflicting override on an already-APPROVED log raises
        409 (fail-closed: owner decisions are never silently lost); a matching
        or absent override is an idempotent no-op.
        """
        _assert_owner_actor(actor)
        if content_value is not None and content_value not in _CONTENT_VALUE_RANK:
            raise ServiceError(
                422, "content_value must be one of high|medium|low|none"
            )
        try:
            return self._attest_locked(
                artifact_id=artifact_id,
                actor=actor,
                should_enter_kb=should_enter_kb,
                content_value=content_value,
            )
        except IntegrityError:
            self.session.rollback()
            try:
                return self._attest_locked(
                    artifact_id=artifact_id,
                    actor=actor,
                    should_enter_kb=should_enter_kb,
                    content_value=content_value,
                )
            except IntegrityError:
                # Persistent collision: never leak a raw DB exception; fail
                # closed with a clean conflict (#88 Codex P2).
                self.session.rollback()
                raise ServiceError(
                    409, "attestation conflict: persistent integrity error"
                ) from None

    def _attest_locked(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        should_enter_kb: bool | None = None,
        content_value: str | None = None,
    ) -> Artifact:
        # Take the write lock BEFORE reading the artifact so every writer is
        # serialized and reads committed state (plan §7.2 arbitration step 1-2).
        self.session.rollback()  # ensure no implicit transaction is open
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            artifact = self.session.get(Artifact, artifact_id)
            if artifact is None:
                raise ServiceError(404, "Artifact not found")
            if artifact.type != ArtifactType.WORK_LOG:
                raise ServiceError(409, "artifact is not a work log")

            if artifact.review_status == ArtifactReviewStatus.APPROVED:
                # Idempotent no-op ONLY when the full evidence pair exists;
                # otherwise fail closed (plan §7.2 semantics).
                self._assert_attestation_evidence(artifact)
                # V2 (#92): a conflicting override on an already-APPROVED log is
                # fail-closed 409 -- never silently discard an owner decision.
                metadata = artifact.metadata_json or {}
                current_seb = bool(metadata.get("should_enter_kb", False))
                current_cv = str(metadata.get("content_value") or "none")
                if should_enter_kb is not None and bool(should_enter_kb) != current_seb:
                    raise ServiceError(409, "conflicting attestation override")
                if content_value is not None and content_value != current_cv:
                    raise ServiceError(409, "conflicting attestation override")
                self.session.rollback()  # release the write lock, no changes
                return artifact
            if artifact.review_status != ArtifactReviewStatus.UNVERIFIED:
                raise ServiceError(
                    409,
                    "only an UNVERIFIED work log can be attested "
                    f"(current: {artifact.review_status.value})",
                )

            # V2 (#92): apply optional KB-eligibility override. The override
            # mutates only the two judgement fields in metadata_json and
            # atomically recomputes checksum; the evidence trio and provenance
            # are untouched.
            metadata = dict(artifact.metadata_json or {})
            prev_seb = bool(metadata.get("should_enter_kb", False))
            prev_cv = str(metadata.get("content_value") or "none")
            next_seb = prev_seb if should_enter_kb is None else bool(should_enter_kb)
            next_cv = prev_cv if content_value is None else content_value
            if next_seb != prev_seb or next_cv != prev_cv:
                metadata["should_enter_kb"] = next_seb
                metadata["content_value"] = next_cv
                artifact.metadata_json = metadata
                artifact.checksum = f"sha256:{_sha256(canonical_json(metadata))}"

            before_status = artifact.review_status
            self.session.add(
                Approval(
                    project_id=artifact.project_id,
                    task_id=artifact.task_id,
                    target_artifact_id=artifact.id,
                    action_type=WORK_LOG_ATTESTATION_ACTION,
                    risk_level=RiskLevel.L1,
                    status=ApprovalStatus.APPROVED,
                    decided_at=now_utc(),
                    rationale="owner attestation of work log",
                )
            )
            artifact.review_status = ArtifactReviewStatus.APPROVED
            self.session.add(artifact)
            append_audit(
                self.session,
                actor=actor.owner_id or "owner",
                action=WORK_LOG_ATTEST_AUDIT_ACTION,
                resource_type="artifact",
                resource_id=artifact.id,
                project_id=artifact.project_id,
                task_id=artifact.task_id,
                before={
                    "review_status": before_status.value,
                    "prev_should_enter_kb": prev_seb,
                    "prev_content_value": prev_cv,
                },
                after={
                    "review_status": ArtifactReviewStatus.APPROVED.value,
                    "next_should_enter_kb": next_seb,
                    "next_content_value": next_cv,
                },
                idempotency_key=f"audit:work_log:attest:{artifact.id}",
            )
            self.session.commit()
        except ServiceError:
            self.session.rollback()
            raise
        except IntegrityError:
            self.session.rollback()
            raise
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(artifact)
        return artifact

    def _assert_attestation_evidence(self, artifact: Artifact) -> None:
        """Fail closed on APPROVED work logs with broken evidence (plan §7.2)."""
        if not _work_log_evidence_intact(self.session, artifact.id):
            raise ServiceError(
                409,
                "approved work log with missing/duplicate/conflicting "
                "attestation evidence",
            )


class KnowledgeHarvester:
    """Harvest DRAFT knowledge candidates from APPROVED work logs (plan §8.2)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def harvest_candidates(self, *, actor: ActorContext) -> list[KnowledgeCandidate]:
        """Submit one DRAFT candidate per eligible, not-yet-harvested work log.

        The ``actor`` is injected by the calling boundary (script/API after
        owner authentication); this service never mints identity. Everything
        funnels through ``KnowledgeService.submit_candidate`` -- so the
        APPROVED-artifact precondition, the ``(artifact_id, statement)``
        idempotency and the owner-only guard all apply unchanged, and nothing
        is ever auto-approved.
        """
        _assert_owner_actor(actor)
        candidate_logs = list(
            self.session.exec(
                select(Artifact).where(
                    Artifact.type == ArtifactType.WORK_LOG,
                    Artifact.review_status == ArtifactReviewStatus.APPROVED,
                )
            )
        )
        # Only logs with complete attestation evidence are consumable; a log
        # flipped to APPROVED by any other path is NOT publishable (plan §7.2).
        logs = [
            log
            for log in candidate_logs
            if _work_log_evidence_intact(self.session, log.id)
        ]
        service = KnowledgeService(self.session)
        created: list[KnowledgeCandidate] = []
        for log in logs:
            metadata = log.metadata_json or {}
            should_enter_kb = bool(metadata.get("should_enter_kb", False))
            content_value = str(metadata.get("content_value") or "none")
            if not (should_enter_kb or content_value in ("high", "medium")):
                continue
            statement = str(metadata.get("new_knowledge") or "").strip()
            if not statement:
                continue
            # NOT EXISTS pre-filter (plan §5.6); submit_candidate's own
            # (artifact_id, statement) idempotency remains the hard guarantee.
            already = self.session.exec(
                select(KnowledgeCandidate).where(
                    KnowledgeCandidate.artifact_id == log.id
                )
            ).first()
            if already is not None:
                continue
            candidate = service.submit_candidate(
                log.id,
                statement,
                project_id=log.project_id,
                tags=map_work_log_tags(metadata),
                actor=actor,
            )
            created.append(candidate)
        return created


class ContentFeed:
    """Read-only content feed (plan §8.3). Never mutates anything."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_content_feed(
        self,
        *,
        actor: ActorContext,
        project_id: str | None = None,
        min_value: str = "medium",
        limit: int = 100,
        offset: int = 0,
        source_platform: str | None = None,
        log_limit: int | None = None,
        log_offset: int | None = None,
        fact_limit: int | None = None,
        fact_offset: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Read-only content feed (plan §8.3), V2 (#92) platform view.

        No ``source_platform`` -> flat merged list (identical to #88, plus a
        ``source_platform`` field on every work_log entry). A ``source_platform``
        value switches to the structured split view
        ``{"work_logs": [...], "facts": [...]}``         with INDEPENDENT pagination:
        ``log_limit``/``log_offset`` scope the platform-filtered logs and
        ``fact_limit``/``fact_offset`` scope the (never platform-filtered) facts.
        Omitted split params fall back to ``limit``/``offset`` (so
        ``?source_platform=codex&offset=10`` windows both slices at 10), and a
        caller that only sets ``source_platform`` still gets sane windowing.
        Facts are always the same eligible set regardless of the log filter, so
        the structured view never explodes to ``2*limit``.
        """
        _assert_owner_actor(actor)
        if min_value not in _CONTENT_VALUE_RANK:
            raise ServiceError(422, "min_value must be one of high|medium|low|none")
        if limit < 1 or limit > FEED_MAX_LIMIT:
            raise ServiceError(422, f"limit must be between 1 and {FEED_MAX_LIMIT}")
        if offset < 0:
            raise ServiceError(422, "offset must be >= 0")
        threshold = _CONTENT_VALUE_RANK[min_value]

        log_stmt = select(Artifact).where(
            Artifact.type == ArtifactType.WORK_LOG,
            Artifact.review_status == ArtifactReviewStatus.APPROVED,
        )
        fact_stmt = select(KnowledgeFact).where(
            KnowledgeFact.status == KnowledgeFactStatus.APPROVED
        )
        if project_id is not None:
            # Project view: project logs + project facts + company-scope facts
            # (company knowledge is visible to every project). NEVER another
            # project's rows.
            log_stmt = log_stmt.where(Artifact.project_id == project_id)
            fact_stmt = fact_stmt.where(
                (KnowledgeFact.project_id == project_id)
                | (KnowledgeFact.project_id == None)  # noqa: E711
            )

        # Materialize the eligible facts once; facts are NEVER platform-filtered
        # and never value-filtered -- they are a self-contained slice.
        facts: list[dict[str, Any]] = [
            {
                "kind": "fact",
                "id": fact.id,
                "series_id": fact.series_id,
                "version": fact.version,
                "project_id": fact.project_id,
                "statement": fact.statement,
                "tags": list(fact.tags or []),
                "created_at": fact.created_at.isoformat(),
            }
            for fact in self.session.exec(fact_stmt)
        ]

        # Eligible, evidence-backed work logs (sorted DESC). The platform filter
        # applies ONLY here, never to facts.
        work_logs: list[dict[str, Any]] = []
        for log in self.session.exec(log_stmt):
            # Defense in depth: only evidence-backed work logs are publishable
            # (plan §7.2). A log flipped to APPROVED by the wrong path is
            # excluded even though its review_status is APPROVED.
            if not _work_log_evidence_intact(self.session, log.id):
                continue
            metadata = log.metadata_json or {}
            value = str(metadata.get("content_value") or "none")
            if _CONTENT_VALUE_RANK.get(value, 0) < threshold:
                continue
            if (
                source_platform is not None
                and str(metadata.get("source_platform") or "") != source_platform
            ):
                continue
            work_logs.append(
                {
                    "kind": "work_log",
                    "id": log.id,
                    "project_id": log.project_id,
                    "report_type": metadata.get("report_type"),
                    "content_value": value,
                    "content_angle": metadata.get("content_angle"),
                    "new_knowledge": metadata.get("new_knowledge"),
                    "source_platform": metadata.get("source_platform"),
                    "created_at": log.created_at.isoformat(),
                }
            )

        if source_platform is None:
            # Flat merged single window -- identical to #88 behaviour.
            entries = [
                (item["created_at"], item["id"], item) for item in work_logs
            ] + [(item["created_at"], item["id"], item) for item in facts]
            entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return [payload for _, _, payload in entries[offset : offset + limit]]

        # Structured split view with independent pagination (plan §1 / v16).
        # Omitted split offsets fall back to the legacy ``offset`` so a caller
        # that sets only ``source_platform`` + ``offset`` still windows both
        # slices consistently (e.g. ``?source_platform=codex&offset=10``).
        log_limit_eff = log_limit if log_limit is not None else limit
        fact_limit_eff = fact_limit if fact_limit is not None else limit
        log_offset_eff = log_offset if log_offset is not None else offset
        fact_offset_eff = fact_offset if fact_offset is not None else offset
        if log_limit_eff < 1 or log_limit_eff > FEED_MAX_LIMIT:
            raise ServiceError(422, f"log_limit must be between 1 and {FEED_MAX_LIMIT}")
        if fact_limit_eff < 1 or fact_limit_eff > FEED_MAX_LIMIT:
            raise ServiceError(422, f"fact_limit must be between 1 and {FEED_MAX_LIMIT}")
        if log_offset_eff < 0 or fact_offset_eff < 0:
            raise ServiceError(422, "log_offset and fact_offset must be >= 0")

        work_logs.sort(
            key=lambda item: (item["created_at"], item["id"]), reverse=True
        )
        facts.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        return {
            "work_logs": work_logs[log_offset_eff : log_offset_eff + log_limit_eff],
            "facts": facts[fact_offset_eff : fact_offset_eff + fact_limit_eff],
        }
