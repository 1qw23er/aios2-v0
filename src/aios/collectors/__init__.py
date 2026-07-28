"""Semi-automated work-log collection adapters (#92 plan §2-§4).

This package turns a platform's raw output into a ``WorkLogSubmit`` draft. It
does NOT touch the database or call ``submit_work_log`` -- submission is the
script layer's job (plan §5/§7), keeping "collection" and "ingestion" cleanly
separated for testability and fail-closed error handling.

Each adapter is a ``BaseCollector`` subclass responsible for ONE platform:
``CodexAdapter`` / ``HermesAdapter`` / ``WorkBuddyAdapter`` / ``CozeAdapter``.
"""

from aios.collectors.base import BaseCollector, CollectorError, RawLog
from aios.collectors.codex import CodexAdapter
from aios.collectors.coze import CozeAdapter
from aios.collectors.hermes import HermesAdapter
from aios.collectors.workbuddy import WorkBuddyAdapter

__all__ = [
    "BaseCollector",
    "CollectorError",
    "RawLog",
    "CodexAdapter",
    "HermesAdapter",
    "WorkBuddyAdapter",
    "CozeAdapter",
]
