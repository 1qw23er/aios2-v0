#!/usr/bin/env python3
"""Collect Hermes work logs into AIOS (#92).

Raw records are pulled from the directory named by ``$HERMES_RAW_DIR``. See
``scripts/_collect_common.py`` for the shared fail-closed contract.
"""

from __future__ import annotations

from _collect_common import collect_main

from aios.collectors.hermes import HermesAdapter

if __name__ == "__main__":
    raise SystemExit(collect_main(HermesAdapter()))
