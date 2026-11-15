"""Watchdog review heartbeat on disk: the out-of-band dead-man signal.

Responsibility: record per owner that a periodic Watchdog review ran and read
storage, with the time by which the next review must have run
(``staleAfter``), or that the owner disabled the review. The file is
``logs_root()/watchdog-heartbeat-<owner>.json``, never TofuDB, so the watcher
outside the application (``serverctl_pkg/oob_alert.py``,
docs/STORAGE_OPERATIONS.md "Out-of-band outage alert") can tell when reviews
stop: a heartbeat past ``staleAfter`` alerts the owner.

``staleAfter`` is the next cron run plus the scheduler's due-claim interval
and the review's own deadline: the heartbeat is written when the review ends.
Writes are atomic and never raise into the review; a heartbeat that cannot be
written goes stale, which is itself the signal.

Entry points: ``record_review``, ``record_configured``, ``heartbeat_path``,
``REVIEW_MAX_RUNTIME_S``.
Dependencies: ``lib.runtime.runtime_paths.logs_root``, ``lib.scheduler.cron``,
``lib.scheduler.contract``.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

from lib.identity import require_user_id
from lib.log import get_logger
from lib.scheduler.contract import DUE_CLAIM_INTERVAL_SECONDS

logger = get_logger(__name__)

HEARTBEAT_VERSION = 1
# The periodic review task's deadline (scheduler max_runtime).
REVIEW_MAX_RUNTIME_S = 300


def _logs_root() -> Path:
    from lib.runtime.runtime_paths import logs_root
    return Path(logs_root())


def heartbeat_path(owner: int) -> Path:
    return _logs_root() / f'watchdog-heartbeat-{require_user_id(owner, context="watchdog heartbeat")}.json'


def stale_after(schedule: str, now: datetime) -> float | None:
    """When the review after ``now`` must have ended, or None for a cron with no next run."""
    from lib.scheduler.cron import next_cron_run
    due = next_cron_run(schedule, from_dt=now)
    if due is None:
        return None
    return due.timestamp() + DUE_CLAIM_INTERVAL_SECONDS + REVIEW_MAX_RUNTIME_S


def _write(owner: int, *, schedule: str, enabled: bool, now: datetime | None) -> None:
    moment = now or datetime.now()
    record = {'version': HEARTBEAT_VERSION, 'owner': require_user_id(owner, context='watchdog heartbeat'),
              'at': moment.timestamp(), 'schedule': schedule, 'enabled': enabled,
              'staleAfter': stale_after(schedule, moment) if enabled else None}
    path = heartbeat_path(owner)
    try:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.stem}-', suffix='.json')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(record, stream)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except Exception as error:
        logger.warning('[Watchdog] review heartbeat not written for owner %s: %s: %s', owner,
                       type(error).__name__, error)


def record_review(owner: int, schedule: str, *, now: datetime | None = None) -> None:
    """A periodic review ran and read storage; the next one is due by ``staleAfter``."""
    _write(owner, schedule=schedule, enabled=True, now=now)


def record_configured(owner: int, schedule: str, enabled: bool, *, now: datetime | None = None) -> None:
    """The owner set the review: disabled stops the dead-man watch, enabled expects the next run."""
    _write(owner, schedule=schedule, enabled=enabled, now=now)


__all__ = ['HEARTBEAT_VERSION', 'REVIEW_MAX_RUNTIME_S', 'heartbeat_path', 'record_configured',
           'record_review', 'stale_after']
