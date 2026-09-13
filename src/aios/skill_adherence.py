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
import re
from typing import Any

# Bump when the semantics of the report change in a way consumers must detect.
# "2" encodes the P2-b fix traceability sub-structure (fix.before/attempt/after
# + lineage hashes); top-level semantics are unchanged so consumers are compat.
ADHERENCE_VALIDATOR_VERSION = "2"

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
