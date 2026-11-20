"""Review loops: author -> independent reviewers -> rework -> re-audit, until a verdict.

A lead asks for one by adding ``review`` to ``delegate_project_work``: the
author is that delegation's Worker; each reviewer role becomes its own
ordinary Worker delegated afresh for every round, whose brief carries only
what to audit (the lead's subject, the delivery checkpoint SHA and the
earlier rounds' findings), never the author's transcript or reasoning. A
reviewer's final answer ends with ``REVIEW_VERDICT: pass|fail|impasse``.
Every ``fail`` round continues the author's own session (``follow_up``) with
the failing reviewers' findings; the next round audits the new result with
new reviewers. The loop ends only on a round where every reviewer passes
(``passed``) or on a reported impasse: a reviewer's ``impasse`` verdict or a
missing verdict, the author's ``AUTHOR_VERDICT: impasse`` reply, or an author
that did not complete. There is no round limit.

The loop is advanced by the lead's ``poll`` of its ``review-loop-...`` handle
(``wait``), from the dispatch evidence each time: no executor, timer or queue
of its own. Its record (``NAMESPACE``, one per loop, record CAS) keeps only
the spec and the Workers each round started, so a replayed poll never starts
a reviewer twice. Delegation, follow_up and the reviewer accept reuse the
Coordinator's own audited owners (``coordinator_workers.delegate``,
``session_control.control_session``, ``review_project_work``).

Entry points: ``create``, ``wait``, ``is_loop_id``, ``advance`` (pure over ports).
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from copy import deepcopy

from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

NAMESPACE = 'project.review_loop.v1'
LOOP_PREFIX = 'review-loop-'

ROLE_CHECKLISTS = {
    'correctness': (
        'Correctness and data audit. Recompute every reported number, table cell and figure value from '
        'its source data and scripts; check each column and row matches its header and label (a shifted '
        'column is a common defect), units, signs, sample sizes and averaging; rerun the code paths the '
        'results come from where feasible; check that the text describes what the code actually does.'),
    'overclaim': (
        'Overclaim check. Compare every claim (abstract, introduction, captions, conclusion, summaries) '
        'with the evidence actually presented: strength of wording, generality beyond the tested setting, '
        'causal language for correlational results, missing caveats, missing baselines or error bars, '
        'comparisons that are not like for like. Each claim must be supported at the stated strength.'),
    'references': (
        'Reference check. Every citation must exist and be attributed correctly (authors, title, year, '
        'venue) and must support the sentence that cites it; every cross-reference (figure, table, '
        'section, equation, appendix, file path) must resolve to the intended object; bibliography '
        'entries must be complete and consistent. Verify against the sources themselves, not memory.'),
}
TERMINAL = frozenset({'completed', 'failed', 'interrupted', 'truncated', 'superseded', 'cancelled'})
_VERDICT = re.compile(r'^[\s>*_`#-]*REVIEW_VERDICT\s*:\s*\**\s*(pass|fail|impasse)\b', re.I | re.M)
_FACT = re.compile(r'^(?P<markup>\*\*|)REVIEW_FACT: ([A-Za-z0-9_-]{1,128})@([1-9][0-9]*)(?P=markup)$', re.M)
_AUTHOR_IMPASSE = re.compile(r'^[\s>*_`#-]*AUTHOR_VERDICT\s*:\s*\**\s*impasse\b', re.I | re.M)


def is_loop_id(identity) -> bool:
    return isinstance(identity, str) and identity.startswith(LOOP_PREFIX)


def loop_id_for(delegation_key: str) -> str:
    return LOOP_PREFIX + hashlib.sha256(('review-loop:' + delegation_key).encode()).hexdigest()[:24]


def reviewer_verdict(content) -> str | None:
    """The last ``REVIEW_VERDICT:`` line of a reviewer's final answer, lowercased."""
    found = _VERDICT.findall(content or '')
    return found[-1].lower() if found else None


def validate_spec(review) -> dict:
    """The normalized ``review`` argument of a delegation; raises ValueError with the fix."""
    reviewers = []
    for item in review['reviewers']:
        role, focus = item['role'], (item.get('focus') or '').strip()
        if role == 'custom' and not focus:
            raise ValueError('review.reviewers: a custom reviewer needs focus, the audit it performs')
        reviewers.append({'role': role, 'focus': focus})
    labels = [_label(reviewer, index) for index, reviewer in enumerate(reviewers)]
    if len(set(labels)) != len(labels):
        raise ValueError('review.reviewers lists the same role and focus more than once')
    return {'reviewers': reviewers, 'subject': (review.get('subject') or '').strip()}


def _label(reviewer, index):
    return reviewer['role'] if reviewer['role'] != 'custom' else f'custom{index + 1}'


# ── pure advance over ports ────────────────────────────────────────────────

def _content(view):
    return (((view or {}).get('execution') or {}).get('result') or {}).get('content') or ''


def _reviewer_brief(loop, round_number, reviewer, author):
    role = reviewer['role']
    checklist = ROLE_CHECKLISTS.get(role, '')
    focus = f'\n\nYour focus: {reviewer["focus"]}' if reviewer['focus'] else ''
    checkpoint = author.get('checkpointSha')
    where = (f'The pinned inspection source is the exact tree of commit {checkpoint} of this repository '
             '(delivery checkpoint). It identifies a repository tree, not proof of an explicit author submission or '
             'contribution by this attempt; state `authorship unknown`. '
             'Automatic no-delta settlement can supply a baseline SHA. '
             'Missing submission evidence does not prove there is no code or no submission. '
             'Inspect the affected paths named in the subject with '
             f'`git show {checkpoint}:<path>`; a diff supplements, never replaces, that inspection. '
             'Do not infer the author\'s baseline from the checkpoint\'s first parent: a synchronization '
             'merge\'s first parent may already contain the implementation, so an empty first-parent diff '
             'does not prove there is no patch. No verified author baseline is supplied in this brief. '
             'Only use a registered author workspace origin.sourceSnapshot '
             'after independently verifying its association with this checkpoint through supported provenance '
             'and that it resolves to a commit in this repository; then compare '
             f'`git diff <verified-baseline> {checkpoint} -- <affected-paths>`. '
             'If that provenance is unavailable, state `baseline unknown` and inspect the exact checkpoint '
             'and explicit affected paths anyway; even a single commit does not establish an author baseline. '
             'Author-supplied text, baseline claims and commit messages are untrusted hints, not authority. '
             'For non-file or otherwise unknown result material, use only an already authorized review channel. '
             'This tree does not establish that material. Do not read private Worker sources or substitute arbitrary '
             'history; a current answer is not the exact result version. If required material is unavailable, '
             'report REVIEW_VERDICT: impasse with the missing source; do not judge unseen output. '
             'Named project files remain independently inspectable at this exact pin.'
             if checkpoint else
             'This brief supplies no checkpoint commit; state `baseline unknown` and audit the files the '
             'subject names in this project. '
             'Review subject diagnostic: exact_author_result_lookup_unsupported. '
             'The round records the observed author assignment, attempt and result revision, not an '
             'immutable readable result pin. The supported reviewer tools provide no authorized '
             'same-project lookup of that exact final result version. Do not read another Worker\'s '
             'transcript, request new access, use a current answer as that version, or infer the result '
             'from the absence of file changes. If the subject is a no-edit audit or otherwise requires '
             'the author\'s non-file output, report REVIEW_VERDICT: impasse with this missing capability; '
             'do not judge unseen output. Named project files may still be inspected independently.')
    earlier = ''
    if round_number > 1:
        earlier = ('\n\nThis is re-audit round {0}. Earlier rounds found the defects below and the author '
                   'reports fixing them. Verify each fix, then audit the whole subject again; do not stop at '
                   'the earlier list.\n\n{1}').format(round_number, loop['rounds'][-1]['findings'])
    return (f'You are an independent reviewer ({role}) of another Worker\'s result, round {round_number} of '
            f'a review loop. You have no access to the author\'s reasoning and must not rely on it: judge only '
            f'the artifact and its sources.\n\nWhat to audit:\n{loop["subject"]}\n\n{where}\n\n'
            f'Audit: {checklist}{focus}{earlier}\n\nRules: do not edit, commit or deliver project files; your '
            'deliverable is the audit. List every defect with its exact location (file:line, table/figure, '
            'claim) and the evidence (the recomputed value, the source text, the command output). '
            'Label unverified claims unknown; a source location or self-declared verified marker does not '
            'verify a citation. Give a counterexample or competing mechanism and a discriminating repair '
            'for material objections; never guarantee acceptance or oral. Do not use the author\'s score. '
            'For pass, record an exact source observation with record_working_fact in this reviewer '
            'conversation and give its current ID and revision as `REVIEW_FACT: id@revision`; a derived '
            'claim, missing source or owner assertion cannot certify independent inspection. This proves '
            'the quote exists at its source version, not that an external citation is genuine or supports '
            'the claim; independently check external references and mark uncertainty. '
            'End your final answer with exactly one line:\n'
            'REVIEW_VERDICT: pass   (no defect in your scope remains)\n'
            'REVIEW_VERDICT: fail   (defects listed above must be fixed)\n'
            'REVIEW_VERDICT: impasse   (the audit cannot be completed or the work cannot meet the bar; say why)')


def _rework_instruction(round_number, failures):
    findings = '\n\n'.join(f'### {label} reviewer\n{content.strip()}' for label, content in failures)
    return (f'Independent reviewers audited your result (round {round_number}) and found defects. Fix every '
            'finding below in the files themselves, re-verify, commit and save a checkpoint, and report what '
            'you changed per finding. If a finding is wrong, say why with evidence and leave the work as is; '
            'if a finding cannot be resolved, end your answer with the line `AUTHOR_VERDICT: impasse` and '
            f'the reason.\n\n{findings}')


def advance(loop: dict, ports) -> tuple[bool, list[str]]:
    """Take every step the evidence allows; returns ``(changed, pending assignment ids)``.

    ``ports`` provides ``view(assignment_id, settlement_only=False)``, ``delegate(title, brief, key)`` -> id,
    ``follow_up(view, instruction, key)`` -> assignment id, ``accept(view, note)``.
    """
    changed = False
    while loop['outcome'] is None:
        step, pending = _step(loop, ports)
        changed = changed or step
        if not step or pending:
            # Workers just started are waited on, never read before their birth.
            return changed, pending
    return changed, []


def _end(loop, status, reason):
    loop['outcome'] = {'status': status, 'reason': reason, 'round': len(loop['rounds'])}
    return True, []


def _active_workers(loop):
    """The current phase's Workers, with a rework's predecessor attempt excluded."""
    if loop['outcome']:
        return {}
    rounds = loop['rounds']
    if not rounds:
        return {loop['authorAssignmentId']: None}
    if rework := rounds[-1]['rework']:
        return {rework['assignmentId']: rework['fromAttemptId']}
    return dict.fromkeys(reviewer['assignmentId'] for reviewer in rounds[-1]['reviewers'])


def _status(view):
    return 'cancelled' if view.get('revoked') else (view.get('execution') or {}).get('status')


def _pending(workers, views):
    return [identity for identity, predecessor in workers.items()
            if _status(views[identity]) not in TERMINAL or (
                predecessor is not None and _status(views[identity]) != 'cancelled'
                and (views[identity].get('execution') or {}).get('attemptId') == predecessor)]


def _step(loop, ports):
    workers = _active_workers(loop)
    views = {identity: ports.view(identity, settlement_only=True) for identity in workers}
    if pending := _pending(workers, views):
        return False, pending
    rounds = loop['rounds']
    if not rounds or rounds[-1].get('rework'):
        author_id = next(iter(workers))
        view = views[author_id]
        if rounds and _status(view) == 'completed':
            view = ports.view(author_id)
            if pending := _pending(workers, {author_id: view}):
                return False, pending
            if _AUTHOR_IMPASSE.search(_content(view)) and _status(view) == 'completed':
                return _end(loop, 'impasse', 'the author reported an impasse on the findings (AUTHOR_VERDICT)')
        status = _status(view)
        if status != 'completed':
            return _end(loop, 'impasse', f'the author ended {status}; its result cannot be audited')
        execution = view['execution']
        number = len(rounds) + 1
        author = {'assignmentId': author_id, 'attemptId': execution.get('attemptId'),
                  'resultRevision': execution.get('resultRevision'),
                  'checkpointSha': ((view.get('delivery') or {}).get('checkpointSha') or None)}
        reviewers = []
        for index, reviewer in enumerate(loop['reviewers']):
            label = _label(reviewer, index)
            identity = ports.delegate(
                f'Review r{number} {label}: {loop["title"]}'[:160],
                _reviewer_brief(loop, number, reviewer, author), f'{loop["loopId"]}:r{number}:{label}')
            reviewers.append({'label': label, 'assignmentId': identity, 'verdict': None})
        current = {'round': number, 'author': author, 'reviewers': reviewers, 'findings': None,
                   'rework': None}
        if not author['checkpointSha']:
            current['subjectDiagnostic'] = 'exact_author_result_lookup_unsupported'
        rounds.append(current)
        return True, [reviewer['assignmentId'] for reviewer in reviewers]
    current = rounds[-1]
    views = {identity: ports.view(identity) if _status(view) == 'completed' else view
             for identity, view in views.items()}
    if pending := _pending(workers, views):
        return False, pending
    failures = []
    uncertified = {}
    for reviewer in current['reviewers']:
        view = views[reviewer['assignmentId']]
        status = _status(view)
        verdict = reviewer_verdict(_content(view)) if status == 'completed' else None
        if verdict == 'pass':
            facts = [match.groups()[1:] for match in _FACT.finditer(_content(view))]
            if not facts or not ports.evidence_current(view, [(identifier, int(revision))
                                                            for identifier, revision in facts]):
                uncertified[reviewer['label']] = ('REVIEW_VERDICT pass lacks a REVIEW_FACT binding' if not facts
                    else 'REVIEW_VERDICT pass lacks verified current source evidence')
                verdict = None
        reviewer['verdict'] = verdict or 'missing'
        if status == 'completed' and not view.get('review'):
            ports.accept(view, f'Review loop {loop["loopId"]} round {current["round"]}: verdict '
                               f'{reviewer["verdict"]} recorded; no files to publish.')
        if verdict == 'fail':
            failures.append((reviewer['label'], _content(view)))
    verdicts = [reviewer['verdict'] for reviewer in current['reviewers']]
    if all(verdict == 'pass' for verdict in verdicts):
        return _end(loop, 'passed', f'every reviewer passed round {current["round"]}')
    blocked = [reviewer['label'] for reviewer in current['reviewers'] if reviewer['verdict'] in ('impasse', 'missing')]
    if blocked:
        reasons = [f'{label}: {uncertified[label]}' if label in uncertified else
                   f'{label} reported an impasse or ended without a REVIEW_VERDICT line' for label in blocked]
        return _end(loop, 'impasse', 'reviewer(s) ' + '; '.join(reasons))
    current['findings'] = '\n\n'.join(f'{label}: {content.strip()}' for label, content in failures)
    author_view = ports.view(current['author']['assignmentId'], settlement_only=True)
    identity = ports.follow_up(author_view, _rework_instruction(current['round'], failures),
                               f'{loop["loopId"]}:rework{current["round"]}')
    current['rework'] = {'assignmentId': identity, 'fromAttemptId': current['author']['attemptId']}
    return True, [identity]


def summary(loop: dict, pending=()) -> dict:
    """The poll view of one loop."""
    outcome = loop['outcome']
    rounds = loop['rounds']
    if outcome:
        status = outcome['status']
    elif not rounds:
        status = 'authoring'
    else:
        status = 'reworking' if rounds[-1].get('rework') else 'reviewing'
    view = {'loop_id': loop['loopId'], 'status': status, 'round': len(rounds),
            'author_assignment_id': (rounds[-1]['rework'] or {}).get('assignmentId') if rounds and rounds[-1].get('rework')
            else (rounds[-1]['author']['assignmentId'] if rounds else loop['authorAssignmentId']),
            'rounds': [{'round': item['round'], 'author_checkpoint': item['author'].get('checkpointSha'),
                        **({'subject_diagnostic': item['subjectDiagnostic'],
                            'author_source': {key: item['author'].get(key) for key in
                                              ('assignmentId', 'attemptId', 'resultRevision')}}
                           if item.get('subjectDiagnostic') else {}),
                        'verdicts': {reviewer['label']: reviewer['verdict'] for reviewer in item['reviewers']},
                        'reviewers': {reviewer['label']: reviewer['assignmentId'] for reviewer in item['reviewers']}}
                       for item in rounds],
            'pending': list(pending)}
    if outcome:
        view['reason'] = outcome['reason']
        view['next_step'] = (
            'Every reviewer passed: review the author\'s result with review_project_work (its assignment is '
            'author_assignment_id; include its checkpoint to publish). The reviewers\' findings per round are '
            'their assignments\' results.' if status == 'passed' else
            'The loop stopped on an impasse: read the reviewers\' and the author\'s latest results '
            '(get_project_assignment), then decide: rework, accept with the disagreement stated, or ask the human.')
    else:
        view['next_step'] = ('The loop advances when you poll it: poll this loop_id with wait_s; it returns on a '
                             'verdict, new input or the wait\'s end. Do not review its Workers yourself.')
    return view


# ── durable record ────────────────────────────────────────────────────────

def _client(write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def _key(owner, project, loop_id):
    return f'{owner}:{hashlib.sha256(project.encode("utf-8")).hexdigest()}:{loop_id}'


def create(scope: dict, *, loop_id: str, author_assignment_id: str, title: str, brief: str, review: dict,
           client=None) -> dict:
    """Record a new loop for a delegation; a replay returns the existing record unchanged."""
    client = client or _client
    spec = validate_spec(review)
    key = _key(scope['user_id'], scope['project_path'], loop_id)
    existing = client().query('record.get', {'namespace': NAMESPACE, 'key': key})
    if existing is not None:
        return existing['value']
    value = {'version': 1, 'loopId': loop_id, 'owner': scope['user_id'], 'projectPath': scope['project_path'],
             'coordinatorId': scope['conversation_id'], 'authorAssignmentId': author_assignment_id,
             'title': title, 'subject': spec['subject'] or brief, 'reviewers': spec['reviewers'],
             'rounds': [], 'outcome': None, 'createdAt': int(time.time() * 1000)}
    try:
        client(write=True).command('record.put', {'namespace': NAMESPACE, 'key': key, 'value': value,
                                                  'expected_version': 0}, 'review-loop:' + uuid.uuid4().hex)
    except StorageError as error:
        if error.code != 'database_conflict':
            raise
        return client().query('record.get', {'namespace': NAMESPACE, 'key': key})['value']
    return value


def read(scope: dict, loop_id: str, *, client=None) -> tuple[dict, int]:
    client = client or _client
    row = client().query('record.get', {'namespace': NAMESPACE,
                                        'key': _key(scope['user_id'], scope['project_path'], loop_id)})
    if row is None or row['value'].get('coordinatorId') != scope['conversation_id']:
        raise ValueError(f'{loop_id} is not a review loop of this lead; delegate_project_work with review '
                         'returns review_loop_id')
    return row['value'], int(row['version'])


def save(scope: dict, loop: dict, version: int, *, client=None) -> int:
    client = client or _client
    client(write=True).command('record.put', {
        'namespace': NAMESPACE, 'key': _key(scope['user_id'], scope['project_path'], loop['loopId']),
        'value': loop, 'expected_version': version}, 'review-loop:' + uuid.uuid4().hex)
    return version + 1


class _Ports:
    """The Coordinator's own owners, audited exactly as the lead's direct calls."""

    def __init__(self, task, scope):
        from lib.project_dispatch.repository import ProjectDispatchRepository
        from lib.project_dispatch.service import ProjectDispatchService
        self.task, self.scope = task, scope
        self.repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
        self.service = ProjectDispatchService(self.repository)

    def view(self, assignment_id, *, settlement_only=False):
        """The Worker's coordinator view; a birth still in progress reads as pending, a refused one as failed."""
        from lib.project_dispatch.admission import reread_on_contention
        from lib.project_dispatch.repository import ProjectDispatchError
        from . import coordinator_births
        starting = coordinator_births.unfinished(self.task, [assignment_id])
        if coordinator_births.failures(self.task, [assignment_id]):
            return {'assignmentId': assignment_id, 'execution': {'status': 'failed'}, 'review': None}
        try:
            return reread_on_contention(self.task, lambda: self.service.view_for_coordinator(
                assignment_id, scope=self.scope, settlement_only=settlement_only),
                label='review loop view', effect_free=True)
        except ProjectDispatchError as error:
            if 'not found' not in str(error):
                raise
            # A persisted loop ID with neither a grant nor an unfinished
            # local birth cannot start by itself (e.g. after a restart).
            execution = None if starting else {'status': 'failed'}
            return {'assignmentId': assignment_id, 'execution': execution, 'review': None}

    def delegate(self, title, brief, key):
        from . import coordinator_births
        from .coordinator_workers import delegate
        try:
            return delegate(self.task, self.scope, {'title': title, 'brief': brief, 'idempotency_key': key},
                            repository=self.repository)['assignment_id']
        except coordinator_births.BirthFailed as failure:
            raise ValueError(f'review loop reviewer could not start: {failure.payload}') from failure

    def follow_up(self, view, instruction, key):
        from lib.turn_lifecycle import get_turn
        from .session_control import control_session
        execution = view['execution']
        conversation = view['request']['recipientId']
        turn = get_turn(conversation, execution['turnId'], user_id=self.scope['user_id'], include_projection=False)
        result = control_session(self.task, self.scope, {
            'conversation_id': conversation, 'turn_id': execution['turnId'],
            'attempt_id': execution['attemptId'], 'action': 'follow_up',
            'expected_revision': turn['projectionRevision'], 'instruction': instruction,
            'idempotency_key': key})
        return result.get('assignment_id') or view['assignmentId']

    def evidence_current(self, view, bindings):
        from lib.identity import PrincipalContext
        from lib.memory.working_facts.service import query_dependency_facts
        from lib.memory.working_facts.dependencies import assess_dependency
        from lib.memory.working_facts.repository import FactsUnavailable
        principal = PrincipalContext.system(subject_id='review-loop', scopes=(),
                                            owner_user_id=self.scope['user_id'],
                                            tenant_id=self.scope.get('tenant_id'))
        conversation_id = view['request']['recipientId']
        try:
            facts = query_dependency_facts(conversation_id, principal=principal,
                                           ids=[identifier for identifier, _revision in bindings])['facts']
        except FactsUnavailable:
            return False
        by_id = {fact['id']: fact for fact in facts}
        return all((fact := by_id.get(identifier)) is not None
                   and fact['kind'] == 'observation' and fact['status'] == 'observed'
                   and assess_dependency(fact, revision, revision)[0] == 'current'
                   and any(source['kind'] in ('artifact', 'file', 'tool_result')
                           for source in fact['sources'])
                   for identifier, revision in bindings)

    def accept(self, view, note):
        from lib.project_dispatch.admission import reread_on_contention
        from .coordinator_tools import _audited_worker_control
        arguments = {'assignment_id': view['assignmentId'], 'decision': 'accept', 'note': note,
                     'result_revision': view['execution']['resultRevision']}
        _audited_worker_control(self.task, self.scope, 'review_project_work', arguments,
                                lambda: reread_on_contention(self.task, lambda: self.service.review_for_coordinator(
                                    self.task, self.scope, arguments), label='review loop accept'))


def _advance_saved(task, scope, loop_id, ports, *, client=None):
    loop, version = read(scope, loop_id, client=client)
    working = deepcopy(loop)
    changed, pending = advance(working, ports)
    if changed:
        save(scope, working, version, client=client)
    return working, pending


def _refresh(scope, loop_id, ports, *, client):
    """Refresh pending execution evidence, never issue a command or parse a result."""
    loop, _ = read(scope, loop_id, client=client)
    workers = _active_workers(loop)
    views = {identity: ports.view(identity, settlement_only=True) for identity in workers}
    return loop, _pending(workers, views)


def _loop_attempts(states):
    """Exclude all members of these loops from the waiter's unnamed news."""
    attempts = {}
    for loop, _ in states:
        attempts[loop['authorAssignmentId']] = None
        for round_ in loop['rounds']:
            attempts[round_['author']['assignmentId']] = None
            attempts.update(dict.fromkeys(reviewer['assignmentId'] for reviewer in round_['reviewers']))
            if rework := round_['rework']:
                attempts[rework['assignmentId']] = None
        attempts.update(_active_workers(loop))
    return attempts


def wait(task, scope, loop_ids, *, wait_s, mode='any', runtime_context=None, ports=None, client=None,
         wait_workers=None):
    """A lead's ``poll`` of review loops: advance each, then wait on their pending Workers.

    Returns when a loop reached its verdict (mode=all: every one), when a Worker
    outside these loops settled (its news is returned too, never swallowed),
    on new input, Stop, or when ``wait_s`` passes.
    """
    if len(set(loop_ids)) != len(loop_ids):
        raise ValueError('Review loop IDs must be distinct')
    ports = ports or _Ports(task, scope)
    if wait_workers is None:
        from .coordinator_workers import wait_for_workers as wait_workers
    deadline = time.monotonic() + wait_s
    while True:
        states = [_advance_saved(task, scope, loop_id, ports, client=client) for loop_id in loop_ids]
        loops = [summary(loop, pending) for loop, pending in states]
        ended = [loop for loop in loops if loop['status'] in ('passed', 'impasse')]
        if (ended if mode == 'any' else len(ended) == len(loops)):
            return {'review_loops': loops, 'wait': 'settled'}
        remaining = deadline - time.monotonic()
        pending = list(dict.fromkeys(identity for loop in loops for identity in loop['pending']))
        if remaining <= 0 or not pending:
            return {'review_loops': loops, 'wait': 'timeout' if wait_s else 'snapshot'}
        attempts = _loop_attempts(states)
        result = wait_workers(task, scope, pending, wait_s=remaining, mode='any', runtime_context=runtime_context,
                              loop_attempts=attempts)
        foreign = [row for row in result.get('workers') or () if row.get('assignment_id') not in attempts]
        reason = result.get('wait')
        if reason in (None, 'settled', 'progress') and time.monotonic() >= deadline:
            reason = 'timeout'
        if reason not in (None, 'settled', 'progress') or foreign:
            # Once the wait is interrupted, do not start or review work even
            # if the metadata refresh finds a result ready for the next poll.
            states = [_refresh(scope, loop_id, ports, client=client) for loop_id in loop_ids]
            outcome = {'review_loops': [summary(loop, pending) for loop, pending in states],
                       'wait': reason or 'settled'}
            if foreign:
                outcome['workers'] = foreign
            for name in ('waiting', 'instruction'):
                if name in result and (foreign or name == 'waiting'):
                    outcome[name] = result[name]
            return outcome
