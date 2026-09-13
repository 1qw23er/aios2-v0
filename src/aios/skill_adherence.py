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
* **P2-e (R-1 closure)**: the skill-adherence defect set is produced by EXACTLY
  ONE authority -- ``jsonschema.Draft202012Validator``, the same engine (and the
  same version) that produces ``overall_contract_valid``. It is evaluated over
  the SKILL-DOMAIN PROJECTION of the merged contract: the skill-declared field
  names (properties + required) minus the task-authoritative conflicts, with
  every property DEFINITION taken from ``merged_schema``. Nested defects are
  reported as canonical PATH STRINGS (``metadata.author``,
  ``sections[1].heading``) inside the SAME ``missing_fields`` /
  ``invalid_fields`` lists the fix whitelist already consumes.
* The former hand-written type/required walk and its inline loop are DELETED,
  not kept as a fallback: two defect-producing paths were the structural duality
  (RR-1) that this module converges away. A task-authoritative (conflicting)
  field is excluded from the skill domain ENTIRELY -- the task schema / overall
  path owns it. ``format`` is excluded (the engine does not check it by default,
  so reporting it would create a REVERSE inconsistency) and
  ``additionalProperties`` is filtered at every level (unrepairable: the fix
  path only adds/changes, never deletes unknown keys).
* The optional 1x directed fix lives in ``execute_task`` (it needs the adapter);
  this module only supplies the merge + the pure checker.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Bump when the semantics of the report change in a way consumers must detect.
# "2" encodes the P2-b fix traceability sub-structure (fix.before/attempt/after
# + lineage hashes). "3" encodes P2-c recursive nested-defect detection. "4"
# encodes P2-d (R-1): the skill-side defect set was converged onto the SAME
# jsonschema authority that produces ``overall_contract_valid``. "5" encodes
# P2-e: that convergence is COMPLETE -- the second (hand-written) defect path is
# deleted, the projection takes its definitions from the merged contract, a
# task-authoritative conflict is excluded from the skill domain entirely, a
# required-only contract (no ``properties``) is now checked, and the reported
# order is the engine's natural error order. The report SHAPE is unchanged
# across all five bumps.
ADHERENCE_VALIDATOR_VERSION = "5"

# Opt-in env flag (default OFF). When "1", execute_task may attempt ONE directed
# completion of missing required skill fields via the adapter.
ADHERENCE_FIX_ENV = "AIOS_SKILL_ADHERENCE_FIX_ENABLED"

# Canonical path grammar: dotted names + bracket indices, e.g. ``metadata.author``
# or ``sections[2].heading``. Used for BOTH the fix whitelist (missing/invalid
# paths) and the blacklist (task-declared fields).
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")

# fix_status values:
#   disabled     -- env flag off (default; record-only)
#   skipped      -- preconditions unsatisfied, the fix was NEVER attempted
#   unsupported  -- adapter does not implement fix_output
#   failed       -- attempted, but the patched result did not clear the gate
#   applied      -- attempted AND all three contract states are true
FIX_STATUS_DISABLED = "disabled"
FIX_STATUS_SKIPPED = "skipped"
FIX_STATUS_UNSUPPORTED = "unsupported"
FIX_STATUS_FAILED = "failed"
FIX_STATUS_APPLIED = "applied"


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


def _iter_skill_schema_defects(
    data: Any,
    projection: dict[str, Any],
) -> list[tuple[str, str]]:
    """P2-e (D1): the ONE defect-producing path for the skill domain.

    Runs the SAME ``jsonschema`` engine (``Draft202012Validator``, same version)
    that produces ``overall_contract_valid`` over the skill-domain PROJECTION of
    the merged contract, so the skill-side defect set is a field-scoped slice of
    the same authority rather than a second, hand-written validator. There is
    deliberately no fallback implementation: two defect-producing paths were the
    structural duality (RR-1) this change removes.

    Returns ``(kind, path)`` pairs in the engine's NATURAL error order
    (``properties`` depth-first, then ``required``), de-duplicated on first
    occurrence -- the ``required`` keyword yields one error per missing name,
    each carrying the full ``validator_value`` list. Classification preserves
    source semantics and is never blanket-converted:

    * ``required``             -> ``"missing"`` (path = parent + missing name)
    * ``additionalProperties`` -> filtered out (C4: unrepairable, would create a
                                  permanently-red pseudo-defect in the fix
                                  whitelist, since the fix path only adds/changes)
    * everything else          -> ``"invalid"`` (type / enum / const / minLength /
                                  pattern / minimum / ...)
    * root-level / pathless error -> dropped (record-only; it can never enter the
                                  fix whitelist)

    Registered residual (R-3, deliberately NOT changed here): any malformed
    schema or validator failure yields ``[]``, because ``compute_adherence`` must
    stay total. That silent-blind-spot surface is not widened by this change --
    the projection is still built from the same narrow merged key set.
    """
    from jsonschema.validators import Draft202012Validator as _Validator

    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    try:
        for error in _Validator(projection).iter_errors(data):
            validator = error.validator
            if validator == "additionalProperties":
                continue
            ap = tuple(error.absolute_path)
            if validator == "required":
                instance = error.instance
                if not isinstance(instance, dict):
                    continue
                for name in error.validator_value or []:
                    if name not in instance:
                        item = ("missing", _format_path(ap + (name,)))
                        if item not in seen:
                            seen.add(item)
                            out.append(item)
            elif ap:
                # type / enum / const / minLength / pattern / minimum / ...
                item = ("invalid", _format_path(ap))
                if item not in seen:
                    seen.add(item)
                    out.append(item)
            # ap empty and not required -> record-only, never enters a list
    except Exception:
        return []
    return out


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

    # 2) skill adherence -- the skill domain is checked by the ONE jsonschema
    #    authority (P2-e / D1). There is no hand-written fallback left.
    #
    #    skill domain (D2):
    #      names       = skill-declared property names + skill-declared required
    #                    names (the report field ``required_fields`` is the latter)
    #      enforceable = names \ conflicts   (D3a/D3b: a task-authoritative field
    #                    is owned by the task schema / overall path, so it is not
    #                    part of the skill domain AT ALL)
    #      projection  = {"type": "object",
    #                     "properties": {k: merged["properties"][k]
    #                                    | k in enforceable},
    #                     "required":   [r | r in merged["required"]
    #                                    and r in enforceable]}
    #
    #    The definitions come from ``merged_schema`` -- the same object whose
    #    validation produces ``overall_contract_valid`` -- so both booleans read
    #    the SAME property semantics.
    required_fields: list[str] = []
    declared_names: set[str] = set()
    for sc in skill_contracts:
        if not sc:
            continue
        for name in sc.get("properties") or {}:
            if isinstance(name, str):
                declared_names.add(name)
        for r in sc.get("required") or []:
            if not isinstance(r, str):
                continue
            declared_names.add(r)
            if r not in required_fields:
                required_fields.append(r)
    enforceable = declared_names - set(conflicts or [])
    projection: dict[str, Any] = {
        "type": "object",
        "properties": {
            k: copy.deepcopy(v)
            for k, v in (merged_schema.get("properties") or {}).items()
            if k in enforceable
        },
        "required": [r for r in (merged_schema.get("required") or []) if r in enforceable],
    }
    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    for kind, path in _iter_skill_schema_defects(data, projection):
        if kind == "missing":
            if path not in missing_fields:
                missing_fields.append(path)
        elif path not in invalid_fields:
            invalid_fields.append(path)

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
        "fix_status": FIX_STATUS_DISABLED,
        # P2-a (D1/D2): structured, non-value-bearing provenance for the fix
        # path. Never carries the model's candidate payload -- only paths.
        "fix_reason": None,
        "fix_rejected_paths": [],
        "fix_applied_paths": [],
    }


def _parse_path(path: Any) -> tuple[Any, ...]:
    """``"sections[2].heading"`` -> ``("sections", 2, "heading")``.

    Unparseable input yields ``()``, which never matches anything -- i.e. an
    unknown path fails CLOSED rather than being silently authorised.
    """
    if not isinstance(path, str) or not path.strip():
        return ()
    parts: list[Any] = []
    for m in _PATH_TOKEN.finditer(path):
        name, idx = m.group(1), m.group(2)
        if name is not None:
            parts.append(name.strip())
        elif idx is not None:
            parts.append(int(idx))
    return tuple(parts)


def _format_path(parts: tuple[Any, ...]) -> str:
    out: list[str] = []
    for p in parts:
        if isinstance(p, int):
            out.append(f"[{p}]")
        elif out:
            out.append(f".{p}")
        else:
            out.append(str(p))
    return "".join(out)


def _matches(candidates: list[tuple[Any, ...]], path: tuple[Any, ...]) -> bool:
    """True if any candidate path is ``path`` itself or one of its ancestors.

    Prefix semantics is what makes the mechanism forward-compatible: a
    whitelist entry of ``outline`` authorises ``outline[1].point`` (a whole
    missing object must be fillable), while a precise entry of
    ``metadata.author`` does NOT authorise ``metadata.extra``.
    """
    for c in candidates:
        if not c or len(c) > len(path):
            continue
        if tuple(path[: len(c)]) == c:
            return True
    return False


def task_owned_paths(task_schema: dict[str, Any]) -> list[str]:
    """Top-level names ``task.output_schema`` declares (properties + required).

    These are authoritative: the fix path may never write them, nor any of
    their subtrees.
    """
    if not isinstance(task_schema, dict):
        return []
    names = set((task_schema.get("properties") or {}).keys())
    names |= {r for r in (task_schema.get("required") or []) if isinstance(r, str)}
    return sorted(names)


def _flatten(node: Any, prefix: tuple[Any, ...], out: list[tuple[tuple[Any, ...], Any]]) -> None:
    """Flatten a candidate object into leaf ``(path, value)`` assignments.

    Flattening (instead of replacing containers) is what makes "never delete"
    structural: every accepted write targets a single leaf, so siblings that
    the model did not mention are simply left alone.
    """
    if isinstance(node, dict) and node:
        for key, value in node.items():
            _flatten(value, prefix + (key,), out)
        return
    if isinstance(node, list) and node:
        for idx, value in enumerate(node):
            _flatten(value, prefix + (idx,), out)
        return
    out.append((prefix, node))


def _ensure_child(cur: Any, seg: Any, next_is_index: bool) -> Any:
    """Return the container at ``seg``, creating it if needed. ``None`` = blocked."""
    if isinstance(seg, int):
        if not isinstance(cur, list):
            return None
        while len(cur) <= seg:
            cur.append(None)
        if not isinstance(cur[seg], (dict, list)):
            cur[seg] = [] if next_is_index else {}
        return cur[seg]
    if not isinstance(cur, dict):
        return None
    if not isinstance(cur.get(seg), (dict, list)):
        cur[seg] = [] if next_is_index else {}
    return cur[seg]


def _set_path(root: dict[str, Any], path: tuple[Any, ...], value: Any) -> bool:
    """Write ``value`` at ``path``. Returns False when the path is unreachable."""
    if not path:
        return False
    cur: Any = root
    for i, seg in enumerate(path[:-1]):
        cur = _ensure_child(cur, seg, isinstance(path[i + 1], int))
        if cur is None:
            return False
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(cur, list):
            return False
        while len(cur) <= last:
            cur.append(None)
        cur[last] = value
        return True
    if not isinstance(cur, dict):
        return False
    cur[last] = value
    return True


def apply_fix_patch(
    original: dict[str, Any],
    patch: dict[str, Any],
    *,
    allowed_paths: list[str],
    protected_paths: list[str],
) -> dict[str, Any]:
    """Treat a directed-completion result as a PATCH, not as an object merge.

    Guarantees (P2-a INV-1..INV-4):

    * **Whitelist** -- only paths under ``allowed_paths`` (the recorded
      ``missing_fields``/``invalid_fields``) may be written, matched at PATH
      level with prefix semantics: ``metadata`` authorises ``metadata.author``,
      but ``metadata.author`` does NOT authorise ``metadata.extra``. Array
      indices are first-class (``sections[1].heading``).
    * **Blacklist priority** -- ``protected_paths`` (task-declared fields) are
      rejected together with their ENTIRE subtree, even if they somehow also
      appear in the whitelist. Checked before the whitelist, so a hit is
      fail-closed.
    * **Add or modify only, never delete** -- the patch is flattened into leaf
      writes, so pre-existing siblings / array elements survive.
    * **Value-free provenance** -- returns rejected paths + reason, never the
      rejected payload.

    Returns ``{"data", "applied_paths", "rejected"}`` where ``rejected`` is a
    list of ``{"path", "reason"}`` with ``reason`` in ``{"protected",
    "not_allowed", "unreachable"}``.
    """
    allowed = [_parse_path(p) for p in allowed_paths]
    protected = [_parse_path(p) for p in protected_paths]
    data = copy.deepcopy(original) if isinstance(original, dict) else {}
    applied: list[str] = []
    rejected: list[dict[str, str]] = []

    leaves: list[tuple[tuple[Any, ...], Any]] = []
    _flatten(patch if isinstance(patch, dict) else {}, (), leaves)
    for path, value in leaves:
        if not path:
            continue
        if _matches(protected, path):
            rejected.append({"path": _format_path(path), "reason": "protected"})
            continue
        if not _matches(allowed, path):
            rejected.append({"path": _format_path(path), "reason": "not_allowed"})
            continue
        if _set_path(data, path, copy.deepcopy(value)):
            applied.append(_format_path(path))
        else:
            rejected.append({"path": _format_path(path), "reason": "unreachable"})
    return {"data": data, "applied_paths": applied, "rejected": rejected}
