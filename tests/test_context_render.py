"""Skill System V1 -- task-context rendering tests.

Covers the last hop before the prompt reaches the agent:
``render_task_context_markdown``. The function must render the
``TaskContext.applicable_skills`` snapshot that ``ContextService._select_skills``
already projected and persisted -- it must never re-query the database, never
re-select, and never reorder. It must also leave every pre-existing field (and
the ``context_hash`` contract, which is computed from the structured payload and
never covered the rendered text) exactly as it was.
"""

from __future__ import annotations

import copy
from pathlib import Path

from sqlmodel import Session

from aios.actor import resolve_owner_actor
from aios.context_render import render_task_context_markdown, task_context_payload
from aios.context_service import ContextService
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    Capability,
    Project,
    RoutingMode,
    SkillReviewDecisionValue,
    SkillStatus,
    Task,
    TaskContext,
    TaskStatus,
)
from aios.skill_service import SkillService

_EXISTING_SECTIONS = (
    "## Project Context",
    "## Dependency Outputs",
    "## Approved Facts",
    "## Relevant Decisions",
    "## Applicable Policies",
    "## Agent Profile",
    "## Source References",
)


def _context(skills: list[dict]) -> TaskContext:
    return TaskContext(
        task_id="tsk_render",
        project_id="prj_render",
        objective="Render the context",
        instructions="Follow the instructions",
        acceptance_criteria=["Criterion A", "Criterion B"],
        project_context={"project_name": "P"},
        dependency_outputs=[],
        approved_facts=[{"statement": "Approved fact one"}],
        relevant_decisions=[],
        applicable_policies=[{"name": "no_secrets"}],
        applicable_skills=skills,
        agent_profile={"name": "writer"},
        source_references=[],
        context_hash="deadbeef",
    )


def _skill(**overrides) -> dict:
    """One ``applicable_skills`` entry exactly as ``_select_skills`` builds it."""
    entry = {
        "skill_kind": "skill",
        "skill_id": "skill_1",
        "name": "outline_first",
        "version": 1,
        "capability_id": "cap_1",
        "capability_name": "drafting",
        "scope": "project",
        "project_id": "prj_render",
        "source_project_id": "prj_render",
        "description": "Outline before writing",
        "steps": [{"step": 1, "do": "outline"}, {"step": 2, "do": "draft"}],
        "tool_bindings": {"editor": "markdown"},
        "execution_strategy": "single_pass",
        "content_hash": "abc123",
        "source_candidate_id": "cand_1",
        "review_decision_id": "rev_1",
        "source_artifact_id": "art_1",
    }
    entry.update(overrides)
    return entry


def _database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def _approved_skill_rig(session: Session):
    """Minimal INTERNAL agent + capability-matched task + one approved skill."""
    project = Project(name="Ctx", objective="Rendered")
    agent = Agent(
        id="agt_render",
        name="Skilled",
        role="writer",
        adapter_type=AdapterType.EXTERNAL,
    )
    capability = Capability(name="drafting", description="Writes")
    session.add_all([project, agent, capability])
    session.flush()
    session.add(
        AgentCapability(agent_id=agent.id, capability_id=capability.id, priority=50)
    )
    task = Task(
        project_id=project.id,
        title="Render",
        description="Execute with skills",
        status=TaskStatus.READY,
        assigned_agent_id=agent.id,
        required_capabilities=[capability.id],
        routing_mode=RoutingMode.FIXED,
    )
    session.add(task)
    session.commit()

    service = SkillService(session)
    candidate = service.submit_candidate(
        "outline_first",
        "Outline before writing",
        capability.id,
        [{"step": 1, "do": "outline"}],
        {"editor": "markdown"},
        "single_pass",
        project_id=project.id,
        actor=resolve_owner_actor(),
    )
    result = service.review_candidate(
        candidate.id,
        SkillReviewDecisionValue.APPROVE,
        "ok",
        actor=resolve_owner_actor(),
    )
    assert result.skill is not None
    return task, result.skill


# --- 1. empty snapshot is inert ---------------------------------------------


def test_empty_skills_keeps_existing_output() -> None:
    md = render_task_context_markdown(_context([]))
    assert "Applicable Skills" not in md
    for section in _EXISTING_SECTIONS:
        assert section in md
    assert "Criterion A" in md
    assert "Approved fact one" in md


def test_empty_skills_output_matches_absent_field() -> None:
    # The default of ``applicable_skills`` is an empty list, so an explicit
    # empty list must render byte-for-byte like a payload that omits the key.
    payload = _context([]).model_dump(mode="json")
    without = dict(payload)
    without.pop("applicable_skills")
    assert render_task_context_markdown(payload) == render_task_context_markdown(without)


# --- 2. a single skill is rendered from its real schema ----------------------


def test_single_skill_renders_its_schema_fields() -> None:
    md = render_task_context_markdown(_context([_skill()]))
    assert "## Applicable Skills" in md
    assert "### Skill: outline_first" in md
    assert "Outline before writing" in md  # description
    assert "outline" in md and "draft" in md  # steps
    assert "editor" in md and "markdown" in md  # tool_bindings
    assert "single_pass" in md  # execution_strategy


def test_skill_content_is_guidance_not_system_instruction() -> None:
    md = render_task_context_markdown(_context([_skill()]))
    # The section must be framed as execution guidance and explicitly state it
    # does not override the task constraints rendered above.
    lowered = md.lower()
    assert "guidance" in lowered
    assert "do not override" in lowered


# --- 3. determinism and stable ordering -------------------------------------


def test_multiple_skills_render_in_stable_order() -> None:
    skills = [
        _skill(skill_id="s1", name="alpha_skill"),
        _skill(skill_id="s2", name="mid_skill"),
        _skill(skill_id="s3", name="zeta_skill"),
    ]
    md = render_task_context_markdown(_context(skills))
    assert md.index("alpha_skill") < md.index("mid_skill") < md.index("zeta_skill")


def test_render_is_byte_identical_for_identical_input() -> None:
    skills = [
        _skill(skill_id="s1", name="alpha_skill"),
        _skill(skill_id="s2", name="beta_skill"),
    ]
    payload = _context(skills).model_dump(mode="json")
    assert render_task_context_markdown(payload) == render_task_context_markdown(payload)
    assert render_task_context_markdown(copy.deepcopy(payload)) == render_task_context_markdown(
        copy.deepcopy(payload)
    )


def test_render_does_not_mutate_the_context() -> None:
    context = _context([_skill()])
    before = copy.deepcopy(context.applicable_skills)
    render_task_context_markdown(context)
    assert context.applicable_skills == before


# --- 4. existing fields are preserved ---------------------------------------


def test_skills_do_not_remove_or_override_existing_fields() -> None:
    md = render_task_context_markdown(_context([_skill()]))
    for section in _EXISTING_SECTIONS:
        assert section in md
    assert "Render the context" in md  # objective
    assert "Follow the instructions" in md  # instructions
    assert "Criterion A" in md  # acceptance criteria
    assert "Approved fact one" in md  # approved_facts
    assert "no_secrets" in md  # applicable_policies


def test_skill_outside_the_snapshot_is_not_rendered() -> None:
    md = render_task_context_markdown(_context([_skill(name="included_skill")]))
    assert "included_skill" in md
    assert "excluded_skill" not in md


# --- 5. sparse fields emit no placeholders ----------------------------------


def test_sparse_skill_emits_no_placeholder_content() -> None:
    sparse = _skill(
        name="sparse_skill",
        description="",
        steps=[],
        tool_bindings={},
        execution_strategy=None,
        capability_id=None,
        capability_name=None,
        version=None,
        skill_id=None,
        content_hash=None,
    )
    md = render_task_context_markdown(_context([sparse]))
    assert "sparse_skill" in md
    assert "None" not in md
    assert "{'step'" not in md  # never a Python dict repr
    assert "#### Steps" not in md  # no empty subsection title
    assert "#### Tool bindings" not in md


# --- 6. integration through the real context engine -------------------------


def test_projected_skill_reaches_the_rendered_prompt(tmp_path: Path) -> None:
    url = _database(tmp_path, "render_integration.db")
    with Session(get_engine(url)) as session:
        task, skill = _approved_skill_rig(session)
        context = ContextService(session).build_context(task.id)
        assert [p["skill_id"] for p in context.applicable_skills] == [skill.id]

        md = render_task_context_markdown(context)
        assert "## Applicable Skills" in md
        assert f"### Skill: {skill.name}" in md
        assert "Outline before writing" in md
        assert "outline" in md
        # The JSON payload path used by the WORKSTATION adapter is unchanged.
        assert task_context_payload(context)["applicable_skills"] == context.applicable_skills


def test_render_uses_the_persisted_snapshot_not_live_state(tmp_path: Path) -> None:
    url = _database(tmp_path, "render_snapshot.db")
    with Session(get_engine(url)) as session:
        task, skill = _approved_skill_rig(session)
        context = ContextService(session).build_context(task.id)
        assert context.applicable_skills

        # Deactivate the live head: a re-query would now project nothing, so a
        # rendered section here proves the snapshot is used, not the database.
        skill.status = SkillStatus.INACTIVE
        session.add(skill)
        session.commit()
        # Re-load the persisted TaskContext row (its frozen snapshot), not the
        # skill table -- the point of the test is that rendering reads the row.
        session.refresh(context)

        md = render_task_context_markdown(context)
        assert f"### Skill: {skill.name}" in md
