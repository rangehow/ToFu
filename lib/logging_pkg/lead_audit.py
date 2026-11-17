"""Durable lead-audit telemetry: one compact JSONL stream kept outside the logs/ budget.

Responsibility
--------------
Keep the evidence a lead-trajectory audit needs (authority refusals of lead
conversations, per-task Turn Trace summaries with per-round aggregates, slow
round preparation, restart outcomes) for at least
:data:`LEAD_AUDIT_RETENTION_DAYS` full days. The general ``logs/`` budget
evicts ``app.log`` rotations after about a day under load, so these records
live in their own subdirectory, which that budget never inventories, and expire
by age only. Format and growth: docs/LOGGING.md, "Durable lead-audit stream".

Entry points
------------
``record(kind, fields)``: append one record (best effort; never raises).
``is_lead_conversation(conv_id)``: the conversations the stream covers.

Dependencies: the standard library and ``lib.logging_pkg.log_retention`` (also
standard-library-only), so the lifecycle manager can write restart outcomes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from lib.logging_pkg.log_retention import append_bytes_locked, ensure_private_log_directory

LEAD_AUDIT_DIRNAME = 'lead-audit'
# Audit recommendation #15 (2026-11-18): exact audits need at least seven full
# days; files of today plus the seven previous local days are kept.
LEAD_AUDIT_RETENTION_DAYS = 7
_FILE_RE = re.compile(r'^lead-audit-(\d{4}-\d{2}-\d{2})\.jsonl$')
# Project and Global Routers and the Router Watchdog are coordinator conversations.
_LEAD_CONVERSATION_PREFIX = 'coordinator-'

_LOGGER = logging.getLogger(__name__)
_PRUNED_DAYS: dict[str, str] = {}
_PRUNE_LOCK = threading.Lock()


def is_lead_conversation(conv_id) -> bool:
    return isinstance(conv_id, str) and conv_id.startswith(_LEAD_CONVERSATION_PREFIX)


def _default_logs_dir() -> Path:
    from runtime_path_policy import get_runtime_base
    return Path(get_runtime_base()) / 'logs'


def stream_directory(logs_dir: str | os.PathLike[str] | None = None) -> Path:
    return Path(logs_dir if logs_dir is not None else _default_logs_dir()) / LEAD_AUDIT_DIRNAME


def _prune(directory: Path, today: str, now: float) -> None:
    oldest = time.strftime('%Y-%m-%d', time.localtime(now - LEAD_AUDIT_RETENTION_DAYS * 86_400))
    for path in directory.iterdir():
        match = _FILE_RE.fullmatch(path.name)
        if match and match.group(1) < oldest and match.group(1) != today and path.is_file():
            path.unlink(missing_ok=True)
            _LOGGER.info('logging.retention_removed path=%s reason=lead_audit_retention_days', path)


def record(kind: str, fields: Mapping, *, logs_dir: str | os.PathLike[str] | None = None,
           now: float | None = None) -> bool:
    """Append ``{"ts", "kind", **fields}`` to today's file; False when it could not."""
    try:
        moment = time.time() if now is None else float(now)
        directory = stream_directory(logs_dir)
        ensure_private_log_directory(directory)
        day = time.strftime('%Y-%m-%d', time.localtime(moment))
        line = json.dumps({'ts': int(moment * 1000), 'kind': kind, **fields},
                          ensure_ascii=False, separators=(',', ':'), default=str)
        append_bytes_locked(directory / f'lead-audit-{day}.jsonl', (line + '\n').encode('utf-8'))
        key = str(directory)
        with _PRUNE_LOCK:
            if _PRUNED_DAYS.get(key) != day:
                _PRUNED_DAYS[key] = day
                _prune(directory, day, moment)
        return True
    except Exception as error:
        # Telemetry never alters the operation it observes.
        _LOGGER.debug('[LeadAudit] record dropped kind=%s: %s', kind, type(error).__name__)
        return False


__all__ = ['LEAD_AUDIT_DIRNAME', 'LEAD_AUDIT_RETENTION_DAYS', 'is_lead_conversation',
           'record', 'stream_directory']
