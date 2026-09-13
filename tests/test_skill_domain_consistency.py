"""P2-d (R-1): skill / overall one-way consistency via jsonschema convergence.

Before P2-d, ``compute_adherence``'s skill-side check was a HAND-WRITTEN subset
(type + required names) of the FULL ``jsonschema`` authority that produces
``overall_contract_valid``. Any skill-declared constraint the hand-written check
did not model (enum / const / non-required fields / nested constraints) produced
a self-contradictory report: ``skill_adherence_valid=True`` while
``overall_contract_valid=False`` -- and never triggered the fix.

P2-d keeps the frozen P2-c hand check verbatim and additionally runs
``jsonschema`` ``iter_errors`` over each skill's declared field set (excluding
task-authoritative conflicts), appending any defect the hand check could not see.
This is AUTHORITY CONVERGENCE (same engine, same merged-field semantics, scoped
to the skill domain), not a second validator.

Scope boundaries (per the signed-off design):

* IN: type, required, enum, const, non-required-field violations, any other
  constraint the merged schema actually delegates to jsonschema with a locatable
  path.
* OUT: ``format`` (jsonschema does not check it by default, else we'd create a
  REVERSE inconsistency); top-level ``additionalProperties`` (merge_contracts
  drops it, so merged never fails on it); nested ``additionalProperties`` (C4:
  unrepairable -- the fix path only adds/changes, never deletes unknown keys);
  anything merge_contracts does not carry; any merge_contracts key-set expansion.
* The triple gate (P2-a), the fix path (P2-a) and the trace sub-structure (P2-b)
  are NOT modified. The validator is record-only; ``skill_adherence_valid`` is
  consumed only by the (default-off) fix path.
"""

from __future__ import annotations

from typing import Any

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
# C.4 #1 -- enum violation on a required field -> invalid_fields + skill False
# ---------------------------------------------------------------------------


def test_enum_violation_on_required_field_reported() -> None:
    task = _task()
    skill = _contract({"mood": {"type": "string", "enum": ["a", "b"]}}, ["mood"])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "mood": "c"}, task, merged, [skill], [])

    assert rep["invalid_fields"] == ["mood"]
    assert rep["missing_fields"] == []
    assert rep["skill_adherence_valid"] is False
    # the defect is also visible to the merged (overall) authority
    assert rep["overall_contract_valid"] is False


# ---------------------------------------------------------------------------
# C.4 #1 (const) -- const violation on a required field -> invalid_fields
# ---------------------------------------------------------------------------


def test_const_violation_on_required_field_reported() -> None:
    task = _task()
    skill = _contract({"version": {"type": "string", "const": "v1"}}, ["version"])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "version": "v2"}, task, merged, [skill], [])

    assert rep["invalid_fields"] == ["version"]
    assert rep["skill_adherence_valid"] is False


# ---------------------------------------------------------------------------
# C.4 #2 -- non-required top-level field PRESENT + type error -> invalid_fields
# ---------------------------------------------------------------------------


def test_non_required_present_type_error_reported() -> None:
    task = _task()
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "outline": 1}, task, merged, [skill], [])

    assert rep["invalid_fields"] == ["outline"]
    assert rep["skill_adherence_valid"] is False


# ---------------------------------------------------------------------------
# C.4 #3 -- non-required top-level field PRESENT + nested required missing
# ---------------------------------------------------------------------------


def test_non_required_present_nested_required_missing_reported() -> None:
    task = _task()
    skill = _contract(
        {"metadata": _obj({"author": {"type": "string"}}, ["author"])},
        ["outline"],
    )
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "outline": "y", "metadata": {}}, task, merged, [skill], []
    )

    assert rep["missing_fields"] == ["metadata.author"]
    assert rep["skill_adherence_valid"] is False


# ---------------------------------------------------------------------------
# C.4 #4 -- non-required PRESENT + nested enum/type violation -> invalid_fields
# ---------------------------------------------------------------------------


def test_non_required_present_nested_constraint_violation_reported() -> None:
    task = _task()
    # metadata is non-required; its nested 'author' is required and has an enum.
    skill = _contract(
        {
            "metadata": _obj(
                {"author": {"type": "string", "enum": ["me"]}}, ["author"]
            )
        },
        [],
    )
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "metadata": {"author": "you"}}, task, merged, [skill], []
    )

    assert rep["invalid_fields"] == ["metadata.author"]
    assert rep["skill_adherence_valid"] is False


# ---------------------------------------------------------------------------
# C.4 #5 -- format violation does NOT produce a defect (reverse-inconsistency guard)
# ---------------------------------------------------------------------------


def test_format_violation_not_reported() -> None:
    task = _task()
    skill = _contract({"email": {"type": "string", "format": "email"}}, ["email"])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "email": "not-an-email"}, task, merged, [skill], []
    )

    # jsonschema does not check 'format' by default, so neither side fails.
    assert rep["invalid_fields"] == []
    assert rep["missing_fields"] == []
    assert rep["skill_adherence_valid"] is True
    assert rep["overall_contract_valid"] is True


# ---------------------------------------------------------------------------
# C.4 #6 -- top-level additionalProperties violation does NOT produce a defect
# (merge_contracts drops the key, so merged never fails on it)
# ---------------------------------------------------------------------------


def test_top_level_additional_properties_not_reported() -> None:
    task = _task()
    skill = _contract({"outline": {"type": "string"}}, ["outline"], additionalProperties=False)
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence(
        {"summary": "x", "outline": "y", "extra": "sneak"}, task, merged, [skill], []
    )

    assert rep["invalid_fields"] == []
    assert rep["missing_fields"] == []
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# C.4 #7 / C4 -- nested additionalProperties is filtered, NOT in the whitelist
# (unrepairable: the fix path only adds/changes, never deletes unknown keys)
# ---------------------------------------------------------------------------


def test_nested_additional_properties_filtered_not_in_whitelist() -> None:
    task = _task()
    skill = _contract(
        {
            "metadata": _obj(
                {"author": {"type": "string"}}, ["author"], additionalProperties=False
            )
        },
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

    # filtered: the extra key is NOT a skill defect, so no pseudo-red light,
    # and (critically) it must never enter the fix whitelist.
    assert rep["invalid_fields"] == []
    assert rep["missing_fields"] == []
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# C.4 #8 -- three-state coherence: no more skill-True / overall-False for the
# skill domain (non-required enum violation used to contradict).
# ---------------------------------------------------------------------------


def test_three_state_coherent_for_skill_domain_violation() -> None:
    task = _task()
    skill = _contract({"mood": {"type": "string", "enum": ["a", "b"]}}, [])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x", "mood": "c"}, task, merged, [skill], [])

    # the defect is now visible to BOTH authorities -> no contradiction
    assert rep["overall_contract_valid"] is False
    assert rep["skill_adherence_valid"] is False


# ---------------------------------------------------------------------------
# C.4 #9 -- the defect reaches the EXISTING fix trigger + whitelist unchanged
# ---------------------------------------------------------------------------


def test_defect_reaches_fix_trigger_and_whitelist(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _task()
    skill = _contract({"mood": {"type": "string", "enum": ["a", "b"]}}, ["mood"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "mood": "c"})

    seen: dict[str, list[str]] = {}

    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            seen["missing"] = list(missing_fields)
            seen["invalid"] = list(invalid_fields)
            return {"mood": "a"}

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Adapt())

    assert seen["invalid"] == ["mood"]
    assert seen["missing"] == []
    assert rep["fix_status"] == "applied"
    assert rep["fix_applied_paths"] == ["mood"]
    assert result.artifacts[0]["data"]["mood"] == "a"
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# C.4 #9 (trace) -- the P2-b fix sub-structure is untouched by P2-d
# ---------------------------------------------------------------------------


def test_p2b_trace_structure_intact_for_enum_defect(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _task()
    skill = _contract({"mood": {"type": "string", "enum": ["a", "b"]}}, ["mood"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "mood": "c"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter_enum()
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
    assert fix["before"]["invalid_fields"] == ["mood"]
    assert fix["before"]["skill_adherence_valid"] is False
    assert fix["status"] == "applied"
    assert len(fix["lineage"]["original_data_sha256"]) == 64


def _adapter_enum() -> Any:
    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            return {"mood": "a"}

    return _Adapt()


# ---------------------------------------------------------------------------
# C.4 #10 -- conflict (task-authoritative) subtree skipped; counterfactual
# (no skip) would produce a permanently-unrepairable pseudo-defect.
# ---------------------------------------------------------------------------


def test_conflict_field_subtree_skipped_but_counterfactual_reports() -> None:
    task = _contract(
        {"summary": {"type": "string"}, "metadata": {"type": "string"}}, ["summary"]
    )
    # skill redefines 'metadata' as an object with an enum constraint -> conflict.
    skill = _contract(
        {
            "metadata": _obj(
                {"author": {"type": "string"}}, ["author"], enum=["x"]
            )
        },
        [],
    )
    merged, _, conflicts = merge_contracts(task, [skill])
    assert conflicts == ["metadata"]  # task definition wins

    data = {"summary": "x", "metadata": {"author": "me", "enum": "y"}}

    rep = compute_adherence(data, task, merged, [skill], [], conflicts=conflicts)
    # skipped -> no fabricated "detected but never fixable" path
    assert rep["invalid_fields"] == []
    assert rep["missing_fields"] == []
    assert rep["skill_adherence_valid"] is True

    # counterfactual: without the C3 skip the nested enum WOULD be reported --
    # that is exactly the pseudo-defect C3 exists to suppress.
    unguarded = compute_adherence(data, task, merged, [skill], [], conflicts=[])
    assert unguarded["invalid_fields"] == ["metadata"]


# ---------------------------------------------------------------------------
# C.4 #11 -- a non-required field that is MISSING is NOT reported (legal)
# ---------------------------------------------------------------------------


def test_non_required_field_missing_not_reported() -> None:
    task = _task()
    skill = _contract({"mood": {"type": "string", "enum": ["a", "b"]}}, [])
    merged, _, _ = merge_contracts(task, [skill])

    rep = compute_adherence({"summary": "x"}, task, merged, [skill], [])

    assert rep["missing_fields"] == []
    assert rep["invalid_fields"] == []
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# C.4 #12 -- flat required schema behaves byte-identically (no flip of P1/P2-a)
# ---------------------------------------------------------------------------


def test_flat_required_schema_zero_flip() -> None:
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


# ---------------------------------------------------------------------------
# C.4 #13 -- validator version is the constant (never a hard-coded literal)
# ---------------------------------------------------------------------------


def test_validator_version_is_constant() -> None:
    assert ADHERENCE_VALIDATOR_VERSION == "4"
    task = _task()
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    rep = compute_adherence({"summary": "x"}, task, merged, [skill], [])
    assert rep["validator_version"] == ADHERENCE_VALIDATOR_VERSION


# ---------------------------------------------------------------------------
# C.4 #14 -- the pure helper classifies source semantics and is defensive
# ---------------------------------------------------------------------------


def test_helper_classifies_required_as_missing_and_filters_additional_properties() -> None:
    local = {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "enum": ["a", "b"]},
            "metadata": _obj(
                {"author": {"type": "string"}}, ["author"], additionalProperties=False
            ),
        },
        "required": ["mood"],
    }
    data = {"mood": "c", "metadata": {"author": "me", "extra": "x"}}

    defects = _iter_skill_schema_defects(data, local)

    kinds = {path: kind for kind, path in defects}
    # enum violation -> invalid
    assert kinds.get("mood") == "invalid"
    # nested required satisfied + nested additionalProperties -> filtered (no entry)
    assert "metadata" not in kinds
    assert "metadata.author" not in kinds
    assert "metadata.extra" not in kinds


def test_helper_defensive_on_malformed_schema() -> None:
    # malformed property (not a dict) must not raise; yields no defects.
    bad = {"type": "object", "properties": {"x": "not-a-dict"}, "required": ["x"]}
    assert _iter_skill_schema_defects({"x": 1}, bad) == []

    # a structurally valid schema with no violations -> empty, no raise.
    good = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    assert _iter_skill_schema_defects({"x": "ok"}, good) == []
