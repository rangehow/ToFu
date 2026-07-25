"""lib/production — Production Substrate (docs/PRODUCTION_PIPELINE_DESIGN.md).

The horizontal layer under every "one sentence → finished product" capability
(video / podcast / PPT / long report): long-job lifecycle, the stage-graph
contract, binary deliverables, and progress projection. Each capability keeps
its own thin **recipe** (the 300–600 lines of real business logic) on top.

P6 status — deliberately partial, and that is the design:

  * ``stages`` (the stage-graph contract + checkpointed resumable runner) has
    been RELOCATED here verbatim from ``lib/motion_video/_stages.py``. It was
    written knowing nothing about video/audio/LLMs precisely so this step
    could be a move, not a rewrite (strangler-fig).
  * ``ProductionRuntime`` / ``deliverable`` / progress double-projection /
    ``_registries()`` discovery / artifacts binary format are NOT here yet.
    Extracting those from two samples (motion + podcast) risks abstracting to
    the wrong shape; the design note argues for a third recipe (P7) first.

``lib/motion_video/_stages.py`` remains as a re-exporting shim so every
existing import keeps working byte-identically.
"""

from __future__ import annotations

from lib.production.stages import (
    STATE_VERSION,
    Stage,
    StageAborted,
    StageFailed,
    load_state,
    run_stages,
    stage_artifact,
    stage_is_done,
)

__all__ = [
    'STATE_VERSION',
    'Stage',
    'StageAborted',
    'StageFailed',
    'load_state',
    'run_stages',
    'stage_artifact',
    'stage_is_done',
]
