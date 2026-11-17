"""Node call journal: a side-effecting command id runs at most once on this node.

Problem: the Home settles a call as ``device_unavailable`` when this node stops
polling mid-command, and a resumed Turn may send the same tool call again
(its command id is the call's idempotency key, ``lib.desktop.routing``). The
in-memory ``RuntimeState.seen`` set dies with the process, so a restarted node
would run the side effect a second time.

Decision: before a command whose tier is not read/control starts, the node
writes ``started`` for its id in a private directory beside its config; on
completion it writes the outcome. A command id that reaches the node again is
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
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

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
                if os.path.getmtime(path) < horizon:
                    os.unlink(path)
            except OSError:
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
        return record if record.get('id') == cmd_id else None

    def _write(self, record: dict) -> None:
        from lib.json_store import write_json_atomic
        write_json_atomic(self._path(record['id']), record, indent=None, mode=0o600)

    def begin(self, cmd: dict):
        """None when the command may run now; else the completion to send instead."""
        cmd_id, cmd_type = cmd['id'], cmd['type']
        if not _journaled(cmd_type, cmd.get('params')):
            return None
        with self.lock:
            record = self._read(cmd_id)
            if record is None:
                self._write({'id': cmd_id, 'type': cmd_type, 'state': 'started',
                             'at': self.clock()})
                return None
        if record.get('state') == 'done':
            logger.info('[Agent] repeated call %s answered from the journal', cmd_id[:12])
            return {'id': cmd_id, 'result': record.get('result'), 'error': record.get('error')}
        return {'id': cmd_id, 'result': None,
                'error': ('device_unavailable: this call already started on this device before '
                          'it lost its connection or restarted; its outcome is unknown and it '
                          'was not run again.')}

    def complete(self, cmd_type: str, params, completion: dict) -> None:
        if not _journaled(cmd_type, params):
            return
        record = {'id': completion['id'], 'type': cmd_type, 'state': 'done', 'at': self.clock(),
                  'result': completion.get('result'), 'error': completion.get('error')}
        try:
            with self.lock:
                self._write(json.loads(json.dumps(record, default=str)))
        except OSError as error:
            logger.warning('[Agent] call journal write failed: %s', error)


__all__ = ['CallJournal', 'journal_dir']
