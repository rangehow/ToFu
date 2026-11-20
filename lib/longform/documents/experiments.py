"""Experiment graph and deterministic results-to-dossier reduction.

Safety follow-up: automatic execution is blocked pending host-authorized
isolated smoke jobs. Generated code never runs in the controlling environment.
The retained graph uses sealed JobService cells only; see
``evaluations/research_loop/AUDIT_CORRECTION.md`` for gaps and frozen history.

This is the link between ideation (``direction.json``, schema
``tofu.research-direction/v1``, written by the ideation stage or any caller)
and the writing pipeline (``pipeline.run_document_pipeline``), which reads the
``dossier.json`` and ``document_brief.json`` this stage writes into the same
run directory. Authority record: docs/DOCUMENT_PIPELINE.md, "Experiment stage".

Stages (``EXPERIMENT_STAGES``), on the shared production workflow executor:

  plan     model writes the plan: hypothesis, dataset, arms (method,
           baselines), primary/secondary metrics, seeds and the success
           criterion as a ``lib.research.observation_audit`` decision rule
  code     preregisters the plan (``preregistration.json``: plan digest +
           time, before any run), model writes ``code/experiment.py`` against
           a fixed cell contract, smoke-runs every arm, the judge model audits
           the code against the plan; failures and blocking audit findings
           are repaired while each repair strictly lowers the failure count
  run      every (arm, seed) cell as one job on any ``lib.compute.jobs``
           backend (``compute_backends.JobsBackend``), under ``nice -n 19``;
           requires owned output receipts, not just existing result files
  analyze  deterministic: per-arm statistics, paired difference and the
           pre-registered decision (supported/refuted/inconclusive) through
           ``observation_audit.paired_statistics``/``measured_outcome``;
           ``results/summary.json``, ``results/table.csv``, ``figures/main.pdf``
  dossier  deterministic: ``dossier.json`` facts whose every number is copied
           from ``results/summary.json`` (the dossier and provenance gates
           check them there), and ``document_brief.json``

Entry point: ``run_experiment_stage(run_dir, ...)``. Dependencies: the
production workflow executor, ``pipeline._chat`` (model provenance per call),
``lib.research.observation_audit`` and ``compute_backends`` (``lib.compute``).
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from lib.log import get_logger
from lib.production.stages import Stage
from lib.production.workflows import AgentToolWorkflow, WorkflowNode, run_workflow

logger = get_logger(__name__)

__all__ = ['run_experiment_stage', 'EXPERIMENT_STAGES', 'DIRECTION_FILE', 'DIRECTION_SCHEMA',
           'read_direction', 'log_intervention', 'summarize_cells', 'dossier_from_summary']

DIRECTION_FILE = 'direction.json'
DIRECTION_SCHEMA = 'tofu.research-direction/v1'
PLAN_FILE = 'plan.json'
PREREGISTRATION_FILE = 'preregistration.json'
INTERVENTIONS_FILE = 'interventions.jsonl'
EXPERIMENT_STAGES = ('plan', 'code', 'run', 'analyze', 'dossier')
_DIRECTION_REQUIRED = ('schema', 'topic', 'title', 'hypothesis', 'rationale', 'related_work')
_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,47}$')
#: Imports that let generated code reach the network or other runs' results.
_FORBIDDEN_CODE = re.compile(
    r'^\s*(?:import|from)\s+(?:urllib|requests|http|socket|httpx|aiohttp|ftplib)\b'
    r'|fetch_openml|load_dataset\(|\.\./results|results/cells', re.MULTILINE)
#: The production stage retry budget (one gated retry), also used per cell.
_RETRY = 1
_STATUS_FILE = 'results/run_status.json'


# ── small helpers ──────────────────────────────────────────────────────────

def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def _persist(ctx: dict, name: str, artifact: dict, markdown: str = '') -> dict:
    folder = Path(ctx['workdir']) / 'artifacts'
    stem = f'E{EXPERIMENT_STAGES.index(name) + 1}-{name}'
    _write_json(folder / f'{stem}.json', artifact)
    if markdown:
        (folder / f'{stem}.md').write_text(markdown, encoding='utf-8')
    return artifact


def _art(ctx: dict, name: str) -> dict:
    return (ctx.get('artifacts') or {}).get(name) or {}


def _chat(ctx: dict, role: str, prompt: str, label: str) -> str:
    from . import pipeline
    return pipeline._chat(ctx, role, [{'role': 'user', 'content': prompt}], label)


def _json_reply(content: str):
    from . import pipeline
    return pipeline._json_reply(content)


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _sig(value: float) -> float:
    """Four significant digits: the precision results are stored and stated at."""
    return float(f'{value:.4g}')


def _fmt(value) -> str:
    """How a stored number is written in fact text: the exact JSON token."""
    return json.dumps(value)


def log_intervention(run_dir, *, actor: str, stage: str, nature: str, detail: str = '') -> dict:
    """Append one intervention to ``interventions.jsonl``. ``actor`` is
    ``human`` (the owner or any person) or ``agent`` (an operator agent acting
    outside the automatic stages); the pilot report counts both."""
    if actor not in ('human', 'agent'):
        raise ValueError('actor must be human or agent')
    row = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'actor': actor, 'stage': stage,
           'nature': nature, 'detail': detail}
    path = Path(run_dir) / INTERVENTIONS_FILE
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row


def read_direction(run_dir) -> dict:
    """The chosen direction; unknown fields are kept (the ideation stage adds
    optional ones), missing required fields are refused."""
    path = Path(run_dir) / DIRECTION_FILE
    direction = json.loads(path.read_text(encoding='utf-8'))
    missing = [field for field in _DIRECTION_REQUIRED if direction.get(field) in (None, '', [])]
    if missing:
        raise ValueError(f'{path}: required direction fields missing: {missing}')
    if direction['schema'] != DIRECTION_SCHEMA:
        raise ValueError(f'{path}: schema {direction["schema"]!r} is not {DIRECTION_SCHEMA!r}')
    return direction


def _budget_seconds(ctx: dict) -> float | None:
    hours = ((ctx['direction'].get('compute') or {}).get('budget_hours'))
    return float(hours) * 3600 if _number(hours) and hours > 0 else None


# ── plan ──────────────────────────────────────────────────────────────────

def _seed_bounds() -> tuple[int, int]:
    from lib.research.observation_audit import _observation_schema
    seeds = _observation_schema()['properties']['samples']['properties']['seeds']
    return int(seeds['minItems']), int(seeds['maxItems'])


def _feedback(ctx: dict, stage: str) -> str:
    errors = (ctx.get('experiment_feedback') or {}).get(stage)
    if not errors:
        return ''
    return ('\n\nYour previous answer was refused for these reasons; fix every one:\n- '
            + '\n- '.join(str(error) for error in errors))


def _record_feedback(ctx: dict, stage: str, errors: list) -> list:
    ctx.setdefault('experiment_feedback', {})[stage] = list(errors)
    return errors


def _run_plan(ctx: dict) -> dict:
    direction = ctx['direction']
    low, high = _seed_bounds()
    budget = _budget_seconds(ctx)
    optional = {key: direction[key] for key in ('gap', 'minimal_experiment', 'expected_signal',
                                                'falsifier', 'novelty') if direction.get(key)}
    prompt = (
        'You design the experiment that tests one research direction. The plan is '
        'pre-registered before any code runs: the success criterion you write decides the '
        'outcome, whatever the numbers turn out to be, and a negative result is a valid '
        'result.\n\n'
        f'Direction:\n{json.dumps({k: direction[k] for k in ("title", "hypothesis", "rationale")}, ensure_ascii=False)}\n'
        + (f'Ideation notes:\n{json.dumps(optional, ensure_ascii=False)}\n' if optional else '')
        + f'\nCompute available: {ctx["environment"]}\n'
        + (f'Total wall-clock budget for all runs: {budget / 3600:.2f} hours, all cells in '
           'parallel.\n' if budget else '')
        + '\nRules: the dataset must be generated in code or bundled with an installed '
        'library (no download, no network). Every arm gets the same data, splits, '
        'training budget and tuning effort; the baseline must be the strongest standard '
        'alternative, not a strawman. Evaluate on a held-out test split that no arm '
        'tunes on. Use the same seed list for every arm (paired by seed); give at least '
        f'five and at most {high} seeds (minimum allowed {low}). The decision rule: the '
        'method is supported when its paired mean improvement over the comparison '
        'baseline on the primary metric, minus uncertainty_multiplier standard errors, '
        'exceeds minimum_effect; refuted when the improvement plus that margin is below '
        'minimum_effect; otherwise inconclusive. The pipeline computes this itself '
        'from the per-seed results: the paired mean difference over seeds and its '
        'standard error (sample standard deviation of the per-seed differences over '
        'the square root of the seed count); do not specify another estimator. '
        'Each cell is one arm at one seed in one configuration: put any other '
        'configurations inside the arm, never as extra cells. Choose minimum_effect as the smallest '
        'improvement a reviewer would call meaningful, in metric units, and justify it.\n\n'
        'Reply ONLY JSON: {"hypothesis":"...","null_hypothesis":"...",'
        '"dataset":{"name":"...","description":"..."},'
        '"arms":[{"name":"identifier","role":"method|baseline|ablation","description":"..."}],'
        '"comparison":{"method":"<arm>","baseline":"<arm>"},'
        '"primary_metric":{"name":"identifier","direction":"higher|lower","description":"..."},'
        '"secondary_metrics":[{"name":"identifier","direction":"higher|lower","description":"..."}],'
        '"seeds":[0,1,2,3,4],'
        '"decision_rule":{"direction":"higher|lower","minimum_effect":0.0,"uncertainty_multiplier":2.0},'
        '"threshold_justification":"...","expected_seconds_per_cell":60}'
        + _feedback(ctx, 'plan'))
    plan = _json_reply(_chat(ctx, 'writer', prompt, 'experiment-plan'))
    plan = plan if isinstance(plan, dict) else {}
    artifact = {'plan': plan, 'digest': _digest(plan), 'direction_digest': _digest(direction)}
    _write_json(Path(ctx['run_dir']) / PLAN_FILE, plan)
    md = ['# Experiment plan', '', f'- hypothesis: {plan.get("hypothesis")}',
          f'- null: {plan.get("null_hypothesis")}',
          f'- dataset: {(plan.get("dataset") or {}).get("name")}',
          f'- arms: {", ".join(str(a.get("name")) for a in plan.get("arms") or [] if isinstance(a, dict))}',
          f'- comparison: {plan.get("comparison")}',
          f'- primary metric: {plan.get("primary_metric")}',
          f'- seeds: {plan.get("seeds")}',
          f'- decision rule: {plan.get("decision_rule")} ({plan.get("threshold_justification")})']
    return _persist(ctx, 'plan', artifact, '\n'.join(md) + '\n')


def plan_errors(plan: dict, *, budget_seconds: float | None = None) -> list[str]:
    """Machine checks on a plan (the plan stage gate)."""
    from lib.research.observation_audit import normalize_decision_rule
    errors = []
    for field in ('hypothesis', 'null_hypothesis', 'dataset', 'arms', 'comparison',
                  'primary_metric', 'seeds', 'decision_rule', 'expected_seconds_per_cell'):
        if plan.get(field) in (None, '', [], {}):
            errors.append(f'plan field {field!r} is missing')
    if errors:
        return errors
    arms = [arm for arm in plan['arms'] if isinstance(arm, dict)]
    names = [str(arm.get('name') or '') for arm in arms]
    if len(names) != len(plan['arms']) or len(set(names)) != len(names):
        errors.append('arm names must be present and unique')
    errors += [f'arm name {name!r} must be an identifier' for name in names
               if not _NAME_RE.match(name)]
    roles = {str(arm.get('name')): arm.get('role') for arm in arms}
    comparison = plan['comparison'] if isinstance(plan['comparison'], dict) else {}
    if roles.get(comparison.get('method')) != 'method':
        errors.append('comparison.method must name an arm whose role is method')
    if roles.get(comparison.get('baseline')) != 'baseline':
        errors.append('comparison.baseline must name an arm whose role is baseline')
    metrics = [plan['primary_metric'], *(plan.get('secondary_metrics') or [])]
    metric_names = [str((m or {}).get('name') or '') for m in metrics if isinstance(m, dict)]
    if len(metric_names) != len(metrics) or len(set(metric_names)) != len(metric_names):
        errors.append('metric names must be present and unique')
    errors += [f'metric name {name!r} must be an identifier' for name in metric_names
               if not _NAME_RE.match(name)]
    errors += [f'metric {m.get("name")!r}: direction must be higher or lower'
               for m in metrics if isinstance(m, dict) and m.get('direction') not in ('higher', 'lower')]
    try:
        rule = normalize_decision_rule(plan['decision_rule'])
        if rule['direction'] != (plan['primary_metric'] or {}).get('direction'):
            errors.append('decision_rule.direction must equal the primary metric direction')
    except ValueError:
        errors.append('decision_rule must be exactly {direction, minimum_effect, '
                      'uncertainty_multiplier} with finite numbers and a positive multiplier')
    seeds = plan['seeds']
    low, high = _seed_bounds()
    if (not isinstance(seeds, list) or any(type(seed) is not int for seed in seeds)
            or len(set(seeds)) != len(seeds) or not low <= len(seeds) <= high):
        errors.append(f'seeds must be {low}-{high} distinct integers')
    per_cell = plan['expected_seconds_per_cell']
    if not _number(per_cell) or per_cell <= 0:
        errors.append('expected_seconds_per_cell must be a positive number')
    elif budget_seconds and per_cell > budget_seconds:
        errors.append(f'one cell is expected to take {per_cell}s, over the '
                      f'{budget_seconds:.0f}s compute budget')
    return errors


def _gate_plan(ctx: dict, art: dict) -> list:
    return _record_feedback(ctx, 'plan', plan_errors(art['plan'],
                                                     budget_seconds=_budget_seconds(ctx)))


# ── preregistration ────────────────────────────────────────────────────────

def _completed_cells(run_dir: Path) -> list[Path]:
    return sorted((run_dir / 'results' / 'cells').glob('*.json'))


def _preregister(ctx: dict) -> dict:
    """Freeze the plan before any run. A plan change after any cell result
    exists is refused: the criterion cannot move once numbers are seen."""
    run_dir = Path(ctx['run_dir'])
    plan = _art(ctx, 'plan')['plan']
    path = run_dir / PREREGISTRATION_FILE
    record = {'plan_sha256': _digest(plan), 'decision_rule': plan['decision_rule'],
              'comparison': plan['comparison'], 'primary_metric': plan['primary_metric'],
              'seeds': plan['seeds'], 'registered_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    if path.is_file():
        previous = json.loads(path.read_text(encoding='utf-8'))
        if previous.get('plan_sha256') == record['plan_sha256']:
            return previous
        if _completed_cells(run_dir):
            raise RuntimeError(
                'preregistration: the plan changed after results exist (registered '
                f'{previous.get("plan_sha256", "")[:12]}, now {record["plan_sha256"][:12]}); '
                'a new plan needs a new run directory')
        record['supersedes'] = previous
    _write_json(path, record)
    return record


# ── code ──────────────────────────────────────────────────────────────────

_CELL_CONTRACT = (
    'Write ONE self-contained Python 3 file, experiment.py, run once per cell as\n'
    '  python experiment.py --arm <arm> --seed <int> --out <path.json> [--smoke]\n'
    'It trains/evaluates exactly that arm with that seed and writes to --out the JSON '
    '{"arm": <arm>, "seed": <seed>, "metrics": {<metric>: <float>, ...}} with every '
    'metric of the plan, measured on the held-out test split. --smoke runs the same code '
    'path on a tiny budget (seconds) for a correctness check. Seed every random '
    'generator from --seed. The data split must be identical for every arm at a given '
    'seed. Never read or write other files except --out; no network; print progress '
    'to stderr. Use only the installed packages named below. Reply with the file in '
    'one ```python block and nothing else.')


def _python_block(content: str) -> str:
    match = re.search(r'```(?:python)?\s*\n(.*?)```', content or '', re.DOTALL)
    return (match.group(1) if match else content or '').strip() + '\n'


def _cell_name(arm: str, seed: int) -> str:
    return f'{arm}__s{seed}'


def _cell_data_errors(data, arm: str, seed: int, metrics: list[str]) -> list[str]:
    """Use the same contract as fresh collection and resumed receipt admission."""
    from .compute_backends import validate_observation
    try:
        validate_observation(data, arm, seed, metrics)
    except ValueError as exc:
        return [f'{arm}/seed {seed}: {exc}']
    return []


def _metric_names(plan: dict) -> list[str]:
    return [plan['primary_metric']['name'],
            *[m['name'] for m in plan.get('secondary_metrics') or []]]


def _smoke(ctx: dict, code_dir: Path) -> list[str]:
    """Generated code may not execute on the controlling host.

    Until a host isolation/authorization port binds the audited bytes to an
    isolated JobService smoke job, fail closed. A model audit is not a sandbox.
    """
    raise RuntimeError('Authorized isolated smoke execution is not wired; generated code was not run')


def _audit_code(ctx: dict, source: str) -> list[dict]:
    """Independent audit of the code against the plan by the judge model."""
    plan = _art(ctx, 'plan')['plan']
    prompt = (
        'You are an independent reviewer auditing experiment code before it runs. Check '
        'it against the pre-registered plan: does every arm implement what the plan says; '
        'do all arms get the same data, split, training budget and tuning effort; is the '
        'test split held out from training and tuning; is anything leaked, hardcoded or '
        'computed differently between arms; are metrics computed correctly? A finding is '
        '"blocking" only when it would make the comparison invalid or the numbers wrong. '
        'Reply ONLY JSON: [{"severity":"blocking|minor","finding":"...","fix":"..."}] '
        '(an empty list when the code is sound).\n\nPlan:\n'
        + json.dumps(plan, ensure_ascii=False) + '\n\nexperiment.py:\n' + source)
    rows = _json_reply(_chat(ctx, 'judge', prompt, 'experiment-code-audit'))
    if (not isinstance(rows, list) or any(not isinstance(row, dict)
            or row.get('severity') not in {'blocking', 'minor'}
            or not isinstance(row.get('finding'), str) or not row['finding']
            for row in rows)):
        raise ValueError('Code audit must return a valid findings list; malformed is not approval')
    return rows


def _run_code(ctx: dict) -> dict:
    """Generate, smoke-run and audit ``code/experiment.py``. A repair is kept
    only while it strictly lowers the failure measure (smoke failures, then
    blocking audit findings); the first non-improving repair is discarded,
    so the loop needs no round count (as render repair)."""
    preregistration = _preregister(ctx)
    plan = _art(ctx, 'plan')['plan']
    code_dir = Path(ctx['run_dir']) / 'code'
    code_dir.mkdir(parents=True, exist_ok=True)
    base = (_CELL_CONTRACT + f'\n\nInstalled: {ctx["environment"]}\n\nPre-registered plan:\n'
            + json.dumps(plan, ensure_ascii=False))
    source = _python_block(_chat(ctx, 'writer', base + _feedback(ctx, 'code'), 'experiment-code'))
    history, best = [], None
    while True:
        (code_dir / 'experiment.py').write_text(source, encoding='utf-8')
        findings = _audit_code(ctx, source)
        blocking = [row for row in findings if row.get('severity') == 'blocking']
        failures = [] if blocking else _smoke(ctx, code_dir)
        measure = (len(failures), len(blocking))
        history.append({'smoke_failures': failures, 'audit': findings,
                        'sha256': hashlib.sha256(source.encode()).hexdigest()})
        if best is not None and measure >= best['measure']:
            source = best['source']
            (code_dir / 'experiment.py').write_text(source, encoding='utf-8')
            history.append({'reverted_to': best['index']})
            break
        best = {'measure': measure, 'source': source, 'index': len(history) - 1,
                'failures': failures, 'blocking': blocking, 'findings': findings}
        if measure == (0, 0):
            break
        problems = failures or [f'{row.get("finding")} — fix: {row.get("fix")}' for row in blocking]
        source = _python_block(_chat(
            ctx, 'writer', base + '\n\nYour current experiment.py:\n```python\n' + source
            + '```\n\nIt fails these checks; return the corrected complete file:\n- '
            + '\n- '.join(problems), 'experiment-code-repair'))
    artifact = {'path': 'code/experiment.py', 'sha256': hashlib.sha256(source.encode()).hexdigest(),
                'preregistration': preregistration, 'attempts': history,
                'smoke_failures': best['failures'], 'blocking_findings': best['blocking'],
                'audit': best['findings']}
    md = ['# Experiment code', '', f'- file: code/experiment.py ({artifact["sha256"][:12]})',
          f'- preregistered plan: {preregistration["plan_sha256"][:12]} at '
          f'{preregistration["registered_at"]}', f'- attempts: {len(history)}', '',
          '## Independent code audit (judge model)', '']
    md += [f'- [{row.get("severity")}] {row.get("finding")}' for row in best['findings']] or ['- no findings']
    md += ['', '## Remaining smoke failures', ''] + ([f'- {f[:400]}' for f in best['failures']] or ['- none'])
    return _persist(ctx, 'code', artifact, '\n'.join(md) + '\n')


def _gate_code(ctx: dict, art: dict) -> list:
    # Historical successful checkpoints cannot bypass the missing isolation port.
    errors = ['Authorized isolated smoke execution is not wired']
    errors += [f'smoke: {failure[:2000]}' for failure in art['smoke_failures']]
    errors += [f'code audit (blocking): {row.get("finding")}' for row in art['blocking_findings']]
    path = Path(ctx['run_dir']) / art['path']
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != art['sha256']:
        errors.append('code/experiment.py differs from the audited version')
    return _record_feedback(ctx, 'code', errors)


# ── run ───────────────────────────────────────────────────────────────────

def _cells(plan: dict) -> list[tuple[str, int]]:
    return [(arm['name'], seed) for seed in plan['seeds'] for arm in plan['arms']]


def _status(ctx: dict, rows: dict, started: float) -> None:
    _write_json(Path(ctx['run_dir']) / _STATUS_FILE,
                {'started_at': started, 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                 'backend': ctx['backend'].name, 'cells': rows})


def _run_run(ctx: dict) -> dict:
    """Consume only host-bound JobService cells and verify each collected result.

    Historical files alone cannot satisfy a stage completion checkpoint.
    """
    run_dir = Path(ctx['run_dir'])
    plan = _art(ctx, 'plan')['plan']
    preregistration = json.loads((run_dir / PREREGISTRATION_FILE).read_text(encoding='utf-8'))
    if preregistration['plan_sha256'] != _digest(plan):
        raise RuntimeError('preregistration does not match the plan; refusing to run')
    cells = ctx['backend'].bindings
    names = {_cell_name(arm, seed) for arm, seed in _cells(plan)}
    code_sha = hashlib.sha256((run_dir / 'code' / 'experiment.py').read_bytes()).hexdigest()
    if code_sha != _art(ctx, 'code')['sha256'] or set(cells) != names:
        raise RuntimeError('Host-approved cells must exactly cover the plan and audited code')
    for name, cell in cells.items():
        if (cell.scientific_plan_sha256 != _digest(plan) or cell.audited_code_sha256 != code_sha
                or cell.out != run_dir / 'results' / 'cells' / f'{name}.json'):
            raise RuntimeError(f'Cell {name}: scientific plan, code or output binding mismatch')
    status_path = run_dir / _STATUS_FILE
    started = (json.loads(status_path.read_text(encoding='utf-8')).get('started_at')
               if status_path.is_file() else None) or time.time()
    rows = ctx['backend'].run(list(cells.values()), emit=ctx.get('emit'),
                              deadline=ctx['backend'].expires_at,
                              abort_check=ctx.get('abort_check'))
    for name, row in rows.items():
        if row.get('state') != 'done':
            continue
        arm, seed = name.rsplit('__s', 1)
        errors = _cell_data_errors(row['observation'], arm, int(seed), _metric_names(plan))
        if errors:
            rows[name] = {**row, 'state': 'failed', 'errors': errors}
    _status(ctx, rows, started)
    in_flight = sorted(name for name, row in rows.items() if row.get('state') == 'in_flight')
    failed = sorted(name for name, row in rows.items() if row.get('state') in {'failed', 'cancelled', 'needs_attention'})
    artifact = {'backend': ctx['backend'].name, 'cells': len(cells),
                'done': sorted(name for name, row in rows.items() if row.get('state') == 'done'),
                'in_flight': in_flight, 'failed': failed,
                'code_sha256': _art(ctx, 'code')['sha256'],
                'failures': {name: rows[name] for name in failed},
                'receipts': {name: row['receipt'] for name, row in rows.items()
                             if row.get('state') == 'done'},
                'observations': {name: row['observation'] for name, row in rows.items()
                                 if row.get('state') == 'done'},
                'receipt_text': {name: row['receipt_text'] for name, row in rows.items()
                                 if row.get('state') == 'done'}}
    md = ['# Runs', '', f'- backend: {artifact["backend"]}', f'- cells: {len(cells)}; done '
          f'{len(artifact["done"])}; in flight {len(in_flight)}; failed {len(failed)}']
    md += [f'- failed {name}: {rows[name].get("errors")}' for name in failed]
    return _persist(ctx, 'run', artifact, '\n'.join(md) + '\n')


def _gate_run(ctx: dict, art: dict) -> list:
    errors, _ = _checked_run(ctx, art)
    return errors


def _checked_run(ctx: dict, art: dict) -> tuple[list, dict]:
    errors, observations = [], {}
    if art['in_flight']:
        errors.append(f'{len(art["in_flight"])} cells are still in flight on the '
                      f'{art["backend"]} backend; rerun when they finish (finished cells are '
                      'kept, never rerun)')
    path = Path(ctx['run_dir']) / 'code' / 'experiment.py'
    if hashlib.sha256(path.read_bytes()).hexdigest() != art['code_sha256']:
        errors.append('code/experiment.py changed after it was audited')
    plan = _art(ctx, 'plan')['plan']
    expected = {_cell_name(arm, seed) for arm, seed in _cells(plan)}
    if (art['failed'] or set(art['done']) != expected
            or set(ctx['backend'].bindings) != expected
            or any(set(art.get(field) or {}) != expected
                   for field in ('receipts', 'receipt_text', 'observations'))):
        errors.append('Every registered seed and arm must finish with owned output receipts; '
                      f'failed or cancelled cells: {sorted(art["failed"])}')
    for name in expected:
        try:
            cell = ctx['backend'].bindings[name]
            if (cell.scientific_plan_sha256 != _digest(plan)
                    or cell.audited_code_sha256 != art['code_sha256']):
                raise ValueError('Scientific plan/code binding mismatch')
            observations[name] = ctx['backend'].validate_receipt(
                cell, art['receipts'][name], art['receipt_text'][name], art['observations'][name])
        except (KeyError, ValueError) as exc:
            errors.append(f'Cell {name}: owned durable receipt missing or stale ({exc})')
    if ctx.get('abort_check') and ctx['abort_check']():
        errors.append('Stop requested before receipt acceptance')
    return errors, observations


# ── analyze ────────────────────────────────────────────────────────────────

def summarize_cells(plan: dict, cells: dict) -> dict:
    """Deterministic statistics over ``{cell_name: cell_json}``. Every number
    is rounded to four significant digits once, here; the paper states these
    stored values. The decision uses only the pre-registered comparison."""
    from lib.research.observation_audit import measured_outcome, paired_statistics
    metrics = [plan['primary_metric'], *(plan.get('secondary_metrics') or [])]
    arms = {}
    for arm in plan['arms']:
        name = arm['name']
        per_metric = {}
        for metric in metrics:
            values = [cells[_cell_name(name, seed)]['metrics'][metric['name']]
                      for seed in plan['seeds'] if _cell_name(name, seed) in cells]
            if not values:
                continue
            mean = math.fsum(values) / len(values)
            std = (math.sqrt(math.fsum((v - mean) ** 2 for v in values) / (len(values) - 1))
                   if len(values) > 1 else 0.0)
            per_metric[metric['name']] = {'mean': _sig(mean), 'std': _sig(std), 'n': len(values),
                                          'min': _sig(min(values)), 'max': _sig(max(values))}
        arms[name] = {'role': arm['role'], 'description': arm.get('description', ''),
                      'metrics': per_metric}
    comparisons = []
    method = plan['comparison']['method']
    rule = plan['decision_rule']
    for arm in plan['arms']:
        if arm['name'] == method:
            continue
        for metric in metrics:
            seeds = [seed for seed in plan['seeds']
                     if _cell_name(method, seed) in cells and _cell_name(arm['name'], seed) in cells]
            row = {'method': method, 'baseline': arm['name'], 'metric': metric['name'],
                   'direction': metric['direction'], 'seeds': seeds, 'n': len(seeds),
                   'preregistered': (arm['name'] == plan['comparison']['baseline']
                                     and metric['name'] == plan['primary_metric']['name'])}
            try:
                mean, se = paired_statistics(
                    [cells[_cell_name(method, s)]['metrics'][metric['name']] for s in seeds],
                    [cells[_cell_name(arm['name'], s)]['metrics'][metric['name']] for s in seeds])
            except ValueError:
                row.update(mean_difference=None, standard_error=None, outcome='inconclusive')
            else:
                differences = [cells[_cell_name(method, s)]['metrics'][metric['name']]
                               - cells[_cell_name(arm['name'], s)]['metrics'][metric['name']]
                               for s in seeds]
                sign = 1 if metric['direction'] == 'higher' else -1
                ordered = sorted(differences)
                middle = len(ordered) // 2
                median = (ordered[middle] if len(ordered) % 2
                          else (ordered[middle - 1] + ordered[middle]) / 2)
                # Reported beside the mean so one extreme seed cannot pass for a
                # consistent effect (an audit found a mean carried by one seed).
                largest = max(differences, key=abs)
                row.update(mean_difference=_sig(mean), standard_error=_sig(se),
                           median_difference=_sig(median),
                           largest_seed_difference=_sig(largest),
                           largest_seed=seeds[differences.index(largest)],
                           per_seed_differences=[_sig(v) for v in differences],
                           method_better_seeds=sum(1 for v in differences if sign * v > 0))
                if row['preregistered']:
                    row['outcome'] = measured_outcome(mean, se, rule)
            comparisons.append(row)
    primary = next(row for row in comparisons if row['preregistered'])
    oriented = None if primary['mean_difference'] is None else (
        primary['mean_difference'] if rule['direction'] == 'higher' else -primary['mean_difference'])
    decision = {
        'outcome': primary['outcome'], 'method': method,
        'baseline': plan['comparison']['baseline'], 'metric': plan['primary_metric']['name'],
        'minimum_effect': rule['minimum_effect'],
        'uncertainty_multiplier': rule['uncertainty_multiplier'],
        'direction': rule['direction'], 'n_seeds': primary['n'],
        'mean_difference': primary['mean_difference'], 'standard_error': primary['standard_error'],
        'improvement': None if oriented is None else _sig(oriented),
        'improvement_lower': None if oriented is None else
        _sig(oriented - rule['uncertainty_multiplier'] * primary['standard_error']),
        'improvement_upper': None if oriented is None else
        _sig(oriented + rule['uncertainty_multiplier'] * primary['standard_error']),
    }
    return {'schema': 'tofu.experiment-summary/v1', 'hypothesis': plan['hypothesis'],
            'dataset': plan['dataset'], 'n_seeds': len(plan['seeds']),
            'seeds': plan['seeds'], 'n_arms': len(plan['arms']),
            'arms': arms, 'comparisons': comparisons, 'decision': decision}


_PLOT_SCRIPT = r'''
import json, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
summary = json.load(open(sys.argv[1]))
cells = json.load(open(sys.argv[2]))
metric = summary["decision"]["metric"]
compared = {summary["decision"]["method"], summary["decision"]["baseline"]}
arms = [a for a in summary["arms"] if metric in summary["arms"][a]["metrics"]]
plt.rcParams.update({"font.size": 8, "pdf.fonttype": 42, "ps.fonttype": 42})
# One row per arm: names stay readable at column width however many arms.
fig, ax = plt.subplots(figsize=(3.3, 0.32 * len(arms) + 0.7))
for row, arm in enumerate(arms):
    stats = summary["arms"][arm]["metrics"][metric]
    color = "#1f77b4" if arm in compared else "#9a9a9a"
    ys = [c["metrics"][metric] for c in cells.values() if c["arm"] == arm]
    ax.scatter(ys, [row + 0.18] * len(ys), s=5, color="#555555", alpha=0.7, zorder=2)
    ax.errorbar(stats["mean"], row, xerr=stats["std"], fmt="o", color=color, ms=4,
                capsize=2, lw=1, zorder=3)
ax.set_yticks(range(len(arms)))
ax.set_yticklabels([a.replace("_", " ") for a in arms])
ax.invert_yaxis()
ax.set_xlabel(metric.replace("_", " "))
ax.grid(axis="x", lw=0.3, alpha=0.5)
values = [c["metrics"][metric] for c in cells.values()]
lows = [summary["arms"][a]["metrics"][metric]["mean"] - summary["arms"][a]["metrics"][metric]["std"] for a in arms]
highs = [summary["arms"][a]["metrics"][metric]["mean"] + summary["arms"][a]["metrics"][metric]["std"] for a in arms]
lo, hi = min(values + lows), max(values + highs)
pad = (hi - lo) * 0.08 or abs(hi) * 0.05 or 1.0
ax.set_xlim(lo - pad, hi + pad)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(sys.argv[3])
'''


def _run_analyze(ctx: dict) -> dict:
    errors, cells = _checked_run(ctx, _art(ctx, 'run'))
    if errors:
        raise RuntimeError('Analysis refused: ' + '; '.join(errors))
    run_dir = Path(ctx['run_dir'])
    plan = _art(ctx, 'plan')['plan']
    preregistration = json.loads((run_dir / PREREGISTRATION_FILE).read_text(encoding='utf-8'))
    expected = {_cell_name(arm, seed) for arm, seed in _cells(plan)}
    if set(cells) != expected:
        raise RuntimeError('Analysis requires the exact registered receipt observations')
    summary = summarize_cells(plan, cells)
    summary['preregistration'] = {'plan_sha256': preregistration['plan_sha256'],
                                  'registered_at': preregistration['registered_at']}
    summary['missing_cells'] = sorted(set(_cell_name(a, s) for a, s in _cells(plan)) - set(cells))
    summary['code_sha256'] = _art(ctx, 'code')['sha256']
    results = run_dir / 'results'
    _write_json(results / 'summary.json', summary)
    with (results / 'table.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['arm', 'role', 'metric', 'mean', 'std', 'n', 'min', 'max'])
        for arm, row in summary['arms'].items():
            for metric, stats in row['metrics'].items():
                writer.writerow([arm, row['role'], metric, stats['mean'], stats['std'],
                                 stats['n'], stats['min'], stats['max']])
    _write_json(results / 'cells_index.json', cells)
    figure = run_dir / 'figures' / 'main.pdf'
    figure.parent.mkdir(parents=True, exist_ok=True)
    script = Path(ctx['workdir']) / 'plot_main.py'
    script.write_text(_PLOT_SCRIPT, encoding='utf-8')
    plot_home = Path(ctx['workdir']) / 'plot-home'
    plot_home.mkdir(mode=0o700, exist_ok=True)
    plot = subprocess.run([ctx['python'], '-I', str(script), str(results / 'summary.json'),
                           str(results / 'cells_index.json'), str(figure)],
                          capture_output=True, text=True,
                          env={'PATH': os.defpath, 'HOME': str(plot_home), 'LANG': 'C.UTF-8',
                               'MPLCONFIGDIR': str(plot_home)})
    artifact = {'summary': 'results/summary.json', 'summary_sha256': _digest(summary),
                'decision': summary['decision'], 'figure': 'figures/main.pdf',
                'plot_error': plot.stderr[-2000:] if plot.returncode else '',
                'plan_sha256': _digest(plan)}
    d = summary['decision']
    md = ['# Analysis', '', f'- pre-registered decision: **{d["outcome"]}**',
          f'- {d["method"]} vs {d["baseline"]} on {d["metric"]}: paired difference '
          f'{d["mean_difference"]} (SE {d["standard_error"]}, n={d["n_seeds"]}); criterion: '
          f'improvement − {d["uncertainty_multiplier"]}×SE > {d["minimum_effect"]}',
          f'- missing cells: {summary["missing_cells"] or "none"}', '',
          '| arm | role | ' + ' | '.join(_metric_names(plan)) + ' |',
          '|---|---|' + '---|' * len(_metric_names(plan))]
    for arm, row in summary['arms'].items():
        md.append(f'| {arm} | {row["role"]} | ' + ' | '.join(
            f'{row["metrics"][m]["mean"]} ± {row["metrics"][m]["std"]}' if m in row['metrics'] else '—'
            for m in _metric_names(plan)) + ' |')
    return _persist(ctx, 'analyze', artifact, '\n'.join(md) + '\n')


def _gate_analyze(ctx: dict, art: dict) -> list:
    errors = []
    run_dir = Path(ctx['run_dir'])
    preregistration = json.loads((run_dir / PREREGISTRATION_FILE).read_text(encoding='utf-8'))
    if preregistration['plan_sha256'] != art['plan_sha256']:
        errors.append('the analysed plan is not the pre-registered plan')
    if art['plot_error'] or not (run_dir / art['figure']).is_file():
        errors.append(f'figure was not produced: {art["plot_error"][-500:]}')
    if art['decision']['outcome'] not in ('supported', 'refuted', 'inconclusive'):
        errors.append('decision outcome is not one of supported/refuted/inconclusive')
    from datetime import datetime
    registered = datetime.strptime(preregistration['registered_at'], '%Y-%m-%dT%H:%M:%S%z')
    status = run_dir / _STATUS_FILE
    started = json.loads(status.read_text(encoding='utf-8')).get('started_at') if status.is_file() else None
    if started is not None and registered.timestamp() > float(started):
        errors.append(f'the plan was registered at {preregistration["registered_at"]}, after the '
                      'first run started: it is not a pre-registration')
    return errors


# ── dossier ────────────────────────────────────────────────────────────────

def _words(name: str) -> str:
    return name.replace('_', ' ')


def dossier_from_summary(plan: dict, summary: dict, *, summary_path: str = 'results/summary.json',
                         figure: str = 'figures/main.pdf', bib: str = 'references.bib',
                         plan_path: str = PLAN_FILE,
                         prereg_path: str = PREREGISTRATION_FILE) -> dict:
    """Dossier facts whose numbers are the stored values of ``summary`` (so
    the dossier gate finds every stated number in ``summary_path``). The
    outcome fact states the pre-registered decision as it came out: a refuted
    or inconclusive hypothesis is written as such, never as a gain."""
    src = {'path': summary_path}
    d = summary['decision']
    primary = d['metric']
    facts = [{
        'id': 'setup', 'kind': 'background', 'status': 'sourced', 'source': src,
        'text': (f'Dataset: {summary["dataset"].get("name")} ({summary["dataset"].get("description", "")}). '
                 f'Arms compared: {", ".join(_words(a) for a in summary["arms"])}; '
                 f'{summary["n_seeds"]} seeds per arm, paired by seed.'),
    }, {
        'id': 'preregistration', 'kind': 'background', 'status': 'sourced',
        'source': {'path': [prereg_path, summary_path]},
        'text': (f'The plan and its success criterion were registered, with the plan\'s '
                 f'SHA-256 digest and time, before the first run: {_words(d["method"])} is '
                 f'supported over {_words(d["baseline"])} when its paired mean improvement in '
                 f'{_words(primary)} minus {_fmt(d["uncertainty_multiplier"])} standard errors '
                 f'exceeds {_fmt(d["minimum_effect"])}, and refuted when the improvement plus '
                 f'that margin is below {_fmt(d["minimum_effect"])}.'),
    }]
    # How each arm works, in the plan's own words: the writer cannot describe
    # the method from numbers alone (an audit found the penalty never stated).
    for arm in plan['arms']:
        facts.append({'id': f'method-{arm["name"]}', 'kind': 'background', 'status': 'sourced',
                      'source': {'path': plan_path},
                      'text': f'{_words(arm["name"])} ({arm["role"]}): {arm.get("description", "")}'})
    for arm, row in summary['arms'].items():
        stats = '; '.join(f'{_words(metric)} {_fmt(v["mean"])} (std {_fmt(v["std"])}, n {v["n"]})'
                          for metric, v in row['metrics'].items())
        facts.append({'id': f'result-{arm}', 'kind': 'result', 'status': 'sourced', 'source': src,
                      'text': f'{_words(arm)} ({row["role"]}): {stats}.'})
    comparison_ids = []
    for index, row in enumerate(summary['comparisons']):
        if row['mean_difference'] is None:
            continue
        fact_id = f'comparison-{index + 1}'
        comparison_ids.append(fact_id)
        facts.append({
            'id': fact_id, 'kind': 'result', 'status': 'sourced', 'source': src,
            'text': (f'{_words(row["method"])} minus {_words(row["baseline"])} in '
                     f'{_words(row["metric"])} ({row["direction"]} is better), paired over '
                     f'{row["n"]} seeds: mean difference {_fmt(row["mean_difference"])}, standard '
                     f'error {_fmt(row["standard_error"])}, median {_fmt(row["median_difference"])}, '
                     f'largest single-seed difference {_fmt(row["largest_seed_difference"])} '
                     f'(seed {row["largest_seed"]}); {_words(row["method"])} is better on '
                     f'{row["method_better_seeds"]} of {row["n"]} seeds'
                     + (' (pre-registered comparison).' if row['preregistered'] else ' (exploratory).')),
        })
    primary_id = next((f'comparison-{i + 1}' for i, row in enumerate(summary['comparisons'])
                       if row['preregistered'] and row['mean_difference'] is not None), None)
    verdict = {
        'supported': (f'The pre-registered hypothesis is supported: {_words(d["method"])} improves '
                      f'{_words(primary)} over {_words(d["baseline"])} by {_fmt(d["improvement"])} '
                      f'(margin {_fmt(d["improvement_lower"])} to {_fmt(d["improvement_upper"])}), '
                      f'above the pre-registered minimum effect {_fmt(d["minimum_effect"])}.'),
        'refuted': (f'The pre-registered hypothesis is refuted: {_words(d["method"])} does not '
                    f'improve {_words(primary)} over {_words(d["baseline"])} by the pre-registered '
                    f'minimum effect {_fmt(d["minimum_effect"])}; its improvement is '
                    f'{_fmt(d["improvement"])} (margin {_fmt(d["improvement_lower"])} to '
                    f'{_fmt(d["improvement_upper"])}).'),
        'inconclusive': (f'The pre-registered comparison is inconclusive: the improvement of '
                         f'{_words(d["method"])} over {_words(d["baseline"])} in {_words(primary)} '
                         + (f'is {_fmt(d["improvement"])} (margin {_fmt(d["improvement_lower"])} '
                            f'to {_fmt(d["improvement_upper"])}), which straddles the minimum '
                            f'effect {_fmt(d["minimum_effect"])}.' if d['improvement'] is not None
                            else 'could not be measured.')),
    }[d['outcome']]
    facts.append({'id': 'outcome', 'kind': 'claim', 'status': 'sourced', 'source': src,
                  'text': verdict, 'evidence': [primary_id, 'fig:main'] if primary_id else []})
    facts.append({'id': 'contribution', 'kind': 'contribution', 'status': 'sourced',
                  'source': src,
                  'text': (f'A pre-registered, seed-paired test of the hypothesis: '
                           f'{summary["hypothesis"]} Outcome: {d["outcome"]}.'),
                  'evidence': ['preregistration', 'setup', *([primary_id] if primary_id else [])]})
    return {'bib': bib, 'facts': facts,
            'figures': [{'label': 'fig:main', 'file': figure,
                         'caption': (f'{_words(primary).capitalize()} per arm '
                                     f'({summary["decision"]["direction"]} is better): circles are '
                                     f'means over {summary["n_seeds"]} seeds with one standard '
                                     'deviation, small dots individual seeds; the two '
                                     'pre-registered arms are in blue.'),
                         'supports': [fid for fid in [primary_id] if fid]}]}


def _run_dossier(ctx: dict) -> dict:
    run_dir = Path(ctx['run_dir'])
    plan = _art(ctx, 'plan')['plan']
    summary = json.loads((run_dir / 'results' / 'summary.json').read_text(encoding='utf-8'))
    bib = ctx['direction'].get('bib') or 'references.bib'
    dossier = dossier_from_summary(plan, summary, bib=bib)
    _write_json(run_dir / 'dossier.json', dossier)
    brief_path = run_dir / 'document_brief.json'
    if not brief_path.is_file() and ctx.get('brief_defaults'):
        direction = ctx['direction']
        brief = {**ctx['brief_defaults'], 'title': direction['title'],
                 'topic': direction['topic'], 'type': 'paper', 'lang': 'en'}
        brief.setdefault('goal', (
            f'Report the pre-registered test of: {direction["hypothesis"]} The outcome '
            f'({summary["decision"]["outcome"]}) is stated as measured.'))
        _write_json(brief_path, brief)
    artifact = {'dossier': 'dossier.json', 'facts': len(dossier['facts']),
                'outcome': summary['decision']['outcome'],
                'dossier_sha256': _digest(dossier), 'brief': brief_path.is_file()}
    return _persist(ctx, 'dossier', artifact,
                    '# Dossier\n\n' + '\n'.join(f'- {f["id"]}: {f["text"]}' for f in dossier['facts']) + '\n')


def _gate_dossier(ctx: dict, art: dict) -> list:
    from .rules import numbers_in, stated_numbers
    run_dir = Path(ctx['run_dir'])
    dossier = json.loads((run_dir / 'dossier.json').read_text(encoding='utf-8'))
    errors = []
    for fact in dossier['facts']:
        paths = fact['source']['path']
        paths = paths if isinstance(paths, list) else [paths]
        source = '\n'.join((run_dir / path).read_text(encoding='utf-8') for path in paths)
        missing = sorted(stated_numbers(fact['text']) - numbers_in(source))
        if missing:
            errors.append(f'fact {fact["id"]} states {missing} absent from {paths}')
    if not (run_dir / str(dossier.get('bib'))).is_file():
        errors.append(f'bibliography {dossier.get("bib")!r} is missing from the run directory')
    if not art['brief']:
        errors.append('document_brief.json is missing (pass brief defaults or write it)')
    return errors


# ── workflow ───────────────────────────────────────────────────────────────

_STAGES = {
    'plan': (_run_plan, _gate_plan, 'model'),
    'code': (_run_code, _gate_code, 'model'),
    'run': (_run_run, _gate_run, 'deterministic'),
    'analyze': (_run_analyze, _gate_analyze, 'deterministic'),
    'dossier': (_run_dossier, _gate_dossier, 'deterministic'),
}


def _implementation_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _workflow(versions: dict) -> AgentToolWorkflow:
    nodes = []
    for name in EXPERIMENT_STAGES:
        run, gate, execution = _STAGES[name]
        nodes.append(WorkflowNode(
            Stage(name, run, gate=gate, retry=_RETRY if execution == 'model' else 0,
                  checkpoint_version=versions[name]),
            execution, {'type': 'object'}))
    return AgentToolWorkflow('documents.experiments', '1', tuple(nodes))


def probe_environment(python: str) -> str:
    """No host interpreter launch: runtime capabilities require host admission."""
    return f'Proposed interpreter {python}; packages and devices unverified, require host admission'


def run_experiment_stage(run_dir: str, *, backend, python: str = sys.executable,
                         writer_model: str = '', judge_model: str = '',
                         brief_defaults: dict | None = None, emit=None,
                         abort_event=None, environment: str | None = None) -> dict:
    """Plan → code → run → analyze → dossier on the shared workflow executor.

    Automatic execution is blocked until authorized isolated smoke jobs are
    wired. ``backend`` consumes host-approved sealed JobService cells only;
    it never creates an authorization from a direction or budget. Raises
    ``StageFailed`` naming the refusing stage (``run`` refuses while cells are
    in flight: rerun to continue; finished cells are never rerun). Every
    artifact stays under ``run_dir/.experiment/artifacts``.
    """
    if not environment:
        raise ValueError('Host-admitted runtime description required; no interpreter probe is permitted')
    run = Path(run_dir).resolve()
    work = run / '.experiment'
    (work / 'state').mkdir(parents=True, exist_ok=True)
    direction = read_direction(run)
    abort_check = (lambda: bool(abort_event is not None and abort_event.is_set()))
    ctx = {'run_dir': str(run), 'workdir': str(work), 'direction': direction,
           'python': python, 'backend': backend, 'emit': emit, 'abort_check': abort_check,
           'brief': {'writer_model': writer_model, 'judge_model': judge_model},
           'brief_defaults': dict(brief_defaults or {}),
           'environment': environment}
    # Plan and code bind to what they were written from, not to this file:
    # a code change must not redraw a plan whose results already exist.
    science = _digest({'direction': direction, 'python': python,
                       'environment': ctx['environment'], 'models': ctx['brief']})
    derived = _digest({'science': science, 'implementation': _implementation_digest()})
    versions = {'plan': science, 'code': derived, 'run': derived,
                'analyze': derived, 'dossier': derived}
    try:
        return run_workflow(_workflow(versions), ctx,
                            state_path=str(work / 'state' / 'experiment.json'),
                            emit=emit, abort_check=abort_check)
    except BaseException as primary:
        if abort_check():
            try:
                backend.cancel(list(backend.bindings.values()))
            except BaseException as cleanup:
                raise BaseExceptionGroup('Experiment interrupted; cancellation needs reconciliation',
                                         [primary, cleanup]) from None
        raise
