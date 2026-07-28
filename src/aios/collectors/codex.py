"""Codex collection adapter (#92 plan §3.2).

Transport: file-backed (``CODEX_RAW_DIR``), the offline/portable mechanism
described in ``aios.collectors.base.read_raw_directory``. Each JSON file is one
Codex session/task output. Real live-API transport can replace ``fetch_raw``
without changing ``normalize`` or the script layer.
"""

from __future__ import annotations

from typing import Any

from aios.collectors.base import BaseCollector, RawLog, read_raw_directory
from aios.schemas import WorkLogSubmit

CODEX_RAW_DIR_ENV = "CODEX_RAW_DIR"


class CodexAdapter(BaseCollector):
    platform = "codex"

    def fetch_raw(self, *, agent: Any, since: str | None = None) -> list[RawLog]:
        return read_raw_directory(CODEX_RAW_DIR_ENV, self.platform)

    def normalize(self, raw: RawLog, *, project_id: str) -> WorkLogSubmit:
        data = raw.raw
        # Untrusted platform identity (agent/owner/author) is intentionally
        # ignored -- never copied into the draft (plan §0.4, §4).
        what_done = str(data.get("goal") or data.get("task") or "")
        why = str(data.get("background") or data.get("context") or "")
        problem = str(data.get("blocker") or data.get("issue") or "")
        solution = str(data.get("action") or data.get("resolution") or "")
        new_knowledge = str(data.get("conclusion") or data.get("takeaway") or "")
        return self._build_submit(
            project_id=project_id,
            what_done=what_done,
            why=why,
            problem=problem,
            solution=solution,
            new_knowledge=new_knowledge,
            source_platform=self.platform,
        )
