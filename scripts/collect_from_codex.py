#!/usr/bin/env python3
"""Collect Codex work logs into AIOS (#92).

See ``scripts/_collect_common.py`` for the shared fail-closed contract. Raw
records are pulled from the directory named by ``$CODEX_RAW_DIR`` (one JSON
file per Codex session/task output).
"""

from __future__ import annotations

from _collect_common import collect_main

from aios.collectors.codex import CodexAdapter

if __name__ == "__main__":
    raise SystemExit(collect_main(CodexAdapter()))
