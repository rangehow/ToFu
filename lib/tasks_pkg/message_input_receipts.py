"""Exact mailbox carrier witnesses for the final physical provider attempt.

Injection is not delivery. Each retry independently filters immutable carrier
text against its actual request body. Only a verified response publishes the
matched IDs; a dropped or summarized carrier cannot be acknowledged.

Delivered history is not re-authorized every round. A carrier the provider
verifiably consumed (found in the request, its IDs in the verified physical
input) is recorded by exact signature; later sends skip scanning and
re-validating it unless one of its IDs is still pending acknowledgement in
the current attempt. Every undelivered or pending carrier is scanned and
validated on every send, and carried history still passes the cheap mailbox
and run-target checks, so a rebound task or stopped child never sends.
"""
from __future__ import annotations

from collections.abc import Mapping

from lib.agent_core.context_errors import MessageInputScopeError
from lib.llm.llm_errors import AbortedError
from lib.tasks_pkg.round_prep_timing import round_prep_lap, round_prep_step


DISPATCH_ROUND_FIELD = '_messageInputDispatchRound'
# Exact signatures of carriers the provider verifiably consumed.
_CONSUMED_FIELD = '_messageInputConsumedWitnesses'


def historical_swarm_context(messages: list[dict], *, binding=None) -> list[dict]:
    """Detach trusted restored history without transferring run receipt rights.

    Only server-owned history restoration calls this projection. It never
    derives a binding from carrier metadata or touches durable mailbox rows.
    Explicit same-run recovery preserves every witness, including malformed or
    foreign ones: the normal dispatch gate must reject those, not hide them.
    """
    from copy import deepcopy
    restored = deepcopy(messages)
    if binding is None:
        for message in restored:
            message.pop('_swarmMessageIds', None)
            message.pop('_swarmMessageMailbox', None)
    return restored


def _signature(witness: dict) -> tuple:
    return witness['family'], witness.get('mailbox'), tuple(witness['ids']), witness['text']


def _pending_ids(task: dict) -> set:
    """IDs the current attempt may still acknowledge: always scanned."""
    attempt = task.get('_projectMessageAttempt')
    pending = {('project', i) for i in (attempt or {}).get('ids') or ()} if isinstance(attempt, Mapping) else set()
    pending.update(('swarm', i) for i in task.get('_swarmMessageAttemptIds') or ())
    return pending


def _partition(task: dict) -> tuple[list, list]:
    """(carriers to scan, consumed carriers carried without a scan)."""
    witnesses = task.get('_messageInputWitnesses') or []
    consumed = task.get(_CONSUMED_FIELD)
    if not consumed:
        return list(witnesses), []
    pending = _pending_ids(task)
    scan, carried = [], []
    for witness in witnesses:
        if (_signature(witness) in consumed
                and not any((witness['family'], i) in pending for i in witness['ids'])):
            carried.append(witness)
        else:
            scan.append(witness)
    return scan, carried


def _record_consumed(task: dict, found: list, verified: dict) -> None:
    current = {_signature(w) for w in task.get('_messageInputWitnesses') or []}
    consumed = (task.get(_CONSUMED_FIELD) or set()) & current
    for witness in found:
        if set(witness['ids']) <= set(verified.get(witness['family']) or ()):
            consumed.add(_signature(witness))
    task[_CONSUMED_FIELD] = consumed


def _validate_input_scope(task: dict, matches: dict) -> None:
    try:
        validate_matches(task, matches)
    except (AbortedError, MessageInputScopeError):
        raise
    except Exception as error:
        raise MessageInputScopeError('Mailbox input scope is no longer available') from error


class _ReceiptLease:
    """Decorate the existing physical-attempt lease, never authorize a route.

    Without a budget delegate this is only an input observer. Dispatch, slot
    reservation, cancellation, and settlement remain with their existing owners.
    """

    def __init__(self, admission, delegate, max_output_tokens):
        self.admission = admission
        self.delegate = delegate
        self.max_output_tokens = (delegate.max_output_tokens if delegate is not None
                                  else max_output_tokens)
        self._dispatched = False

    @property
    def dispatched(self):
        return self.delegate.dispatched if self.delegate is not None else self._dispatched

    def mark_dispatched(self):
        if self.delegate is not None:
            self.delegate.mark_dispatched()
        self._dispatched = True

    def prepare_input(self, body):
        # Recompute from the sealed, translated input after every dispatcher
        # wait. Canonical-body membership cannot prove physical consumption.
        self.admission.matches = None
        from lib.tasks_pkg.context_dependencies import observe_wire
        try:
            observe_wire(self.admission.task, [{'role': 'user', 'content': text} for text in _strings(body)]
                         if self.admission.task.get('_workingFactDependencyPending') else None)
        except PermissionError as error:
            raise MessageInputScopeError('Working fact input scope is no longer available') from error
        matches, _found = _scan(self.admission.task, body)
        _validate_input_scope(self.admission.task, matches)
        if self.delegate is not None:
            prepare = getattr(self.delegate, 'prepare_input', None)
            if callable(prepare):
                prepare(body)
        self.admission.matches = matches

    def confirm_input(self, *, successful):
        if not successful:
            from lib.tasks_pkg.context_dependencies import observe_wire
            observe_wire(self.admission.task, None)
        if self.delegate is not None:
            confirm = getattr(self.delegate, 'confirm_input', None)
            if callable(confirm):
                confirm(successful=successful)

    def note_provider_refusal(self):
        # Only the budget delegate distinguishes proven-zero from unconfirmed
        # spend; a plain input observer has no account to settle.
        note = getattr(self.delegate, 'note_provider_refusal', None)
        if callable(note):
            note()

    def settle(self, usage=None, *, unsent=False):
        if self.delegate is not None:
            self.delegate.settle(usage, unsent=unsent)


class ReceiptAdmission:
    """Request-local observer composed at the canonical prepared-input seam."""

    def __init__(self, task, budget_admit):
        self.task = task
        self.budget_admit = budget_admit
        self.matches = None

    def __call__(self, **counts):
        delegate = self.budget_admit(**counts) if self.budget_admit is not None else None
        return _ReceiptLease(self, delegate, counts['max_output_tokens'])


def receipt_admission(task: dict, budget_admit):
    if not task.get('_messageInputWitnesses') and not task.get('_workingFactDependencyPending'):
        return None
    return ReceiptAdmission(task, budget_admit)


def remember(task: dict, messages: list[dict]) -> None:
    witnesses = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, list) and content and isinstance(content[-1], Mapping):
            # A carrier with media (attached files) ends with its own text block
            # (context_composer.append_injection); that block is the witness.
            content = content[-1].get('text')
        if not isinstance(content, str) or not content:
            continue
        for family, id_key in (('swarm', '_swarmMessageIds'), ('project', '_projectMessageIds')):
            ids = message.get(id_key)
            if ids:
                mailbox_key = '_swarmMessageMailbox' if family == 'swarm' else '_projectMailbox'
                witnesses.append({'family': family, 'ids': list(ids), 'text': content,
                                  'mailbox': message.get(mailbox_key)})
    task['_messageInputWitnesses'] = witnesses


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key in ('content', 'text', 'parts', 'messages', 'input', 'instructions', 'system', 'prompt'):
            if key in value:
                yield from _strings(value[key])
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _scan(task: dict, body: dict) -> tuple[dict, list]:
    """(matched IDs, carriers found) among the carriers that need a scan."""
    scan, _carried = _partition(task)
    if not scan:
        return {'swarm': [], 'project': []}, []
    # Provider input fields only: tools, metadata, routing and diagnostics are
    # not proof that the model received an injected mailbox carrier.
    strings = list(_strings(body))
    exact = set(strings)
    found = [witness for witness in scan if witness['text'] in exact]
    residual = [witness for witness in scan if witness['text'] not in exact]
    # A translated request may embed a carrier inside a larger string. New
    # carriers sit at the end of the request, so search from the end.
    for text in reversed(strings):
        if not residual:
            break
        remaining = []
        for witness in residual:
            (found if witness['text'] in text else remaining).append(witness)
        residual = remaining
    found_ids = {id(witness) for witness in found}
    found = [witness for witness in scan if id(witness) in found_ids]
    matched = {'swarm': [], 'project': []}
    for witness in found:
        matched[witness['family']].extend(witness['ids'])
    return {key: list(dict.fromkeys(ids)) for key, ids in matched.items()}, found


def matched_ids(task: dict, body: dict) -> dict:
    return _scan(task, body)[0]


def _carriers_by_id(witnesses: list) -> dict:
    index = {}
    for witness in witnesses:
        for message_id in witness['ids']:
            index.setdefault((witness['family'], message_id), []).append(witness)
    return index


def validate_matches(task: dict, matches: dict) -> None:
    """Authorize matched carriers, plus the cheap scope checks of carried ones.

    One durable ``find`` for every matched project ID and one sender check per
    distinct sender: the authority being checked is the sender's, not the row's.
    """
    _scan_set, carried = _partition(task)
    index = _carriers_by_id(task.get('_messageInputWitnesses') or [])

    def carriers(family):
        return ([w for i in matches[family] for w in index.get((family, i), ())]
                + [w for w in carried if w['family'] == family])

    swarm = carriers('swarm')
    if swarm:
        from lib.swarm.run_messages import validate_round_target, RunMessageRepository
        binding = task.get('_swarmMessageBinding')
        if not binding:
            raise PermissionError('Restored child message has no current run authority')
        validate_round_target(task, binding, round_num=task.get(DISPATCH_ROUND_FIELD))
        key = RunMessageRepository(binding).key
        if any(w.get('mailbox') != key for w in swarm):
            raise PermissionError('Restored child message belongs to another run')
    project_carriers = carriers('project')
    if project_carriers:
        from lib.conversations.project_messages import identity, validate_sender, ProjectMailbox
        owner, recipient, project = identity(task)
        repository = ProjectMailbox(owner, project, recipient)
        if any(w.get('mailbox') != repository.key for w in project_carriers):
            raise PermissionError('Restored project message belongs to another mailbox')
        if matches['project']:
            rows = repository.find(matches['project'])
            if set(rows) != set(matches['project']):
                raise PermissionError('Restored project message has no durable original')
            for sender in dict.fromkeys(row['sender'] for row in rows.values()):
                validate_sender(owner, sender, project, recipient=recipient)


def stream_with_receipts(stream, task: dict, body: dict, **kwargs):
    from lib.llm.stream_result import ensure_provider_stream_result
    task.pop('_verifiedMessageInputIds', None)
    task.pop('_messageInputPhysicalIds', None)
    # Only the root loop's dispatch of its active round shares that round's
    # target check: the loop drops the round memo when that dispatch ends, so
    # a later model call (e.g. post-loop synthesis) starts with a complete check.
    task[DISPATCH_ROUND_FIELD] = task.get('_activeRoundNum')
    try:
        round_prep_lap(task, 'dispatch')
        with round_prep_step(task, 'receipts'):
            matches, found = _scan(task, body)
            _validate_input_scope(task, matches)
        result = stream(task, body, **kwargs)
        if ensure_provider_stream_result(result).is_verified_complete:
            # Named non-transport/test seams retain canonical matching. The
            # production manager always supplies the selected physical input.
            verified = task.get('_messageInputPhysicalIds', matches)
            task['_verifiedMessageInputIds'] = verified
            _record_consumed(task, found, verified)
        return result
    finally:
        task.pop('_messageInputPhysicalIds', None)
        task.pop(DISPATCH_ROUND_FIELD, None)
