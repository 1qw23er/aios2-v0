#!/usr/bin/env python3
"""Run all four V2 (#92) collectors and aggregate their exit status.

This is the cron entry point (plan §7): it is the ONLY component that provides
cross-platform failure isolation. Each platform is collected independently via
``run_one`` -- a platform-level ``CollectorError`` or a per-record error in one
platform never aborts the others. The aggregate ``platform_errors +
record_errors`` decides the final exit code, so automation can tell success
from "collected but with dropped/failed records" (plan §7 / v17).

Per-platform ``--*-agent`` refs are optional; a platform whose agent ref is
omitted is skipped (not an error). A mismatch between an agent's platform and
the collector platform is a config error (counted, never silently absorbed).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _collect_common import run_one  # noqa: E402
from _owner_cli import add_owner_args, authenticate_owner_cli  # noqa: E402

from aios.collectors.codex import CodexAdapter  # noqa: E402
from aios.collectors.coze import CozeAdapter  # noqa: E402
from aios.collectors.hermes import HermesAdapter  # noqa: E402
from aios.collectors.workbuddy import WorkBuddyAdapter  # noqa: E402
from aios.db import get_database_url, get_engine, run_migrations  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--codex-agent", default=None)
    parser.add_argument("--hermes-agent", default=None)
    parser.add_argument("--workbuddy-agent", default=None)
    parser.add_argument("--coze-agent", default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--dry-run", action="store_true")
    add_owner_args(parser)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    owner_actor = authenticate_owner_cli(args)
    run_migrations()
    with Session(get_engine(get_database_url())) as session:
        platforms = [
            (CodexAdapter(), args.codex_agent),
            (HermesAdapter(), args.hermes_agent),
            (WorkBuddyAdapter(), args.workbuddy_agent),
            (CozeAdapter(), args.coze_agent),
        ]
        total_platform_errors = 0
        total_record_errors = 0
        for adapter, agent_ref in platforms:
            if not agent_ref:
                print(f"[skip] {adapter.platform}: no --{adapter.platform}-agent")
                continue
            pe, re = run_one(
                adapter,
                session=session,
                actor=owner_actor,
                project_id=args.project_id,
                agent_ref=agent_ref,
                since=args.since,
                dry_run=args.dry_run,
            )
            total_platform_errors += pe
            total_record_errors += re

    total = total_platform_errors + total_record_errors
    if total:
        print(
            f"collect_all: {total_platform_errors} platform error(s), "
            f"{total_record_errors} record error(s) -> exit 1",
            file=sys.stderr,
        )
        return 1
    print("collect_all: all platforms collected with no errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
