#!/usr/bin/env python3
"""Harvest knowledge candidates from attested work logs (#88 plan §10).

Calls ``KnowledgeHarvester.harvest_candidates`` through the CLI owner
authentication boundary. Only APPROVED (owner-attested) work logs whose
``should_enter_kb`` is true or ``content_value`` is high/medium produce a
DRAFT ``KnowledgeCandidate`` -- the candidate still requires the owner's
normal knowledge review; nothing is auto-approved.

Usage
-----
    python scripts/harvest_candidates.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _owner_cli import add_owner_args, authenticate_owner_cli  # noqa: E402

from aios.db import get_database_url, get_engine, run_migrations  # noqa: E402
from aios.services import ServiceError  # noqa: E402
from aios.work_log import KnowledgeHarvester  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_owner_args(parser)
    args = parser.parse_args()
    actor = authenticate_owner_cli(args)

    run_migrations()
    with Session(get_engine(get_database_url())) as session:
        try:
            created = KnowledgeHarvester(session).harvest_candidates(actor=actor)
        except ServiceError as error:
            print(f"error {error.status_code}: {error.detail}", file=sys.stderr)
            return 1
        print(f"{len(created)} new DRAFT candidate(s):")
        for candidate in created:
            print(f"  {candidate.id}  artifact={candidate.artifact_id}  tags={candidate.tags}")
        if created:
            print("next step: owner knowledge review (existing /knowledge/candidates flow).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
