"""P2-a: skill-fix boundary -- patch filtering + the triple contract gate.

Scope is frozen to GAP-5 (a fix must not touch task-owned fields) and GAP-6 (a
fix that breaks the hard task contract must never be admitted), plus the
agreed decisions:

* D1 -- failure records structured ``rejected_paths``/``reason``, never the
  candidate payload.
* D2 -- ``fix_status="skipped"`` separates "never attempted" from "failed".
* D3 -- only the PRIMARY artifact may be overwritten.
* D4 -- path-level whitelist/blacklist lands now, so P2-c (recursive detection)
  does not have to rework the filter.

Every test below maps to an acceptance item signed off in the P2-a design
review.
"""

from __future__ import annotations

from typing import Any

from aios.execution import ExecutionResult, _apply_skill_adherence
from aios.skill_adherence import apply_fix_patch, merge_contracts, task_owned_paths


def _contract(
    properties: dict[str, Any], required: list[str], **extra: Any
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# task_owned_paths
# ---------------------------------------------------------------------------


def test_task_owned_paths_covers_properties_and_required() -> None:
    task = _contract({"summary": {"type": "string"}}, ["summary", "body"])
    assert task_owned_paths(task) == ["body", "summary"]


def test_task_owned_paths_tolerates_empty_schema() -> None:
    assert task_owned_paths({}) == []


# ---------------------------------------------------------------------------
# apply_fix_patch -- whitelist / blacklist / add-modify-only
# ---------------------------------------------------------------------------


def test_patch_fills_only_the_missing_path() -> None:
    out = apply_fix_patch(
        {"summary": "x"},
        {"summary": "CLOBBERED", "outline": "filled"},
        allowed_paths=["outline"],
        protected_paths=["summary"],
    )
    assert out["data"] == {"summary": "x", "outline": "filled"}
    assert out["applied_paths"] == ["outline"]
    assert out["rejected"] == [
        {"path": "summary", "reason": "protected"},
    ]


def test_patch_rejects_non_defect_field() -> None:
    out = apply_fix_patch(
        {"summary": "x"},
        {"outline": "filled", "notes": "extra"},
        allowed_paths=["outline"],
        protected_paths=["summary"],
    )
    assert "notes" not in out["data"]
    assert {"path": "notes", "reason": "not_allowed"} in out["rejected"]


def test_patch_blacklist_beats_whitelist() -> None:
    """A task-owned field is rejected even if it is also listed as a defect."""
    out = apply_fix_patch(
        {"body": "original"},
        {"body": "CLOBBERED"},
        allowed_paths=["body"],
        protected_paths=["body"],
    )
    assert out["data"] == {"body": "original"}
    assert out["rejected"] == [{"path": "body", "reason": "protected"}]


def test_patch_protects_whole_subtree() -> None:
    out = apply_fix_patch(
        {"metadata": {"author": "me"}, "outline": "old"},
        {"metadata": {"author": "them", "extra": 1}},
        allowed_paths=["metadata.author", "outline"],
        protected_paths=task_owned_paths(
            _contract({"metadata": {"type": "object"}}, [])
        ),
    )
    # metadata is task-declared => its ENTIRE subtree is refused, fail-closed.
    assert out["data"]["metadata"] == {"author": "me"}
    assert {"path": "metadata.author", "reason": "protected"} in out["rejected"]


def test_patch_prefix_semantics_object_fill() -> None:
    """``metadata`` authorised => the whole missing object may be filled."""
    out = apply_fix_patch(
        {},
        {"metadata": {"author": "me", "rev": 2}},
        allowed_paths=["metadata"],
        protected_paths=[],
    )
    assert out["data"] == {"metadata": {"author": "me", "rev": 2}}


def test_patch_precise_path_does_not_authorise_sibling() -> None:
    out = apply_fix_patch(
        {"metadata": {"author": "old"}},
        {"metadata": {"author": "new", "extra": "sneak"}},
        allowed_paths=["metadata.author"],
        protected_paths=[],
    )
    assert out["data"] == {"metadata": {"author": "new"}}
    assert {"path": "metadata.extra", "reason": "not_allowed"} in out["rejected"]


def test_patch_never_deletes_existing_keys() -> None:
    """Recursive merge: pre-existing siblings of a patched object survive."""
    out = apply_fix_patch(
        {"metadata": {"author": "me", "rev": 1}},
        {"metadata": {"author": "them"}},
        allowed_paths=["metadata.author"],
        protected_paths=[],
    )
    assert out["data"] == {"metadata": {"author": "them", "rev": 1}}


def test_patch_replaces_invalid_value() -> None:
    out = apply_fix_patch(
        {"outline": 123},
        {"outline": "ok"},
        allowed_paths=["outline"],
        protected_paths=[],
    )
    assert out["data"] == {"outline": "ok"}


def test_patch_index_path_is_path_level() -> None:
    out = apply_fix_patch(
        {"sections": [{"heading": "a"}, {"heading": "b"}]},
        {"sections": [{"heading": "a"}, {"heading": "fixed"}]},
        allowed_paths=["sections[1].heading"],
        protected_paths=[],
    )
    assert out["data"]["sections"][1]["heading"] == "fixed"
    assert out["applied_paths"] == ["sections[1].heading"]


def test_patch_unparseable_path_fails_closed() -> None:
    out = apply_fix_patch({}, {"outline": "filled"}, allowed_paths=[""], protected_paths=[])
    assert out["data"] == {}
    assert out["rejected"] == [{"path": "outline", "reason": "not_allowed"}]


# ---------------------------------------------------------------------------
# _apply_skill_adherence -- acceptance items 1..9
# ---------------------------------------------------------------------------


def test_fix_cannot_clobber_summary(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result,
        task,
        merged,
        [skill],
        [],
        [],
        _adapter({"outline": "filled", "summary": "CLOBBERED"}),
    )
    assert result.artifacts[0]["data"]["summary"] == "x"
    assert rep["fix_status"] == "applied"
    assert {"path": "summary", "reason": "protected"} in rep["fix_rejected_paths"]


def test_fix_cannot_clobber_body(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}, "body": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "body": "keep me"})

    rep = _apply_skill_adherence(
        result,
        task,
        merged,
        [skill],
        [],
        [],
        _adapter({"outline": "filled", "body": "CLOBBERED"}),
    )
    assert result.artifacts[0]["data"]["body"] == "keep me"
    assert {"path": "body", "reason": "protected"} in rep["fix_rejected_paths"]


def test_fix_cannot_introduce_non_defect_field(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result,
        task,
        merged,
        [skill],
        [],
        [],
        _adapter({"outline": "filled", "bonus": "nope"}),
    )
    assert "bonus" not in result.artifacts[0]["data"]
    assert {"path": "bonus", "reason": "not_allowed"} in rep["fix_rejected_paths"]


def test_fix_only_fills_missing_fields(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["missing_fields"] == ["outline"] or rep["missing_fields"] == []
    assert result.artifacts[0]["data"] == {"summary": "x", "outline": "filled"}
    assert rep["fix_applied_paths"] == ["outline"]


def test_fix_only_replaces_invalid_fields(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x", "outline": 123})  # wrong type

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "ok"})
    )
    assert rep["invalid_fields"] == ["outline"] or rep["skill_adherence_valid"] is True
    assert result.artifacts[0]["data"] == {"summary": "x", "outline": "ok"}


def test_fix_breaking_task_schema_is_rejected(monkeypatch) -> None:
    """GAP-6: a fix that satisfies skill adherence but breaks the hard contract
    must NOT be admitted -- that would be worse than no fix at all."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    # Strict task contract: no properties beyond `summary` are permitted.
    task = _contract({"summary": {"type": "string"}}, ["summary"], additionalProperties=False)
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    merged.pop("additionalProperties", None)  # merged contract still allows outline
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["fix_status"] == "failed"
    assert rep["fix_applied"] is False
    assert rep["fix_reason"] == "post_fix_contract_invalid"
    # original artifact preserved
    assert result.artifacts[0]["data"] == {"summary": "x"}


def test_fix_applied_only_when_all_three_states_hold(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["task_schema_valid"] is True
    assert rep["skill_adherence_valid"] is True
    assert rep["overall_contract_valid"] is True
    assert rep["fix_status"] == "applied"
    assert rep["fix_applied"] is True


def test_fix_failure_preserves_original_artifact(monkeypatch) -> None:
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
    assert result.artifacts[0]["data"] == {"summary": "x"}


def test_fix_skipped_when_task_schema_already_invalid(monkeypatch) -> None:
    """Acceptance #9: an already-illegal artifact must not be laundered into a
    "skill fix succeeded" record."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": 123})  # hard contract already broken

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["task_schema_valid"] is False
    assert rep["fix_status"] == "skipped"
    assert rep["fix_reason"] == "task_schema_invalid"
    assert rep["fix_attempted"] is False
    assert rep["fix_applied"] is False
    assert result.artifacts[0]["data"] == {"summary": 123}


def test_fix_only_overwrites_primary_artifact(monkeypatch) -> None:
    """D3: sibling artifacts stay byte-identical."""
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"}, {"kind": "side", "value": 7})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["fix_status"] == "applied"
    assert result.artifacts[0]["data"] == {"summary": "x", "outline": "filled"}
    assert result.artifacts[1]["data"] == {"kind": "side", "value": 7}


def test_fix_disabled_is_still_record_only(monkeypatch) -> None:
    monkeypatch.delenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", raising=False)
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    rep = _apply_skill_adherence(
        result, task, merged, [skill], [], [], _adapter({"outline": "filled"})
    )
    assert rep["fix_status"] == "disabled"
    assert rep["fix_attempted"] is False
    assert result.artifacts[0]["data"] == {"summary": "x"}


def test_fix_adapter_without_fix_output_is_unsupported(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SKILL_ADHERENCE_FIX_ENABLED", "1")
    task = _contract({"summary": {"type": "string"}}, ["summary"])
    skill = _contract({"outline": {"type": "string"}}, ["outline"])
    merged, _, _ = merge_contracts(task, [skill])
    result = _result_with({"summary": "x"})

    class _Plain:
        pass

    rep = _apply_skill_adherence(result, task, merged, [skill], [], [], _Plain())
    assert rep["fix_status"] == "unsupported"
    assert rep["fix_attempted"] is False
    assert result.artifacts[0]["data"] == {"summary": "x"}
