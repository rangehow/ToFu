"""lib/optimizer/ — Daily Optimizer.

Runs once a day, mines logs / audit events / daily reports, and produces
structured optimisation proposals.  A narrow whitelist of low-risk actions
(v1: ``block_search_domain``) is auto-applied; everything else is stored
as ``pending_review`` for a human to approve via the REST API.

Each auto-applied action carries a ``ttl_days`` — the next run
automatically reverts expired actions.  A simple outcome-metric feedback
loop (see ``analyzer._compute_post_apply_metrics``) teaches the LLM
whether its previous proposals actually helped.

Public entry point:
    run_once(dry_run: bool = False) -> dict
"""

from lib.log import get_logger

from .orchestrator import run_once

logger = get_logger(__name__)

__all__ = ['run_once']
