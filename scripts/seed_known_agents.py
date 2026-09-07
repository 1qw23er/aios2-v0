#!/usr/bin/env python3
"""Seed the known internal agents into the Agent registry (idempotent).

Gap #3: gives the local CLI tooling real, registry-validated identities
(``workbuddy`` / ``gpt``) instead of free-text strings. Safe to re-run: rows
that already exist are reported as ``already_present`` and never overwritten.

Usage:
    python scripts/seed_known_agents.py            # human-readable table
    python scripts/seed_known_agents.py --json     # machine-readable status map
"""
from __future__ import annotations

import argparse
import json

from aios.db import make_session
from aios.known_agents import seed_known_agents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the status map as JSON instead of a human-readable table.",
    )
    args = parser.parse_args(argv)

    session = make_session()
    try:
        status = seed_known_agents(session)
    finally:
        session.close()

    if args.json:
        print(json.dumps(status, ensure_ascii=False))
    else:
        for agent_id, state in status.items():
            print(f"{agent_id}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
