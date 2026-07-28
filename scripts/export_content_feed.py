#!/usr/bin/env python3
"""Export the content feed as Markdown (#88 plan §10).

Calls ``ContentFeed.get_content_feed`` (read-only) through the CLI owner
authentication boundary and renders the allowed-scope work logs + APPROVED
knowledge facts as a Markdown document -- the MVP hand-off format for Hermes
content sourcing (no realtime API, plan §13).

Usage
-----
    python scripts/export_content_feed.py                          # company view
    python scripts/export_content_feed.py --project-id prj_x       # project view
    python scripts/export_content_feed.py --min-value high --output feed.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _owner_cli import add_owner_args, authenticate_owner_cli  # noqa: E402

from aios.db import get_database_url, get_engine, run_migrations  # noqa: E402
from aios.services import ServiceError  # noqa: E402
from aios.work_log import ContentFeed  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-id", default=None, help="project view; omit for company view")
    parser.add_argument("--min-value", default="medium", choices=["none", "low", "medium", "high"])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--output", help="output .md file (default: stdout)")
    add_owner_args(parser)
    return parser.parse_args()


def _render(entries: list[dict], *, project_id: str | None, min_value: str) -> str:
    scope = project_id or "company (all)"
    generated = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [
        "# AIOS Content Feed",
        "",
        f"- scope: `{scope}`",
        f"- min_value: `{min_value}`",
        f"- generated_at: `{generated}`",
        f"- entries: {len(entries)}",
        "",
    ]
    for entry in entries:
        if entry["kind"] == "work_log":
            lines += [
                f"## [work_log] {entry['id']}",
                "",
                f"- project: `{entry['project_id']}` | report_type: `{entry['report_type']}`"
                f" | content_value: `{entry['content_value']}`"
                f" | created_at: `{entry['created_at']}`",
                f"- content_angle: {entry['content_angle'] or '(none)'}",
                "",
                f"> {entry['new_knowledge']}",
                "",
            ]
        else:
            scope_label = entry["project_id"] or "company"
            lines += [
                f"## [fact] {entry['series_id']} v{entry['version']} ({entry['id']})",
                "",
                f"- scope: `{scope_label}` | tags: `{', '.join(entry['tags'])}`"
                f" | created_at: `{entry['created_at']}`",
                "",
                f"> {entry['statement']}",
                "",
            ]
    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    actor = authenticate_owner_cli(args)

    run_migrations()
    with Session(get_engine(get_database_url())) as session:
        try:
            entries = ContentFeed(session).get_content_feed(
                actor=actor,
                project_id=args.project_id,
                min_value=args.min_value,
                limit=args.limit,
                offset=args.offset,
            )
        except ServiceError as error:
            print(f"error {error.status_code}: {error.detail}", file=sys.stderr)
            return 1

    markdown = _render(entries, project_id=args.project_id, min_value=args.min_value)
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
        print(f"wrote {len(entries)} entries -> {args.output}")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
