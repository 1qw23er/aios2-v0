"""P2-b: GAP-4 -- fix before/after snapshot + full traceability.

Scope is frozen to GAP-4 only: the ``fix`` sub-structure carried on the
adherence report returned by :func:`aios.execution._apply_skill_adherence`.

It deliberately does NOT touch the P2-a patch filter / triple gate (GAP-5/6),
nor the P2-c recursive detection, nor scheduler/budget/retry/callback/
workforce/Skill projection. Zero migration, no new env, no new entity/API.

Every test below maps to an acceptance item signed off in the P2-b design
review (C1 dual-track / C2 hashes / C3 version bump + the frozen rules).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from aios.execution import ExecutionResult, _apply_skill_adherence
from aios.skill_adherence import (
    ADHERENCE_VALIDATOR_VERSION,
    merge_contracts,
)


def _contract(properties: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    schema.update(extra)
    return schema


def _result_with(data: dict[str, Any], *extra_data: dict[str, Any]) -> ExecutionResult:
    artifacts = [{"type": "json", "uri": "u", "summary": "s", "data": data}]
    artifacts += [
        {"type": "json", "uri": f"u{i}", "summary": "s", "data": d}
        for i, d in enumerate(extra_data, start=1)
    ]
    return ExecutionResult(summary="s", claims=[], artifacts=artifacts)


def _adapter(returns: Any):
    class _Adapt:
        def fix_output(self, *, partial_data, missing_fields, invalid_fields, contract):
            if isinstance(returns, BaseException):
                raise returns
            return returns

    return _Adapt()


def _sha(data: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# C1 (c) -- dual-track: fix.before is the pre-fix report, never overwritten
# ---------------------------------------------------------------------------


def test_fix_before_is_deepcopy_independent_of_top_and_after(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    before = rep["fix"]["before"]
    after = rep["fix"]["after"]

    # before is a SEPARATE object from both the top-level report and fix.after.
    assert before is not rep
    assert before is not after

    # mutating before must not leak into the final report or fix.after.
    before["missing_fields"].append("MUT_BEFORE")
    assert "MUT_BEFORE" not in rep["missing_fields"]
    assert "MUT_BEFORE" not in after.get("missing_fields", [])

    # mutating the top-level report must not leak back into before.
    rep["missing_fields"].append("MUT_TOP")
    assert "MUT_TOP" not in before["missing_fields"]


def test_fix_before_never_overwritten_by_after(monkeypatch) -> None:
    """GAP-4 core: even after a successful fix, fix.before keeps the ORIGINAL
    (invalid) report -- it is not replaced by the post-fix revalidation."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    before = rep["fix"]["before"]
    # original was invalid (outline missing) and carries the defect paths.
    assert before["skill_adherence_valid"] is False
    assert before["missing_fields"] == ["outline"]
    # the post-fix state is valid -- proof that before was NOT overwritten.
    assert rep["fix"]["after"]["skill_adherence_valid"] is True
    assert rep["skill_adherence_valid"] is True


# ---------------------------------------------------------------------------
# fix.after -- recorded on failure AND on skip, never faked
# ---------------------------------------------------------------------------


def test_fix_after_records_real_revalidation_on_gate_failure(monkeypatch) -> None:
    """GAP-6 gate failure: fix.after must hold the ACTUAL revalidation of the
    candidate (so we can see *why* it still failed), not a placeholder."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    # Strict task contract: no extra properties permitted.
    task = _contract({"summary": {"type": "string"}}, ["summary"], additionalProperties=False)
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    merged.pop("additionalProperties", None)  # merged contract still allows outline
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["fix_status"] == "failed"
    assert rep["fix_reason"] == "post_fix_contract_invalid"

    after = rep["fix"]["after"]
    # a real compute_adherence result (has the 3-state keys, no "revalidated" flag)
    assert "task_schema_valid" in after
    assert after.get("revalidated") is None
    # the candidate still violates the HARD task contract (additionalProperties)
    # -- that is exactly why the triple gate rejected it.
    assert after["task_schema_valid"] is False
    # original is preserved + recorded
    assert rep["fix"]["before"]["skill_adherence_valid"] is False


def test_fix_after_records_explicit_reason_when_skipped(monkeypatch) -> None:
    """Skipped (pre-condition unsatisfied): no revalidation is performed, so
    fix.after records the REASON explicitly -- it must NOT fabricate a result."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": 123})  # hard task contract already broken

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["fix_status"] == "skipped"
    assert rep["fix_reason"] == "task_schema_invalid"
    assert rep["fix"]["after"] == {"revalidated": False, "reason": "task_schema_invalid"}
    # before still present and reflects the original (illegal) artifact
    assert rep["fix"]["before"]["task_schema_valid"] is False


def test_fix_after_records_reason_on_exception(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter(RuntimeError("boom"))
    )
    assert rep["fix_status"] == "failed"
    assert rep["fix_reason"] == "fix_output_error"
    assert rep["fix"]["after"] == {"revalidated": False, "reason": "fix_output_error"}
    # attempt meta is still recorded even though the fix blew up
    assert rep["fix"]["attempt"] is not None


# ---------------------------------------------------------------------------
# C2 -- fix.attempt carries structure only, never the candidate value
# ---------------------------------------------------------------------------


def test_fix_attempt_has_no_candidate_value(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled", "body": "x"})
    )
    attempt = rep["fix"]["attempt"]
    assert attempt is not None
    # only structured provenance -- the candidate PAYLOAD is never stored
    assert "candidate" not in attempt
    assert set(attempt.keys()) == {
        "allowed_paths",
        "protected_paths",
        "candidate_type",
        "applied_paths",
        "rejected_paths",
    }
    assert attempt["allowed_paths"] == ["outline"]
    assert attempt["candidate_type"] == "dict"
    assert attempt["applied_paths"] == ["outline"]
    # rejected entries are {path, reason} only
    for r in attempt["rejected_paths"]:
        assert set(r.keys()) == {"path", "reason"}


# ---------------------------------------------------------------------------
# C2 -- fix.lineage: hashes only, no raw/candidate/final data persisted
# ---------------------------------------------------------------------------


def test_fix_lineage_hashes_present_no_candidate_hash(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    lineage = rep["fix"]["lineage"]
    assert "original_data_sha256" in lineage
    assert "final_data_sha256" in lineage
    assert len(lineage["original_data_sha256"]) == 64
    assert len(lineage["final_data_sha256"]) == 64
    # candidate value NOT persisted -> no candidate hash (C2 permission to omit)
    assert "candidate_data_sha256" not in lineage
    # original != final because the fix changed the data
    assert lineage["original_data_sha256"] != lineage["final_data_sha256"]
    assert lineage["original_data_sha256"] == _sha({"summary": "x"})
    assert lineage["final_data_sha256"] == _sha({"summary": "x", "outline": "filled"})
    assert lineage["refs"]["candidate"] == "not persisted (P2-b C2)"


def test_fix_lineage_original_eq_final_when_not_applied(monkeypatch) -> None:
    monkeypatch.delenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", raising=False)
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    lineage = rep["fix"]["lineage"]
    assert lineage["original_data_sha256"] == lineage["final_data_sha256"]
    assert lineage["original_data_sha256"] == _sha({"summary": "x"})


# ---------------------------------------------------------------------------
# primary artifact only -- both the fix and the hash recording
# ---------------------------------------------------------------------------


def test_only_primary_artifact_in_fix_and_hashes(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"}, {"kind": "side", "value": 7})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["fix_status"] == "applied"
    # sibling untouched
    assert result.artifacts[1]["data"] == {"kind": "side", "value": 7}
    # lineage covers the PRIMARY artifact only
    lineage = rep["fix"]["lineage"]
    assert lineage["original_data_sha256"] == _sha({"summary": "x"})
    assert lineage["final_data_sha256"] == _sha({"summary": "x", "outline": "filled"})


# ---------------------------------------------------------------------------
# C1 (c) -- top-level adherence keeps FINAL semantics (consumer compat)
# ---------------------------------------------------------------------------


def test_top_level_adherence_keeps_final_semantics(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    # top-level reflects the post-fix FINAL state
    assert rep["skill_adherence_valid"] is True
    assert rep["missing_fields"] == []
    assert rep["fix_status"] == "applied"
    # and it agrees with fix.after
    assert rep["missing_fields"] == rep["fix"]["after"]["missing_fields"]


# ---------------------------------------------------------------------------
# C3 -- validator version is MODULE-OWNED; no version literal is hardcoded here
# (the concrete value is pinned once, by the P2-c acceptance suite, so a future
# bump never reddens this P2-b traceability file).
# ---------------------------------------------------------------------------


def test_validator_version_is_module_owned() -> None:
    assert isinstance(ADHERENCE_VALIDATOR_VERSION, str)
    assert ADHERENCE_VALIDATOR_VERSION


def test_report_validator_version_matches_module_constant(monkeypatch) -> None:
    monkeypatch.delenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", raising=False)
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["validator_version"] == ADHERENCE_VALIDATOR_VERSION


# ---------------------------------------------------------------------------
# disabled / unsupported -- fix sub-structure still present & traceable
# ---------------------------------------------------------------------------


def test_fix_disabled_still_has_before_and_reason_after(monkeypatch) -> None:
    monkeypatch.delenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", raising=False)
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["fix_status"] == "disabled"
    assert rep["fix"]["before"]["fix_status"] == "disabled"
    assert rep["fix"]["attempt"] is None
    assert rep["fix"]["after"] == {"revalidated": False, "reason": "fix_disabled"}
    assert rep["fix"]["status"] == "disabled"


def test_fix_unsupported_records_reason_after(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    class _Plain:
        pass

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Plain())
    assert rep["fix_status"] == "unsupported"
    assert rep["fix"]["attempt"] is None
    assert rep["fix"]["after"] == {"revalidated": False, "reason": "fix_unsupported"}
