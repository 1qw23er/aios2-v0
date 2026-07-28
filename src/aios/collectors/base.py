"""Collector base classes and shared invariants (#92 plan §3).

``BaseCollector`` defines the contract every platform adapter honors:

* ``fetch_raw`` -- pull raw platform output. ANY failure (missing config,
  network/IO error, malformed payload) MUST raise ``CollectorError`` rather
  than swallow the error or fabricate a log (fail-closed, plan §0.7).
* ``normalize`` -- a PURE function mapping one ``RawLog`` + a caller-verified
  ``project_id`` to a ``WorkLogSubmit`` draft. No DB / network / clock access,
  so it can be exhaustively unit-tested.

``RawLog`` is the transport-agnostic record ``fetch_raw`` produces.

The shared ``_build_submit`` helper enforces the V2 invariants (plan §4):
``content_value`` defaults to ``"low"`` and ``should_enter_kb`` to ``False``
so collected drafts are NOT auto-harvestable (KB eligibility is decided by the
owner at attest time), and NO ``produced_by_agent_id`` / ``task_ref`` is set
(avoids the #88 §6 task_ref requirement). Untrusted platform identity is
dropped by each adapter's ``normalize`` -- never copied into the draft.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aios.schemas import WorkLogSubmit


class CollectorError(Exception):
    """Raised by ``fetch_raw`` on any platform access/parse failure.

    The script layer catches this to skip/abort a platform WITHOUT crashing the
    whole collection run (plan §0.7, §7).
    """


@dataclass
class RawLog:
    """One raw record pulled from a platform (transport-agnostic)."""

    external_id: str  # platform-side unique id (drives the idempotency key)
    captured_at: str  # ISO8601 UTC, platform production time
    raw: dict[str, Any]  # platform payload (untrusted; display/normalize only)
    source_platform: str  # "codex" | "hermes" | "workbuddy" | "coze"


class BaseCollector(abc.ABC):
    """Subclass for each collection platform (plan §3)."""

    platform: str = ""

    @abc.abstractmethod
    def fetch_raw(self, *, agent: Any, since: str | None = None) -> list[RawLog]:
        """Pull raw platform output.

        Network/parse/config failure MUST raise ``CollectorError`` (never
        return dirty or partial data). ``agent`` is the already-resolved,
        platform-checked ``Agent`` configured for this collection; ``since`` is
        an optional ISO8601 lower bound supplied by the script.
        """

    @abc.abstractmethod
    def normalize(self, raw: RawLog, *, project_id: str) -> WorkLogSubmit:
        """Map one ``RawLog`` to a ``WorkLogSubmit`` draft (pure function)."""

    # -- shared invariant enforcement (plan §4) ---------------------------------

    @staticmethod
    def _build_submit(
        *,
        project_id: str,
        what_done: str,
        why: str,
        problem: str,
        solution: str,
        new_knowledge: str,
        source_platform: str,
    ) -> WorkLogSubmit:
        """Construct a draft obeying every V2 invariant.

        ``content_value`` is pinned to ``"low"`` and ``should_enter_kb`` to
        ``False`` so a collected draft is never auto-harvestable; the owner
        opts it into the KB via the attest override. No agent provenance is
        set. ``source_platform`` is recorded for feed display/filtering only
        (never written to ``provenance``).

        A platform record may legitimately omit optional detail fields (e.g. a
        Codex log with no blocker). Rather than crash ``normalize`` (which the
        script loop calls per record) we substitute a ``"-"`` placeholder so
        the draft is always valid and the record is ingested; the owner still
        reviews/attests every draft before it can enter the KB.
        """
        return WorkLogSubmit(
            project_id=project_id,
            report_type="daily",
            what_done=what_done or "-",
            why=why or "-",
            problem=problem or "-",
            solution=solution or "-",
            new_knowledge=new_knowledge or "-",
            content_value="low",
            should_enter_kb=False,
            content_angle=(new_knowledge or "")[:80],
            source_platform=source_platform,
        )


def read_raw_directory(env_var: str, platform: str) -> list[RawLog]:
    """File-backed transport shared by every adapter (offline / portable).

    Reads every ``*.json`` file under the directory named by ``env_var``. Each
    file is one raw record; ``external_id`` / ``captured_at`` are taken from the
    file (falling back to the filename stem and "" respectively). Missing env
    var or a non-directory path raises ``CollectorError``; an unreadable or
    malformed file also raises ``CollectorError`` (fail-closed, plan §0.7).
    """
    import os

    path = os.environ.get(env_var)
    if not path:
        raise CollectorError(f"{platform} collector: missing ${env_var}")
    directory = Path(path)
    if not directory.is_dir():
        raise CollectorError(f"{platform} collector: {path} is not a directory")

    logs: list[RawLog] = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError as exc:
        # Directory enumeration must also stay inside the CollectorError
        # boundary so a single inaccessible platform dir cannot crash the
        # whole multi-platform run (fail-closed + cross-platform isolation).
        raise CollectorError(
            f"{platform} collector: cannot list {path}: {exc}"
        ) from exc
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            # UnicodeError covers invalid UTF-8, which would otherwise escape
            # fetch_raw and abort collect_all before the other platforms run.
            raise CollectorError(
                f"{platform} collector: cannot read {fp.name}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise CollectorError(
                f"{platform} collector: {fp.name} is not a JSON object"
            )
        external_id = str(data.get("external_id") or fp.stem)
        captured_at = str(data.get("captured_at") or "")
        logs.append(
            RawLog(
                external_id=external_id,
                captured_at=captured_at,
                raw=data,
                source_platform=platform,
            )
        )
    return logs
