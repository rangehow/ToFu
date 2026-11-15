#!/usr/bin/env python3
"""Verify frozen working bytes and reuse the landing engine's completed evidence.

Usage: python3 scripts/verify.py {run,plan} [TEST FILE/DIRECTORY ...] [--json]
Policy and execution belong to lib.project_mod.verification.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.project_mod.verification import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
