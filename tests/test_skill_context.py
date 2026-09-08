"""Skill System V1 -- context engine integration tests (Contract §16).

Covers the single execution seam: ``ContextService._select_skills`` through
``build_context``. Determinism is the hard condition: same inputs + same DB
skill state => byte-identical ``applicable_skills`` => same ``context_hash``;
a version bump must change the hash; an old TaskContext row must keep
replaying the version it captured (immutable snapshot).
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from aios.actor import resolve_owner_actor
from aios.context_service import ContextService
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentTrustLevel,
    Capability,
    Project,
    RoutingMode,
    SkillReviewDecisionValue,
    Task,
    TaskContext,
    TaskStatus,
)
from aios.skill_service import SkillService


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


class Rig:
    """Minimal project + INTERNAL agent + capability-matched task."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.project = Project(name="Ctx", objective="Determinism")
        self.agent = Agent(
            id="agt_skill",
            name="Skilled",
            role="writer",
            adapter_type=AdapterType.EXTERNAL,
        )
        self.capability = Capability(name="drafting", description="Writes")
        session.add_all([self.project, self.agent, self.capability])
        session.flush()
        session.add(
            AgentCapability(
                agent_id=self.agent.id,
                capability_id=self.capability.id,
                priority=50,
            )
        )
        self.task = Task(
            project_id=self.project.id,
            title="Use skills",
            description="Execute with skills",
            status=TaskStatus.READY,
            assigned_agent_id=self.agent.id,
            required_capabilities=[self.capability.id],
            routing_mode=RoutingMode.FIXED,
        )
        session.add(self.task)
        session.commit()

    def approve_skill(
        self,
        *,
        name: str = "outline_first",
        steps: list[dict] | None = None,
        project_id: str | None = None,
    ):
        service = SkillService(self.session)
        candidate = self._submit(service, name, steps, project_id)
        result = service.review_candidate(
            candidate.id,
            SkillReviewDecisionValue.APPROVE,
            "ok",
            actor=resolve_owner_actor(),
        )
        assert result.skill is not None
        return result.skill

    def _submit(self, service: SkillService, name: str, steps, project_id):
        # Project-scoped submissions need no artifact; company-scoped ones are
        # rejected without provenance (accepted deviation). Scope tests use a
        # second project for company-wide skills where needed.
        return service.submit_candidate(
            name,
            f"Procedure {name}",
            self.capability.id,
            steps if steps is not None else [{"step": 1, "do": "outline"}],
            {},
            "single_pass",
            project_id=project_id if project_id is not None else self.project.id,
            actor=resolve_owner_actor(),
        )

    def build(self) -> TaskContext:
        return ContextService(self.session).build_context(self.task.id)


def test_no_skill_means_empty_applicable_skills(tmp_path: Path) -> None:
    url = database(tmp_path, "ctx_empty.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        context = rig.build()
        assert context.applicable_skills == []


def test_capability_match_injects_skill(tmp_path: Path) -> None:
    url = database(tmp_path, "ctx_match.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        skill = rig.approve_skill()
        context = rig.build()
        assert len(context.applicable_skills) == 1
        projection = context.applicable_skills[0]
        assert projection["skill_id"] == skill.id
        assert projection["version"] == 1
        assert projection["capability_id"] == rig.capability.id
        assert projection["content_hash"] == skill.content_hash
        assert projection["steps"] == [{"step": 1, "do": "outline"}]


def test_external_agent_receives_no_skill(tmp_path: Path) -> None:
    url = database(tmp_path, "ctx_external.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        rig.agent.trust_level = AgentTrustLevel.VERIFIED_EXTERNAL
        session.add(rig.agent)
        session.commit()
        rig.approve_skill()
        context = rig.build()
        assert context.applicable_skills == []


def test_no_capability_intersection_receives_no_skill(tmp_path: Path) -> None:
    url = database(tmp_path, "ctx_nointersect.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        # The agent also holds a second capability; the task requires only that
        # one, while the skill is attached to the first. The effective
        # intersection (task ∩ agent) is {auditing}, so the drafting skill is
        # not eligible.
        auditing = Capability(name="auditing", description="Audits")
        session.add(auditing)
        session.flush()
        session.add(
            AgentCapability(
                agent_id=rig.agent.id,
                capability_id=auditing.id,
                priority=40,
            )
        )
        rig.approve_skill()  # attached to rig.capability ("drafting")
        rig.task.required_capabilities = [auditing.id]
        session.add(rig.task)
        session.commit()
        context = rig.build()
        assert context.applicable_skills == []


def test_same_inputs_same_hash(tmp_path: Path) -> None:
    url = database(tmp_path, "ctx_stable.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        rig.approve_skill()
        first = rig.build()
        second = rig.build()
        assert first.id == second.id
        assert first.context_hash == second.context_hash
        assert first.applicable_skills == second.applicable_skills


def test_new_version_changes_hash_but_old_snapshot_keeps_old_version(
    tmp_path: Path,
) -> None:
    url = database(tmp_path, "ctx_snapshot.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        v1 = rig.approve_skill(steps=[{"step": 1, "do": "outline"}])
        old_context = rig.build()
        assert [p["version"] for p in old_context.applicable_skills] == [1]

        # v2: same identity, new content.
        v2 = rig.approve_skill(
            steps=[{"step": 1, "do": "outline"}, {"step": 2, "do": "polish"}]
        )
        assert v2.version == 2
        new_context = rig.build()
        assert new_context.id != old_context.id
        assert new_context.context_hash != old_context.context_hash
        assert [p["version"] for p in new_context.applicable_skills] == [2]

        # Replay: the OLD row is untouched and still carries v1.
        session.refresh(old_context)
        assert [p["version"] for p in old_context.applicable_skills] == [1]
        assert old_context.applicable_skills[0]["skill_id"] == v1.id


def test_projection_order_is_deterministic(tmp_path: Path) -> None:
    url = database(tmp_path, "ctx_order.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        rig.approve_skill(name="zeta_skill")
        rig.approve_skill(name="alpha_skill")
        rig.approve_skill(name="mid_skill")
        context = rig.build()
        names = [p["name"] for p in context.applicable_skills]
        assert names == sorted(names) == ["alpha_skill", "mid_skill", "zeta_skill"]


def test_projection_truncates_at_ten(tmp_path: Path) -> None:
    url = database(tmp_path, "ctx_limit.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        for i in range(13):
            rig.approve_skill(name=f"skill_{i:02d}")
        context = rig.build()
        assert len(context.applicable_skills) == 10
        names = [p["name"] for p in context.applicable_skills]
        assert names == sorted(names)
        assert names[:3] == ["skill_00", "skill_01", "skill_02"]


def test_skill_projection_writes_audit(tmp_path: Path) -> None:
    from aios.audit import AuditLog

    url = database(tmp_path, "ctx_audit.db")
    with Session(get_engine(url)) as session:
        rig = Rig(session)
        skill = rig.approve_skill()
        rig.build()
        audits = list(
            session.exec(
                select(AuditLog).where(AuditLog.action == "skill.projected")
            )
        )
        assert len(audits) == 1
        audit = audits[0]
        assert audit.resource_id == skill.id
        assert audit.idempotency_key.startswith("audit:skill:projection:")
        assert audit.after_snapshot["version"] == 1
