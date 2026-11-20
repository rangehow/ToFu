"""Standalone synthetic work/parity probe; run with Python -I -S -B, not pytest.

The complete _model_context owner executes with explicit imported fixture ports.
No lib package initializer, SQL adapter, conftest, or installed dependency loads.
Only its source file may be opened; SQLite may connect only to :memory:.
Projection, continuation, validation and resume dependencies are substituted:
this verifies owner-level query reuse, not those dependencies or durability.
"""
import __future__
import builtins
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace


SOURCE = Path(__file__).absolute().parents[2] / 'lib/storage_authority_sql/operations_pkg/_model_context.py'
EXPECTED_READS = int(sys.argv[1]) if len(sys.argv) > 1 else 1
RECEIPTS = []


class Confined(RuntimeError):
    pass


def audit(event, args):
    if event == 'open':
        path, mode, flags = args
        if path != str(SOURCE) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise Confined('non-source file access refused')
    elif event == 'sqlite3.connect' and args[0] != ':memory:':
        raise Confined('non-memory SQLite refused')
    elif event == 'import':
        raise Confined('unprepared import refused')
    elif event.startswith(('socket.', 'subprocess.', 'ctypes.', 'os.', '_thread.')):
        raise Confined('external effect refused')


class FixtureStorageError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def forbidden(*args, **kwargs):
    raise Confined('unexpected dependency call')


def required_text(payload, key, maximum=512):
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise FixtureStorageError('database_protocol_error', f'Invalid {key} in storage request')
    return value


def integer(payload, key, *, default=None, minimum=None, maximum=None):
    value = payload.get(key, default)
    if (type(value) is not int or minimum is not None and value < minimum
            or maximum is not None and value > maximum):
        raise FixtureStorageError('database_protocol_error', f'Invalid {key} in storage request')
    return value


def witness(turn):
    return [turn['turnId'], hashlib.sha256(json.dumps(turn, sort_keys=True).encode()).hexdigest()]


def public_turn(session, row):
    session.projections.append(row['turn_id'])
    return json.loads(row['public_json'])


def projection_page(turn, page):
    assert page == 0
    return {'pageCount': 1, 'turn': turn}


def owner_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name in ('__future__', 'hashlib', 'json', 'collections.abc'):
        return builtins.__import__(name, globals, locals, fromlist, level)
    ports = {
        (0, 'lib.storage.errors'): SimpleNamespace(StorageError=FixtureStorageError),
        (0, 'lib.storage.model_continuation'): SimpleNamespace(settled_witness=witness),
        (0, 'lib.storage.turn_projection_pages'): SimpleNamespace(turn_projection_page=projection_page),
        (1, '_common'): SimpleNamespace(_required_text=required_text, _integer=integer),
        (1, '_turns_core'): SimpleNamespace(_turn_public=public_turn,
                                           _optional_row_value=lambda row, key: row.get(key)),
        (1, '_model_continuation'): SimpleNamespace(
            continuation_observe=lambda session, identity, **kw: (session.cached is not None, deepcopy(session.cached))),
        (1, '_archives'): SimpleNamespace(resume_selection_inputs=lambda session, identity: []),
        (1, '_turns_write'): SimpleNamespace(
            _attempt_later_turns=lambda *args: None,
            _attempt_resume_anchor=lambda *args: {'fixture': 'anchor'},
            _attempt_resume_projection=lambda session, target, public, *args: deepcopy(public['projection']),
            _attempt_source=lambda *args: ('attempt-target', {'status': 'failed'})),
    }
    if (level, name) not in ports:
        raise Confined(f'unexpected owner import: {level}:{name}')
    return ports[level, name]


def load_owner():
    module = ModuleType('fixture_model_context')
    module.__dict__['__builtins__'] = dict(vars(builtins), __import__=owner_import)
    with open(str(SOURCE), encoding='utf-8') as handle:
        source = handle.read()
    exec(compile(source, str(SOURCE), 'exec'), module.__dict__)
    assert not any(name == 'lib' or name.startswith('lib.') for name in sys.modules)
    return module


def prior_offer(self, header, *, target):
    """Pre-change page materialization, including the repeated target SELECT."""
    if self.after is not None:
        if header['turn_id'] == self.after:
            if target:
                raise FixtureStorageError('database_protocol_error', 'context page cursor is outside the selected suffix')
            self.after = None
        return
    if self.full:
        return
    row = self.session.fetch_one(
        'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
        'AND user_id=? AND turn_id=?', (self.conv, self.owner, header['turn_id']))
    if row is None:
        raise FixtureStorageError('database_internal', 'Model context Turn disappeared')
    turn = self.fixture_module._context_row(public_turn(self.session, row))
    size = len(json.dumps(turn, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    if self.turns and self.bytes + size > self.limit:
        self.full = True
        return
    self.bytes += size
    self.turns.append(turn)


class Session:
    def __init__(self, rows, cached=None):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            'CREATE TABLE storage_conversation_turns ('
            'conversation_id TEXT,user_id INTEGER,turn_id TEXT,lane_id TEXT,'
            'ordinal INTEGER,parent_turn_id TEXT,status TEXT,current_attempt_id TEXT,'
            'projection_revision INTEGER,model_context_display_offset INTEGER,public_json TEXT)')
        for row in reversed(rows):
            self.connection.execute(
                'INSERT INTO storage_conversation_turns VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('fixture', 1, row['turnId'], row['laneId'], row['ordinal'],
                 row['parentTurnId'], row['status'], row['currentAttemptId'],
                 row['projectionRevision'], row.get('modelContextDisplayOffset'), json.dumps(row)))
        self.cached = cached
        self.queries, self.projections = [], []
        self.connection.execute('BEGIN')

    def fetch_one(self, sql, params):
        self.queries.append((sql, params))
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self, sql, params):
        self.queries.append((sql, params))
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def close(self):
        assert self.connection.in_transaction
        self.connection.rollback()
        self.connection.close()


def rows_fixture():
    rows = []
    for index, projection in enumerate((
            {'content': 'request 雪', 'toolRounds': [], 'unknown': {'nested': [False, 0, None]}},
            {'content': None, 'thinking': '', 'attachments': []},
            {'toolRounds': [{'calls': [{'id': 'call-1', 'result': ['value', None]}]}]},
    )):
        rows.append({'turnId': ('prior', 'terminal', 'target')[index], 'laneId': 'main',
                     'ordinal': (index + 1) * 10, 'parentTurnId': None if index == 0 else rows[-1]['turnId'],
                     'status': 'failed' if index == 2 else 'completed',
                     'currentAttemptId': 'attempt-target' if index == 2 else None,
                     'projectionRevision': index + 3, 'projection': projection})
    rows.append(dict(rows[0], turnId='branch', laneId='branch', ordinal=15))
    return rows


def proof(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update((json.dumps([row['turnId'], row['parentTurnId'], row['status'],
                                  row['currentAttemptId'], row['projectionRevision']],
                                 ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
    return {'version': 2, 'count': len(rows), 'digest': digest.hexdigest()}


def invoke(module, rows, payload, cached=None, *, legacy=False, full_after_first=False):
    session = Session(rows, cached)
    offer, init = module._Page.offer, module._Page.__init__
    module._Page.fixture_module = module
    if legacy:
        module._Page.offer = prior_offer
    if full_after_first:
        def charged(page, *args):
            init(page, *args)
            # Exercise page exhaustion with synthetic prior byte accounting,
            # without changing the production limit or allocating a huge Turn.
            page.bytes = page.limit
        module._Page.__init__ = charged
    try:
        try:
            result = module.read_model_context(session, payload)
        except FixtureStorageError as exc:
            result = {'error': [exc.code, exc.message]}
        queries, projections = list(session.queries), list(session.projections)
    finally:
        module._Page.offer, module._Page.__init__ = offer, init
        session.close()
    return result, queries, projections


def check(module, name, *, request=None, cached=None, ids=None, status=None,
          covered=0, error=None, full_after_first=False, target_selected=True):
    rows = rows_fixture()
    payload = {'conversation_id': 'fixture', 'user_id': 1, 'turn_id': 'target'} | (request or {})
    before = invoke(module, rows, payload, cached, legacy=True, full_after_first=full_after_first)
    after = invoke(module, rows, payload, cached, full_after_first=full_after_first)
    assert after[0] == before[0], (name, 'output parity')
    assert after[2] == before[2], (name, 'projection order/count')
    target_query = ('SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
                    'AND user_id=? AND turn_id=?', ('fixture', 1, 'target'))
    old_count, new_count = before[1].count(target_query), after[1].count(target_query)
    if payload['turn_id'] == 'target' and payload['user_id'] == 1:
        assert old_count == (2 if target_selected else 1), (name, old_count)
        assert new_count == (EXPECTED_READS if target_selected else 1), (name, new_count)
    assert len(before[1]) - len(after[1]) == old_count - new_count
    expected_queries = list(before[1])
    if old_count > new_count:
        expected_queries.pop(expected_queries.index(target_query, 1))
    assert after[1] == expected_queries, (name, 'other queries changed')
    if error is not None:
        assert after[0] == {'error': error}, name
    elif ids is not None:
        result = after[0]
        assert [row['turnId'] for row in result['turns']] == ids, name
        assert result['historyProof'] == proof(rows[:2]), name
        assert result['covered'] == covered and result['continuationStatus'] == status, name
        assert result['continuation'] == (cached if covered or status == 'legacy_checkpoint' else None), name
        assert result['nextTurnId'] == (ids[-1] if full_after_first and ids[-1] != 'target' else None), name
        assert result['proofEligible'] is True
        if 'target' in ids:
            projection = result['turns'][-1]['projection']
            assert 'content' not in projection and 'thinking' not in projection
            assert projection['toolRounds'][0]['calls'][0]['result'] == ['value', None]
        if 'terminal' in ids:
            terminal = next(row for row in result['turns'] if row['turnId'] == 'terminal')
            assert terminal['projection'] == {'content': None, 'thinking': '', 'attachments': []}
        if 'continue_plan' not in payload:
            assert 'continuePlan' not in result and 'continueSelectionInputs' not in result
    else:
        assert after[0] is None
    digest = hashlib.sha256(json.dumps(after[0], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    RECEIPTS.append({'case': name, 'targetSelects': [old_count, new_count],
                     'allSelects': [len(before[1]), len(after[1])], 'outputSha256': digest})
    return after[0]


def main():
    assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    sys.addaudithook(audit)
    for operation in (lambda: open('/fixture-forbidden-config'),
                      lambda: sqlite3.connect('/fixture-forbidden.sqlite'),
                      lambda: sys.audit('socket.connect', None, None),
                      lambda: sys.audit('subprocess.Popen', None, None, None, None),
                      lambda: owner_import('lib')):
        try:
            operation()
        except Confined:
            pass
        else:
            raise AssertionError('confinement control escaped')
    module = load_owner()
    full = ['prior', 'terminal', 'target']
    check(module, 'replay-no-checkpoint', ids=full, status='checkpoint_missing')
    check(module, 'cold', request={'replay': False}, ids=full, status='replay_disabled')
    check(module, 'exclusive-cursor', request={'after_turn_id': 'prior'},
          ids=['terminal', 'target'], status='checkpoint_missing')
    check(module, 'cursor-at-target', request={'after_turn_id': 'target'}, target_selected=False,
          error=['database_protocol_error', 'context page cursor is outside the selected suffix'])
    check(module, 'unknown-cursor', request={'after_turn_id': 'absent'}, target_selected=False,
          error=['database_protocol_error', 'context page cursor is outside the selected suffix'])
    check(module, 'missing-target', request={'turn_id': 'absent'}, target_selected=False)
    check(module, 'foreign-owner', request={'user_id': 2}, target_selected=False)
    rows = rows_fixture()
    cached = {'historyProof': proof(rows[:1]), 'turns': [witness(rows[1])],
              'messages': [{'role': 'assistant', 'content': 'retained'}]}
    check(module, 'verified-prefix', cached=cached, ids=['target'], status='available', covered=2)
    check(module, 'refuted-prefix', cached=cached | {'historyProof': proof([])},
          ids=full, status='terminal_changed')
    check(module, 'changed-history', cached=cached | {'historyProof': dict(proof(rows[:1]), digest='0' * 64)},
          ids=full, status='history_changed')
    check(module, 'legacy-prefix', cached={'messages': cached['messages']}, ids=full, status='legacy_checkpoint')
    check(module, 'page-full-before-target', ids=['prior'], status='checkpoint_missing',
          full_after_first=True, target_selected=False)
    check(module, 'paged-resume', request={'after_turn_id': 'terminal'}, ids=['target'],
          status='checkpoint_missing', full_after_first=True)
    plan = {'attempt_id': 'attempt-target', 'projection_revision': 5,
            'history_digest': proof(rows[:2])['digest'], 'selection': True}
    result = check(module, 'cold-continue-selection', request={'replay': False, 'continue_plan': plan},
                   ids=full, status='replay_disabled')
    assert result['continuePlan'] == {'historyDigest': proof(rows[:2])['digest'],
                                      'anchor': {'fixture': 'anchor'}, 'projection': rows[2]['projection']}
    assert result['continueSelectionInputs'] == {
        'version': 1, 'continuationStatus': 'checkpoint_missing', 'covered': 0,
        'continuation': None, 'archives': []}
    check(module, 'stale-continue', request={'replay': False, 'continue_plan': dict(plan, history_digest='0' * 64)},
          error=['turn_projection_stale', 'The Continue source or history changed.'])
    assert not any(name == 'lib' or name.startswith('lib.') for name in sys.modules)
    print(json.dumps({'confinementControls': 5, 'cases': RECEIPTS}, sort_keys=True))
    print('PASS: isolated synthetic owner invariants and query accounting; no timing measurement')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        sys.exit(1)
