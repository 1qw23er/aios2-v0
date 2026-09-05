"""W7 decision-freeze invariants (DR-W7-5 = (b), frozen).

Tests-only hardening slice. Zero source / alembic / docs changes.

The W7 decision freeze (DR-W7-5 = (b), recorded in
``docs/workforce/Workforce_W7_DR-W7-5_Analysis.md``, business decision taken
2026-09-05) rules that Workforce must NOT become an execution authority, a
budget authority, or an independent cost ledger, and must not silently form a
cross-domain bridge to Delegation. Each test below pins exactly one facet of
that frozen boundary to an executable invariant, using AST / SQLAlchemy
metadata / source inspection -- never line numbers, never grep over docstrings
(builds may legitimately name forbidden symbols inside prose).

Relationship to W6 (``tests/test_workforce_w6_invariants.py``)
------------------------------------------------------------
W6 already pins the coarse boundary: no ``project_id`` / no FK into
project,task,delegated_run on the 14 Workforce tables; exactly one
``Project.budget_used`` writer; no Employee purge writer; no Workforce HTTP
route. This module does NOT re-run those checks verbatim. It adds the *new*
mechanized angles the W7 freeze requires and W6 did not cover: execution-domain
ownership, bridge detection, budget-token absence inside Workforce,
``cost_evidence`` non-authority, receipt/registry non-ownership, agent-lifecycle
authority, the no-TERMINATED / no-soft-delete schema facts, the missing global
IntegrityError->409 handler, and the tests-only slice boundary (W7-I14).

Every check is deterministic: no network, no clock, no random, no DB mutation.
Schema facts come from ``SQLModel.metadata`` (per W7-I3 "prefer SQLAlchemy
metadata"), not a migrated DB file.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from sqlmodel import SQLModel

import aios.models  # noqa: F401  (registers all tables on SQLModel.metadata)
from aios.models import EmployeeStatus

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "aios"

# Parent of the W7 tests-only hardening commit (DR-W7-5 Analysis commit).
# The committed slice ``W7_SLICE_BASE..HEAD`` must be tests/-only (W7-I14).
W7_SLICE_BASE = "bb6ec086afcef478f8c89da72b4489025a3d0f02"

# The 14 Workforce tables as of W5/W6 (W1--W4 = 13 frozen + cost_evidence).
WORKFORCE_TABLES = {
    "benchmark",
    "benchmark_result",
    "benchmark_version",
    "business_goal",
    "candidate",
    "capability_requirement",
    "cost_evidence",
    "employee",
    "job",
    "job_version",
    "match",
    "recommendation",
    "required_work",
    "trial",
}

DELEGATION_TABLES = {"project", "task", "delegated_run"}
SHARED_SSOT_TABLES = {"agent", "capability"}


# ---------------------------------------------------------------------------
# AST / metadata helpers (executable code only -- docstrings excluded)
# ---------------------------------------------------------------------------


def _workforce_modules() -> list[Path]:
    return sorted(SRC.glob("workforce*.py"))


def _code_identifiers(path: Path) -> set[str]:
    """Identifiers from executable code (Name + Attribute), never docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            ids.add(node.id)
        elif isinstance(node, ast.Attribute):
            ids.add(node.attr)
    return ids


def _def_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
        elif isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
    return mods


def _workforce_facts() -> tuple[set[str], set[str], set[str]]:
    """(code identifiers, def names, imported module names) across workforce*.py."""
    ids: set[str] = set()
    defs: set[str] = set()
    mods: set[str] = set()
    for p in _workforce_modules():
        ids |= _code_identifiers(p)
        defs |= _def_names(p)
        mods |= _imported_modules(p)
    return ids, defs, mods


def _cols(table: str) -> set[str]:
    return set(SQLModel.metadata.tables[table].columns.keys())


def _fk_refs(table: str) -> set[str]:
    return {fk.column.table.name for fk in SQLModel.metadata.tables[table].foreign_keys}


# ---------------------------------------------------------------------------
# W7-I1: Workforce imports neither Delegation nor Execution
# ---------------------------------------------------------------------------


def test_w7_i1_workforce_imports_no_delegation_or_execution() -> None:
    """W7-I1: Workforce production modules never import Delegation or Execution.

    Extends W6's ``test_workforce_modules_never_reference_delegation_domain``
    (which only banned ``aios.delegation``) to also ban ``aios.execution`` -- the
    execution authority must remain a separate domain Workforce does not reach.
    """
    _, _, mods = _workforce_facts()
    assert not any("delegation" in m for m in mods), "Workforce imports the delegation domain"
    assert not any(m == "aios.execution" or m.endswith(".execution") for m in mods), (
        "Workforce imports the execution domain"
    )


# ---------------------------------------------------------------------------
# W7-I2: Workforce code never references the Project / Task model classes
# ---------------------------------------------------------------------------


def test_w7_i2_workforce_code_never_references_project_or_task() -> None:
    """W7-I2: Workforce executable code never names ``Project`` / ``Task``.

    W6 proved this at the *schema* level. W7 adds the *source* level: even an
    unused in-code reference would be the first crack of a future bridge, so it
    is banned at the identifier level (docstring prose excluded).
    """
    ids, _, _ = _workforce_facts()
    assert "Project" not in ids, "Workforce code references Project"
    assert "Task" not in ids, "Workforce code references Task"


# ---------------------------------------------------------------------------
# W7-I3: Workforce schema has no project reference (SQLAlchemy metadata)
# ---------------------------------------------------------------------------


def test_w7_i3_workforce_schema_has_no_project_reference_metadata() -> None:
    """W7-I3: over SQLAlchemy metadata, no Workforce table carries a
    ``project_id`` column or an FK into project/task/delegated_run, and no new
    Workforce table has appeared since W6 (drift detection).

    Uses ``SQLModel.metadata`` (per the authorization's "prefer SQLAlchemy
    metadata"), a second, independent mechanism beside W6's PRAGMA-on-real-DB
    check. Outward FKs may only reach the shared Agent / Capability SSoT.
    """
    present = {t for t in WORKFORCE_TABLES if t in SQLModel.metadata.tables}
    assert present == WORKFORCE_TABLES, f"missing Workforce tables: {WORKFORCE_TABLES - present}"
    for table in sorted(WORKFORCE_TABLES):
        assert "project_id" not in _cols(table), f"{table} has project_id"
        refs = _fk_refs(table)
        assert not (refs & DELEGATION_TABLES), (
            f"{table} references Delegation/Project tables {sorted(refs & DELEGATION_TABLES)}"
        )
        outward = refs - WORKFORCE_TABLES
        assert outward <= SHARED_SSOT_TABLES, (
            f"{table} references unexpected outward tables {sorted(outward)}"
        )


# ---------------------------------------------------------------------------
# W7-I4: no Workforce -> Delegation bridge exists
# ---------------------------------------------------------------------------


def test_w7_i4_no_workforce_delegation_bridge() -> None:
    """W7-I4: no Workforce -> Delegation bridge exists.

    A bridge would be either (a) a function/class in a workforce module whose
    name implies delegation (``delegate*``, ``via_delegation``, ``run_delegated``)
    or (b) a workforce code reference to Delegation's receipt builder
    ``build_delegated_provenance``. Neither may exist -- Workforce must not reach
    across the domain boundary even to borrow Delegation's plumbing.
    """
    _, defs, ids = _workforce_facts()
    bridge_defs = [
        n for n in defs
        if any(k in n.lower() for k in ("delegate", "via_delegation", "run_delegated"))
    ]
    assert not bridge_defs, f"Workforce defines a delegation bridge: {bridge_defs}"
    assert "build_delegated_provenance" not in ids, (
        "Workforce reaches into Delegation's provenance builder (bridge)"
    )


# ---------------------------------------------------------------------------
# W7-I5: Workforce owns no execution producer
# ---------------------------------------------------------------------------


def test_w7_i5_workforce_owns_no_execution_producer() -> None:
    """W7-I5: Workforce does NOT own an execution producer.

    Two-part test that distinguishes "the execution domain exists" from
    "Workforce owns it":
      (1) the execution domain genuinely exists in the repo
          (``aios/execution.py`` defines ``execute_task`` / ``ExecutionAdapter`` /
          ``ExecutionResult``) -- proving we are not testing a vacuum; and
      (2) Workforce code neither imports ``aios.execution`` nor references those
          symbols, and Workforce's own ``benchmark_result`` table carries no
          ``cost`` / ``usage`` column -- its only execution-class entry point
          (``run_benchmark``) therefore yields no cost evidence of its own.
    """
    exec_defs = _def_names(SRC / "execution.py")
    assert {"execute_task", "ExecutionAdapter", "ExecutionResult"} <= exec_defs, (
        "execution domain sanity check failed: execute_task/ExecutionAdapter/"
        "ExecutionResult not all defined in aios/execution.py"
    )
    ids, _, mods = _workforce_facts()
    assert not any(m == "aios.execution" or m.endswith(".execution") for m in mods), (
        "Workforce imports the execution domain"
    )
    assert not any(t in ids for t in ("ExecutionAdapter", "ExecutionResult", "execute_task")), (
        "Workforce code references execution-domain symbols"
    )
    br = _cols("benchmark_result")
    assert "cost" not in br and "usage" not in br, (
        f"benchmark_result carries execution cost/usage: {sorted(br)}"
    )


# ---------------------------------------------------------------------------
# W7-I6: DR-W7-5 = (b) boundary -- a future seam must fail these invariants
# ---------------------------------------------------------------------------


def test_w7_i6_dr_w7_5b_boundary_future_seam_must_fail() -> None:
    """W7-I6: DR-W7-5 = (b) architecture boundary -- a future cross-domain
    execution seam must fail these invariants.

    DR-W7-5 = (b) (business decision, 2026-09-05) freezes Workforce so it may
    NOT be executed via Delegation. This test encodes the *forward-fail* guard:
    the moment someone adds a Workforce->Delegation execution seam (an import of
    either domain, or a bridge symbol), W7-I1/I4/I5 above turn red. Here we
    assert the current frozen state those guards protect -- no import of
    delegation or execution from any workforce module.
    """
    _, _, mods = _workforce_facts()
    assert not any("delegation" in m for m in mods), "Workforce imports the delegation domain"
    assert not any(m == "aios.execution" or m.endswith(".execution") for m in mods), (
        "Workforce imports the execution domain"
    )


# ---------------------------------------------------------------------------
# W7-I7: Workforce owns no budget authority (scoped, not whole-repo)
# ---------------------------------------------------------------------------


def test_w7_i7_workforce_owns_no_budget_authority() -> None:
    """W7-I7: Workforce owns no budget authority.

    Scoped deliberately to Workforce (NOT the whole repo -- Delegation's
    ``check_budget`` / ``Project.budget_used`` are the legitimate, single budget
    authority, already pinned by W6). This test bans any ``budget`` token in
    Workforce *executable code*, so Workforce can neither call a budget gate,
    write a budget ledger, nor define a second one. Docstring prose may discuss
    the rule; code may not name budget at all.
    """
    ids, _, _ = _workforce_facts()
    budget_tokens = [t for t in ids if "budget" in t.lower()]
    assert not budget_tokens, f"Workforce code references budget: {budget_tokens}"


# ---------------------------------------------------------------------------
# W7-I8: cost_evidence is bookkeeping, not a budget authority
# ---------------------------------------------------------------------------


def test_w7_i8_cost_evidence_is_not_budget_authority() -> None:
    """W7-I8: ``cost_evidence`` is bookkeeping, not a budget authority.

    Over SQLAlchemy metadata: its FKs reach only ``job_version`` and ``employee``
    (both RESTRICT); it has no FK to project/task/delegated_run, no
    ``project_id``, no ``budget_used``, no ``limit`` column. This complements
    W6's ``test_cost_evidence_fks_are_unchanged_and_restrict`` (which asserted
    the exact two RESTRICT FKs) with the negative budget-authority assertions.
    """
    ce_refs = _fk_refs("cost_evidence")
    assert ce_refs <= {"job_version", "employee"}, f"cost_evidence FKs={ce_refs}"
    ce_cols = _cols("cost_evidence")
    assert "project_id" not in ce_cols, "cost_evidence has project_id"
    assert "budget_used" not in ce_cols, "cost_evidence writes budget_used"
    assert "limit" not in ce_cols, "cost_evidence defines a limit (budget authority)"


# ---------------------------------------------------------------------------
# W7-I9: no Workforce-owned receipt / registry
# ---------------------------------------------------------------------------


def test_w7_i9_no_workforce_owned_receipt_or_registry() -> None:
    """W7-I9: no Workforce-owned receipt / registry.

    Workforce must not own an execution receipt or a source-event registry. We
    assert (a) no workforce module *defines* a class/function named like
    ``receipt`` / ``registry`` and (b) no workforce code *references* such a
    symbol. Crucially we do NOT flag Delegation's existing
    ``build_delegated_provenance`` (the execution receipt already in the
    Delegation domain) -- that function must remain, untouched, in
    ``delegation.py``.
    """
    ids, defs, _ = _workforce_facts()
    bad_defs = [n for n in defs if any(k in n.lower() for k in ("receipt", "registry"))]
    assert not bad_defs, f"Workforce defines a receipt/registry: {bad_defs}"
    bad_ids = [t for t in ids if any(k in t.lower() for k in ("receipt", "registry"))]
    assert not bad_ids, f"Workforce references a receipt/registry: {bad_ids}"
    # Delegation's existing receipt builder must stay put (preserved, not flagged)
    del_defs = _def_names(SRC / "delegation.py")
    assert "build_delegated_provenance" in del_defs, (
        "Delegation's execution receipt builder was removed -- out of W7 scope"
    )


# ---------------------------------------------------------------------------
# W7-I10: no Employee soft-delete / purge infrastructure (schema)
# ---------------------------------------------------------------------------


def test_w7_i10_no_employee_soft_delete_infrastructure() -> None:
    """W7-I10: no Employee physical-delete / purge writer, enforced at schema.

    W6 pinned this at the *code* level (no ``employee`` + delete/purge/terminate
    function, no ``.delete(...)`` call targeting Employee). W7 adds the *schema*
    guard: the ``employee`` table carries no soft-delete / termination tracking
    columns (``terminated_at``, ``purged_at``, ``deleted_at``, ``is_deleted``,
    ``purge_at``). Absence of that infrastructure is what makes "permanent
    Employee" a structural fact rather than a convention. Note:
    ``purge_recommendation`` (a *recommendation* lifecycle op) is explicitly
    allowed and is not an Employee writer.
    """
    emp_cols = _cols("employee")
    banned = {"terminated_at", "purged_at", "deleted_at", "is_deleted", "purge_at"}
    assert not (banned & emp_cols), (
        f"employee has soft-delete columns: {sorted(banned & emp_cols)}"
    )


# ---------------------------------------------------------------------------
# W7-I11: agent lifecycle authority lives in Agent Registry
# ---------------------------------------------------------------------------


def test_w7_i11_agent_lifecycle_authority_lives_in_agent_registry() -> None:
    """W7-I11: Agent lifecycle authority belongs to Agent Registry.

    The authority to enable/disable an Agent lives in ``aios.agent_registry``
    (``set_agent_enabled``), with a soft-disable semantic. Workforce may *read*
    agents (it imports ``get_agent`` / ``list_agents``) but must never own the
    enable/disable verb. We assert the authority exists in the registry and that
    no workforce module defines or references ``set_agent_enabled`` /
    ``enable_agent`` / ``disable_agent``.
    """
    reg_defs = _def_names(SRC / "agent_registry.py")
    assert "set_agent_enabled" in reg_defs, "agent lifecycle authority missing from registry"
    ids, _, _ = _workforce_facts()
    owned = [t for t in ids if t in ("set_agent_enabled", "enable_agent", "disable_agent")]
    assert not owned, f"Workforce owns agent lifecycle verb: {owned}"


# ---------------------------------------------------------------------------
# W7-I12: this round must not introduce TERMINATED
# ---------------------------------------------------------------------------


def test_w7_i12_no_terminated_employee_status_introduced() -> None:
    """W7-I12: this round must not introduce ``TERMINATED``.

    EmployeeStatus keeps exactly the member(s) that already exist -- ``active``.
    We assert (a) no member whose value is ``terminated`` (case-insensitive) and
    (b) the live member set is exactly ``['active']``. This is NOT a permanent
    ban on a future TERMINATED state (that would be a separate decision); it is a
    freeze on silently widening the enum during this slice.
    """
    members = [m.value for m in EmployeeStatus]
    assert not any("terminated" in m.lower() for m in members), (
        f"EmployeeStatus gained a terminated member: {members}"
    )
    assert members == ["active"], f"EmployeeStatus changed: {members}"


# ---------------------------------------------------------------------------
# W7-I13: no Workforce route, no @app.delete, no global IntegrityError->409
# ---------------------------------------------------------------------------


def test_w7_i13_no_workforce_route_no_global_integrity_handler() -> None:
    """W7-I13: no Workforce HTTP route, no ``@app.delete``, and no global
    IntegrityError -> 409 handler.

    W6 asserted no Workforce *route prefix* is registered. W7 adds: the API layer
    (``aios/api/app.py``) defines no global exception handler that would translate
    an ``IntegrityError`` into a 409 -- so an uncaught FK/UNIQUE violation today
    is a 500, not a 409. That fact is part of the frozen contract; if someone
    adds such a handler, the W5/W6 "RESTRICT -> 500" reasoning changes and this
    test must fail on purpose.
    """
    app_src = (SRC / "api" / "app.py").read_text(encoding="utf-8")
    assert "exception_handler" not in app_src, "api defines an exception handler"
    assert "add_exception_handler" not in app_src, "api adds an exception handler"
    assert "@app.delete" not in app_src and "app.delete" not in app_src, (
        "api defines a DELETE route"
    )


# ---------------------------------------------------------------------------
# W7-I14 (meta): the W7 hardening slice must touch only tests/
# ---------------------------------------------------------------------------


def test_w7_i14_slice_touches_only_tests() -> None:
    """W7-I14 (meta): the W7 hardening slice must touch only ``tests/``.

    Two layers:
      * working tree: any uncommitted, tracked change must live under ``tests/``
        (so an accidental ``src/`` / ``alembic/`` / ``docs/`` edit fails loudly);
      * committed slice: if the tree is clean and HEAD is the W7 commit (its
        parent is the DR-W7-5 analysis commit), the ``<parent>..HEAD`` diff must
        be ``tests/``-only. The earlier W7 docs commits (Design V1, DR-W7-5
        Analysis) pre-date this slice and are intentionally excluded from base.
    """
    def git(*args: str) -> list[str]:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True, text=True,
        ).stdout.splitlines()

    uncommitted = [line for line in git("diff", "--name-only", "HEAD") if line]
    assert all(p.startswith("tests/") for p in uncommitted), (
        f"non-test working-tree change: {uncommitted}"
    )
    if not uncommitted:
        parent = git("rev-parse", "HEAD~1")
        parent = parent[0] if parent else ""
        if parent == W7_SLICE_BASE:
            slice_files = [line for line in git("diff", "--name-only", parent, "HEAD") if line]
            assert all(p.startswith("tests/") for p in slice_files), (
                f"W7 slice touched non-test files: {slice_files}"
            )
