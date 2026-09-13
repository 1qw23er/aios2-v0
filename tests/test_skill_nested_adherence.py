"""P2-c: GAP-1/2/3 -- recursive (nested) skill-adherence detection.

``_type_ok`` only inspects the TOP level, and ``compute_adherence`` only walked
the top-level ``required`` fields of each skill contract. A skill-required
object / array whose SUBTREE was structurally wrong therefore stayed invisible:
the report claimed ``skill_adherence_valid=True`` while
``overall_contract_valid`` (jsonschema, fully recursive) was ``False`` -- a
self-contradictory 3-state report -- and the fix trigger
(``not skill_adherence_valid``) never fired.

P2-c descends the subtree of a top-level skill ``required`` field that is
present and type-correct, reporting each defect as a full PATH STRING in the
canonical grammar (``metadata.author`` / ``sections[1].heading``) appended to
the EXISTING ``missing_fields`` / ``invalid_fields`` -- so the P2-a whitelist
consumes it with zero rework (the D4 commitment recorded in
``test_skill_fix_boundary.py``).

Frozen scope (C1/C2/C3):

* C2 -- only ``required`` names are inspected at every level;
  ``additionalProperties`` is NOT consulted; ``_type_ok`` is untouched.
* C3 -- a top-level field that is task-authoritative (``conflicts``) is never
  descended into: its subtree is blacklisted from the fix path, so a path found
  there could never be repaired.
* ``apply_fix_patch`` / the triple gate (P2-a) and the ``fix`` sub-structure
  (P2-b) are NOT modified.
"""

from __future__ import annotations

from typing import Any

from aios.execution import ExecutionResult, _apply_skill_adherence
from aios.skill_adherence import (
    ADHERENCE_VALIDATOR_VERSION,
    _collect_nested_defects,
    compute_adherence,
    merge_contracts,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _contract(properties: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    schema.update(extra)
    return schema


def _obj(properties: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    return _contract(properties, required, **extra)


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _task() -> dict[str, Any]:
    return _contract({"summary": {"type": "string"}}, ["summary"])


def _metadata_skill() -> dict[str, Any]:
    """skill requires an object field ``metadata`` with a required ``author``."""
    return _contract(
        {"metadata": _obj({"author": {"type": "string"}}, ["author"])},
        ["metadata"],
    )


def _sections_skill() -> dict[str, Any]:
    """skill requires an array field ``sections`` of objects with ``heading``."""
    return _contract(
        {"sections": _arr(_obj({"heading": {"type": "string"}}, ["heading"]))},
        ["sections"],
    )


def _result_with(data: dict[str, Any]) -> ExecutionResult:
    return ExecutionResult(
        summary="s",
        claims=[],
        artifacts=[{"type": "json", "uri": "u", "summary": "s", "data": data}],
    )


def _adapter(returns: Any):
    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            if isinstance(returns, BaseException):
                raise returns
            return returns

    return _Adapt()


# ---------------------------------------------------------------------------
# C2 -- nested required field missing -> full path in missing_fields
# ---------------------------------------------------------------------------


def test_nested_required_missing_reports_full_path() -> None:
    task = _task()
    skill = _metadata_skill()
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "metadata": {}}, task, merged, [skill], [])

    assert rep["missing_fields"] == ["metadata.author"]
    assert rep["invalid_fields"] == []
    assert rep["skill_adherence_valid"] is False


def test_nested_required_missing_does_not_replace_top_level_entry() -> None:
    """The top-level defect record is preserved; the nested path is ADDED."""
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

    assert rep["missing_fields"] == ["outline", "metadata.author"]


# ---------------------------------------------------------------------------
# C2 -- nested field type error -> full path in invalid_fields
# ---------------------------------------------------------------------------


def test_nested_field_wrong_type_reports_full_path() -> None:
    task = _task()
    skill = _metadata_skill()
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "metadata": {"author": 123}}, task, merged, [skill], []
    )

    assert rep["invalid_fields"] == ["metadata.author"]
    assert rep["missing_fields"] == []
    assert rep["skill_adherence_valid"] is False


def test_nested_object_type_error_reported_at_its_own_path() -> None:
    """A required nested field present with the WRONG CONTAINER type."""
    task = _task()
    skill = _contract(
        {"outline": _obj({"sections": _arr({"type": "object"})}, ["sections"])},
        ["outline"],
    )
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "outline": {"sections": "not-a-list"}}, task, merged, [skill], []
    )

    assert rep["invalid_fields"] == ["outline.sections"]
    assert rep["skill_adherence_valid"] is False


# ---------------------------------------------------------------------------
# C2 -- array item defects carry the index
# ---------------------------------------------------------------------------


def test_array_item_field_missing_includes_index() -> None:
    task = _task()
    skill = _sections_skill()
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "sections": [{"heading": "a"}, {}]}, task, merged, [skill], []
    )

    assert rep["missing_fields"] == ["sections[1].heading"]
    assert rep["skill_adherence_valid"] is False


def test_array_item_field_wrong_type_includes_index() -> None:
    task = _task()
    skill = _sections_skill()
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "sections": [{"heading": "a"}, {"heading": 5}]},
        task,
        merged,
        [skill],
        [],
    )

    assert rep["invalid_fields"] == ["sections[1].heading"]
    assert rep["skill_adherence_valid"] is False


def test_array_item_itself_wrong_type_includes_index() -> None:
    task = _task()
    skill = _contract({"tags": _arr({"type": "string"})}, ["tags"])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "tags": ["ok", 7]}, task, merged, [skill], [])

    assert rep["invalid_fields"] == ["tags[1]"]
    assert rep["skill_adherence_valid"] is False


def test_deep_nesting_reports_full_path() -> None:
    """outline -> sections[] -> heading: true recursion, not one extra level."""
    task = _task()
    sections_def = _obj(
        {"sections": _arr(_obj({"heading": {"type": "string"}}, ["heading"]))},
        ["sections"],
    )
    skill = _contract({"outline": sections_def}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "outline": {"sections": [{}]}}, task, merged, [skill], []
    )

    assert rep["missing_fields"] == ["outline.sections[0].heading"]


# ---------------------------------------------------------------------------
# GAP-1/2/3 core -- the three states become coherent again
# ---------------------------------------------------------------------------


def test_nested_defect_makes_skill_valid_false_and_report_coherent() -> None:
    """Before P2-c: skill_adherence_valid=True while overall_contract_valid=False."""
    task = _task()
    skill = _metadata_skill()
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "metadata": {}}, task, merged, [skill], [])

    assert rep["overall_contract_valid"] is False
    # the defect is now VISIBLE to the skill side too -> no more contradiction
    assert rep["skill_adherence_valid"] is False


# ---------------------------------------------------------------------------
# the recursive defect reaches the EXISTING fix trigger + whitelist (D4)
# ---------------------------------------------------------------------------


def test_nested_defect_reaches_fix_trigger_and_applies(monkeypatch) -> None:
    """A nested path must flow through the frozen P2-a filter unchanged."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _task()
    skill = _metadata_skill()
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "metadata": {}})

    seen: dict[str, list[str]] = {}

    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            seen["missing"] = list(missing_fields)
            seen["invalid"] = list(invalid_fields)
            return {"metadata": {"author": "me"}}

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Adapt())

    # 1) the nested defect reached the adapter prompt payload...
    assert seen["missing"] == ["metadata.author"]
    assert seen["invalid"] == []
    # 2) ...and the fix was attempted+admitted through the EXISTING whitelist
    assert rep["fix_status"] == "applied"
    assert rep["fix_applied_paths"] == ["metadata.author"]
    assert result.artifacts[0]["data"]["metadata"] == {"author": "me"}
    assert rep["skill_adherence_valid"] is True


def test_nested_fix_still_gated_when_candidate_still_defective(monkeypatch) -> None:
    """P2-a triple gate unchanged: a nested fix whose candidate still violates
    the skill contract is rejected and the original artifact preserved."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _task()
    skill = _metadata_skill()
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "metadata": {}})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"metadata": {"author": 123}})
    )

    assert rep["fix_status"] == "failed"
    assert rep["fix_reason"] == "post_fix_contract_invalid"
    assert rep["fix_applied"] is False
    # the gate revalidated the CANDIDATE recursively -> still a nested defect
    after = rep["fix"]["after"]
    assert after["skill_adherence_valid"] is False
    assert after["invalid_fields"] == ["metadata.author"]
    # original artifact preserved
    assert result.artifacts[0]["data"] == {"summary": "x", "metadata": {}}


def test_nested_defect_on_task_invalid_artifact_is_skipped(monkeypatch) -> None:
    """INV-6 unchanged: an already-illegal artifact is never laundered into a
    'skill fix applied' record, even when the defect is nested."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    # Strict task contract: `metadata` is NOT permitted at all.
    task = _contract({"summary": {"type": "string"}}, ["summary"], additionalProperties=False)
    skill = _metadata_skill()
    merged, _, _ = merge_contracts(task, [skill])
    merged.pop("additionalProperties", None)
    result = _result_with({"summary": "x", "metadata": {}})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"metadata": {"author": "me"}})
    )

    assert rep["task_schema_valid"] is False
    assert rep["fix_status"] == "skipped"
    assert rep["fix_reason"] == "task_schema_invalid"
    assert rep["fix_attempted"] is False
    assert result.artifacts[0]["data"] == {"summary": "x", "metadata": {}}


# ---------------------------------------------------------------------------
# C3 -- task-authoritative top-level fields are NOT descended into
# ---------------------------------------------------------------------------


def test_conflict_top_level_field_skips_recursion() -> None:
    """C3: a conflicting field is Task-authoritative; its subtree is blacklisted
    from the fix path, so reporting a path there would be a permanently
    unrepairable pseudo-defect."""
    task = _contract(
        {
            "summary": {"type": "string"},
            "metadata": _obj({"author": {"type": "string"}}, ["author"]),
        },
        ["summary"],
    )
    skill = _contract(
        {"metadata": _obj({"title": {"type": "string"}}, ["title"])},
        ["metadata"],
    )
    merged, _, conflicts = merge_contracts(task, [skill])
    assert conflicts == ["metadata"]  # task definition wins

    rep = compute_adherence(
        {"summary": "x", "metadata": {}}, task, merged, [skill], [], conflicts=conflicts
    )

    # skipped -> no fabricated "detected but never fixable" path
    assert rep["missing_fields"] == []
    assert rep["invalid_fields"] == []

    # counterfactual: without the C3 skip the nested path WOULD be reported --
    # that is exactly the pseudo-defect C3 exists to suppress.
    unguarded = compute_adherence(
        {"summary": "x", "metadata": {}}, task, merged, [skill], [], conflicts=[]
    )
    assert unguarded["missing_fields"] == ["metadata.title"]


# ---------------------------------------------------------------------------
# C2 -- non-required top-level fields do not trigger recursion
# ---------------------------------------------------------------------------


def test_non_required_top_level_field_not_recursed() -> None:
    task = _task()
    skill = _contract(
        {
            "outline": {"type": "string"},
            "metadata": _obj({"author": {"type": "string"}}, ["author"]),
        },
        ["outline"],  # metadata is declared but NOT required
    )
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "outline": "y", "metadata": {}}, task, merged, [skill], []
    )

    assert rep["missing_fields"] == []
    assert rep["invalid_fields"] == []
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# C2 -- additionalProperties stays out of this round (P2-d scope)
# ---------------------------------------------------------------------------


def test_additional_properties_not_detected_by_p2c() -> None:
    task = _task()
    skill = _contract(
        {"metadata": _obj({"author": {"type": "string"}}, ["author"], additionalProperties=False)},
        ["metadata"],
    )
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "metadata": {"author": "me", "extra": "sneak"}},
        task,
        merged,
        [skill],
        [],
    )

    # an additional nested property is NOT a P2-c defect: the fix path can only
    # add/modify, so it could never remove it anyway.
    assert rep["missing_fields"] == []
    assert rep["invalid_fields"] == []
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# regression -- flat schemas behave byte-identically
# ---------------------------------------------------------------------------


def test_flat_skill_contract_behaviour_unchanged() -> None:
    task = _task()
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])

    missing = compute_adherence({"summary": "x"}, task, merged, [skill], [])
    assert missing["missing_fields"] == ["outline"]
    assert missing["invalid_fields"] == []

    wrong = compute_adherence({"summary": "x", "outline": 1}, task, merged, [skill], [])
    assert wrong["invalid_fields"] == ["outline"]
    assert wrong["missing_fields"] == []

    ok = compute_adherence({"summary": "x", "outline": "y"}, task, merged, [skill], [])
    assert ok["skill_adherence_valid"] is True
    assert ok["missing_fields"] == []
    assert ok["invalid_fields"] == []


def test_flat_object_field_without_nested_required_adds_nothing() -> None:
    """An object field with no nested ``required`` list yields no nested paths."""
    task = _task()
    skill = _contract({"metadata": _obj({"author": {"type": "string"}}, [])}, ["metadata"])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "metadata": {}}, task, merged, [skill], [])

    assert rep["missing_fields"] == []
    assert rep["invalid_fields"] == []
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# P2-b trace structure is untouched by P2-c
# ---------------------------------------------------------------------------


def test_p2b_trace_structure_intact_for_nested_defect(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _task()
    skill = _metadata_skill()
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "metadata": {}})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"metadata": {"author": "me"}})
    )

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
    # the pre-fix snapshot keeps the ORIGINAL (nested-defective) report
    assert fix["before"]["missing_fields"] == ["metadata.author"]
    assert fix["before"]["skill_adherence_valid"] is False
    assert fix["status"] == "applied"
    # primary artifact only + lineage unchanged in shape
    assert result.artifacts[0]["data"] == {"summary": "x", "metadata": {"author": "me"}}
    assert len(fix["lineage"]["original_data_sha256"]) == 64


# ---------------------------------------------------------------------------
# C1 -- validator version is "3" (P2-c semantics change)
# ---------------------------------------------------------------------------


def test_validator_version_is_three() -> None:
    assert ADHERENCE_VALIDATOR_VERSION == "3"


def test_report_validator_version_is_three() -> None:
    task = _task()
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    rep = compute_adherence({"summary": "x"}, task, merged, [skill], [])
    assert rep["validator_version"] == "3"
    assert rep["validator_version"] == ADHERENCE_VALIDATOR_VERSION


# ---------------------------------------------------------------------------
# the pure helper itself -- defensive, never raises
# ---------------------------------------------------------------------------


def test_collect_nested_defects_is_defensive_on_odd_inputs() -> None:
    # fdef not a dict / no "type" / value mismatch: no defects, no exception.
    assert _collect_nested_defects({}, "not-a-dict", ("f",)) == ([], [])
    assert _collect_nested_defects(None, {}, ("f",)) == ([], [])
    assert _collect_nested_defects([], {"type": "object"}, ("f",)) == ([], [])
    assert _collect_nested_defects({}, {"type": "array"}, ("f",)) == ([], [])
    # array with non-dict "items" (tuple validation / malformed) -> skipped
    assert _collect_nested_defects([1, 2], {"type": "array", "items": ["x"]}, ("f",)) == ([], [])
    # malformed required entry (non str) is ignored rather than crashing
    assert _collect_nested_defects({}, {"type": "object", "required": [1]}, ("f",)) == ([], [])
