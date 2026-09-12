"""Plan A: skill output-contract merge + adherence checking (record-only).

This module is the single execution seam for "does the produced artifact obey
the skills that were in effect for this task?". It is deliberately FREE of any
side effects (no DB, no model calls) so it can be unit-tested in isolation and
called from :func:`aios.execution.execute_task`.

Design (per the agreed Plan A boundaries):

* The *final output contract* = ``task.output_schema`` UNION the
  ``required_output_contract`` of every applicable skill.
* ``task.output_schema`` stays authoritative on a field-name conflict (a skill
  may not redefine an existing task field's type); conflicts are recorded, not
  silently overridden.
* Adherence is **record-only by default**: a skill-field violation does NOT fail
  the task. The hard ``validate(instance, task.output_schema)`` in
  ``execute_task`` remains the only thing that can fail a run. The three states
  (``task_schema_valid`` / ``skill_adherence_valid`` / ``overall_contract_valid``)
  are reported into ``Artifact.metadata_json["adherence"]``.
* The optional 1x directed fix lives in ``execute_task`` (it needs the adapter);
  this module only supplies the merge + the pure checker.
"""

from __future__ import annotations

import copy
from typing import Any

# Bump when the semantics of the report change in a way consumers must detect.
ADHERENCE_VALIDATOR_VERSION = "1"

# Opt-in env flag (default OFF). When "1", execute_task may attempt ONE directed
# completion of missing required skill fields via the adapter.
ADHERENCE_FIX_ENV = "AIOS_SKILL_ADHERENCE_FIX_ENABLED"


def merge_contracts(
    task_schema: dict[str, Any],
    skill_contracts: list[dict[str, Any]],
) -> tuple[dict[str, Any], set[str], list[str]]:
    """Merge ``task.output_schema`` with applicable skills' contracts.

    Returns ``(merged_schema, skill_fields, conflicts)``.

    * ``skill_fields`` -- property names that originate from a skill contract
      (used for provenance / reporting).
    * ``conflicts`` -- task-field names a skill also declared with a *different*
      definition; the task definition wins and the conflict is recorded.
    """
    merged: dict[str, Any] = {
        "type": "object",
        "properties": dict(task_schema.get("properties") or {}),
        "required": list(task_schema.get("required") or []),
    }
    skill_fields: set[str] = set()
    conflicts: list[str] = []
    for sc in skill_contracts:
        if not sc:
            continue
        for fname, fdef in (sc.get("properties") or {}).items():
            if fname in merged["properties"]:
                if merged["properties"][fname] != fdef:
                    conflicts.append(fname)
                # Task definition is authoritative -- do NOT override.
            else:
                merged["properties"][fname] = copy.deepcopy(fdef)
                skill_fields.add(fname)
        for r in sc.get("required") or []:
            if r not in merged["required"]:
                merged["required"].append(r)
            skill_fields.add(r)
    return merged, skill_fields, conflicts


def _type_ok(value: Any, fdef: dict[str, Any]) -> bool:
    ftype = fdef.get("type")
    if ftype is None:
        return True
    if ftype == "object":
        return isinstance(value, dict)
    if ftype == "array":
        return isinstance(value, list)
    if ftype == "string":
        return isinstance(value, str)
    if ftype in ("integer", "number"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ftype == "boolean":
        return isinstance(value, bool)
    if ftype == "null":
        return value is None
    return True


def compute_adherence(
    data: dict[str, Any],
    task_schema: dict[str, Any],
    merged_schema: dict[str, Any],
    skill_contracts: list[dict[str, Any]],
    skills_meta: list[dict[str, Any]],
    *,
    conflicts: list[str] | None = None,
    validator_version: str = ADHERENCE_VALIDATOR_VERSION,
) -> dict[str, Any]:
    """Compute the three-state adherence report. Never raises.

    ``skills_meta`` is the lightweight list
    ``[{"skill_id", "name", "version"}, ...]`` captured at execution time (the
    immutable projected snapshot), so the report is self-describing.
    """
    from jsonschema import validate as _js_validate
    from jsonschema.exceptions import ValidationError as _JSValidationError

    # 1) task schema -- the hard contract (execute_task still enforces this).
    task_valid = True
    try:
        _js_validate(instance=data, schema=task_schema)
    except _JSValidationError:
        task_valid = False

    # 2) skill adherence -- every skill-required field present AND type-correct.
    required_fields: list[str] = []
    for sc in skill_contracts:
        for r in sc.get("required") or []:
            if r not in required_fields:
                required_fields.append(r)
    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    for sc in skill_contracts:
        props = sc.get("properties") or {}
        for fname, fdef in props.items():
            if fname not in (sc.get("required") or []):
                continue
            if fname not in data:
                if fname not in missing_fields:
                    missing_fields.append(fname)
            elif not _type_ok(data.get(fname), fdef) and fname not in invalid_fields:
                invalid_fields.append(fname)
    skill_valid = not missing_fields and not invalid_fields

    # 3) overall -- merged contract validity (task + skill fields).
    overall_valid = True
    try:
        _js_validate(instance=data, schema=merged_schema)
    except _JSValidationError:
        overall_valid = False

    return {
        "validator_version": validator_version,
        "skills": skills_meta,
        "task_schema_valid": task_valid,
        "skill_adherence_valid": skill_valid,
        "overall_contract_valid": overall_valid,
        "required_fields": required_fields,
        "missing_fields": missing_fields,
        "invalid_fields": invalid_fields,
        "conflicts": list(conflicts or []),
        "fix_attempted": False,
        "fix_applied": False,
        "fix_status": "disabled",
    }
