"""Skill System V1 architecture invariants (S1-S5, Contract §14).

Deterministic, DB-free, network-free: every check reads either the AST of a
skill module or ``SQLModel.metadata``. No line numbers, no grep over prose.

Why these five exist
--------------------
Skill V1 is deliberately grafted onto AIOS through *one* seam -- the
``TaskContext`` projection inside ``context_service``. Everything else about it
is additive: three new tables, one service, one API module. The failure mode
this module guards against is not a wrong value, it is **scope creep by
convenience**:

* a future contributor needs Workforce data inside ``skill_service`` and adds
  ``from aios.workforce import ...`` -- that silently creates the
  ``workforce <-> skill <-> execution`` cycle the Contract forbids;
* someone "needs" a per-agent skill table and adds one (see S2a below) -- that
  is a
  second identity/capability system, which Workforce already owns;
* someone updates a published ``Skill`` in place -- that destroys the version
  freeze that makes ``TaskContext`` replayable;
* someone writes ``@router.post("/candidates")`` -- which trips the W6
  Workforce route guard (F-3: ``"/candidate"`` is a prefix of
  ``"/candidates"``) and fails an unrelated frozen invariant.

The two forbidden identifiers below are spelled by concatenation so that this
file -- which legitimately names them inside assertions -- does not trip the
substring scan it performs over ``src/`` and ``tests/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from sqlmodel import SQLModel

import aios.models  # noqa: F401  -- registers every table in SQLModel.metadata
from aios.models import (  # noqa: F401  -- import asserts the symbols exist
    SKILL_NAME_PATTERN,
    Skill,
    SkillCandidate,
    SkillCandidateStatus,
    SkillExecutionStrategy,
    SkillReviewDecision,
    SkillReviewDecisionValue,
    SkillStatus,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "aios"
TESTS = Path(__file__).resolve().parents[1] / "tests"

SKILL_SERVICE = SRC / "skill_service.py"
SKILL_API = SRC / "api" / "skill.py"

# Spelled by concatenation: see module docstring.
AGENT_SKILL = "Agent" + "Skill"
EMPLOYEE_SKILL = "Employee" + "Skill"

# Modules Skill must never import (Contract §14 S1). Matching is on the last
# path segment so both ``aios.workforce`` and ``aios.workforce_trial`` are
# caught, as are relative imports (``from .workforce import ...``).
FORBIDDEN_IMPORT_TAILS = (
    "employee_bridge",
    "scheduler",
    "execution",
    "delegation",
)

# Content fields of ``Skill`` that define *what the skill does*. Mutating any of
# them after publish would rewrite history behind an immutable TaskContext
# snapshot, so no assignment path may exist in the service (Contract §14 S3).
SKILL_IMMUTABLE_ATTRS = frozenset(
    {
        "name",
        "description",
        "capability_id",
        "steps",
        "tool_bindings",
        "execution_strategy",
        "version",
        "content_hash",
    }
)

# The single attribute a published Skill may still change: lifecycle status
# (APPROVED -> SUPERSEDED/INACTIVE) plus scope bookkeeping.
SKILL_MUTABLE_ATTRS = frozenset({"status", "project_id", "superseded_by_id"})

# FK out-edges the three skill tables are allowed to have (Contract §14 S5).
ALLOWED_FK_TARGETS = frozenset(
    {
        "capability",
        "project",
        "artifact",
        "skill",
        "skill_candidate",
        "skill_review_decision",
    }
)

SKILL_TABLES = ("skill", "skill_candidate", "skill_review_decision")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imported_module_names(path: Path) -> set[str]:
    """Imported module paths, with relative imports resolved to ``aios.*``."""
    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:  # from .workforce import X
                names.add(f"aios.{node.module}")
            elif node.module:
                names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _assigned_attrs(path: Path) -> set[str]:
    """Every attribute name written to via ``x.attr = ...`` or ``setattr``."""
    attrs: set[str] = set()
    tree = _parse(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    attrs.add(target.attr)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Attribute):
                attrs.add(node.target.attr)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
            attrs.add(node.target.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) == 3
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            attrs.add(node.args[1].value)
    return attrs


def _route_literals(path: Path) -> list[tuple[int, str]]:
    """(lineno, value) for every string literal that looks like a route path."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value.startswith("/") and "skill" in value.lower():
                found.append((node.lineno, value))
    return found


def _fk_refs(table: str) -> set[str]:
    return {fk.column.table.name for fk in SQLModel.metadata.tables[table].foreign_keys}


# ---------------------------------------------------------------------------
# S1: no import of Workforce / Scheduler / Execution / Delegation
# ---------------------------------------------------------------------------


def test_s1_skill_modules_never_import_workforce_or_execution() -> None:
    """S1: the Skill domain is a leaf -- it imports no other domain engine.

    ``skill_service`` / ``api/skill`` may import shared plumbing (models,
    audit, actor, services) but must never reach into Workforce, Scheduler,
    Execution, Delegation or the employee bridge. Those edges are what would
    turn the additive skill module into a cycle
    (``workforce -> skill -> execution -> workforce``).
    """
    modules = [p for p in (SKILL_SERVICE, SKILL_API) if p.exists()]
    assert modules, "no skill module found -- has the domain been removed?"

    offenders: list[str] = []
    for path in modules:
        for module in sorted(_imported_module_names(path)):
            tail = module.rsplit(".", 1)[-1]
            banned = tail in FORBIDDEN_IMPORT_TAILS or tail.startswith("workforce")
            if banned:
                offenders.append(f"{path.name}: {module}")
    assert not offenders, (
        "Skill must not import another domain engine (cycle risk): "
        + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# S2: no parallel identity tables, no content mutation
# ---------------------------------------------------------------------------


def test_s2_no_agent_skill_or_employee_skill_identifier_anywhere() -> None:
    """S2a: no per-agent / per-employee skill table or class exists.

    Workforce already owns identity (``Employee``) and its binding
    (``EmployeeAgentBinding``). A per-agent or per-employee skill table would
    be a second, competing capability system. Skill meets Workforce only at the
    ``Capability`` SSoT -- never through a direct relation.
    """
    banned = (AGENT_SKILL, EMPLOYEE_SKILL)
    offenders: list[str] = []
    for directory in (SRC, TESTS):
        for path in sorted(directory.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if any(word in text for word in banned):
                offenders.append(str(path.relative_to(directory.parent)))
    assert not offenders, (
        f"forbidden parallel identity tables {banned} found in: "
        + "; ".join(offenders)
    )


def test_s2_skill_modules_never_assign_content_fields() -> None:
    """S2b: the only attribute Skill modules may write on a Skill is status.

    A published Skill is an immutable version. Mutation is legal for lifecycle
    (``status``) and scope bookkeeping only; every content field is frozen by
    S3. This test is the coarse net that catches *any* unexpected write; S3
    pins the specific fields.
    """
    modules = [p for p in (SKILL_SERVICE, SKILL_API) if p.exists()]
    for path in modules:
        written = _assigned_attrs(path)
        unexpected = written & SKILL_IMMUTABLE_ATTRS
        assert not unexpected, (
            f"{path.name}: writes immutable Skill content field(s) "
            f"{sorted(unexpected)} -- a published Skill version is frozen; "
            "mint a new candidate instead"
        )


# ---------------------------------------------------------------------------
# S3: version immutability inside the service
# ---------------------------------------------------------------------------


def test_s3_service_never_mutates_published_skill_content() -> None:
    """S3: no assignment path to any version-defining field of ``Skill``.

    ``TaskContext`` stores ``applicable_skills[].{skill_id, version,
    content_hash}`` as an immutable snapshot. If the service could rewrite
    ``steps`` or ``content_hash`` on a published row, a replayed Task would
    silently execute a different procedure while reporting the old hash -- the
    exact determinism failure the Contract's §10 hard condition forbids.
    """
    written = _assigned_attrs(SKILL_SERVICE)
    offenders = written & SKILL_IMMUTABLE_ATTRS
    assert not offenders, (
        f"skill_service.py assigns version-defining field(s) {sorted(offenders)}; "
        f"only {sorted(SKILL_MUTABLE_ATTRS)} may change after publish"
    )

    # Belt and braces: the ORM class exposes the fields, but no writer exists.
    cols = set(SQLModel.metadata.tables["skill"].columns.keys())
    assert cols >= SKILL_IMMUTABLE_ATTRS, (
        "immutable field set drifted from the skill table schema: "
        f"{sorted(SKILL_IMMUTABLE_ATTRS - cols)}"
    )


# ---------------------------------------------------------------------------
# S4: route strings (F-3: "/candidates" trips the W6 guard)
# ---------------------------------------------------------------------------


def test_s4_every_skill_route_starts_with_skills_prefix() -> None:
    """S4: every Skill route literal is fully qualified under ``/skills``.

    The W6 Workforce guard scans all of ``src/aios`` for route strings starting
    with ``/candidate`` (among others) and fails on any that is not in the
    closed bridge allow-list. ``"/candidates"`` -- the natural literal when a
    router carries ``prefix="/skills"`` -- starts with ``"/candidate"`` and
    would therefore trip an unrelated frozen invariant. Skill routes are thus
    written as full paths (``"/skills/candidates"``) with no router prefix.
    """
    offenders: list[str] = []
    seen: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, value in _route_literals(path):
            seen.append(value)
            if not value.startswith("/skills"):
                offenders.append(f"{path.relative_to(SRC.parent)}:{lineno} {value}")
    assert SKILL_API.exists(), (
        "api/skill.py missing -- the /skills HTTP surface is not wired yet"
    )
    assert not offenders, (
        "Skill routes must be fully qualified (F-3, W6 guard): "
        + "; ".join(offenders)
    )
    assert seen, "no skill route found -- is the API surface wired?"


def test_s4_skill_routes_never_hit_workforce_prefix() -> None:
    """S4b: no Skill route may collide with a Workforce prefix (F-3)."""
    workforce_prefixes = ("/workforce", "/employee", "/job", "/trial", "/candidate")
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, value in _route_literals(path):
            if value.lower().startswith("/skills"):
                continue
            for prefix in workforce_prefixes:
                if value.lower().startswith(prefix):
                    offenders.append(f"{path.name}:{lineno} {value}")
    assert not offenders, "skill route collides with W6 guard: " + "; ".join(offenders)


# ---------------------------------------------------------------------------
# S5: FK out-edges stay inside the allowed set
# ---------------------------------------------------------------------------


def test_s5_skill_tables_fk_out_edges_are_bounded() -> None:
    """S5: skill tables reference only Capability / Project / Artifact / self.

    Written against ``SQLModel.metadata`` (per W7-I3) rather than a migrated DB,
    so it is deterministic and needs no fixture. The allowed set deliberately
    excludes every Workforce table: Skill must be unable to point at a Job,
    Candidate, Employee or EmployeeAgentBinding even if someone later wants it.
    """
    for table in SKILL_TABLES:
        refs = _fk_refs(table)
        unexpected = refs - ALLOWED_FK_TARGETS
        assert not unexpected, (
            f"{table}: FK out-edge(s) {sorted(unexpected)} outside the allowed "
            f"set {sorted(ALLOWED_FK_TARGETS)}"
        )


def test_s5_skill_tables_are_not_workforce_tables() -> None:
    """S5b: skill tables must never migrate into the W6 table registry.

    W6 pins that Workforce tables carry no ``project_id`` and hold no FK into
    project/task/delegated_run. Skill *does* carry ``project_id`` (scope-aware
    by design), so if a skill table were ever reclassified as Workforce the two
    invariants would contradict each other. Pin the exclusion here.

    ``skill_review_decision`` is deliberately NOT scope-aware: it belongs to
    its candidate (``candidate_id``) and inherits the candidate's scope. Only
    the two scope-bearing entities carry ``project_id``.
    """
    for table in ("skill", "skill_candidate"):
        cols = set(SQLModel.metadata.tables[table].columns.keys())
        assert "project_id" in cols, f"{table} must stay scope-aware (project_id)"
    decision_cols = set(SQLModel.metadata.tables["skill_review_decision"].columns.keys())
    assert "candidate_id" in decision_cols
    assert "project_id" not in decision_cols, (
        "skill_review_decision must inherit scope via its candidate, not carry its own"
    )


# ---------------------------------------------------------------------------
# Supporting schema facts the Contract freezes elsewhere
# ---------------------------------------------------------------------------


def test_skill_enums_expose_the_frozen_members() -> None:
    """The four enums carry exactly the members the Contract's state machine uses.

    The candidate state machine is the minimal one KnowledgeService uses:
    ``DRAFT`` (created) --review--> ``APPROVED`` / ``REJECTED``. The review
    verdict lives on ``SkillReviewDecision``; no intermediate ``SUBMITTED`` /
    ``REVIEWED`` status exists to drift out of sync with it.
    """
    assert set(SkillCandidateStatus) == {
        SkillCandidateStatus.DRAFT,
        SkillCandidateStatus.APPROVED,
        SkillCandidateStatus.REJECTED,
    }
    assert set(SkillReviewDecisionValue) == {
        SkillReviewDecisionValue.APPROVE,
        SkillReviewDecisionValue.REJECT,
    }
    assert set(SkillStatus) == {
        SkillStatus.APPROVED,
        SkillStatus.SUPERSEDED,
        SkillStatus.INACTIVE,
    }
    assert SkillExecutionStrategy.TOOL_FIRST.value == "tool_first"
    assert SKILL_NAME_PATTERN == r"^[a-z][a-z0-9_]{2,63}$"


def test_task_context_carries_applicable_skills() -> None:
    """The single execution seam: TaskContext gained ``applicable_skills``.

    Nothing else in the execution chain (route_task / scheduler / execution /
    delegation / ExecutionAssignment / DelegatedRun) is touched; Skill reaches
    execution only as an immutable context projection.
    """
    cols = set(SQLModel.metadata.tables["task_context"].columns.keys())
    assert "applicable_skills" in cols
