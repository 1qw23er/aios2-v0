"""WorkBuddy collection adapter (#92 plan §3.2).

Transport: file-backed (``WORKBUDDY_RAW_DIR``). WorkBuddy is the AIOS control
plane; exporting its artifact / task results to a directory is the "export ->
normalize" path described in the plan. See ``read_raw_directory`` for contract.
"""

from __future__ import annotations

from typing import Any

from aios.collectors.base import BaseCollector, RawLog, read_raw_directory
from aios.schemas import WorkLogSubmit

WORKBUDDY_RAW_DIR_ENV = "WORKBUDDY_RAW_DIR"


class WorkBuddyAdapter(BaseCollector):
    platform = "workbuddy"

    def fetch_raw(self, *, agent: Any, since: str | None = None) -> list[RawLog]:
        return read_raw_directory(WORKBUDDY_RAW_DIR_ENV, self.platform)

    def normalize(self, raw: RawLog, *, project_id: str) -> WorkLogSubmit:
        data = raw.raw
        what_done = str(data.get("title") or data.get("artifact") or "")
        why = str(data.get("context") or data.get("task_context") or "")
        problem = str(data.get("problem") or data.get("to_solve") or "")
        solution = str(data.get("deliverable") or data.get("output") or "")
        new_knowledge = str(data.get("insight") or data.get("new_understanding") or "")
        return self._build_submit(
            project_id=project_id,
            what_done=what_done,
            why=why,
            problem=problem,
            solution=solution,
            new_knowledge=new_knowledge,
            source_platform=self.platform,
        )
