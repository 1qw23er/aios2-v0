from __future__ import annotations

import json
from typing import Any

from aios.models import TaskContext


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
