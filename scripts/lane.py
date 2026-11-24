#!/usr/bin/env python3
"""``python3 scripts/lane.py ...`` — the command spelling in AGENTS.md.

The CLI owner is ``lib.project_mod.lane_cli``. This wrapper is a
track-but-export-excluded keeper (see
``tests/meta/test_gitignore_covers_export_excludes.py``); detached spare
preparation spawns the tracked module directly, so an exported tree without
this file still prepares its next lane.

Contract: docs/modules/project_integration.md, "Lanes".
"""
from __future__ import annotations

import sys
from pathlib import Path

# Direct CLI execution must find the same shared owners as imports do.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.project_mod.lane_cli import main  # noqa: E402 - after the path setup above

if __name__ == '__main__':
    raise SystemExit(main())
