#!/usr/bin/env python3
"""Submit an AI-worker work log (#88 plan §10).

Reads the 7 report fields from a JSON file (``--json``) or interactively, then
calls ``WorkLogService.submit_work_log`` through the CLI owner-authentication
boundary (credentials verified against AIOS_OWNER_ID / AIOS_OWNER_API_KEY --
never self-minted).

The mandatory Idempotency-Key is generated when not supplied and ALWAYS
printed, so a failed run can be retried safely with ``--idempotency-key``.

Usage
-----
    python scripts/submit_work_log.py --project-id prj_x --json log.json
    python scripts/submit_work_log.py --project-id prj_x            # interactive
    python scripts/submit_work_log.py --project-id prj_x --json log.json \
        --idempotency-key 4f2a...                                   # replay
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _owner_cli import add_owner_args, authenticate_owner_cli  # noqa: E402

from aios.db import get_database_url, get_engine, run_migrations  # noqa: E402
from aios.services import ServiceError  # noqa: E402
from aios.work_log import REPORT_TYPES, WorkLogService  # noqa: E402

REPORT_FIELDS = ("what_done", "why", "problem", "solution", "new_knowledge")
OPTIONAL_FIELDS = (
    "task_ref",
    "produced_by_agent_id",
    "execution_assignment_id",
    "content_value",
    "should_enter_kb",
    "content_angle",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--report-type", default="daily", choices=sorted(REPORT_TYPES))
    parser.add_argument("--json", dest="json_file", help="JSON file with the report fields")
    for field in REPORT_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}", dest=field)
    parser.add_argument("--task-ref")
    parser.add_argument("--agent-id", dest="produced_by_agent_id")
    parser.add_argument("--assignment-id", dest="execution_assignment_id")
    parser.add_argument("--content-value", choices=["none", "low", "medium", "high"])
    parser.add_argument("--should-enter-kb", action="store_true")
    parser.add_argument("--content-angle")
    parser.add_argument(
        "--idempotency-key",
        help="client idempotency key; omitted -> generated and printed for replay",
    )
    add_owner_args(parser)
    return parser.parse_args()


def _collect_fields(args: argparse.Namespace) -> dict:
    """Merge JSON file < CLI flags < interactive prompts for the 7 fields."""
    data: dict = {}
    if args.json_file:
        data = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    for field in REPORT_FIELDS:
        flag_value = getattr(args, field)
        if flag_value is not None:
            data[field] = flag_value
        if not str(data.get(field, "")).strip():
            data[field] = input(f"{field}: ").strip()
    for field in OPTIONAL_FIELDS:
        flag_value = getattr(args, field, None)
        if flag_value not in (None, False):
            data[field] = flag_value
    return data


def main() -> int:
    args = _parse_args()
    actor = authenticate_owner_cli(args)
    fields = _collect_fields(args)

    client_key = args.idempotency_key or uuid.uuid4().hex
    print(f"Idempotency-Key: {client_key}  (use --idempotency-key to replay)")

    run_migrations()
    with Session(get_engine(get_database_url())) as session:
        try:
            artifact, _created = WorkLogService(session).submit_work_log(
                project_id=args.project_id,
                report_type=args.report_type,
                what_done=fields["what_done"],
                why=fields["why"],
                problem=fields["problem"],
                solution=fields["solution"],
                new_knowledge=fields["new_knowledge"],
                idempotency_key=client_key,
                actor=actor,
                task_ref=fields.get("task_ref"),
                produced_by_agent_id=fields.get("produced_by_agent_id"),
                execution_assignment_id=fields.get("execution_assignment_id"),
                content_value=fields.get("content_value"),
                should_enter_kb=bool(fields.get("should_enter_kb", False)),
                content_angle=fields.get("content_angle"),
            )
        except ServiceError as error:
            print(f"error {error.status_code}: {error.detail}", file=sys.stderr)
            return 1
        print(
            f"work log {artifact.id} [{artifact.review_status.value}] "
            f"project={artifact.project_id}"
        )
        print("next step: owner attestation -> python scripts/attest_work_log.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
