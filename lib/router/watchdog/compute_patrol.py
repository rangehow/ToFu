"""Watchdog compute-patrol intake: an off-host patrol's flags become fault signals.

Responsibility: an off-host liveness patrol (docs/COMPUTE_PATROL.md; the Hope
patrol is ``scripts/hope_patrol``) cannot reach Tofu. Each cycle it publishes
``publication.json`` under ``<data root>/compute_patrol/u<owner>/<patrol>/``.
The Watchdog's periodic review calls ``ingest_compute_patrols`` before it
builds its snapshot. Each flagged episode becomes one Watchdog fault signal of
source ``patrol``. Its identity is (patrol, kind, job or condition, since), so
replays and later cycles of one episode are the same signal, and a signal the
Watchdog resolved never reopens. The intake resolves open patrol signals whose
episode is no longer published, and all of them when the patrol was stopped
on purpose. A publication past its ``staleAfter`` becomes one ``patrol_stale``
signal; the next fresh publication resolves it. While a patrol is stale its
job signals stay open, since nothing proves they recovered. The patrol only
notifies, and so does every hint here: no job is ever stopped. Recording and
waking go through the shared best-effort boundary in ``signals``.

A publication is untrusted input from a shared volume: it is schema-checked
against ``contracts/compute_patrol_v1.json``, and its text reaches the
Watchdog only inside the nonhuman signal notice.

Entry points: ingest_compute_patrols, plan_patrol_signals, read_publication,
patrol_root, PATROL_SOURCE. Dependencies: lib.runtime.runtime_paths,
router.watchdog.signals, jsonschema.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
import time

from lib.identity import require_user_id
from lib.log import get_logger

from ..contract import CONTRACT
from .signals import FaultSignals, _bounded, _report

logger = get_logger(__name__)

PATROL_SOURCE = 'patrol'
_CONTRACT_PATH = Path(__file__).resolve().parents[3] / 'contracts' / 'compute_patrol_v1.json'
_PATROL_NAME = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_KINDS = {'job_hung': 'compute_job_hung', 'job_queued_stale': 'compute_job_queued_stale',
          'patrol_degraded': 'compute_patrol_degraded', 'patrol_stale': 'compute_patrol_stale'}
PATROL_ERROR_KINDS = frozenset(_KINDS.values())
# The patrol, not the Watchdog, observed the recovery; say so where the contract records it.
_BY_PRODUCER = ({'resolved_by': 'producer'}
                if 'resolvedBy' in CONTRACT['$defs']['watchdogSignal']['properties'] else {})
# The owner's patrol publishes Hope's own clock (China Standard Time).
_CST = timezone(timedelta(hours=8))
_JOB_HINT = ('Off-host liveness patrol (docs/COMPUTE_PATROL.md). Tell the Global Router once with '
             'watchdog_notify_router, citing this signal: it routes the flag to the owning lead, named above when '
             'the patrol inferred it. The patrol only notifies: never stop, resubmit or reprioritize the job, and '
             'never ask anyone but its lead to. The intake closes this signal itself when the patrol stops flagging '
             'the job (it recovered, started, ended or a lead claimed it in claims.json).')
_PATROL_HINT = ('Off-host liveness patrol (docs/COMPUTE_PATROL.md). While it is stale or degraded, hung or stale '
                'jobs go unseen. Tell the Global Router once with watchdog_notify_router; the runbook in '
                'docs/COMPUTE_PATROL.md says how to restart it or what the owner must renew. The intake closes this '
                'signal itself when a fresh, healthy publication arrives.')


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    contract = json.loads(_CONTRACT_PATH.read_text(encoding='utf-8'))
    return Draft202012Validator({'$schema': contract['$schema'], '$defs': contract['$defs'],
                                 '$ref': '#/$defs/publication'})


def patrol_root(owner: int) -> Path:
    from lib.runtime.runtime_paths import data_root
    # TODO(enterprise): one directory per owner under the deployment's data root.
    return Path(data_root()) / 'compute_patrol' / f'u{require_user_id(owner, context="compute patrol")}'


def read_publication(path: Path) -> tuple[dict | None, str | None]:
    """(publication, None) when valid; (None, reason) when missing or invalid."""
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None, 'missing'
    except (OSError, ValueError) as error:
        return None, f'unreadable: {type(error).__name__}'
    error = next(_validator().iter_errors(value), None)
    if error is not None:
        location = '.'.join(str(part) for part in error.absolute_path) or 'publication'
        return None, f'invalid at {location}: {error.message[:200]}'
    return value, None


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=_CST).strftime('%Y-%m-%d %H:%M CST')


def _duration(ms: int) -> str:
    minutes = max(0, ms) // 60000
    return f'{minutes // 60} h {minutes % 60:02d} min'


def _job_signal(name: str, publication: dict, flag: dict, now: int) -> dict:
    subject = {'role': 'compute', 'patrol': name, 'jobId': flag['jobId']}
    if flag.get('queue'):
        subject['queue'] = flag['queue']
    if flag.get('projectPath'):
        subject['projectPath'] = flag['projectPath']
    held = 'hung' if flag['kind'] == 'job_hung' else 'queued'
    detail = (f"Hope job {flag['jobId']} ({flag.get('name') or 'unnamed'}) in {flag.get('queue') or 'an unknown queue'}"
              f", {flag.get('cards') if flag.get('cards') is not None else '?'} cards, state {flag.get('state') or '?'}"
              f", creator {flag.get('creator') or '?'}; owning project: {flag.get('project') or 'not inferred'}"
              f"; {held} for {_duration(now - flag['since'])} (since {_iso(flag['since'])}). Evidence: "
              f"{flag['evidence']} Patrol cycle {publication['cycle']} published {_iso(publication['at'])} "
              f"from {publication['host']}.")
    return {'identity': [name, flag['kind'], flag['jobId'], flag['since']], 'subject': subject,
            'error': {'kind': _KINDS[flag['kind']], 'detail': _bounded('detail', detail),
                      'hint': _bounded('hint', _JOB_HINT)}}


def _patrol_signal(name: str, kind: str, key, detail: str) -> dict:
    return {'identity': [name, kind, *key], 'subject': {'role': 'compute', 'patrol': name},
            'error': {'kind': _KINDS[kind], 'detail': _bounded('detail', detail), 'hint': _bounded('hint', _PATROL_HINT)}}


def plan_patrol_signals(name: str, publication: dict, *, now: int) -> dict:
    """Pure plan for one patrol: ``{'signals': [...], 'stale': bool, 'stopped': bool}``.

    ``signals`` are the episodes that must be open now. When the patrol is
    stale its earlier job signals are kept (``keep_open``), never resolved on
    missing evidence.
    """
    if publication.get('stopped'):
        return {'signals': [], 'stale': False, 'stopped': True, 'keep_open': False}
    if now > publication['staleAfter']:
        detail = (f"The {name} liveness patrol last published at {_iso(publication['at'])} (cycle "
                  f"{publication['cycle']}) from {publication['host']}; its next publication was due by "
                  f"{_iso(publication['staleAfter'])} and none arrived ({_duration(now - publication['staleAfter'])} "
                  'late): the patrol job stopped or cannot write. Hung and stale jobs go unseen until it is restarted '
                  '(docs/COMPUTE_PATROL.md runbook).')
        return {'signals': [_patrol_signal(name, 'patrol_stale', [publication['at']], detail)], 'stale': True,
                'stopped': False, 'keep_open': True}
    signals = []
    for flag in publication['flags']:
        if flag['kind'] == 'patrol_degraded':
            detail = (f"The {name} liveness patrol is degraded ({flag.get('condition') or 'unspecified'}) since "
                      f"{_iso(flag['since'])}: {flag['evidence']}")
            signals.append(_patrol_signal(name, 'patrol_degraded', [flag.get('condition') or '', flag['since']],
                                          detail))
        elif flag.get('jobId'):
            signals.append(_job_signal(name, publication, flag, now))
    return {'signals': signals, 'stale': False, 'stopped': False, 'keep_open': False}


def _open_patrol_signals(signals: FaultSignals, name: str) -> list[dict]:
    found, cursor = [], None
    while True:
        page = signals.open_page(cursor)
        found += [signal for signal in page['signals']
                  if signal['source'] == PATROL_SOURCE and signal['subject'].get('patrol') == name]
        cursor = page['next_cursor']
        if cursor is None:
            return found


def _resolution(plan: dict, publication: dict, signal: dict) -> str:
    if plan['stopped']:
        return (f"The patrol was stopped on purpose at {_iso(publication['at'])} (control/STOP); this flag is no "
                'longer watched.')
    if signal['error']['kind'] == _KINDS['patrol_stale']:
        return f"The patrol published again at {_iso(publication['at'])} (cycle {publication['cycle']})."
    return (f"Patrol cycle {publication['cycle']} at {_iso(publication['at'])} no longer flags this episode: the job "
            'recovered, started, ended or was claimed, or the condition cleared.')


def _ingest_one(owner: int, name: str, path: Path, *, now: int, client, dispatch) -> dict:
    publication, problem = read_publication(path)
    if publication is None:
        if problem != 'missing':
            logger.warning('[RouterWatchdog] compute patrol %s publication %s', name, problem)
        return {'patrol': name, 'skipped': problem}
    if publication['owner'] != owner or publication['patrol'] != name:
        logger.warning('[RouterWatchdog] compute patrol %s publication names owner %s patrol %s; ignored',
                       name, publication['owner'], publication['patrol'])
        return {'patrol': name, 'skipped': 'owner or patrol mismatch'}
    plan = plan_patrol_signals(name, publication, now=now)
    signals = FaultSignals(owner, client=client)
    wanted = set()
    for spec in plan['signals']:
        wanted.add(signals.signal_id(PATROL_SOURCE, spec['identity']))

        def build(owner, watchdog, spec=spec):
            # (identity, subject, error, condition, episode parts): only the patrol observes recovery.
            return (spec['identity'], spec['subject'], spec['error'], {'kind': 'producer'},
                    spec['identity'][:3])
        _report(owner, PATROL_SOURCE, build, client=client, dispatch=dispatch)
    resolved = []
    for signal in _open_patrol_signals(signals, name):
        if signal['signalId'] in wanted:
            continue
        if plan['keep_open'] and signal['error']['kind'] != _KINDS['patrol_stale']:
            continue
        signals.resolve([signal['signalId']], _resolution(plan, publication, signal), **_BY_PRODUCER)
        resolved.append(signal['signalId'])
    return {'patrol': name, 'cycle': publication['cycle'], 'stale': plan['stale'], 'stopped': plan['stopped'],
            'open': sorted(wanted), 'resolved': resolved}


def ingest_compute_patrols(owner: int, *, now: int | None = None, client=None, dispatch=None) -> list[dict]:
    """Turn every patrol publication of this owner into signals; never raises into the review."""
    try:
        owner = require_user_id(owner, context='compute patrol intake')
        root = patrol_root(owner)
        if not root.is_dir():
            return []
        moment = int(time.time() * 1000) if now is None else int(now)
        results = []
        for path in sorted(root.glob('*/publication.json')):
            name = path.parent.name
            if not _PATROL_NAME.fullmatch(name):
                continue
            try:
                results.append(_ingest_one(owner, name, path, now=moment, client=client, dispatch=dispatch))
            except Exception:
                logger.warning('[RouterWatchdog] compute patrol %s intake failed owner=%s', name, owner, exc_info=True)
        return results
    except Exception:
        logger.warning('[RouterWatchdog] compute patrol intake failed owner=%s', owner, exc_info=True)
        return []


__all__ = ['PATROL_ERROR_KINDS', 'PATROL_SOURCE', 'ingest_compute_patrols', 'patrol_root', 'plan_patrol_signals',
           'read_publication']
