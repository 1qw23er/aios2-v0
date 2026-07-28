#!/usr/bin/env python3
"""Owner attestation of work logs (#88 plan §10).

Lists UNVERIFIED work logs and lets the owner attest them one by one via
``WorkLogService.attest_work_log`` -- the ONLY path that APPROVEs a work log
(atomic Approval + status flip + AuditLog, plan §7.2). Credentials are
verified by the CLI authentication boundary; the script never self-mints an
owner actor.

Usage
-----
    python scripts/attest_work_log.py                    # interactive review
    python scripts/attest_work_log.py --artifact-id art_x  # attest one directly
    python scripts/attest_work_log.py --yes              # attest all UNVERIFIED
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlmodel import Session, select

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _owner_cli import add_owner_args, authenticate_owner_cli  # noqa: E402

from aios.db import get_database_url, get_engine, run_migrations  # noqa: E402
from aios.models import Artifact, ArtifactReviewStatus, ArtifactType  # noqa: E402
from aios.services import ServiceError  # noqa: E402
from aios.work_log import WorkLogService  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifact-id", help="attest this specific work log only")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="attest every listed UNVERIFIED work log without prompting",
    )
    add_owner_args(parser)
    return parser.parse_args()


def _describe(log: Artifact) -> str:
    metadata = log.metadata_json or {}
    what_done = str(metadata.get("what_done", ""))[:60]
    return (
        f"{log.id}  project={log.project_id}  type={metadata.get('report_type')}  "
        f"value={metadata.get('content_value')}  what_done={what_done!r}"
    )


def main() -> int:
    args = _parse_args()
    actor = authenticate_owner_cli(args)

    run_migrations()
    with Session(get_engine(get_database_url())) as session:
        service = WorkLogService(session)

        if args.artifact_id:
            targets = [args.artifact_id]
        else:
            logs = session.exec(
                select(Artifact)
                .where(
                    Artifact.type == ArtifactType.WORK_LOG,
                    Artifact.review_status == ArtifactReviewStatus.UNVERIFIED,
                )
                .order_by(Artifact.created_at)
            ).all()
            if not logs:
                print("no UNVERIFIED work logs.")
                return 0
            print(f"{len(logs)} UNVERIFIED work log(s):")
            targets = []
            for log in logs:
                print(f"  {_describe(log)}")
                if args.yes or input("  attest? [y/N] ").strip().lower() == "y":
                    targets.append(log.id)

        attested = 0
        for artifact_id in targets:
            try:
                artifact = service.attest_work_log(artifact_id=artifact_id, actor=actor)
            except ServiceError as error:
                print(
                    f"error {error.status_code} on {artifact_id}: {error.detail}",
                    file=sys.stderr,
                )
                return 1
            print(f"attested {artifact.id} -> {artifact.review_status.value}")
            attested += 1
        print(f"done: {attested} attested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
