"""Shared logic for the V2 (#92) collection scripts.

Every ``collect_from_<platform>.py`` and ``collect_all.py`` funnels through
here so the fail-closed contract (plan §7) lives in exactly one place:

* resolve the configured ``Agent`` from ``--agent-ref``;
* assert ``Agent.platform == adapter.platform`` (else a config error -- never
  collect under a mismatched namespace, plan §7 / v18);
* ``fetch_raw``; a platform-level ``CollectorError`` aborts THIS platform but is
  caught by ``collect_all`` so the other platforms keep going;
* per record: ``normalize`` -> ``submit_work_log``; any exception is audited as
  a record error, the record is skipped, and the run continues;
* aggregate ``platform_errors + record_errors``; a non-zero total -> exit 1 so
  cron / ``collect_all`` can detect and retry (plan §7 / v17).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _owner_cli import add_owner_args, authenticate_owner_cli  # noqa: E402

from aios.actor import ActorContext  # noqa: E402
from aios.collectors.base import BaseCollector, CollectorError  # noqa: E402
from aios.db import get_database_url, get_engine, run_migrations  # noqa: E402
from aios.models import Agent  # noqa: E402
from aios.work_log import WorkLogService  # noqa: E402

COLLECTOR_ERROR_ACTION = "collector.error"
COLLECTOR_RECORD_ERROR_ACTION = "collector.record_error"
COLLECTOR_CONFIG_ERROR_ACTION = "collector.config_error"


def _write_audit(
    session: Session,
    *,
    actor: ActorContext,
    action: str,
    project_id: str,
    resource_id: str,
    detail: str,
) -> None:
    from aios.audit import append_audit

    append_audit(
        session,
        actor=actor.owner_id or "owner",
        action=action,
        resource_type="agent",
        resource_id=resource_id,
        project_id=project_id,
        task_id=None,
        before={},
        after={"error": detail[:500]},
        idempotency_key=f"collector:audit:{uuid.uuid4().hex}",
    )
    session.commit()


def _parse_args(platform: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Collect {platform} work logs into AIOS (#92)"
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--agent-ref",
        required=True,
        help="registered Agent id (its platform MUST match this script)",
    )
    parser.add_argument("--since", default=None, help="optional ISO8601 lower bound")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print normalized WorkLogSubmit drafts without writing to the DB",
    )
    add_owner_args(parser)
    return parser.parse_args()


def run_one(
    adapter: BaseCollector,
    *,
    session: Session,
    actor: ActorContext,
    project_id: str,
    agent_ref: str,
    since: str | None,
    dry_run: bool,
) -> tuple[int, int]:
    """Collect one platform. Returns ``(platform_errors, record_errors)``."""
    agent = session.get(Agent, agent_ref)
    if agent is None:
        _write_audit(
            session,
            actor=actor,
            action=COLLECTOR_CONFIG_ERROR_ACTION,
            project_id=project_id,
            resource_id=agent_ref,
            detail=f"unknown --agent-ref {agent_ref!r}",
        )
        return (1, 0)

    # Platform provenance sanity: never collect under a mismatched Agent
    # namespace (plan §7 / v18 fail-closed).
    if agent.platform != adapter.platform:
        _write_audit(
            session,
            actor=actor,
            action=COLLECTOR_CONFIG_ERROR_ACTION,
            project_id=project_id,
            resource_id=agent.id,
            detail=(
                f"agent platform {agent.platform!r} != "
                f"collector platform {adapter.platform!r}"
            ),
        )
        return (1, 0)

    platform_errors = 0
    record_errors = 0
    try:
        raws = adapter.fetch_raw(agent=agent, since=since)
    except CollectorError as exc:
        _write_audit(
            session,
            actor=actor,
            action=COLLECTOR_ERROR_ACTION,
            project_id=project_id,
            resource_id=agent.id,
            detail=str(exc),
        )
        return (1, 0)

    service = WorkLogService(session)
    for raw in raws:
        # normalize is INSIDE the per-record boundary so a malformed raw
        # (e.g. a pydantic ValidationError) becomes a skipped record error
        # rather than aborting the whole platform run (fail-closed, plan §7).
        try:
            submit = adapter.normalize(raw, project_id=project_id)
        except Exception as exc:  # noqa: BLE001  ingestion boundary: one bad record must not abort the run
            record_errors += 1
            if dry_run:
                print(
                    f"[dry-run] {adapter.platform}:{raw.external_id}: SKIP "
                    f"(normalize failed: {exc})",
                    file=sys.stderr,
                )
            else:
                _write_audit(
                    session,
                    actor=actor,
                    action=COLLECTOR_RECORD_ERROR_ACTION,
                    project_id=project_id,
                    resource_id=agent.id,
                    detail=f"{adapter.platform}:{raw.external_id}: {exc}",
                )
            continue
        if dry_run:
            print(f"[dry-run] {adapter.platform}:{raw.external_id} -> {submit.model_dump()}")
            continue
        try:
            service.submit_work_log(
                project_id=project_id,
                report_type=submit.report_type,
                what_done=submit.what_done,
                why=submit.why,
                problem=submit.problem,
                solution=submit.solution,
                new_knowledge=submit.new_knowledge,
                idempotency_key=f"collector:{agent.id}:{raw.external_id}",
                actor=actor,
                content_value=submit.content_value,
                should_enter_kb=submit.should_enter_kb,
                content_angle=submit.content_angle,
                source_platform=adapter.platform,
            )
        except Exception as exc:  # noqa: BLE001  ingestion boundary: skip + audit, never abort (plan §7)
            _write_audit(
                session,
                actor=actor,
                action=COLLECTOR_RECORD_ERROR_ACTION,
                project_id=project_id,
                resource_id=agent.id,
                detail=f"{adapter.platform}:{raw.external_id}: {exc}",
            )
            record_errors += 1
    return (platform_errors, record_errors)


def collect_main(adapter: BaseCollector) -> int:
    args = _parse_args(adapter.platform)
    owner_actor = authenticate_owner_cli(args)
    run_migrations()
    with Session(get_engine(get_database_url())) as session:
        platform_errors, record_errors = run_one(
            adapter,
            session=session,
            actor=owner_actor,
            project_id=args.project_id,
            agent_ref=args.agent_ref,
            since=args.since,
            dry_run=args.dry_run,
        )
    total = platform_errors + record_errors
    if total:
        print(
            f"{adapter.platform}: {platform_errors} platform error(s), "
            f"{record_errors} record error(s) -> exit 1",
            file=sys.stderr,
        )
        return 1
    print(f"{adapter.platform}: collection complete (no errors).")
    return 0
