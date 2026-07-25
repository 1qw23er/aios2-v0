#!/usr/bin/env python3
"""
Validate the REAL LLMExecutionAdapter success path against NVIDIA's
deepseek-v4-pro on a machine with direct NVIDIA reachability.

This reuses the production adapter code in aios.execution -- no mocks.
It proves: prompt -> real model call -> JSON parse -> ExecutionResult
with a real artifact, using YOUR NVIDIA_API_KEY.

Usage (on your directly-connected machine):
    export NVIDIA_API_KEY="nvapi-..."
    python scripts/validate_nvidia_success_path.py

Optional overrides (env):
    AIOS_AGENT_MODEL     (default deepseek-ai/deepseek-v4-pro)
    AIOS_AGENT_BASE_URL  (default https://integrate.api.nvidia.com/v1)
"""
from __future__ import annotations

import json
import os
import sys

# Import the REAL package from this repo (src layout)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(REPO_ROOT, "src"), REPO_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from aios.execution import ExecutionError, LLMExecutionAdapter  # noqa: E402


def main() -> int:
    raw = os.getenv("NVIDIA_API_KEY") or os.getenv("AIOS_AGENT_API_KEY")
    if not raw:
        print(
            "Set NVIDIA_API_KEY (or AIOS_AGENT_API_KEY) first, e.g.:\n"
            '  set NVIDIA_API_KEY=nvapi-...   (Windows CMD, no quotes)',
            file=sys.stderr,
        )
        return 2

    # --- key sanity check: fail fast instead of bubbling up as adapter error ---
    key = raw.strip()
    if key != raw:
        print("NOTE: stripped leading/trailing whitespace from NVIDIA_API_KEY")
    nonascii = [(i, hex(ord(c))) for i, c in enumerate(key) if ord(c) > 127]
    if nonascii:
        print(
            "ERROR: NVIDIA_API_KEY contains non-ASCII characters (placeholder not "
            "replaced?):",
            file=sys.stderr,
        )
        print(f"  bad chars at: {nonascii}", file=sys.stderr)
        print(f"  repr = {key!r}", file=sys.stderr)
        print(
            "  -> paste your REAL key (nvapi-... ~70 chars, pure ASCII).",
            file=sys.stderr,
        )
        return 2
    placeholders = ("你的真实key", "你的完整key", "your", "完整", "真实", "...")
    if any(p in key for p in placeholders):
        print(
            f"ERROR: NVIDIA_API_KEY looks like a placeholder, not a real key: {key!r}",
            file=sys.stderr,
        )
        return 2
    if len(key) < 40:
        print(
            f"ERROR: NVIDIA_API_KEY too short (len={len(key)}); real keys are ~70 chars.",
            file=sys.stderr,
        )
        return 2

    # api_key passed explicitly -> the adapter reads it instead of AIOS_AGENT_API_KEY.
    adapter = LLMExecutionAdapter(api_key=key)  # base_url/model default to NVIDIA

    print(f"-> base_url : {adapter.base_url}")
    print(f"-> model    : {adapter.model}")
    print(f"-> api_key  : {key[:6]}... (len={len(key)})")

    # Minimal valid TaskContext (dict form; validated by TaskContext.model_validate).
    ctx = {
        "task_id": "tsk_validate_success",
        "project_id": "prj_validate_success",
        "objective": "Return a one-line health status as JSON.",
        "instructions": "Respond ONLY with a JSON object matching the schema.",
        "context_hash": "validate-success-001",
    }
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "status": {"type": "string", "enum": ["ok", "degraded"]},
        },
        "required": ["summary", "status"],
    }

    print("\n-> Calling NVIDIA deepseek-v4-pro (real network) ...")
    try:
        result = adapter.run(
            task_id=ctx["task_id"],
            task_context=ctx,
            output_schema=schema,
            idempotency_key="validate-success-001",
        )
    except ExecutionError as exc:
        print(
            f"Adapter raised ExecutionError: category={exc.category} detail={exc.detail}"
        )
        return 1

    print("\nSUCCESS PATH VERIFIED")
    print(f"  summary  : {result.summary}")
    print(f"  artifacts: {len(result.artifacts)}")
    for art in result.artifacts:
        print(f"    - type={art['type']} uri={art['uri']}")
        print(f"      data={json.dumps(art['data'], ensure_ascii=False)}")

    data = result.artifacts[0]["data"] if result.artifacts else {}
    if "summary" in data and "status" in data:
        print("\nartifact data matches output_schema (summary + status present)")
        return 0
    print("\nartifact data missing expected schema fields", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
