"""Node call journal: a side-effecting command id runs at most once on this node.

Problem: the Home settles a call as ``device_unavailable`` when this node stops
polling mid-command, and a resumed Turn may send the same tool call again
(its command id is the call's idempotency key, ``lib.desktop.routing``). The
in-memory ``RuntimeState.seen`` set dies with the process, so a restarted node
would run the side effect a second time.

Decision: before a command whose tier is not read/control starts, the node
writes ``started`` for its id in a private directory beside its config; on
completion it writes the outcome (except Codex/egress: only a sensitive_done marker). A command id that reaches the node again is
answered from the journal: the recorded outcome when it finished, or a typed
``outcome unknown`` refusal when the node died while running it. Nothing is
re-run. Records retire after the contract's retention
(``x-idempotency-journal-retention-seconds``), swept when the journal opens.

Alternatives considered: deduplicating only on the Home (a Home restart loses
its queue); journaling read commands (re-running a read is harmless and
screenshots would fill the disk).

Entry points: CallJournal (begin, complete), journal_dir.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

_SENSITIVE_TYPES = frozenset(('codex_login_read', 'egress_http', 'egress_http_stream'))
_SENSITIVE_ERROR = (
    'device_unavailable: sensitive result was not retained; this call completed and was '
    'not run again. Use a new call id for a fresh authorized request.')

_CONTRACT = Path(__file__).resolve().parents[2] / 'contracts/device_routing_v1.schema.json'


def _retention_seconds() -> int:
    return json.loads(_CONTRACT.read_text(encoding='utf-8'))[
        'x-idempotency-journal-retention-seconds']


def journal_dir() -> str:
    from lib.desktop_agent.config import config_path
    return os.path.splitext(config_path())[0] + '.calls'


def _journaled(cmd_type: str, params) -> bool:
    from lib.desktop_wire.node_protocol import required_tier
    return required_tier(cmd_type, params if isinstance(params, dict) else {}) not in (
        None, 'read', 'control')


class CallJournal:
    def __init__(self, directory: str | None = None, *, clock=time.time):
        self.directory = directory or journal_dir()
        self.clock = clock
        self.retention = _retention_seconds()
        self.lock = threading.Lock()
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        if os.name == 'posix':
            os.chmod(self.directory, 0o700)
        self._sweep()

    def _path(self, cmd_id: str) -> str:
        return os.path.join(self.directory,
                            hashlib.sha256(cmd_id.encode('utf-8')).hexdigest() + '.json')

    def _sweep(self) -> None:
        horizon = self.clock() - self.retention
        try:
            names = os.listdir(self.directory)
        except OSError as error:
            logger.warning('[Agent] call journal unreadable: %s', error)
            return
        for name in names:
            path = os.path.join(self.directory, name)
            try:
                if not name.endswith('.json') or len(name) != 69:
                    continue
                int(name[:-5], 16)
                stat = os.stat(path)
                if os.name == 'posix':
                    os.chmod(path, 0o600)
                if stat.st_mtime < horizon:
                    os.unlink(path)
                    continue
                with open(path, encoding='utf-8') as handle:
                    record = json.load(handle)
                if (isinstance(record, dict) and
                        isinstance(record.get('type'), str) and
                        record.get('type') in _SENSITIVE_TYPES):
                    redacted = {key: record.get(key) for key in ('id', 'type', 'state', 'at')}
                    if record.get('state') in ('done', 'sensitive_done'):
                        redacted['state'] = 'sensitive_done'
                    self._write(redacted, times=(stat.st_atime_ns, stat.st_mtime_ns), path=path)
            except (ValueError, TypeError):
                # Invalid records remain as evidence; begin refuses them.
                continue

    def _read(self, cmd_id: str):
        try:
            with open(self._path(cmd_id), encoding='utf-8') as handle:
                record = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            # An unreadable record is not proof the call never ran.
            logger.warning('[Agent] call journal record unreadable: %s', error)
            return {'id': cmd_id, 'state': 'started'}
        if (not isinstance(record, dict) or record.get('id') != cmd_id or
                not isinstance(record.get('type'), str) or
                record.get('state') not in ('started', 'done', 'sensitive_done') or
                not isinstance(record.get('at'), (int, float))):
            return {'id': cmd_id, 'state': 'started'}
        return record

    def _write(self, record: dict, *, times=None, path=None) -> None:
        # Set migration timestamps before replacement so retention never observes
        # a refreshed horizon, even if the process stops immediately afterwards.
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.journal-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(record, handle)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            if times is not None:
                os.utime(temporary, ns=times)
            os.replace(temporary, path or self._path(record['id']))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def begin(self, cmd: dict):
        """None when the command may run now; else the completion to send instead."""
        cmd_id, cmd_type = cmd['id'], cmd['type']
        with self.lock:
            record = self._read(cmd_id)
            if record is None:
                if cmd_type not in _SENSITIVE_TYPES and not _journaled(cmd_type, cmd.get('params')):
                    return None
                self._write({'id': cmd_id, 'type': cmd_type, 'state': 'started',
                             'at': self.clock()})
                return None
        if (record.get('state') == 'sensitive_done' or
                (record.get('state') == 'done' and
                 (cmd_type in _SENSITIVE_TYPES or record.get('type') in _SENSITIVE_TYPES))):
            return {'id': cmd_id, 'result': None, 'error': _SENSITIVE_ERROR}
        if record.get('state') == 'done':
            logger.info('[Agent] repeated call %s answered from the journal', cmd_id[:12])
            return {'id': cmd_id, 'result': record.get('result'), 'error': record.get('error')}
        return {'id': cmd_id, 'result': None,
                'error': ('device_unavailable: this call already started on this device before '
                          'it lost its connection or restarted; its outcome is unknown and it '
                          'was not run again.')}

    def complete(self, cmd_type: str, params, completion: dict) -> None:
        try:
            with self.lock:
                stored = self._read(completion['id']) or {}
                stored_type = stored.get('type')
                sensitive = cmd_type in _SENSITIVE_TYPES or stored_type in _SENSITIVE_TYPES
                if not sensitive and not _journaled(cmd_type, params):
                    return
                record = {'id': completion['id'], 'type': cmd_type,
                          'state': 'done', 'at': self.clock()}
                if sensitive:
                    record['type'] = stored_type if stored_type in _SENSITIVE_TYPES else cmd_type
                    record['state'] = 'sensitive_done'
                else:
                    record.update(result=completion.get('result'), error=completion.get('error'))
                self._write(json.loads(json.dumps(record, default=str)))
        except OSError as error:
            logger.warning('[Agent] call journal write failed: %s', error)


__all__ = ['CallJournal', 'journal_dir']
