"""Acceptance checks of delegated work: numbered requirements and per-check verdicts.

A request's ``acceptance`` is immutable with its brief (the same authority). The
Worker reads it as a numbered list (``worker_section``); a Coordinator review
judges each check by its 1-based number (``judged``): an accept judges every
check and none may be unmet, while rework and reject may judge some or none.
Views derive ``counts``, in which a check without a recorded verdict is unknown,
never met. Decision: docs/PROJECT_COORDINATION.md, "Acceptance checks".
Entry points: ``own_checks`` (delegation), ``worker_section`` (Worker input),
``judged`` (review), ``counts`` (views and receipts). Depends only on the
dispatch error type; it reads and writes no storage.
"""
from __future__ import annotations

from collections import Counter

from .repository import ProjectDispatchError


class AcceptanceRefused(ProjectDispatchError):
    """Checks or verdicts that cannot be recorded as given; nothing was written."""
    execution_started = False

    def __init__(self, code, message, details, recovery):
        super().__init__(message)
        self.code, self.details, self.recovery = code, details, recovery


def own_checks(arguments):
    """A delegation's own checks, trimmed; checks equal after trimming are refused."""
    checks = [check.strip() for check in arguments.get('acceptance') or ()]
    repeated = sorted(check for check, count in Counter(checks).items() if count > 1)
    if repeated:
        raise AcceptanceRefused(
            'acceptance_checks_repeated', 'acceptance lists the same check more than once',
            {'repeated': repeated}, 'No Worker was created. Merge the repeated checks, then delegate again.')
    return checks


def worker_section(checks):
    """The Worker input's numbered checks; empty for work without checks."""
    if not checks:
        return ''
    return ('\n\nAcceptance checks (your result is reviewed against each one by its number; for every '
            'check, report the evidence that it is met, or state that it is unmet or could not be '
            'verified):\n' + '\n'.join(f'{number}. {check}' for number, check in enumerate(checks, 1)))


def judged(checks, decision, results):
    """The review's verdicts in check order, or a typed refusal before anything is recorded."""
    results = sorted((dict(item) for item in results or ()), key=lambda item: item['check'])
    numbers = Counter(item['check'] for item in results)
    foreign = sorted(number for number in numbers if number > len(checks))
    repeated = sorted(number for number, count in numbers.items() if count > 1)
    if foreign or repeated:
        raise AcceptanceRefused(
            'acceptance_results_invalid',
            (f'acceptance_results must judge checks 1-{len(checks)} of this assignment, each at most once'
             if checks else 'This assignment has no acceptance checks'),
            {'checks': len(checks), 'not_found': foreign, 'repeated': repeated},
            'Nothing was recorded. Send one result per check number of request.acceptance in '
            'get_project_assignment, or omit acceptance_results when the assignment has no checks.')
    if decision != 'accept':
        return results
    missing = [number for number in range(1, len(checks) + 1) if number not in numbers]
    if missing:
        raise AcceptanceRefused(
            'acceptance_results_incomplete', 'An accept must judge every acceptance check',
            {'checks': len(checks), 'missing': missing},
            'Nothing was recorded. Judge each missing check with the evidence you read (verdict unknown '
            'when you could not verify it), then review again.')
    unmet = [item['check'] for item in results if item['verdict'] == 'unmet']
    if unmet:
        raise AcceptanceRefused(
            'acceptance_unmet', 'Work with an unmet acceptance check cannot be accepted', {'unmet': unmet},
            'Nothing was recorded. Review with decision=rework (then delegate the repair with rework_of) '
            'or decision=reject, keeping these acceptance_results.')
    return results


def counts(checks, results):
    """Verdicts over every check; a check without a recorded verdict is unknown, never met."""
    verdicts = {item['check']: item['verdict'] for item in results or ()}
    tally = dict.fromkeys(('met', 'unmet', 'unknown'), 0)
    for number in range(1, len(checks) + 1):
        tally[verdicts.get(number, 'unknown')] += 1
    return tally
