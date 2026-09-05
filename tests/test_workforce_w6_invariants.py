"""W6 decision-freeze invariants (``docs/workforce/Workforce_W6_Design_V1.md``).

The W6 decision freeze is overwhelmingly a set of "do NOT build this" rulings.
A frozen decision is only real when something fails the moment it is broken, so
each test below pins exactly one decision to an executable invariant.

Decision -> artefact mapping
----------------------------
* **DR-D1-1 = (d)** no Workforce-native cost source event is introduced.
  *No code artefact*: the dormant-writer invariant already lives in
  ``tests/test_workforce_cost_evidence_w5.py::test_writer_has_no_caller_in_v1``
  and ``::test_v1_population_is_zero_without_a_caller``. Nothing is duplicated
  here, and no producer is created.
* **DR-D1-2 = (a)** reuse Delegation execution / budget gate / authoritative
  ledger; never form a second budget authority.
  -> ``test_workforce_chain_has_no_project_or_delegation_reference``,
     ``test_workforce_modules_never_reference_delegation_domain``,
     ``test_budget_used_has_exactly_one_writer``.
* **DR-D1-3** deferred (producer owner is a business decision) -> *no code
  artefact*.
* **DR-D1-4 = (b)** no source-event registry; the future caller/producer owns
  source-event identity truth -> *no code artefact*. The P-9 gap (the writer
  validates non-emptiness, not row existence) is therefore left OPEN on
  purpose; closing it was judged out of the authorized scope.
* **DR-D4-1 = (A)** no ``TERMINATED`` status; permanent Employee retained.
  -> ``test_employee_status_has_exactly_one_member``,
     ``test_no_employee_terminate_writer_exists``.
* **DR-D4-2** 409 mapping approved, but no Workforce HTTP surface exists.
  -> ``test_no_workforce_http_route_is_registered`` (the mapping itself is not
  implemented, because there is no route to map).
* **DR-D4-3 = (a)** physical purge permanently forbidden.
  -> ``test_no_employee_delete_or_purge_writer_exists``,
     ``test_no_delete_call_targets_employee``.
* **DR-D4-4** (terminate is terminal / replay adopts / terminated employees may
  still carry historical cost evidence) -- **NOT IMPLEMENTED**. These
  sub-semantics are conditional on DR-D4-1 = (B), which was NOT chosen. They
  are recorded in the design document only.
* **DR-D4-5** add a test-level invariant instead of schema -> the static
  delete-writer tests above; **no migration, no column**.

Design findings **F-1** (an Employee row is protected only by
``cost_evidence``; nothing at DB level forbids deleting an Employee that has no
evidence) and **F-2** (``employee.agent_id`` is ``NO ACTION``, not RESTRICT)
are PRESERVED, not fixed -- see ``test_employee_fk_lifecycle_is_frozen``.

Everything here is additive: one new test module, zero source changes, zero
migrations.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

from aios.models import EmployeeStatus

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "aios"

# The 14 Workforce tables as of W5 (W1--W4 = 13 frozen + cost_evidence).
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

# Tables owned by the Delegation / Project domain. Workforce must never point
# at them (Design V1 P7: dependency direction, no cycle).
DELEGATION_TABLES = {"project", "task", "delegated_run"}

# The only outward references Workforce is allowed: the shared Agent /
# Capability single source of truth (Alpha-1), reused, never redefined.
SHARED_SSOT_TABLES = {"agent", "capability"}

WORKFORCE_PREFIXES = (
    "/workforce",
    "/employee",
    "/job",
    "/trial",
    "/candidate",
)


def _columns(db_path: Path, table: str) -> set[str]:
    """Column names of ``table`` in the migrated template DB."""
    with sqlite3.connect(str(db_path)) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _foreign_keys(db_path: Path, table: str) -> dict[str, tuple[str, str]]:
    """``{from_column: (referenced_table, on_delete)}`` for ``table``."""
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    # row = (id, seq, ref_table, from_col, to_col, on_update, on_delete, match)
    return {row[3]: (row[2], row[6]) for row in rows}


def _iter_source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _function_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


# ---------------------------------------------------------------------------
# DR-D1-2 (a): one budget authority, one dependency direction
# ---------------------------------------------------------------------------


def test_workforce_chain_has_no_project_or_delegation_reference(
    template_db_path: Path,
) -> None:
    """DR-D1-2 (a) / I-W6-3 / P7: Workforce never references Project/Delegation.

    Proven on the real migrated schema: no ``project_id`` column and no FK into
    ``project`` / ``task`` / ``delegated_run`` on any of the 14 Workforce
    tables. Outward references may only reach the shared Agent / Capability
    SSoT, so the dependency direction stays one-way (and acyclic).
    """
    for table in sorted(WORKFORCE_TABLES):
        assert "project_id" not in _columns(template_db_path, table), (
            f"{table} carries a project_id: Workforce must stay project-free (I-W6-3)"
        )
        refs = {ref for ref, _ in _foreign_keys(template_db_path, table).values()}
        assert not (refs & DELEGATION_TABLES), (
            f"{table} references Delegation/Project tables {sorted(refs & DELEGATION_TABLES)}"
        )
        outward = refs - WORKFORCE_TABLES
        assert outward <= SHARED_SSOT_TABLES, (
            f"{table} references unexpected outward tables {sorted(outward)}"
        )


def test_cost_evidence_fks_are_unchanged_and_restrict(
    template_db_path: Path,
) -> None:
    """W6 changes no FK and no ON DELETE: ``cost_evidence`` stays W5-exact."""
    assert _foreign_keys(template_db_path, "cost_evidence") == {
        "job_version_id": ("job_version", "RESTRICT"),
        "employee_id": ("employee", "RESTRICT"),
    }


def test_workforce_modules_never_reference_delegation_domain() -> None:
    """DR-D1-2 (a) / BA-1..BA-3: Workforce code never touches the ledger.

    AST-level scan (names/attributes/imports) of every ``workforce*.py``
    module: no ``check_budget``, no ``budget_used``, no ``DelegatedRun`` /
    ``delegated_run``, and no import of ``aios.delegation``. Docstring prose
    may describe the rule; executable code may not reference it.
    """
    needles = ("check_budget", "budget_used", "DelegatedRun", "delegated_run")
    for path in sorted(SRC.glob("workforce*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        identifiers: set[str] = set()
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
            elif isinstance(node, ast.Import):
                identifiers.update(alias.name for alias in node.names)
        hits = [n for n in needles if n in identifiers]
        assert not hits, f"{path.name} references budget/delegation symbols: {hits}"
        assert not any("delegation" in m for m in modules), (
            f"{path.name} imports the delegation module: a second budget authority"
        )


def test_budget_used_has_exactly_one_writer() -> None:
    """BA-1 / DR-D1-2 (a): ``Project.budget_used`` has exactly one writer.

    The single writer is ``delegation._accrue_budget``. A second assignment
    anywhere in ``src/`` would be a second authoritative ledger, which the
    decision freeze forbids.
    """
    writers: list[tuple[str, int]] = []
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "budget_used":
                    writers.append((path.name, node.lineno))
    assert len(writers) == 1, (
        f"expected exactly one Project.budget_used writer, found {writers}"
    )
    assert writers[0][0] == "delegation.py", (
        f"the only budget_used writer must live in delegation.py, got {writers[0]}"
    )


# ---------------------------------------------------------------------------
# DR-D4-1 (A) / DR-D4-3 (a) / DR-D4-5: permanent Employee, no purge
# ---------------------------------------------------------------------------


def test_employee_status_has_exactly_one_member() -> None:
    """DR-D4-1 (A): EmployeeStatus still has exactly one member, ``active``.

    A state machine with a single member and zero outbound edges cannot leave
    ACTIVE -- that is the permanent-Employee model, not an oversight.
    """
    assert [member.value for member in EmployeeStatus] == ["active"]


def test_no_employee_terminate_writer_exists() -> None:
    """DR-D4-1 (A): no terminate / deactivate / offboard writer for Employee."""
    banned = ("terminate", "deactivate", "offboard")
    for path in _iter_source_files():
        for name in _function_names(path):
            lowered = name.lower()
            if "employee" in lowered and any(word in lowered for word in banned):
                raise AssertionError(
                    f"{path.name}::{name} -- termination is frozen by DR-D4-1 (A)"
                )


def test_no_employee_delete_or_purge_writer_exists() -> None:
    """DR-D4-3 (a): no delete / purge / archive writer for Employee, ever."""
    banned = ("delete", "purge", "remove", "archive", "destroy")
    for path in _iter_source_files():
        for name in _function_names(path):
            lowered = name.lower()
            if "employee" in lowered and any(word in lowered for word in banned):
                raise AssertionError(
                    f"{path.name}::{name} -- physical purge is permanently forbidden "
                    "(DR-D4-3 (a))"
                )


def test_no_delete_call_targets_employee() -> None:
    """DR-D4-3 (a) / DR-D4-5: no ``*.delete(...)`` call site touches Employee.

    The repo has exactly three delete sites (agent registry, context
    retention, withdrawn/rejected recommendation); none may ever be turned
    into an Employee delete without reopening DR-D4-3.
    """
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete"
            ):
                for arg in node.args:
                    rendered = ast.unparse(arg).lower()
                    assert "employee" not in rendered and "emp" not in rendered, (
                        f"{path.name}:{node.lineno} deletes an Employee row "
                        f"({ast.unparse(arg)}) -- forbidden by DR-D4-3 (a)"
                    )


def test_employee_fk_lifecycle_is_frozen(template_db_path: Path) -> None:
    """Rule 8: FK lifecycle is preserved, not changed -- F-2 included as-is.

    ``employee.agent_id`` is deliberately recorded as ``NO ACTION`` (F-2); it
    is a documented finding, not something W6 silently "fixes".
    """
    assert _foreign_keys(template_db_path, "employee") == {
        "job_version_id": ("job_version", "RESTRICT"),
        "job_id": ("job", "RESTRICT"),
        "agent_id": ("agent", "NO ACTION"),  # F-2: accepted finding
        "trial_id": ("trial", "RESTRICT"),
        "candidate_id": ("candidate", "RESTRICT"),
    }


# ---------------------------------------------------------------------------
# DR-D4-2: no Workforce HTTP surface yet
# ---------------------------------------------------------------------------


def test_no_workforce_http_route_is_registered() -> None:
    """DR-D4-2: no Workforce route exists, so no 409 mapping is implemented.

    Knowledge-candidate routes (``/knowledge/candidates/...``) are a different
    domain and are not matched here. The day a Workforce route is added, this
    test fails on purpose: the approved §4.5 mapping must then be implemented
    at the service layer in the same change.
    """
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if value.startswith(WORKFORCE_PREFIXES):
                    raise AssertionError(
                        f"{path.name}:{node.lineno} registers Workforce route "
                        f"{value!r} -- implement the DR-D4-2 409 mapping first"
                    )
