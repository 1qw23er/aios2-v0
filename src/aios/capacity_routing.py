"""Capacity-aware routing -- observation + ordering helpers (V1, PR-1: unwired).

This module is the **pure-compute / pure-read** half of Capacity-aware Routing
V1. It contains NO routing decision, NO DB write, NO entity mutation, NO
``ExecutionAssignment`` creation, NO audit emission, NO budget check, and NO
task/run claim. It only:

  * parses the ``AIOS_CAPACITY_ROUTING`` env config (fail-safe to disabled);
  * projects the per-agent in-flight ``DelegatedRun`` count from the database
    with a single GROUP BY SELECT;
  * exposes a deterministic, testable ``order_by_capacity`` pure function that
    re-orders *already ranked* candidates by load.

Why it must stay read-only
---------------------------
Capacity is an **advisory observation**, never an authority. The real execution
authority stays with ``claim_task_for_execution`` (Task-level at-most-once CAS)
and ``DelegatedRun.lease_owner`` (run-level fence). If this module wrote state
it would become a second execution authority -- explicitly forbidden by the V1
design (GO WITH CONDITIONS, C-6). The write ban is pinned by a source-level test
in ``tests/test_capacity_routing.py``.

Capacity definition (single fact)
----------------------------------
::

    in_flight(agent) := COUNT(DelegatedRun
                             WHERE agent_id = agent.id
                               AND status IN INFLIGHT_RUN_STATUSES)

``INFLIGHT_RUN_STATUSES`` is imported from ``aios.delegation`` -- it is NOT
re-declared here. That tuple is pinned by a test as the complement of
``TERMINAL_RUN_STATUSES``; a second literal would drift. This is the ONLY signal
V1 consumes: no latency, no failure rate, no cost, no heartbeat, no lease.

Cost is excluded by design (C-7): ``Task.estimated_cost`` (pre-estimate, same
for every candidate), ``DelegatedRun.cost`` (post-measured, would fabricate a
future price), and ``Project.budget_used`` (governance ledger) never enter
routing. The budget hard gate stays ``delegation.check_budget``.

Configuration (GAP-3 Stage 2 precedent -- same fail-safe contract)
-------------------------------------------------------------------
::

    AIOS_CAPACITY_ROUTING='{"default_max_inflight": 4, "agents": {"agt_x": 2}}'

* unset / empty          -> disabled (zero behaviour change)
* invalid JSON / non-object -> disabled + warning (never raises)
* non-positive limit      -> skipped for that entry + warning
* no default and no agents -> disabled (nothing to act on)

Determinism: ``order_by_capacity`` is a total order over the candidate dicts
produced by ``scheduler._rank`` (which already carry ``minimum_priority``,
``total_priority`` and ``agent_id``). ``agent_id`` is the final tie-break, so
the order is unique for a given input.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlmodel import Session

from aios.delegation import INFLIGHT_RUN_STATUSES
from aios.models import DelegatedRun

logger = logging.getLogger(__name__)

#: Env var holding the capacity-routing config as JSON. Unset == disabled.
CAPACITY_ROUTING_ENV = "AIOS_CAPACITY_ROUTING"


class CapacityRouteConfig(NamedTuple):
    """Parsed ``AIOS_CAPACITY_ROUTING`` payload. Empty/disabled when unset/invalid.

    ``enabled`` is True iff at least one usable limit is present (a
    ``default_max_inflight`` or at least one valid ``agents`` entry). A valid
    but empty object ``{}`` is therefore disabled -- matching "unset == disabled".
    """

    enabled: bool
    default_max_inflight: int | None
    agents: Mapping[str, int]


def load_capacity_routing(
    env: Mapping[str, str] | None = None,
) -> CapacityRouteConfig:
    """Read and parse the capacity-routing config (env defaults to ``os.environ``).

    ``env`` exists so tests pass an explicit mapping instead of monkeypatching the
    process environment -- exactly like ``model_pricing.load_model_pricing``.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return parse_capacity_routing(source.get(CAPACITY_ROUTING_ENV))


def parse_capacity_routing(raw: str | None) -> CapacityRouteConfig:
    """Parse a capacity-routing payload; unusable input is dropped, never fatal."""
    if not raw or not raw.strip():
        return CapacityRouteConfig(enabled=False, default_max_inflight=None, agents={})
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("%s is not valid JSON -- capacity routing is DISABLED", CAPACITY_ROUTING_ENV)
        return CapacityRouteConfig(enabled=False, default_max_inflight=None, agents={})
    if not isinstance(payload, dict):
        logger.warning(
            "%s must be a JSON object -- capacity routing is DISABLED",
            CAPACITY_ROUTING_ENV,
        )
        return CapacityRouteConfig(enabled=False, default_max_inflight=None, agents={})

    default_max = _coerce_positive_int(payload.get("default_max_inflight"))

    agents: dict[str, int] = {}
    raw_agents = payload.get("agents")
    if isinstance(raw_agents, Mapping):
        for agent_id, value in raw_agents.items():
            limit = _coerce_positive_int(value)
            if limit is None:
                logger.warning(
                    "ignoring invalid %s agents entry for %r",
                    CAPACITY_ROUTING_ENV,
                    agent_id,
                )
                continue
            agents[agent_id] = limit

    enabled = default_max is not None or bool(agents)
    return CapacityRouteConfig(enabled=enabled, default_max_inflight=default_max, agents=agents)


class CapacitySnapshot(NamedTuple):
    """A capacity observation: parsed config + a per-agent in-flight projection.

    Built by ``build_capacity_snapshot`` (PR-2 will call that right before
    ranking). Pure data; the methods only read it.
    """

    enabled: bool
    default_max_inflight: int | None
    agents: Mapping[str, int]
    in_flight_by_agent: Mapping[str, int]

    def in_flight(self, agent_id: str) -> int:
        """Active DelegatedRun count for ``agent_id`` (0 when the agent is absent)."""
        return int(self.in_flight_by_agent.get(agent_id, 0))

    def max_inflight(self, agent_id: str) -> int | None:
        """Effective capacity ceiling for ``agent_id`` (override beats default)."""
        if agent_id in self.agents:
            return self.agents[agent_id]
        return self.default_max_inflight

    def saturated(self, agent_id: str) -> bool:
        """True only when a ceiling is configured AND the agent has reached it.

        An agent with no ceiling (no override, no default) is never saturated --
        its ``in_flight`` still participates in the secondary ordering, it just
        can never hard-block.
        """
        ceiling = self.max_inflight(agent_id)
        if ceiling is None:
            return False
        return self.in_flight(agent_id) >= ceiling


def build_capacity_snapshot(
    config: CapacityRouteConfig,
    in_flight_by_agent: Mapping[str, int],
) -> CapacitySnapshot:
    """Combine parsed config with a DB projection into an immutable snapshot."""
    return CapacitySnapshot(
        enabled=config.enabled,
        default_max_inflight=config.default_max_inflight,
        agents=config.agents,
        in_flight_by_agent=dict(in_flight_by_agent),
    )


def project_in_flight_by_agent(session: Session) -> dict[str, int]:
    """One-shot GROUP BY projection of per-agent in-flight DelegatedRun count.

    Capacity = the number of DelegatedRuns (``status IN INFLIGHT_RUN_STATUSES``)
    whose ``agent_id`` is non-NULL, grouped by agent. LOCAL runs
    (``agent_id IS NULL``) never form a group, so they are excluded by the GROUP
    BY key -- they cannot inflate any agent's load. Pure SELECT: no writes.

    This deliberately does NOT read ``Task.status == RUNNING`` as a capacity
    signal: recovery rewrites stranded RUNNING tasks to FAILED, which would leave
    "reclaimed work" still counting as load. The DelegatedRun in-flight set is the
    single authority for capacity (see design doc B2).
    """
    rows = session.execute(
        select(DelegatedRun.agent_id, func.count(DelegatedRun.id))
        .where(DelegatedRun.status.in_(INFLIGHT_RUN_STATUSES))
        .where(DelegatedRun.agent_id.is_not(None))
        .group_by(DelegatedRun.agent_id)
    ).all()
    return {agent_id: int(count) for agent_id, count in rows}


def order_by_capacity(
    ranked: Sequence[Mapping[str, Any]],
    snapshot: CapacitySnapshot,
) -> list[dict[str, Any]]:
    """Re-order already-ranked candidates by load, keeping capability order first.

    ``ranked`` is a sequence of candidate dicts already sorted by ``_rank``; each
    must carry ``minimum_priority``, ``total_priority`` and ``agent_id``. This is
    a PURE function -- it returns a NEW list and never mutates ``ranked``.

    Sort key (capability is absolute priority; load only breaks ties within the
    same capability tier)::

        (-minimum_priority, -total_priority, saturated, in_flight, agent_id)

    ``saturated`` (False=0 < True=1) pushes saturated agents after idle ones;
    ``in_flight`` (ascending) prefers the less busy agent; ``agent_id`` (the
    primary key) is the unique, static final tie-break. When ``snapshot.enabled``
    is False the input order is returned unchanged (zero behaviour change).
    """
    if not snapshot.enabled:
        return [dict(c) for c in ranked]
    return sorted(
        (dict(c) for c in ranked),
        key=lambda c: (
            -c["minimum_priority"],
            -c["total_priority"],
            snapshot.saturated(c["agent_id"]),
            snapshot.in_flight(c["agent_id"]),
            c["agent_id"],
        ),
    )


def _coerce_positive_int(value: Any) -> int | None:
    """Coerce ``value`` to a positive int, else None (skipped, never fatal)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return None
