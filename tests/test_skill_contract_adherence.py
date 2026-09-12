"""Plan A: skill output-contract merge + adherence (record-only) + optional fix.

Covers the pure helpers (``merge_contracts`` / ``compute_adherence``), the
``_apply_skill_adherence`` seam (record-only default, optional 1x directed fix),
and the end-to-end wiring inside ``execute_task`` (merged contract rendered into
the prompt + adherence report persisted to ``Artifact.metadata_json``).
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from sqlmodel import Session

from aios.context_service import ContextService
from aios.db import get_engine, run_migrations
from aios.execution import ExecutionResult, _apply_skill_adherence, execute_task
from aios.models import Project, Task, TaskStatus
from aios.skill_adherence import (
    ADHERENCE_VALIDATOR_VERSION,
    compute_adherence,
    merge_contracts,
)


def _contract(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


# ---------------------------------------------------------------------------
# merge_contracts
# ---------------------------------------------------------------------------


def test_merge_task_only_is_passthrough() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    merged, skill_fields, conflicts = merge_contracts(task, [])
    assert merged == task
    assert skill_fields == set()
    assert conflicts == []


def test_merge_skill_adds_field_and_required() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, skill_fields, conflicts = merge_contracts(task, [skill])
    assert "outline" in merged["properties"]
    assert "outline" in merged["required"]
    assert skill_fields == {"outline"}
    assert conflicts == []


def test_merge_two_skills_union_required() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    s1 = _contract({"a": {"type": "string"}}, ["a"])
    s2 = _contract({"b": {"type": "integer"}}, ["b"])
    merged, skill_fields, _ = merge_contracts(task, [s1, s2])
    assert set(merged["required"]) == {"summary", "a", "b"}
    assert skill_fields == {"a", "b"}


def test_merge_conflict_task_wins_recorded() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    # skill redefines an existing task field with a different type -> conflict.
    skill = _contract({"summary": {"type": "integer"}}, [])
    merged, skill_fields, conflicts = merge_contracts(task, [skill])
    assert merged["properties"]["summary"] == {"type": "string"}  # task authoritative
    assert conflicts == ["summary"]
    assert skill_fields == set()  # not a skill-origin field


def test_merge_empty_skill_contract_is_ignored() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    merged, skill_fields, conflicts = merge_contracts(task, [{}])
    assert merged == task
    assert skill_fields == set()
    assert conflicts == []


# ---------------------------------------------------------------------------
# compute_adherence (record-only reporting)
# ---------------------------------------------------------------------------


def test_compute_adherence_all_valid_no_skills() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    data = {"summary": "x"}
    rep = compute_adherence(data, task, task, [], [])
    assert rep["task_schema_valid"] is True
    assert rep["skill_adherence_valid"] is True
    assert rep["overall_contract_valid"] is True
    assert rep["required_fields"] == []
    assert rep["validator_version"] == ADHERENCE_VALIDATOR_VERSION


def test_compute_adherence_skill_required_missing() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    rep = compute_adherence(
        {"summary": "x"},  # outline missing
        task,
        merged,
        [skill],
        [{"skill_id": "s1", "name": "sk", "version": 1}],
    )
    assert rep["task_schema_valid"] is True  # task-only still valid
    assert rep["skill_adherence_valid"] is False
    assert rep["overall_contract_valid"] is False
    assert rep["missing_fields"] == ["outline"]
    assert rep["required_fields"] == ["outline"]
    assert rep["skills"] == [{"skill_id": "s1", "name": "sk", "version": 1}]


def test_compute_adherence_skill_field_wrong_type() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    rep = compute_adherence(
        {"summary": "x", "outline": 123},  # wrong type
        task,
        merged,
        [skill],
        [],
    )
    assert rep["skill_adherence_valid"] is False
    assert rep["invalid_fields"] == ["outline"]


def test_compute_adherence_skill_required_present_valid() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    rep = compute_adherence({"summary": "x", "outline": "y"}, task, merged, [skill], [])
    assert rep["skill_adherence_valid"] is True
    assert rep["overall_contract_valid"] is True


def test_compute_adherence_conflicts_propagated() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"summary": {"type": "integer"}}, [])
    merged, _, conflicts = merge_contracts(task, [skill])
    rep = compute_adherence({"summary": "x"}, task, merged, [skill], [], conflicts=conflicts)
    assert rep["conflicts"] == ["summary"]


# ---------------------------------------------------------------------------
# _apply_skill_adherence (record-only + optional fix)
# ---------------------------------------------------------------------------


def _result_with(data: dict[str, Any]) -> ExecutionResult:
    return ExecutionResult(
        summary="s",
        claims=[],
        artifacts=[{"type": "json", "uri": "u", "summary": "s", "data": data}],
    )


def test_apply_adherence_record_only_no_fix(monkeypatch) -> None:
    monkeypatch.delenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", raising=False)
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})  # outline missing

    class _Adapt:  # no fix_output -> record-only
        pass

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Adapt())
    assert rep["skill_adherence_valid"] is False
    assert rep["fix_status"] == "disabled"
    # original artifact untouched
    assert result.artifacts[0]["data"] == {"summary": "x"}


def test_apply_adherence_fix_enabled_but_unsupported(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    class _Adapt:  # fix enabled but adapter has no fix_output
        pass

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Adapt())
    assert rep["fix_status"] == "unsupported"
    assert result.artifacts[0]["data"] == {"summary": "x"}


def test_apply_adherence_fix_applied(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            return {"outline": "filled"}

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Adapt())
    assert rep["fix_status"] == "applied"
    assert rep["skill_adherence_valid"] is True
    # directed completion overlaid onto the original (summary preserved)
    assert result.artifacts[0]["data"] == {"summary": "x", "outline": "filled"}


def test_apply_adherence_fix_failed_preserves_original(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            raise RuntimeError("boom")

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Adapt())
    assert rep["fix_status"] == "failed"
    assert rep["skill_adherence_valid"] is False
    # original artifact preserved on fix failure
    assert result.artifacts[0]["data"] == {"summary": "x"}


# ---------------------------------------------------------------------------
# end-to-end wiring inside execute_task
# ---------------------------------------------------------------------------


@pytest.fixture
def eng(tmp_path):
    url = f"sqlite:///{tmp_path / 'adh.db'}"
    run_migrations(url)
    e = get_engine(url)
    with Session(e) as s:
        s.add(Project(id="p1", name="P1", objective="x"))
        s.add(
            Task(
                id="t1",
                project_id="p1",
                title="T",
                description="d",
                status=TaskStatus.READY,
                output_schema=_contract({"summary": {"type": "string"}}, ["summary"]),
            )
        )
        s.commit()
    yield e


class _CaptureAdapter:
    captured_schema: dict[str, Any] | None = None

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def run(self, *, task_id, task_context, output_schema, idempotency_key):
        _CaptureAdapter.captured_schema = output_schema
        return ExecutionResult(
            summary="s",
            claims=[],
            artifacts=[
                {"type": "json", "uri": "u", "summary": "s", "data": self.data}
            ],
        )


def _patch_heavy_lifts(monkeypatch, ctx) -> None:
    monkeypatch.delenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", raising=False)
    monkeypatch.setattr("aios.execution.route_task", lambda *a, **k: types.SimpleNamespace(id="a1"))
    monkeypatch.setattr("aios.execution.claim_task_for_execution", lambda *a, **k: True)
    monkeypatch.setattr(
        "aios.orchestrator.Orchestrator.process_pending", lambda self: None
    )
    monkeypatch.setattr(
        ContextService, "build_context", lambda self, task_id, assignment_id=None: ctx
    )


def test_execute_task_writes_adherence_record_only_and_renders_merged_contract(
    monkeypatch, eng
) -> None:
    ctx = types.SimpleNamespace(
        context_hash="ctx1",
        applicable_skills=[
            {
                "skill_id": "s1",
                "name": "sk",
                "version": 1,
                "required_output_contract": _contract(
                    {"outline": {"type": "string"}}, ["outline"]
                ),
            }
        ],
    )
    _patch_heavy_lifts(monkeypatch, ctx)

    _CaptureAdapter.captured_schema = None
    with Session(eng) as s:
        art = execute_task(s, "t1", "idem", adapter=_CaptureAdapter({"summary": "x"}))
        adh = art.metadata_json["adherence"]

    # P0 root cause addressed: the merged contract (with the skill field) is what
    # was rendered into the prompt.
    assert _CaptureAdapter.captured_schema is not None
    assert _CaptureAdapter.captured_schema["properties"].get("outline") == {"type": "string"}

    # Record-only: skill field missing is reported, NOT a task failure.
    assert adh["skill_adherence_valid"] is False
    assert adh["missing_fields"] == ["outline"]
    assert adh["task_schema_valid"] is True
    assert adh["fix_status"] == "disabled"
    with Session(eng) as s:
        assert s.get(Task, "t1").status == TaskStatus.DONE


def test_execute_task_fix_applies_end_to_end(monkeypatch, eng) -> None:
    ctx = types.SimpleNamespace(
        context_hash="ctx1",
        applicable_skills=[
            {
                "skill_id": "s1",
                "name": "sk",
                "version": 1,
                "required_output_contract": _contract(
                    {"outline": {"type": "string"}}, ["outline"]
                ),
            }
        ],
    )
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    monkeypatch.setattr("aios.execution.route_task", lambda *a, **k: types.SimpleNamespace(id="a1"))
    monkeypatch.setattr("aios.execution.claim_task_for_execution", lambda *a, **k: True)
    monkeypatch.setattr("aios.orchestrator.Orchestrator.process_pending", lambda self: None)
    monkeypatch.setattr(
        ContextService, "build_context", lambda self, task_id, assignment_id=None: ctx
    )

    class _FixAdapter:
        def run(self, *, task_id, task_context, output_schema, idempotency_key):
            return ExecutionResult(
                summary="s",
                claims=[],
                artifacts=[
                    {"type": "json", "uri": "u", "summary": "s", "data": {"summary": "x"}}
                ],
            )

        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            return {"outline": "fixed"}

    with Session(eng) as s:
        art = execute_task(s, "t1", "idem2", adapter=_FixAdapter())
        adh = art.metadata_json["adherence"]

    assert adh["fix_status"] == "applied"
    assert adh["skill_adherence_valid"] is True
    assert adh["missing_fields"] == []
