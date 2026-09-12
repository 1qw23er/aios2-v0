from __future__ import annotations

import json
from typing import Any

from aios.models import TaskContext

# Framing for the ``## Applicable Skills`` block. Skills carry executable
# procedure, so the text must present them as task guidance the agent may follow
# while working -- never as a system-level instruction that could be read as
# outranking the objective, the acceptance criteria or the policy set.
_SKILLS_INTRO = (
    "The following approved skills are execution guidance for this task. They "
    "describe how the work may be carried out and do not override the objective, "
    "acceptance criteria, policies, budgets, or approvals above."
)


def task_context_payload(context: TaskContext | dict[str, Any]) -> dict[str, Any]:
    if isinstance(context, TaskContext):
        return context.model_dump(mode="json")
    return TaskContext.model_validate(context).model_dump(mode="json")


def render_task_context_markdown(context: TaskContext | dict[str, Any]) -> str:
    payload = task_context_payload(context)
    lines = [
        "# Task Context",
        "",
        f"- Context ID: {payload['id']}",
        f"- Context Hash: {payload['context_hash']}",
        f"- Task ID: {payload['task_id']}",
        f"- Project ID: {payload['project_id']}",
        f"- Assigned Agent ID: {payload.get('assigned_agent_id') or 'unassigned'}",
        "",
        "## Objective",
        "",
        payload["objective"],
        "",
        "## Instructions",
        "",
        payload["instructions"],
        "",
        "## Acceptance Criteria",
        "",
    ]
    lines.extend(f"- {item}" for item in payload["acceptance_criteria"])
    _json_section(lines, "Project Context", payload["project_context"])
    _json_section(lines, "Dependency Outputs", payload["dependency_outputs"])
    _json_section(lines, "Approved Facts", payload["approved_facts"])
    _json_section(lines, "Relevant Decisions", payload["relevant_decisions"])
    _json_section(lines, "Applicable Policies", payload["applicable_policies"])
    _skills_section(lines, payload.get("applicable_skills") or [])
    _json_section(lines, "Agent Profile", payload["agent_profile"])
    _json_section(lines, "Source References", payload["source_references"])
    return "\n".join(lines).rstrip() + "\n"


def _json_section(lines: list[str], title: str, value: Any) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "```json",
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )


def _skills_section(lines: list[str], skills: list[dict[str, Any]]) -> None:
    """Render the already-projected ``applicable_skills`` snapshot (Skill V1).

    The list is exactly what ``ContextService._select_skills`` selected, gated
    and persisted on this TaskContext. This renderer therefore never queries the
    database, never re-selects, never re-orders and never expands the scope
    beyond the snapshot -- anything absent from the snapshot is absent from the
    prompt. An empty snapshot emits nothing, so contexts without skills render
    byte-for-byte as before.
    """
    if not skills:
        return
    lines.extend(["", "## Applicable Skills", "", _SKILLS_INTRO])
    for skill in skills:
        if isinstance(skill, dict):
            _skill_entry(lines, skill)


def _skill_entry(lines: list[str], skill: dict[str, Any]) -> None:
    name = skill.get("name")
    if not isinstance(name, str) or not name.strip():
        # Never fabricate an identity: a nameless entry is skipped entirely.
        return
    lines.extend(["", f"### Skill: {name}"])
    _bullet(lines, "Description", skill.get("description"))
    _bullet(lines, "Capability", _capability_label(skill))
    _bullet(lines, "Version", skill.get("version"))
    _bullet(lines, "Scope", skill.get("scope"))
    _bullet(lines, "Execution strategy", skill.get("execution_strategy"))
    _steps_block(lines, skill.get("steps"))
    _tool_bindings_block(lines, skill.get("tool_bindings"))
    _provenance_block(lines, skill)


def _steps_block(lines: list[str], steps: Any) -> None:
    if not isinstance(steps, list):
        return
    rendered = [text for text in (_step_text(step) for step in steps) if text]
    if not rendered:
        return
    lines.extend(["", "#### Steps", ""])
    lines.extend(f"{index}. {text}" for index, text in enumerate(rendered, start=1))


def _step_text(step: Any) -> str:
    if isinstance(step, dict):
        return "; ".join(f"{key}: {_scalar(step[key])}" for key in sorted(step))
    return _scalar(step)


def _tool_bindings_block(lines: list[str], bindings: Any) -> None:
    if not isinstance(bindings, dict) or not bindings:
        return
    lines.extend(
        [
            "",
            "#### Tool bindings",
            "",
            "Declared context only: a binding records an intended tool for the "
            "procedure and does not invoke, grant or authorise any tool.",
            "",
        ]
    )
    lines.extend(f"- {key}: {_scalar(bindings[key])}" for key in sorted(bindings))


def _provenance_block(lines: list[str], skill: dict[str, Any]) -> None:
    entries = [
        ("Skill ID", skill.get("skill_id")),
        ("Content hash", skill.get("content_hash")),
    ]
    entries = [(label, value) for label, value in entries if _present(value)]
    if not entries:
        return
    lines.extend(["", "#### Provenance", ""])
    for label, value in entries:
        _bullet(lines, label, value)


def _capability_label(skill: dict[str, Any]) -> str | None:
    name = skill.get("capability_name")
    capability_id = skill.get("capability_id")
    if name and capability_id:
        return f"{name} ({capability_id})"
    return name or capability_id


def _bullet(lines: list[str], label: str, value: Any) -> None:
    if not _present(value):
        return
    lines.append(f"- {label}: {_scalar(value)}")


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple)):
        return bool(value)
    return True


def _scalar(value: Any) -> str:
    # Strings render verbatim; everything else is rendered as canonical JSON so
    # nested values stay stable and never leak Python's ``repr`` into the prompt.
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
