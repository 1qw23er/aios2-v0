import json
from pathlib import Path

from aios.adapters.external import ExternalWorkstationAdapter, TaskPacket
from aios.models import TaskContext


def packet(task_id: str) -> TaskPacket:
    return TaskPacket(
        task_id=task_id,
        project={"id": "prj_1", "objective": "Publish"},
        role="writer",
        instructions="Write",
        acceptance_criteria=["Safe"],
        output_schema={"type": "object"},
    )


def test_legacy_external_export_is_unchanged(tmp_path: Path) -> None:
    adapter = ExternalWorkstationAdapter(tmp_path / "outbox", tmp_path / "inbox")

    exported = adapter.export_task(packet("tsk_legacy"), "# Legacy context")

    assert exported.packet_path.is_file()
    assert exported.context_path.read_text(encoding="utf-8") == "# Legacy context"
    assert not (exported.packet_path.parent / "task_context.json").exists()


def test_external_export_includes_structured_and_readable_context(tmp_path: Path) -> None:
    adapter = ExternalWorkstationAdapter(tmp_path / "outbox", tmp_path / "inbox")
    context = TaskContext(
        id="ctx_1",
        task_id="tsk_context",
        project_id="prj_1",
        assigned_agent_id="agt_1",
        objective="Publish safely",
        instructions="Write the article",
        acceptance_criteria=["Cited", "Safe"],
        project_context={"project_name": "Campaign"},
        dependency_outputs=[{"artifact_id": "art_1", "summary": "Approved evidence"}],
        approved_facts=[
            {
                "fact_id": "fact_1",
                "statement": "Reviewed fact",
                "source_artifact_id": "art_1",
            }
        ],
        relevant_decisions=[{"title": "Review", "content": "Human review"}],
        applicable_policies=[{"name": "Safety", "content": "Protect secrets"}],
        agent_profile={"name": "Writer", "limitations": ["no_publish"]},
        source_references=[
            {
                "resource_type": "artifact",
                "resource_id": "art_1",
                "version": "checksum",
                "inclusion_reason": "completed_dependency_output",
            }
        ],
        context_hash="c" * 64,
    )

    exported = adapter.export_task(
        packet(context.task_id),
        "ignored legacy text",
        task_context=context,
    )

    assert exported.context_json_path is not None
    payload = json.loads(exported.context_json_path.read_text(encoding="utf-8"))
    assert payload["id"] == context.id
    assert payload["context_hash"] == context.context_hash
    markdown = exported.context_path.read_text(encoding="utf-8")
    assert "# Task Context" in markdown
    assert "Publish safely" in markdown
    assert "Reviewed fact" in markdown
    assert "Protect secrets" in markdown
    assert "art_1" in markdown
    task_packet = json.loads(exported.packet_path.read_text(encoding="utf-8"))
    assert "task_context" not in task_packet
