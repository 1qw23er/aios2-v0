"""Coze collection adapter (#92 plan §3.2).

Transport: file-backed (``COZE_RAW_DIR``). Coze workflow run results (or
webhook-returned JSON) are dropped into the directory for the pull collector to
ingest. See ``read_raw_directory`` for contract.
"""

from __future__ import annotations

from typing import Any

from aios.collectors.base import BaseCollector, RawLog, read_raw_directory
from aios.schemas import WorkLogSubmit

COZE_RAW_DIR_ENV = "COZE_RAW_DIR"


class CozeAdapter(BaseCollector):
    platform = "coze"

    def fetch_raw(self, *, agent: Any, since: str | None = None) -> list[RawLog]:
        return read_raw_directory(COZE_RAW_DIR_ENV, self.platform)

    def normalize(self, raw: RawLog, *, project_id: str) -> WorkLogSubmit:
        data = raw.raw
        what_done = str(data.get("workflow") or data.get("name") or "")
        why = str(data.get("trigger_reason") or data.get("business_trigger") or "")
        problem = str(data.get("blocker") or data.get("flow_issue") or "")
        solution = str(data.get("output") or data.get("result") or "")
        new_knowledge = str(data.get("experience") or data.get("lesson") or "")
        return self._build_submit(
            project_id=project_id,
            what_done=what_done,
            why=why,
            problem=problem,
            solution=solution,
            new_knowledge=new_knowledge,
            source_platform=self.platform,
        )
