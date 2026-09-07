#!/usr/bin/env python3
"""Push a WorkBuddy content draft (A-E format) into AIOS (#108-A).

Gap-list item #1: the "WorkBuddy research + first draft" agent produces content
in the A-E format (选题 / 事实素材 / 文章结构 / 初稿 / 配图需求) but has no
mechanism to land it in AIOS. This script is the thin adapter: it calls
``ContentDraftService.create_content_draft`` so the draft becomes a real
``Artifact(type=CONTENT_DRAFT)`` with a frozen checksum, revision binder, and
audit record -- exactly what the owner-inbox / GPT editor chain expects next.

Design constraints (consistent with the 2026-08-24 content-workflow spec):
* ZERO migration -- reuses ``ArtifactType.CONTENT_DRAFT`` and existing services.
* Identity is SERVER-DERIVED: the actor is ``resolve_agent_actor("workbuddy",
  project_id)``. The script never invents an owner/agent identity from input.
* This script only CREATES the draft. The GPT editor later calls
  ``update_content_draft`` (new revision) + ``submit_content_draft``. This script
  NEVER approves or publishes -- external publish stays a human action.
* Idempotent via ``idempotency_key`` (UNIQUE on AuditLog; Artifact also carries
  it). Re-running with the same key is a safe no-op at the audit layer.

Usage:
    python scripts/wb_draft_to_aios.py \
        --project-id <project> \
        --topic "..." \
        --file draft.md \
        [--series-id "黎叔AI创业实验室"] [--phase idea] [--task-id X] \
        [--idempotency-key K] [--json]

Or pipe the body on stdin:
    cat draft.md | python scripts/wb_draft_to_aios.py --project-id P --topic T --body -
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aios.actor import resolve_agent_actor
from aios.agent_registry import get_agent
from aios.attribution import build_aimi_signup_url
from aios.content_draft import DEFAULT_SERIES_ID, ContentDraftService
from aios.db import make_session
from aios.known_agents import WORKBUDDY_AGENT_ID


def _parse_ae_sections(text: str) -> dict[str, str]:
    """Best-effort extraction of A-E section markers from a draft file.

    Sections are headed by ``### A.`` / ``### B.`` ... or ``## A.`` etc. The
    full body is always stored verbatim as the draft ``body``; section parsing
    only enriches ``topic``/``outline`` when markers are present. Missing
    markers degrade gracefully (no error).
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        # Match "### A. 选题" / "## C. 文章结构" / "# E. 配图需求"
        if len(stripped) >= 2 and stripped[0] in "#" and ". " in stripped[1:8]:
            head = stripped.lstrip("#").strip()
            key = head[0].upper()  # A/B/C/D/E
            if key in "ABCDE":
                current = key
                sections.setdefault(key, [])
                continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _build_outline(sections: dict[str, str]) -> list[str] | None:
    """Derive a bullet outline from section C (文章结构) when present."""
    c = sections.get("C")
    if not c:
        return None
    bullets = [
        ln.lstrip("-*• ").strip()
        for ln in c.splitlines()
        if ln.strip().startswith(("-", "*", "•", "·"))
    ]
    return bullets or None


def _build_conversion_anchors(sections: dict[str, str]) -> list[dict[str, str]] | None:
    """Extract CTA lines from section C/D as conversion anchors (best-effort)."""
    text = sections.get("C", "") + "\n" + sections.get("D", "")
    anchors: list[dict[str, str]] = []
    for ln in text.splitlines():
        s = ln.strip().lstrip("-*• ").strip()
        if s.startswith("CTA") or "行动号召" in s or "关注" in s or "注册" in s:
            anchors.append({"anchor": s, "kind": "cta"})
    return anchors or None


def create_draft(
    *,
    project_id: str,
    topic: str,
    body: str,
    series_id: str = DEFAULT_SERIES_ID,
    phase: str = "idea",
    task_id: str | None = None,
    idempotency_key: str | None = None,
    attribution_key: str | None = None,
) -> dict[str, str]:
    """Create a CONTENT_DRAFT artifact as the WorkBuddy agent. Returns a summary.

    The returned dict also includes the per-article ``attribution_key`` (minted at
    draft creation when not supplied) and the trackable ``aimi_signup_url`` CTA
    payload -- the single piece of data the published article must carry so the
    "publish -> signup" loop (gap #2) becomes measurable.
    """
    sections = _parse_ae_sections(body)
    outline = _build_outline(sections)
    anchors = _build_conversion_anchors(sections)

    session = make_session()
    try:
        svc = ContentDraftService(session)
        # Registry-validated identity (fail-closed): confirm WORKBUDDY_AGENT_ID
        # ("workbuddy") exists in the Agent registry before minting an agent
        # actor. This upgrades the previously free-text identity into a
        # registry-backed one; get_agent raises ServiceError(404) if the row is
        # absent, so an unseeded or tampered registry refuses to produce a draft.
        # The LOCAL CLI trust boundary still holds -- only the operator running
        # this script reaches this path, never an external / gateway request.
        get_agent(session, WORKBUDDY_AGENT_ID)
        actor = resolve_agent_actor(WORKBUDDY_AGENT_ID, project_id)
        artifact = svc.create_content_draft(
            project_id=project_id,
            actor=actor,
            topic=topic,
            body=body,
            phase=phase,
            outline=outline,
            conversion_anchors=anchors,
            series_id=series_id,
            task_id=task_id,
            idempotency_key=idempotency_key,
            attribution_key=attribution_key,
        )
        key = (artifact.metadata_json or {}).get("attribution_key", "")
        return {
            "artifact_id": artifact.id,
            "revision_count": str(artifact.revision_count),
            "checksum": artifact.checksum,
            "review_status": str(artifact.review_status),
            "type": str(artifact.type),
            "producer": (artifact.metadata_json or {}).get("producer", ""),
            "attribution_key": key,
            "aimi_signup_url": build_aimi_signup_url(key) if key else "",
        }
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True, help="AIOS project id")
    parser.add_argument("--topic", required=True, help="Draft topic (一行标题/主题)")
    parser.add_argument(
        "--file",
        default=None,
        help="Path to an A-E draft file. Use '-' with --body - to read stdin instead.",
    )
    parser.add_argument(
        "--body",
        default=None,
        help="Inline body. Use '-' to read from stdin.",
    )
    parser.add_argument("--series-id", default=DEFAULT_SERIES_ID)
    parser.add_argument("--phase", default="idea")
    parser.add_argument("--task-id", default=None)
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="Idempotency key; re-running with the same key is a safe no-op.",
    )
    parser.add_argument(
        "--attribution-key",
        default=None,
        help="Per-article attribution key for the Aimi lead-gen loop. Omit to "
        "auto-mint one (recommended; the key is frozen into the draft metadata).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    # Resolve body source: --file, --body -, or --body <text>
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    elif args.body == "-":
        body = sys.stdin.read()
    elif args.body:
        body = args.body
    else:
        parser.error("one of --file or --body (or --body -) is required")

    result = create_draft(
        project_id=args.project_id,
        topic=args.topic,
        body=body,
        series_id=args.series_id,
        phase=args.phase,
        task_id=args.task_id,
        idempotency_key=args.idempotency_key,
        attribution_key=args.attribution_key,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Draft created in AIOS:")
        print(f"  artifact_id    = {result['artifact_id']}")
        print(f"  revision_count = {result['revision_count']}")
        print(f"  checksum       = {result['checksum']}")
        print(f"  review_status  = {result['review_status']}")
        print(f"  producer       = {result['producer']}")
        print(f"  type           = {result['type']}")
        print(f"  attribution_key= {result['attribution_key']}")
        print(f"  aimi_signup_url= {result['aimi_signup_url']}")
        print("\nNext: GPT editor calls update_content_draft (new revision)")
        print("       then submit_content_draft for independent review.")
        print("Embed aimi_signup_url in the published article to close the loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
