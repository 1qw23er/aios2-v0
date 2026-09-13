"""P2-e (RR-1 closure): ONE jsonschema authority for the skill domain.

P2-d converged the *missed-defect surface* but left TWO defect-producing paths
inside ``skill_adherence.py``: the frozen P2-c hand-written walk and the
jsonschema slice check. That structural duality was RR-1. P2-e removes it -- the
hand-written path is DELETED and ``skill_adherence_valid`` is derived from
exactly one source: the same ``Draft202012Validator`` engine (same version) that
produces ``overall_contract_valid``, evaluated over the skill-domain PROJECTION
of the merged contract.

This file gates that claim structurally AND behaviourally:

* the deleted helpers must not come back (symbol-level assertion);
* ``_iter_skill_schema_defects`` must be the ONLY defect source (sentinel test);
* ``skill_adherence_valid is False ⟹ overall_contract_valid is False`` over a
  battery covering every constraint kind the merged schema delegates to
  jsonschema (INV-R1a);
* completeness -- each constraint kind must actually be detected;
* domain separation -- a task-only defect must not turn the skill state red;
* D3a/D3b -- a task-authoritative conflict is excluded from the skill domain;
* D4 -- a required-only contract (no ``properties``) is still checked;
* D2 -- the projection's property DEFINITIONS come from ``merged``;
* a deterministic natural error order.

Out of scope by agreement (NOT changed here): ``format`` (the engine does not
check it by default), ``additionalProperties`` at any level (C4: the fix path
only adds/changes, so it would be a permanently-red pseudo-defect), the
``merge_contracts`` key set, and the P2-a / P2-b / triple-gate machinery.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from aios import skill_adherence as sa
from aios.execution import ExecutionResult, _apply_skill_adherence
from aios.skill_adherence import (
    ADHERENCE_VALIDATOR_VERSION,
    _iter_skill_schema_defects,
    compute_adherence,
    merge_contracts,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _contract(properties: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "required": required}
    schema.update(extra)
    return schema


def _obj(properties: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    return _contract(properties, required, **extra)


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _task() -> dict[str, Any]:
    return _contract({"summary": {"type": "string"}}, ["summary"])


def _result_with(data: dict[str, Any]) -> ExecutionResult:
    return ExecutionResult(
        summary="s",
        claims=[],
        artifacts=[{"type": "json", "uri": "u", "summary": "s", "data": data}],
    )


# ---------------------------------------------------------------------------
# structural gate -- the duality must not silently return
# ---------------------------------------------------------------------------


def test_hand_written_detector_symbols_are_gone() -> None:
    """D1: the second defect-producing path is DELETED, not kept as a fallback."""
    assert not hasattr(sa, "_type_ok")
    assert not hasattr(sa, "_collect_nested_defects")
    src = inspect.getsource(sa)
    assert "def _type_ok" not in src
    assert "def _collect_nested_defects" not in src


def test_exactly_one_jsonschema_iter_errors_call_site() -> None:
    """The skill domain must have exactly ONE engine call site (the authority)."""
    assert inspect.getsource(sa).count("iter_errors(") == 1


def test_jsonschema_helper_is_the_only_defect_source(monkeypatch) -> None:
    """Behavioural proof of single authority: a sentinel from the jsonschema
    helper is the WHOLE report -- any surviving hand-written path would add its
    own entry on top (``outline`` here)."""
    task = _task()
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])

    calls: list[dict[str, Any]] = []

    def _sentinel(data: Any, projection: dict[str, Any]) -> list[tuple[str, str]]:
        calls.append(projection)
        return [("missing", "sentinel.only")]

    monkeypatch.setattr(sa, "_iter_skill_schema_defects", _sentinel)
    rep = compute_adherence({"summary": "x"}, task, merged, [skill], [])

    assert calls, "the jsonschema authority must be invoked"
    assert rep["missing_fields"] == ["sentinel.only"]
    assert rep["invalid_fields"] == []
    assert rep["skill_adherence_valid"] is False
    assert rep["overall_contract_valid"] is False


def test_projection_is_the_skill_domain_slice_of_merged(monkeypatch) -> None:
    """D2: the projection handed to the engine carries the merged DEFINITIONS and
    the merged required list, restricted to the skill-declared names."""
    task = _task()
    skill = _contract(
        {
            "outline": {"type": "string"},
            "metadata": _obj({"author": {"type": "string"}}, ["author"]),
        },
        ["outline"],
    )
    merged, _, _ = merge_contracts(task, [skill])
    captured: list[dict[str, Any]] = []

    def _capture(data: Any, projection: dict[str, Any]) -> list[tuple[str, str]]:
        captured.append(projection)
        return []

    monkeypatch.setattr(sa, "_iter_skill_schema_defects", _capture)
    compute_adherence({"summary": "x"}, task, merged, [skill], [])

    assert len(captured) == 1  # ONE projection, not one per skill
    projection = captured[0]
    assert projection["type"] == "object"
    # definitions are the merged ones (byte-identical), restricted to the
    # skill-declared names -- nothing is re-derived from the narrow contract
    expected = {k: v for k, v in merged["properties"].items() if k in {"outline", "metadata"}}
    assert projection["properties"] == expected
    assert projection["required"] == ["outline"]
    # the task-authoritative 'summary' is never part of the skill domain
    assert "summary" not in projection["properties"]


# ---------------------------------------------------------------------------
# INV-R1a -- skill_adherence_valid is False  ==>  overall_contract_valid is False
# ---------------------------------------------------------------------------

_T = _task()

_INV_R1A_CASES: list[tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = [
    (
        "top_required_missing",
        _T,
        [_contract({"outline": {"type": "string"}}, ["outline"])],
        {"summary": "x"},
    ),
    (
        "top_type_error",
        _T,
        [_contract({"outline": {"type": "string"}}, ["outline"])],
        {"summary": "x", "outline": 1},
    ),
    (
        "enum_violation",
        _T,
        [_contract({"mood": {"type": "string", "enum": ["a", "b"]}}, ["mood"])],
        {"summary": "x", "mood": "c"},
    ),
    (
        "const_violation",
        _T,
        [_contract({"version": {"type": "string", "const": "v1"}}, ["version"])],
        {"summary": "x", "version": "v2"},
    ),
    (
        "min_length_violation",
        _T,
        [_contract({"outline": {"type": "string", "minLength": 5}}, ["outline"])],
        {"summary": "x", "outline": "ab"},
    ),
    (
        "pattern_violation",
        _T,
        [_contract({"slug": {"type": "string", "pattern": "^a"}}, ["slug"])],
        {"summary": "x", "slug": "b"},
    ),
    (
        "minimum_violation",
        _T,
        [_contract({"count": {"type": "integer", "minimum": 10}}, ["count"])],
        {"summary": "x", "count": 3},
    ),
    (
        "integer_type_violation",
        _T,
        [_contract({"count": {"type": "integer"}}, ["count"])],
        {"summary": "x", "count": 1.5},
    ),
    (
        "boolean_type_violation",
        _T,
        [_contract({"flag": {"type": "boolean"}}, ["flag"])],
        {"summary": "x", "flag": "yes"},
    ),
    (
        "object_where_array_expected",
        _T,
        [_contract({"tags": _arr({"type": "string"})}, ["tags"])],
        {"summary": "x", "tags": {"not": "a-list"}},
    ),
    (
        "nested_required_missing",
        _T,
        [_contract({"metadata": _obj({"author": {"type": "string"}}, ["author"])}, ["metadata"])],
        {"summary": "x", "metadata": {}},
    ),
    (
        "nested_type_error",
        _T,
        [_contract({"metadata": _obj({"author": {"type": "string"}}, ["author"])}, ["metadata"])],
        {"summary": "x", "metadata": {"author": 7}},
    ),
    (
        "nested_enum_violation",
        _T,
        [
            _contract(
                {"metadata": _obj({"author": {"type": "string", "enum": ["me"]}}, ["author"])},
                ["metadata"],
            )
        ],
        {"summary": "x", "metadata": {"author": "you"}},
    ),
    (
        "deep_nesting_missing",
        _T,
        [
            _contract(
                {
                    "outline": _obj(
                        {"sections": _arr(_obj({"heading": {"type": "string"}}, ["heading"]))},
                        ["sections"],
                    )
                },
                ["outline"],
            )
        ],
        {"summary": "x", "outline": {"sections": [{}]}},
    ),
    (
        "array_item_type_error",
        _T,
        [_contract({"tags": _arr({"type": "string"})}, ["tags"])],
        {"summary": "x", "tags": ["ok", 7]},
    ),
    (
        "array_item_nested_required_missing",
        _T,
        [
            _contract(
                {"sections": _arr(_obj({"heading": {"type": "string"}}, ["heading"]))},
                ["sections"],
            )
        ],
        {"summary": "x", "sections": [{"heading": "a"}, {}]},
    ),
    (
        "non_required_present_type_error",
        _T,
        [_contract({"outline": {"type": "string"}}, [])],
        {"summary": "x", "outline": 1},
    ),
    (
        "non_required_present_nested_missing",
        _T,
        [_contract({"metadata": _obj({"author": {"type": "string"}}, ["author"])}, [])],
        {"summary": "x", "metadata": {}},
    ),
    (
        "required_only_contract_no_properties",
        _T,
        [{"type": "object", "required": ["outline"]}],
        {"summary": "x"},
    ),
    (
        "empty_properties_mapping_with_required",
        _T,
        [{"type": "object", "properties": {}, "required": ["outline"]}],
        {"summary": "x"},
    ),
    (
        "two_skills_both_defective",
        _T,
        [
            _contract({"a": {"type": "string"}}, ["a"]),
            _contract({"b": {"type": "string"}}, ["b"]),
        ],
        {"summary": "x", "a": 1},
    ),
    (
        "multi_skill_nested_defect",
        _T,
        [
            _contract({"a": {"type": "string"}}, ["a"]),
            _contract({"metadata": _obj({"author": {"type": "string"}}, ["author"])}, []),
        ],
        {"summary": "x", "a": "ok", "metadata": {}},
    ),
    (
        "defect_with_filtered_additional_properties",
        _T,
        [
            _contract(
                {
                    "metadata": _obj(
                        {"author": {"type": "string"}}, ["author"], additionalProperties=False
                    )
                },
                ["metadata"],
            )
        ],
        {"summary": "x", "metadata": {"author": 1, "extra": "sneak"}},
    ),
    (
        "null_not_allowed",
        _T,
        [_contract({"outline": {"type": "string"}}, ["outline"])],
        {"summary": "x", "outline": None},
    ),
]


@pytest.mark.parametrize(
    "name,task,skills,data",
    _INV_R1A_CASES,
    ids=[c[0] for c in _INV_R1A_CASES],
)
def test_inv_r1a_skill_false_implies_overall_false(
    name: str, task: dict[str, Any], skills: list[dict[str, Any]], data: dict[str, Any]
) -> None:
    merged, _, _ = merge_contracts(task, skills)
    rep = compute_adherence(data, task, merged, skills, [])

    assert rep["skill_adherence_valid"] is False, f"{name}: skill defect not detected"
    assert rep["overall_contract_valid"] is False, f"{name}: INV-R1a violated"
    assert rep["missing_fields"] or rep["invalid_fields"], f"{name}: red light with no path"


def test_inv_r1a_holds_for_every_case_or_skill_is_green() -> None:
    """Exhaustive double check: never a red skill state against a green merged
    state, regardless of which side is defective."""
    for name, task, skills, data in _INV_R1A_CASES:
        merged, _, _ = merge_contracts(task, skills)
        rep = compute_adherence(data, task, merged, skills, [])
        if rep["skill_adherence_valid"] is False:
            assert rep["overall_contract_valid"] is False, name


# ---------------------------------------------------------------------------
# domain separation -- a task-only defect must not turn the skill state red
# ---------------------------------------------------------------------------


def test_task_only_defect_does_not_turn_skill_state_red() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, [])  # declared, not required
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": 123, "outline": "ok"}, task, merged, [skill], [])

    assert rep["task_schema_valid"] is False
    assert rep["skill_adherence_valid"] is True
    assert rep["overall_contract_valid"] is False


# ---------------------------------------------------------------------------
# D3a / D3b -- a task-authoritative conflict is OUT of the skill domain
# ---------------------------------------------------------------------------


def test_d3a_conflict_field_absent_is_not_a_skill_defect() -> None:
    """N1: the old behaviour produced a permanently-unrepairable pseudo-defect
    (``missing=["metadata"]``), which the fix blacklist rejects forever."""
    task = _contract(
        {
            "summary": {"type": "string"},
            "metadata": _obj({"author": {"type": "string"}}, ["author"]),
        },
        ["summary"],
    )
    skill = _contract({"metadata": _obj({"title": {"type": "string"}}, ["title"])}, ["metadata"])
    merged, _, conflicts = merge_contracts(task, [skill])
    assert conflicts == ["metadata"]

    rep = compute_adherence({"summary": "x"}, task, merged, [skill], [], conflicts=conflicts)

    assert rep["missing_fields"] == []
    assert rep["invalid_fields"] == []
    assert rep["skill_adherence_valid"] is True
    # ...while the task / overall path still owns the field
    assert rep["overall_contract_valid"] is False


def test_d3b_conflict_field_type_error_no_longer_fakes_skill_false() -> None:
    """N2: the value satisfies the TASK definition but not the skill's. The old
    behaviour reported ``skill=False`` against ``overall=True`` -- a false
    positive AND a reverse-direction inconsistency."""
    task = _contract(
        {"summary": {"type": "string"}, "metadata": {"type": "string"}}, ["summary", "metadata"]
    )
    skill = _contract({"metadata": _obj({"author": {"type": "string"}}, ["author"])}, ["metadata"])
    merged, _, conflicts = merge_contracts(task, [skill])
    assert conflicts == ["metadata"]

    rep = compute_adherence(
        {"summary": "x", "metadata": "ok"}, task, merged, [skill], [], conflicts=conflicts
    )

    assert rep["skill_adherence_valid"] is True
    assert rep["overall_contract_valid"] is True


def test_conflict_field_still_enforced_by_the_task_path() -> None:
    """The exclusion is scoped to the SKILL domain only -- the task schema keeps
    its authority over the conflicting field."""
    task = _contract(
        {"summary": {"type": "string"}, "metadata": {"type": "string"}}, ["summary", "metadata"]
    )
    skill = _contract({"metadata": _obj({"author": {"type": "string"}}, ["author"])}, ["metadata"])
    merged, _, conflicts = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "metadata": 5}, task, merged, [skill], [], conflicts=conflicts
    )

    assert rep["task_schema_valid"] is False
    assert rep["overall_contract_valid"] is False
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# D4 -- a required-only contract (no ``properties``) is still checked
# ---------------------------------------------------------------------------


def test_d4_required_only_contract_missing_field_is_detected() -> None:
    """The old early-return guard skipped the whole block, leaving a LIVE R-1
    hole: ``skill=True`` against ``overall=False``."""
    task = _task()
    skill = {"type": "object", "required": ["outline"]}
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x"}, task, merged, [skill], [])

    assert rep["missing_fields"] == ["outline"]
    assert rep["skill_adherence_valid"] is False
    assert rep["overall_contract_valid"] is False


def test_d4_required_only_contract_satisfied_is_green() -> None:
    task = _task()
    skill = {"type": "object", "required": ["outline"]}
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "outline": "y"}, task, merged, [skill], [])

    assert rep["missing_fields"] == []
    assert rep["skill_adherence_valid"] is True
    assert rep["overall_contract_valid"] is True


# ---------------------------------------------------------------------------
# D2 -- the projection's DEFINITIONS come from ``merged``
# ---------------------------------------------------------------------------


def test_projection_definition_source_is_merged_not_the_contract() -> None:
    """Counterfactual (conflicts suppressed): the reported path follows the
    MERGED definition (``metadata.author``, the task's own field) rather than
    the narrow contract's ``metadata.title`` -- proof that the definition source
    is ``merged``. In production ``conflicts`` is always non-empty here, so the
    field is excluded entirely (D3a)."""
    task = _contract(
        {
            "summary": {"type": "string"},
            "metadata": _obj({"author": {"type": "string"}}, ["author"]),
        },
        ["summary"],
    )
    skill = _contract({"metadata": _obj({"title": {"type": "string"}}, ["title"])}, ["metadata"])
    merged, _, conflicts = merge_contracts(task, [skill])
    assert conflicts == ["metadata"]

    unguarded = compute_adherence(
        {"summary": "x", "metadata": {}}, task, merged, [skill], [], conflicts=[]
    )
    assert unguarded["missing_fields"] == ["metadata.author"]


# ---------------------------------------------------------------------------
# D5 -- natural, deterministic error order
# ---------------------------------------------------------------------------


def test_natural_order_reports_nested_before_top_level_required() -> None:
    task = _task()
    skill = _contract(
        {
            "outline": {"type": "string"},
            "metadata": _obj({"author": {"type": "string"}}, ["author"]),
        },
        ["outline", "metadata"],
    )
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "metadata": {}}, task, merged, [skill], [])

    assert rep["missing_fields"] == ["metadata.author", "outline"]


def test_order_is_deterministic_across_repeated_runs() -> None:
    task = _task()
    skill = _contract(
        {
            "alpha": {"type": "string"},
            "beta": _obj({"gamma": {"type": "string"}}, ["gamma"]),
        },
        ["alpha", "beta"],
    )
    merged, _, _ = merge_contracts(task, [skill])
    first = compute_adherence({"summary": "x", "beta": {}}, task, merged, [skill], [])
    for _ in range(5):
        again = compute_adherence({"summary": "x", "beta": {}}, task, merged, [skill], [])
        assert again["missing_fields"] == first["missing_fields"]


# ---------------------------------------------------------------------------
# the helper classifies source semantics and never raises
# ---------------------------------------------------------------------------


def test_helper_classification_and_dedup() -> None:
    projection = {
        "type": "object",
        "properties": {"mood": {"type": "string", "enum": ["a", "b"]}},
        "required": ["mood", "outline"],
    }
    defects = _iter_skill_schema_defects({"mood": "c"}, projection)

    kinds = {path: kind for kind, path in defects}
    assert kinds == {"mood": "invalid", "outline": "missing"}
    # the required keyword yields one error per name -- entries must be unique
    assert len(defects) == len(set(defects))


def test_helper_never_raises_on_malformed_projection() -> None:
    bad = {"type": "object", "properties": {"x": "nope"}}
    assert _iter_skill_schema_defects({"x": 1}, bad) == []


# ---------------------------------------------------------------------------
# C4 -- nested additionalProperties can never enter the fix whitelist
# ---------------------------------------------------------------------------


def test_nested_additional_properties_never_enters_the_whitelist(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _task()
    skill = _contract(
        {"metadata": _obj({"author": {"type": "string"}}, ["author"], additionalProperties=False)},
        ["metadata"],
    )
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "metadata": {"extra": "sneak"}})

    seen: dict[str, list[str]] = {}

    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            seen["missing"] = list(missing_fields)
            seen["invalid"] = list(invalid_fields)
            return {"metadata": {"author": "me", "extra": "sneak"}}

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Adapt())

    # only the nested REQUIRED defect is a candidate; the extra key never is
    assert seen["missing"] == ["metadata.author"]
    assert seen["invalid"] == []
    assert rep["fix"]["attempt"]["allowed_paths"] == ["metadata.author"]
    assert {"path": "metadata.extra", "reason": "not_allowed"} in rep["fix_rejected_paths"]
    # the gate still refuses (the merged contract keeps the nested
    # additionalProperties), so the original artifact survives unchanged
    assert rep["fix_status"] == "failed"
    assert result.artifacts[0]["data"] == {"summary": "x", "metadata": {"extra": "sneak"}}


# ---------------------------------------------------------------------------
# P2-b trace + version
# ---------------------------------------------------------------------------


def test_validator_version_is_five() -> None:
    assert ADHERENCE_VALIDATOR_VERSION == "5"
    task = _task()
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    rep = compute_adherence({"summary": "x"}, task, merged, [skill], [])
    assert rep["validator_version"] == "5"


def test_p2b_trace_shape_intact_under_single_authority(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _task()
    skill = _contract({"metadata": _obj({"author": {"type": "string"}}, ["author"])}, ["metadata"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "metadata": {}})

    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            return {"metadata": {"author": "me"}}

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Adapt())

    fix = rep["fix"]
    assert set(fix.keys()) == {"before", "attempt", "after", "status", "reason", "lineage"}
    assert set(fix["lineage"].keys()) == {"original_data_sha256", "final_data_sha256", "refs"}
    assert set(fix["attempt"].keys()) == {
        "allowed_paths",
        "protected_paths",
        "candidate_type",
        "applied_paths",
        "rejected_paths",
    }
    assert "candidate" not in fix["attempt"]
    assert fix["before"]["missing_fields"] == ["metadata.author"]
    assert fix["status"] == "applied"
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# R-3 is a REGISTERED RESIDUAL -- not fixed here, and its impact scope must not
# be widened. These tests pin the measured boundary.
# ---------------------------------------------------------------------------


def test_r3_odd_schema_cannot_silently_erase_a_locatable_defect() -> None:
    """The broad ``except Exception`` inside the helper still swallows engine
    failures (R-3, deliberately NOT fixed in this change). Measured bound: an
    exotic property definition must never turn a genuinely locatable defect of
    another skill into a silent pass -- the call either raises LOUDLY (the merged
    contract is meta-invalid, and ``jsonschema.validate`` meta-validates schemas
    via ``check_schema`` before iterating) or the locatable defect survives."""
    task = _task()
    odd = {"type": "object", "properties": {"x": "nope"}, "required": []}
    enum_skill = {
        "type": "object",
        "properties": {"mood": {"type": "string", "enum": ["a", "b"]}},
        "required": [],
    }
    skills = [odd, enum_skill]
    merged, _, _ = merge_contracts(task, skills)

    for data in ({"summary": "x", "mood": "c"}, {"summary": "x", "x": 1, "mood": "c"}):
        try:
            rep = compute_adherence(data, task, merged, skills, [])
        except Exception:
            continue  # loud failure -- not the silent blind spot
        assert rep["skill_adherence_valid"] is False, data
        assert rep["invalid_fields"] == ["mood"], data


def test_r3_pathless_engine_error_is_record_only_but_overall_still_red() -> None:
    """A ``false`` subschema is a VALID JSON Schema, so the merged contract is
    meta-valid and the overall state correctly goes red. The engine reports the
    violation WITHOUT a path, so the documented record-only rule (a pathless
    defect can never become a fix candidate) keeps it out of both lists. This is
    the bounded, pre-existing convention -- not a new silent hole."""
    task = _task()
    skill = {"type": "object", "properties": {"x": False}, "required": ["x"]}
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "x": 1}, task, merged, [skill], [])

    assert rep["overall_contract_valid"] is False
    assert rep["missing_fields"] == []
    assert rep["invalid_fields"] == []


def test_r3_helper_remains_total_on_malformed_input() -> None:
    """R-3's "never raises" duty is preserved verbatim: the helper returns a list
    for malformed / unresolvable projections instead of propagating."""
    for projection in (
        {"type": "object", "properties": {"x": "nope"}},
        {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}},
        {"type": "object", "properties": {}, "required": ["absent"]},
    ):
        for data in ({}, {"x": 1}):
            assert isinstance(_iter_skill_schema_defects(data, projection), list)
