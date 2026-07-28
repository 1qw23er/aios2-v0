"""Hermes collection adapter (#92 plan §3.2).

Transport: file-backed (``HERMES_RAW_DIR``). Each JSON file is one Hermes
agent output (Hermes already runs on the server, so exporting its output to a
directory is the natural local pull). See ``read_raw_directory`` for contract.
"""

from __future__ import annotations

from typing import Any

from aios.collectors.base import BaseCollector, RawLog, read_raw_directory
from aios.schemas import WorkLogSubmit

HERMES_RAW_DIR_ENV = "HERMES_RAW_DIR"


class HermesAdapter(BaseCollector):
    platform = "hermes"

    def fetch_raw(self, *, agent: Any, since: str | None = None) -> list[RawLog]:
        return read_raw_directory(HERMES_RAW_DIR_ENV, self.platform)

    def normalize(self, raw: RawLog, *, project_id: str) -> WorkLogSubmit:
        data = raw.raw
        what_done = str(data.get("topic") or data.get("subject") or "")
        why = str(data.get("need") or data.get("background") or "")
        problem = str(data.get("pain") or data.get("challenge") or "")
        solution = str(data.get("draft") or data.get("strategy") or "")
        new_knowledge = str(data.get("methodology") or data.get("playbook") or "")
        return self._build_submit(
            project_id=project_id,
            what_done=what_done,
            why=why,
            problem=problem,
            solution=solution,
            new_knowledge=new_knowledge,
            source_platform=self.platform,
        )
