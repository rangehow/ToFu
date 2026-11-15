"""The owner's opportunity registry and promised -> delivered ledger.

Owner: docs/ROUTER.md R10 (portfolio). The Global Router records opportunities;
the scheduled review starts the ones authority allows; every started initiative
ends with a measured outcome or an explicit negative result. One record per
opportunity in portable record CAS (namespace ``portfolio`` of
contracts/router_v1.schema.json), keyed by owner, so the registry survives a
restart and another owner never reads it. The record itself is the ledger:
``initiative.promise`` is frozen at start, ``outcome`` at close, so promised and
delivered can never drift apart. There is no second list beside the board, the
digest or the cross-project view. ``deferral`` records the latest measured
reason a review did not start a proposal (a held provider route, a full or
unreadable GPU queue, a start the Project Router did not accept); a definite
non-acceptance returns a started record to ``proposed`` and advances
``startAttempt``, so the next start is a fresh forward, never a replay of the
failed one.

Entry points: ``RouterPortfolio.propose|revise|approve|begin|dispatched|release|
defer|settle|close|read|open|page_closed|close_expired`` and ``kill_triggered``.
Dependencies: router.contract (schema validation), storage record CAS.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import uuid

from lib.identity import require_user_id
from lib.storage.errors import StorageError

from ..contract import NAMESPACES, validate

NAMESPACE = NAMESPACES['portfolio']
TERMINAL = ('delivered', 'negative', 'dropped')
# What a replay of propose/revise must repeat; state and timestamps may advance.
FROZEN = ('projectPath', 'title', 'problem', 'kind', 'source', 'evidence', 'value',
          'validation', 'approval', 'dependencies', 'killCondition', 'deadline', 'instructionId')


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def kill_triggered(condition: dict, metric_value) -> bool:
    """Whether the measured metric satisfies the falsifiable stop rule."""
    if metric_value is None:
        return False
    comparator = condition['comparator']
    threshold = condition['threshold']
    return {'<': metric_value < threshold, '<=': metric_value <= threshold,
            '>': metric_value > threshold, '>=': metric_value >= threshold,
            '==': metric_value == threshold}[comparator]


def _page_rows() -> int:
    from lib.project_dispatch.history import PAGE_ROWS
    return PAGE_ROWS


class RouterPortfolio:
    """Owner-scoped opportunity records (proposals, initiatives, ledger)."""

    def __init__(self, owner: int, *, client=None, clock=None):
        self.owner = require_user_id(owner, context='router portfolio')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.clock = clock or (lambda: datetime.now())
        self.prefix = f'{self.owner}:'

    def _now(self) -> int:
        return int(self.clock().timestamp() * 1000)

    def _key(self, opportunity_id: str) -> str:
        return self.prefix + opportunity_id

    def _row(self, opportunity_id: str) -> dict | None:
        key = self._key(opportunity_id)
        row = self.client().query('record.get', {'namespace': NAMESPACE, 'key': key})
        if row is None:
            return None
        try:
            value = validate(row['value'], 'portfolioOpportunity')
            if value['owner'] != self.owner or type(row['version']) is not int or row['version'] < 1:
                raise ValueError('Portfolio record escaped its owner')
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError('database_integrity', 'Invalid portfolio record') from error
        row['key'] = key
        return row

    def read(self, opportunity_id: str) -> dict:
        row = self._row(opportunity_id)
        if row is None:
            raise ValueError(f'Unknown opportunity {opportunity_id}')
        return deepcopy(row['value'])

    def state_of(self, opportunity_id: str) -> str | None:
        """An opportunity's current state, closed ones included; None when unknown."""
        row = self._row(opportunity_id)
        return None if row is None else row['value']['state']

    def _put(self, key: str, value: dict, version: int, command: str) -> None:
        self.client(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': key, 'value': value, 'expected_version': version,
        }, command)

    def _transition(self, opportunity_id: str, change) -> dict:
        """CAS one state change; ``change`` returns the next value or None (no-op)."""
        while True:
            row = self._row(opportunity_id)
            if row is None:
                raise ValueError(f'Unknown opportunity {opportunity_id}')
            value = change(deepcopy(row['value']))
            if value is None:
                return deepcopy(row['value'])
            value['updatedAt'] = self._now()
            try:
                self._put(row['key'], validate(value, 'portfolioOpportunity'), row['version'],
                          'portfolio:' + uuid.uuid4().hex)
                return value
            except StorageError as error:
                if error.code != 'database_conflict':
                    raise

    def _list(self, prefix: str, cursor: str | None, limit: int) -> tuple[list[dict], str | None]:
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError('Invalid cursor')
        rows = self.client().query('record.list', {
            'namespace': NAMESPACE, 'prefix': prefix, 'after_key': prefix + cursor if cursor else '',
            'limit': limit + 1})
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise StorageError('database_integrity', 'Invalid portfolio page')
        values = []
        for row in rows[:limit]:
            key = row.get('key') if isinstance(row, dict) else None
            if not isinstance(key, str) or not key.startswith(prefix):
                raise StorageError('database_integrity', 'Portfolio page escaped its owner')
            try:
                value = validate(row['value'], 'portfolioOpportunity')
            except (KeyError, TypeError, ValueError) as error:
                raise StorageError('database_integrity', 'Invalid portfolio record') from error
            if value['owner'] != self.owner:
                raise StorageError('database_integrity', 'Portfolio record escaped its owner')
            values.append((key[len(prefix):], value))
        next_cursor = values[-1][0] if len(rows) > limit else None
        return values, next_cursor

    def open(self) -> list[dict]:
        """Every proposed and started opportunity (the review's whole candidate set)."""
        values, cursor = self._list(self.prefix, None, _page_rows())
        while cursor is not None:
            page, cursor = self._list(self.prefix, cursor, _page_rows())
            values.extend(page)
        return [value for _, value in values if value['state'] not in TERMINAL]

    def page_closed(self, *, cursor: str | None = None) -> dict:
        values, next_cursor = self._list(self.prefix, cursor, _page_rows())
        return {'closed': [value for _, value in values if value['state'] in TERMINAL],
                'closed_cursor': next_cursor}

    def propose(self, arguments: dict) -> dict:
        """Record a new opportunity. Idempotent per (owner, idempotency_key)."""
        value = self._build(arguments)
        opportunity_id = 'po-' + digest([self.owner, arguments['idempotency_key']])[:32]
        value.update({'version': 1, 'owner': self.owner, 'opportunityId': opportunity_id,
                      'state': 'proposed', 'createdAt': self._now(), 'updatedAt': self._now(),
                      'initiative': None, 'outcome': None, 'deferral': None, 'startAttempt': 0})
        existing = self._row(opportunity_id)
        if existing is None:
            try:
                self._put(self._key(opportunity_id), validate(value, 'portfolioOpportunity'), 0,
                          'portfolio:' + digest(opportunity_id))
                return value
            except StorageError as error:
                if error.code != 'database_conflict':
                    raise
                existing = self._row(opportunity_id)
                if existing is None:
                    raise
        if any(existing['value'][name] != value[name] for name in FROZEN):
            raise ValueError('idempotency_key was reused for a different opportunity')
        return deepcopy(existing['value'])

    def revise(self, arguments: dict) -> dict:
        """Revise an open proposal; a started initiative's promise is immutable."""
        value = self._build(arguments, opportunity_id=arguments['opportunity_id'])
        current = self.read(arguments['opportunity_id'])
        if current['state'] != 'proposed':
            raise ValueError('Only a proposed opportunity can be revised; a started initiative is measured, not rewritten')
        # A failed start left a durable forward under its attempt's command id;
        # the revision keeps the counter so the next start never reuses it.
        value.update({'version': 1, 'owner': self.owner, 'opportunityId': arguments['opportunity_id'],
                      'state': 'proposed', 'createdAt': current['createdAt'], 'initiative': None,
                      'outcome': current['outcome'], 'deferral': None,
                      'startAttempt': current['startAttempt']})
        return self._transition(arguments['opportunity_id'], lambda _: value)

    def _build(self, arguments: dict, *, opportunity_id: str | None = None) -> dict:
        from lib.router.forwarding import _target_project
        deadline = None
        if arguments.get('deadline'):
            try:
                deadline = int(datetime.fromisoformat(arguments['deadline'].replace('Z', '+00:00'))
                               .timestamp() * 1000)
            except (TypeError, ValueError) as error:
                raise ValueError('deadline must be an ISO date-time') from error
        validation = arguments['validation']
        gpu_queue = validation.get('gpu_queue')
        if validation['gpu_hours'] > 0 and not gpu_queue:
            raise ValueError('A validation that needs GPU hours names its gpu_queue (connection, queue, '
                             'gpu_count): its start waits for that queue\u2019s measured capacity')
        kill = arguments['kill_condition']
        return {
            'projectPath': _target_project({'project_path': '', 'user_id': self.owner},
                                           arguments['project_path']),
            'title': arguments['title'], 'problem': arguments['problem'],
            'kind': arguments.get('kind', 'project'), 'source': arguments['source'],
            'evidence': list(arguments['evidence']),
            'value': dict(arguments['value']),
            'validation': {'step': validation['step'],
                           'cost': {'gpuHours': validation['gpu_hours'],
                                    'modelCny': validation['model_cny'],
                                    'wallHours': validation['wall_hours']},
                           'gpuQueue': None if not gpu_queue else {
                               'connection': gpu_queue['connection'], 'queue': gpu_queue['queue'],
                               'gpuType': gpu_queue.get('gpu_type'), 'gpuCount': gpu_queue['gpu_count']}},
            'approval': {'required': bool(arguments['approval_required']),
                         'reason': arguments['approval_reason'],
                         'grantedTurnId': None, 'grantedAt': None},
            'dependencies': list(arguments.get('dependencies') or []),
            'killCondition': {'metric': kill['metric'], 'comparator': kill['comparator'],
                              'threshold': kill['threshold'], 'withinHours': kill['within_hours']},
            'deadline': deadline,
            'instructionId': arguments.get('instruction_id'),
        }

    def approve(self, opportunity_id: str, *, source_turn: dict) -> dict:
        """The owner's own message approves an opportunity that otherwise waits."""
        from lib.turn_initiation import require_human_turn
        require_human_turn(source_turn)
        turn_id = source_turn.get('turnId')
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError('The approving message has no Turn ID')
        if self.read(opportunity_id)['state'] != 'proposed':
            raise ValueError('Only a proposed opportunity can be approved')

        def change(value):
            approval = dict(value['approval'], grantedTurnId=turn_id, grantedAt=self._now())
            return dict(value, approval=approval)
        return self._transition(opportunity_id, change)

    def begin(self, opportunity_id: str, *, started_by: str, review_id: str | None,
              admission: dict) -> dict:
        """Freeze the promised outcome and why it starts before any start effect.

        The ledger's promised side and the admission facts (rank, authority,
        measured resources) are durable before the forward is sent. The trigger
        names this start attempt, so its forward is idempotent on retry and
        distinct from any earlier attempt the Project Router did not accept.
        """
        if started_by not in ('review', 'router'):
            raise ValueError('started_by must be review or router')

        def change(value):
            if value['state'] != 'proposed':
                raise ValueError(f'Opportunity {opportunity_id} is {value["state"]}; closed or started work is never restarted')
            promise = {'amount': value['value']['amount'], 'unit': value['value']['unit'],
                       'probability': value['value']['probability'], 'metric': value['killCondition']['metric'],
                       'dueAt': self._now() + int(value['killCondition']['withinHours'] * 3_600_000)}
            trigger = f'portfolio:{opportunity_id}:{value["startAttempt"]}'
            initiative = {'startedAt': self._now(), 'startedBy': started_by, 'reviewId': review_id,
                          'promise': promise, 'trigger': trigger, 'admission': admission,
                          'commandId': None, 'disposition': None, 'dispatchedAt': None,
                          'request': None}
            return dict(value, state='started', initiative=initiative, deferral=None)
        return self._transition(opportunity_id, change)

    def dispatched(self, opportunity_id: str, *, command_id: str, disposition: str,
                   request: dict | None) -> dict:
        """Record that the Project Router accepted the start, and the request it opened."""
        if disposition not in ('started', 'steered', 'queued'):
            raise ValueError('Invalid dispatch disposition')

        def change(value):
            if value['state'] != 'started' or value['initiative'] is None:
                raise ValueError('Only a started initiative records its dispatch')
            initiative = dict(value['initiative'], commandId=command_id, disposition=disposition,
                              dispatchedAt=self._now(), request=request)
            return dict(value, initiative=initiative)
        return self._transition(opportunity_id, change)

    def release(self, opportunity_id: str, *, deferral: dict) -> dict:
        """Return a start the Project Router did not accept to ``proposed``.

        Nothing runs for it, so it is not an initiative and promises nothing;
        the recorded reason is why it waits, and the next start is a new attempt.
        """
        def change(value):
            if value['state'] != 'started' or value['initiative']['disposition'] is not None:
                raise ValueError('Only an unaccepted start returns to proposed')
            return dict(value, state='proposed', initiative=None, deferral=deferral,
                        startAttempt=value['startAttempt'] + 1)
        return self._transition(opportunity_id, change)

    def defer(self, opportunity_id: str, *, deferral: dict) -> dict:
        """Record the measured reason a review did not start this proposal."""
        def change(value):
            if value['state'] != 'proposed':
                raise ValueError('Only a proposal is deferred')
            return dict(value, deferral=deferral)
        return self._transition(opportunity_id, change)

    def settle(self, opportunity_id: str, arguments: dict) -> dict:
        """Close a started initiative with its measured outcome.

        A measurement satisfying the kill condition is an explicit negative
        result, not a delivery.
        """
        outcome = arguments['outcome']

        def change(value):
            if value['state'] != 'started' or value['initiative'] is None:
                raise ValueError('Only a started initiative can settle; record a proposal with propose, or drop it')
            promise = value['initiative']['promise']
            killed = kill_triggered(value['killCondition'], outcome.get('metric_value'))
            measured = {'verdict': 'negative' if killed else 'delivered',
                        'reason': 'kill_condition' if killed else 'measured',
                        'amount': outcome['amount'], 'metricValue': outcome.get('metric_value'),
                        'evidence': list(outcome['evidence']), 'note': outcome.get('note', ''),
                        'closedAt': self._now()}
            return dict(value, state=measured['verdict'], outcome=measured)
        return self._transition(opportunity_id, change)

    def close(self, opportunity_id: str, *, verdict: str, reason: str, note: str = '',
              evidence: list[str] | None = None) -> dict:
        """Explicit closure without a measurement: dropped, or negative (no value)."""
        if verdict not in ('negative', 'dropped'):
            raise ValueError('An unmeasured close is negative or dropped; a measured one uses settle')

        def change(value):
            if value['state'] in TERMINAL:
                return None
            closed = {'verdict': verdict, 'reason': reason, 'amount': None, 'metricValue': None,
                      'evidence': list(evidence or []), 'note': note, 'closedAt': self._now()}
            return dict(value, state=verdict, outcome=closed)
        return self._transition(opportunity_id, change)

    def close_expired(self) -> list[dict]:
        """The review's deterministic kill sweep: proposals past their value deadline,
        and started initiatives whose promised due time passed with no outcome."""
        now = self._now()
        closed = []
        for value in self.open():
            if value['state'] == 'proposed' and value['deadline'] is not None and value['deadline'] <= now:
                closed.append(self.close(value['opportunityId'], verdict='dropped', reason='expired',
                                         note='The opportunity deadline passed before it was started.'))
            elif value['state'] == 'started' and value['initiative']['promise']['dueAt'] <= now:
                closed.append(self.close(value['opportunityId'], verdict='negative', reason='no_outcome_by_due',
                                         note='No measured outcome by the promised due time; the kill '
                                              'condition closes this initiative instead of letting it run on.'))
        return closed


__all__ = ['NAMESPACE', 'RouterPortfolio', 'TERMINAL', 'kill_triggered', 'digest']
