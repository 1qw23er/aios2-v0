"""Stable, semantically-named internal agents for aios-v0 (Gap #3: real-agent-identity).

The local CLI tooling (e.g. ``scripts/wb_draft_to_aios.py``) previously minted a
free-text ``"workbuddy"`` actor with no registry backing. This module gives those
identities *real* ``Agent`` rows so the identity becomes **registry-validated**
(fail-closed via ``agent_registry.get_agent`` -> 404 if absent).

Two stable agents are defined, matching the 2026-08-24 content-workflow spec:

* ``workbuddy`` -- 蟹将: 业务研究员 + 初稿执行 Agent (research / structure / draft).
* ``gpt``       -- 总编 + 视觉总监 (editorial restructure / de-AI tone / visuals).

Design constraints (consistent with the rest of the registry):
* ZERO migration -- only inserts ``agent`` rows at runtime via ``seed_known_agents``.
* Both agents are INTERNAL trust + CLI adapter (local, in-process). Per the
  registration API rule, local adapters MUST omit ``delegation_mode`` (None);
  only external adapters (API/EXTERNAL) specify a delegation mode.
* Seeding is idempotent: re-running never duplicates rows or fails.
"""
from __future__ import annotations

from typing import Any

from aios.models import AdapterType, Agent, AgentTrustLevel

# Semantic, stable agent ids (used as the Agent.id primary key).
WORKBUDDY_AGENT_ID = "workbuddy"  # 蟹将: 业务研究员 + 初稿执行 Agent
GPT_AGENT_ID = "gpt"  # 总编 + 视觉总监

KNOWN_AGENTS: dict[str, dict[str, Any]] = {
    WORKBUDDY_AGENT_ID: {
        "name": "WorkBuddy (蟹将)",
        "role": "业务研究员 + 初稿执行 Agent",
        "adapter_type": AdapterType.CLI,
        "delegation_mode": None,
        "trust_level": AgentTrustLevel.INTERNAL,
        "capabilities": [
            "topic_research",
            "content_structuring",
            "draft_writing",
            "fact_gathering",
        ],
        "limitations": [
            "不追求最终成稿文学性",
            "不强行做最终视觉创作",
        ],
        "permissions": ["content_draft:create"],
        "platform": "workbuddy",
    },
    GPT_AGENT_ID: {
        "name": "GPT (总编)",
        "role": "内容总编 + 视觉总监",
        "adapter_type": AdapterType.CLI,
        "delegation_mode": None,
        "trust_level": AgentTrustLevel.INTERNAL,
        "capabilities": [
            "editorial_restructure",
            "headline_optimization",
            "de_ai_tone",
            "final_visual_generation",
        ],
        "limitations": [
            "依赖 WorkBuddy 提供的初稿与事实素材",
        ],
        "permissions": ["content_draft:edit"],
        "platform": "gpt",
    },
}


def seed_known_agents(session: Any) -> dict[str, str]:
    """Idempotently insert KNOWN_AGENTS into the Agent registry.

    Returns a mapping ``{agent_id: "inserted" | "already_present"}`` so callers
    (and the seed script) can report what happened. Existing rows are left
    untouched -- this never overwrites a row an operator may have tuned by hand.
    """
    status: dict[str, str] = {}
    for agent_id, spec in KNOWN_AGENTS.items():
        existing = session.get(Agent, agent_id)
        if existing is not None:
            status[agent_id] = "already_present"
            continue
        agent = Agent(
            id=agent_id,
            name=spec["name"],
            role=spec["role"],
            adapter_type=spec["adapter_type"],
            delegation_mode=spec["delegation_mode"],
            trust_level=spec["trust_level"],
            capabilities=list(spec.get("capabilities", [])),
            limitations=list(spec.get("limitations", [])),
            permissions=list(spec.get("permissions", [])),
            platform=spec.get("platform"),
        )
        session.add(agent)
        status[agent_id] = "inserted"
    session.commit()
    return status
