"""Portability report: per scenario, score spread across models and whether Tofu's gain holds per model.

Reads one or more run directories' ``results.jsonl`` and emits JSON plus a
Markdown table. For each (scenario, model): baseline mean, tofu mean, paired
gain with a 95% task-bootstrap CI and its sign ('+' gain established, '-' loss
established, '0' CI covers zero). Per scenario: the tofu- and baseline-arm score
spread (range, SD) across models and the number of distinct registered creators.
Infrastructure failures are counted once in their comparison, either for an
exact deployment or as unattributed. Legacy rows are preserved as diagnostics
and never enter modern comparisons.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from evaluations.portability.journal import (
    COLLECTION_STAGE_PREFIX, comparison_namespace, historical_persistence_failure, read_journal,
)
from evaluations.portability.stats import mean, paired_gain, spread


_REPORT_SECTIONS = {'legacy_unverified', 'journal_issues'}


def _has_binding_fields(record: dict) -> bool:
    return 'attempt_id' in record or 'artifact_sha256' in record


def _predates_artifact_binding(record: dict) -> bool:
    # The current writer never emits a path alone. Unlinked failures with no
    # artifact claim can still be current attempts and remain comparable.
    return (not _has_binding_fields(record)
            and ('artifact_path' in record
                 or (record.get('error_kind') is None and record.get('error') is None)))


class _JournalRecords(list):
    def __init__(self):
        super().__init__()
        self.issues = []


def _unscore(record, error):
    return {**record, 'score': None, 'error_kind': 'infrastructure', 'error': error}


def load(directories: list) -> list:
    from evaluations.portability.run import valid_artifact
    records = _JournalRecords()
    for directory in directories:
        for path in sorted(Path(directory).resolve().rglob('results.jsonl')):
            if any(part.startswith(COLLECTION_STAGE_PREFIX) for part in path.parts):
                continue
            journal_rows, issues = read_journal(path)
            records.issues.extend(issues)
            for record in journal_rows:
                if (_has_binding_fields(record) and record.get('error_kind') != 'infrastructure'
                        and not historical_persistence_failure(record)
                        and comparison_namespace(record) is not None and not valid_artifact(path, record)):
                    record = _unscore(record, record.get('error')
                                      or 'Referenced artifact is unavailable or does not match its journal')
                records.append(record)
    return records


def summarise(records: list, *, treatment: str = 'tofu', control: str = 'baseline') -> dict:
    """Summarise comparison rows (use ``load`` to verify on-disk artifact bindings)."""
    groups = defaultdict(list)
    unverified = []
    for record in records:
        if historical_persistence_failure(record):
            record = _unscore(record, record['error'])
        namespace = comparison_namespace(record)
        if namespace is None or _predates_artifact_binding(record):
            unverified.append(record)
        else:
            groups[json.dumps(namespace, sort_keys=True)].append(record)
    out = {}
    counts = defaultdict(int)
    for group in groups.values():
        counts[group[0]['scenario']] += 1
    for namespace, group in sorted(groups.items()):
        scenario = group[0]['scenario']
        # Simple singleton IDs remain compatible; all generated keys use a
        # reserved prefix and carry the full structured identity, not a suffix.
        if counts[scenario] == 1 and not scenario.startswith('[') and scenario not in _REPORT_SECTIONS:
            key = scenario
        else:
            key = json.dumps([scenario, json.loads(namespace)], sort_keys=True)
        info = _summarise_namespace(json.loads(namespace), group, treatment=treatment, control=control)
        for row in info['models']:
            row['namespace'] = {**info['namespace'], 'model': row['model'], **row['namespace']}
        out[key] = info
    if unverified:
        out['legacy_unverified'] = {'status': 'legacy_unverified', 'records': unverified}
    if issues := getattr(records, 'issues', None):
        out['journal_issues'] = {'status': 'journal_issues', 'issues': issues}
    return out


def _deployment_matches(deployment: tuple, judge_model: str | None) -> bool:
    def digest(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{64}', value))

    subject, judge = deployment
    return digest(subject) and (judge is None if judge_model is None else digest(judge))


def _summarise_namespace(namespace: dict, records: list, *, treatment: str, control: str) -> dict:
    cells = defaultdict(dict)  # (model, deployment, arm) -> task scores
    creators = {}
    infra = Counter()
    unattributed = defaultdict(Counter)
    errors = Counter()
    deployments = defaultdict(set)
    judge_model = namespace['judge_model']
    for record in records:
        model, arm = record['model'], record['arm']
        creators[model] = record.get('creator')
        deployment = (record.get('subject_deployment'), record.get('judge_deployment'))
        if record.get('error_kind') == 'infrastructure':
            if _deployment_matches(deployment, judge_model):
                deployments[model].add(deployment)
                infra[(model, deployment)] += 1
            else:
                unattributed[model][arm] += 1
            continue
        if type(record.get('score')) not in (int, float) or not 0 <= record['score'] <= 1:
            continue
        if record.get('error') or record.get('error_kind') is not None:
            deployment = (None, None)
        deployments[model].add(deployment)
        key = (model, deployment, arm)
        if record.get('error'):
            errors[key] += 1
        cells[key][record['task']] = record['score']

    rows = []
    variants = [(model, deployment) for model in sorted(creators)
                for deployment in sorted(deployments[model] or {(None, None)}, key=json.dumps)]
    for model, deployment in variants:
        base = cells.get((model, deployment, control), {})
        tofu = cells.get((model, deployment, treatment), {})
        matched = _deployment_matches(deployment, judge_model)
        rows.append({'model': model, 'deployment_matched': matched,
                     'subject_deployment': deployment[0], 'judge_deployment': deployment[1],
                     'namespace': {'subject_deployment': deployment[0],
                                   'judge_deployment': deployment[1]},
                     'creator': creators[model],
                     'baseline': mean(list(base.values())) if base else None,
                     'tofu': mean(list(tofu.values())) if tofu else None,
                     'n_baseline': len(base), 'n_tofu': len(tofu),
                     'errors': {control: errors[(model, deployment, control)],
                                treatment: errors[(model, deployment, treatment)]},
                     'infrastructure_failures': infra[(model, deployment)],
                     'paired': paired_gain(tofu, base) if matched else paired_gain({}, {})})
    return {
        **namespace, 'namespace': namespace, 'environments': [namespace['environment']],
        'creators': sorted({c for c in creators.values() if c}),
        'spread_tofu': spread([r['tofu'] for r in rows if r['tofu'] is not None]),
        'spread_baseline': spread([r['baseline'] for r in rows if r['baseline'] is not None]),
        'gain_holds_for': sum(1 for r in rows if r['paired']['holds'] is True),
        'gain_reversed_for': sum(1 for r in rows if r['paired']['holds'] is False),
        'infrastructure_failures': infra.total() + sum(c.total() for c in unattributed.values()),
        'unattributed_infrastructure_failures': dict(unattributed),
        'models': rows,
    }


def _fmt(value, digits=3):
    return '—' if value is None else f'{value:.{digits}f}'


def markdown(summary: dict) -> str:
    lines = []
    for key, info in summary.items():
        if info.get('status') == 'journal_issues':
            lines.extend(['### Journal issues', '', 'Only the valid prefix of each affected journal was read.', ''])
            for issue in info['issues']:
                lines.append(f'- {issue["kind"]}: `{issue["path"]}` line {issue["line"]}')
            lines.append('')
            continue
        if info.get('status') == 'legacy_unverified':
            lines.extend(['### Legacy / unverified', '',
                          f'{len(info["records"])} record(s) without verified comparison or artifact evidence. '
                          'Preserved in JSON; excluded from statistics and infrastructure counts.', ''])
            continue
        scenario = info.get('scenario', key)
        lines.append(f'### {scenario} (v{info["version"]}, metric `{info["metric"]}`'
                     + (f', judge `{info["judge_model"]}`' if info['judge_model'] else '') + ')')
        lines.append('')
        lines.append(f'Comparison: `{json.dumps(info["namespace"], sort_keys=True)}`')
        lines.append('')
        lines.append('| model | subject / judge deployment | creator | baseline | tofu | gain [95% CI] | sign | n | errors b/t | infra |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|')
        for row in info['models']:
            paired = row['paired']
            ci = '—' if paired['ci'] is None else f'[{paired["ci"][0]:+.3f}, {paired["ci"][1]:+.3f}]'
            gain = '—' if paired['gain'] is None else f'{paired["gain"]:+.3f}'
            deployment = f'{row["subject_deployment"] or "unknown"} / {row["judge_deployment"] or "none"}'
            lines.append(f'| {row["model"]} | {deployment} | {row["creator"]} | {_fmt(row["baseline"])} | {_fmt(row["tofu"])} | '
                         f'{gain} {ci} | {paired["sign"]} | {paired["n"]} | '
                         f'{row["errors"]["baseline"]}/{row["errors"]["tofu"]} | {row["infrastructure_failures"]} |')
        if unattributed := info['unattributed_infrastructure_failures']:
            lines.extend(['', 'Unattributed infrastructure failures (by model and arm): '
                          f'`{json.dumps(unattributed, sort_keys=True)}`.'])
        st, sb = info['spread_tofu'], info['spread_baseline']
        lines.append('')
        lines.append(f'Spread across {len(info["models"])} model/deployment rows / {len(info["creators"])} creators — '
                     f'tofu range {_fmt(st["range"])} (SD {_fmt(st["sd"])}), baseline range {_fmt(sb["range"])} '
                     f'(SD {_fmt(sb["sd"])}). Gain established for {info["gain_holds_for"]}, reversed for '
                     f'{info["gain_reversed_for"]}, unresolved for '
                     f'{len(info["models"]) - info["gain_holds_for"] - info["gain_reversed_for"]}. '
                     f'Environments: {", ".join(info["environments"])}.')
        lines.append('')
    return '\n'.join(lines)
