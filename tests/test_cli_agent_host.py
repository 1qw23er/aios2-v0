"""Unit tests for ``scripts/cli_agent_host.py`` (CLI agent host).

The host is the piece that lets a **closed CLI agent** — one with no HTTP API —
be delegated to over the WORKSTATION file protocol. It reuses
``scripts/workstation_runner``'s packet/result contract and only swaps the
executor: instead of POSTing to an LLM endpoint, it drives a local agent CLI.

These tests cover the host's pure decision logic only (argv construction,
environment hardening, output parsing). They deliberately never spawn a real
CLI, so they stay fast and hermetic in CI.

Three behaviours are pinned because each one was a real, hard-won failure:

* ``SERVER__PORT`` must be stripped from the child environment — the WorkBuddy
  app injects it, and the codebuddy CLI then tries to bind the same port and
  hangs forever with zero output.
* ``{JSON_SCHEMA}`` must be substituted per task, otherwise every platform
  receives the literal placeholder instead of the task's output schema.
* Structured output must be read from ``structured_output`` when present: with
  ``--tools StructuredOutput`` the CLI puts the schema-conformant object there
  and leaves ``result`` empty, so text-only parsing would fail.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts.cli_agent_host import (
    JSON_SCHEMA_TOKEN,
    _child_env,
    argv_for_platform,
    extract_result_text,
    extract_structured_output,
    parse_json_object,
    render_prompt,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}


def test_argv_substitutes_the_json_schema_placeholder() -> None:
    for platform in ("workbuddy", "claude_code"):
        argv = argv_for_platform(platform, json_schema=SCHEMA)
        assert JSON_SCHEMA_TOKEN not in " ".join(argv), (
            f"{platform}: the schema placeholder must be replaced per task"
        )
        assert any(json.loads(a) == SCHEMA for a in argv if a.startswith("{")), (
            f"{platform}: expected the compact schema JSON in argv"
        )


def test_argv_includes_json_schema_flag() -> None:
    argv = argv_for_platform("claude_code", json_schema=SCHEMA)
    assert "--json-schema" in argv


def test_workbuddy_argv_keeps_structured_output_tool() -> None:
    """``--tools ""`` disables the StructuredOutput tool the schema relies on."""
    argv = argv_for_platform("workbuddy", json_schema=SCHEMA)
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == "StructuredOutput"


def test_argv_override_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AIOS_CLI_CLAUDE_CODE_ARGV", json.dumps(["/bin/echo", JSON_SCHEMA_TOKEN])
    )
    argv = argv_for_platform("claude_code", json_schema=SCHEMA)
    assert argv[0] == "/bin/echo"
    assert json.loads(argv[1]) == SCHEMA


def test_argv_override_rejects_non_string_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_CLI_CLAUDE_CODE_ARGV", json.dumps([1, 2]))
    with pytest.raises(RuntimeError, match="JSON array of strings"):
        argv_for_platform("claude_code", json_schema=SCHEMA)


def test_unknown_platform_has_no_builtin_argv() -> None:
    with pytest.raises(RuntimeError, match="no built-in CLI"):
        argv_for_platform("definitely_not_a_platform")


def test_child_env_drops_server_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single most important hardening: otherwise the CLI hangs forever."""
    monkeypatch.setenv("SERVER__PORT", "16890")
    assert "SERVER__PORT" not in _child_env()


def test_extract_structured_output_prefers_the_structured_field() -> None:
    """codebuddy: a JSON array whose result element carries `structured_output`."""
    payload = [
        {"type": "assistant", "content": "thinking"},
        {
            "type": "result",
            "result": "",  # empty by design when structured output is used
            "structured_output": {"title": "t"},
            "is_error": False,
        },
    ]
    assert extract_structured_output(json.dumps(payload)) == {"title": "t"}


def test_extract_structured_output_accepts_encoded_string() -> None:
    payload = [{"type": "result", "result": "", "structured_output": '{"title": "t"}'}]
    assert extract_structured_output(json.dumps(payload)) == {"title": "t"}


def test_extract_structured_output_returns_none_when_absent() -> None:
    payload = {"type": "result", "result": '{"title": "t"}', "is_error": False}
    assert extract_structured_output(json.dumps(payload)) is None


def test_extract_result_text_reads_claude_shape() -> None:
    payload = {"type": "result", "result": '{"title": "t"}', "is_error": False}
    assert extract_result_text(json.dumps(payload)) == '{"title": "t"}'


def test_extract_result_text_rejects_empty_result() -> None:
    payload = {"type": "result", "result": "", "is_error": False}
    with pytest.raises(RuntimeError, match="empty result"):
        extract_result_text(json.dumps(payload))


def test_extract_raises_when_cli_reports_error() -> None:
    payload = {"type": "result", "result": "boom", "is_error": True}
    with pytest.raises(RuntimeError, match="CLI reported an error"):
        extract_result_text(json.dumps(payload))


def test_extract_raises_on_non_json_output() -> None:
    with pytest.raises(RuntimeError, match="not JSON"):
        extract_result_text("not json at all")


def test_parse_json_object_tolerates_fence_and_prose() -> None:
    text = 'Sure! Here you go:\n```json\n{"title": "t"}\n```\nHope that helps.'
    assert parse_json_object(text) == {"title": "t"}


def test_parse_json_object_tolerates_double_encoding() -> None:
    assert parse_json_object('"{\\"title\\": \\"t\\"}"') == {"title": "t"}


def test_parse_json_object_scans_balanced_braces() -> None:
    assert parse_json_object('noise {"title": "t"} trailing') == {"title": "t"}


def test_parse_json_object_raises_when_no_object() -> None:
    with pytest.raises(RuntimeError, match="no JSON object"):
        parse_json_object("I cannot produce JSON today")


def test_render_prompt_includes_schema_and_instructions() -> None:
    from scripts.workstation_runner import TaskPacket

    packet = TaskPacket(
        task_id="t1",
        project={"id": "p"},
        role="copywriter",
        instructions="write it",
        inputs=[{"k": "v"}],
        acceptance_criteria=["must be Chinese"],
        output_schema=SCHEMA,
    )
    prompt = render_prompt(task=packet, context="ctx", output_schema=SCHEMA)
    assert "write it" in prompt
    assert "must be Chinese" in prompt
    # schema is rendered with indent=2, so assert on content not exact spacing
    assert '"required"' in prompt and '"title"' in prompt
    assert "copywriter" in prompt
