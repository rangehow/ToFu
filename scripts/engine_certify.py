#!/usr/bin/env python3
"""Certify one commit's storage engine and publish the record restarts deploy.

Runs the engine gate (scripts/engine_certification/gate.py) for --commit
(default HEAD) in a private checkout: build, certify with and without online
background work, cargo lib and fastpath specifications, the native pytest
subset, a production-shaped load on --fixture and the rollback drill against
the deployed (and last certified) engine. Only when every check passed it
publishes packages/tofu-db/target/engine-certifications/<source>/<contract>/,
which restart preparation then deploys. Every run keeps its evidence under
engine-certifications/runs/. Contract: docs/NATIVE_STORAGE_LIFECYCLE.md,
"Engine certification gate".

Usage: python3 scripts/engine_certify.py --fixture AUTHORITY_DIR [--commit REV]
           [--previous-engine PATH ...] [--work-root DIR] [--note TEXT]
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.engine_certification.gate import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
