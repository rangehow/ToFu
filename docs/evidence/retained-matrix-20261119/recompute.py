"""Offline retained-evidence projection; never imports runners or executes tasks.

Run: python3 recompute.py --matrix /path/to/eval-matrix-20261112 --out NEW_DIRECTORY
The output directory must not exist. The historical CSV is the fixed comparison
universe: later attempts are deliberately not substituted into historical cells.
"""
import argparse
import collections
import csv
import hashlib
import io
import json
from pathlib import Path


def project(matrix):
    source = matrix / 'analysis/per_trial.csv'
    content = source.read_bytes()
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    files = {'analysis/per_trial.csv': hashlib.sha256(content).hexdigest()}
    decisions, groups = [], collections.defaultdict(list)
    identities = collections.Counter((r['model'], r['harness'], r['run'], r['task']) for r in rows)
    for row in rows:
        key = (row['model'], row['harness'], row['run'], row['task'])
        reason = None
        if identities[key] != 1:
            reason = 'duplicate_identity'
        elif row['invalid']:
            reason = 'csv_invalid'
        elif row['in_extended_E'] != '1':
            reason = 'outside_extended_E'
        elif row['outcome'] not in ('pass', 'fail'):
            reason = 'not_measured'
        elif row['ingress_isolation'] != 'isolated':
            reason = 'pre_isolation_or_unknown'
        else:
            attempt = matrix / 'runs' / row['model'] / row['source_run_dir'] / 'trials' / row['harness'].split('@')[0] / row['task'] / ('attempt-' + row['attempt'])
            if (attempt / 'invalidated.json').exists():
                reason = 'subsequently_invalidated'
                p = attempt / 'invalidated.json'
                files[str(p.relative_to(matrix))] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                evidence = {}
                for name in ('state.json', 'result.json'):
                    p = attempt / name
                    try:
                        raw = p.read_bytes()
                        files[str(p.relative_to(matrix))] = hashlib.sha256(raw).hexdigest()
                        evidence[name] = json.loads(raw)
                    except FileNotFoundError:
                        reason = 'missing_raw_metadata'
                        break
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        reason = 'malformed_raw_metadata'
                        break
                if not reason:
                    state, result = evidence['state.json'], evidence['result.json']
                    iso = result.get('ingress_isolation') or {}
                    expected = [
                        '-I INPUT -s {container_subnet} -p tcp --dport 8080 -j DROP',
                        '-I DOCKER-USER -s {container_subnet} -d {vm_subnet} -p tcp --dport 8080 -j DROP',
                    ]
                    if not all(iso.get(k) for k in ('container_subnet', 'vm_subnet', 'rules')) or iso.get('port') != 8080:
                        reason = 'isolation_metadata_missing'
                    elif iso['rules'] != [rule.format(**iso) for rule in expected]:
                        reason = 'isolation_rules_not_expected'
                    elif state.get('phase') not in ('scored', 'no_reward'):
                        reason = 'raw_state_not_measured'
                    elif result.get('task') != row['task'] or result.get('arm') != row['harness'].split('@')[0]:
                        reason = 'raw_identity_mismatch'
                    elif any(((record.get('reward') or 0) >= 1.0) != (row['outcome'] == 'pass') for record in (state, result)):
                        reason = 'raw_outcome_mismatch'
                    elif row['contamination'] not in ('', 'clean'):
                        reason = 'contamination_flagged'
        decision = {'model': row['model'], 'harness': row['harness'], 'run': row['run'], 'task': row['task'], 'attempt': row['attempt'], 'decision': reason or 'eligible'}
        decisions.append(decision)
        if not reason:
            groups[(row['model'], row['harness'])].append(row)
    cells = []
    for (model, harness), selected in sorted(groups.items()):
        run_tasks = collections.defaultdict(set)
        for row in selected:
            run_tasks[row['run']].add(row['task'])
        cells.append({'model': model, 'harness': harness, 'n': len(selected), 'passes': sum(r['outcome'] == 'pass' for r in selected), 'tasks': len({r['task'] for r in selected}), 'runs': {k: len(v) for k, v in sorted(run_tasks.items())}, 'complete_89_task_runs': sum(len(v) == 89 for v in run_tasks.values()), 'recorded_list_cost_usd': round(sum(float(r['cost_usd'] or 0) for r in selected), 6), 'recorded_calls': sum(int(r['calls'] or 0) for r in selected)})
    pairs = []
    for model in sorted({m for m, h in groups}):
        labels = sorted(h for m, h in groups if m == model)
        for index, a in enumerate(labels):
            for b in labels[index + 1:]:
                left = {(r['run'], r['task']): r for r in groups[(model, a)]}
                right = {(r['run'], r['task']): r for r in groups[(model, b)]}
                shared = sorted(left.keys() & right.keys())
                if not shared:
                    continue
                pairs.append({'model': model, 'a': a, 'b': b, 'matched_run_tasks': len(shared), 'matched_tasks': len({t for r, t in shared}), 'a_passes': sum(left[k]['outcome'] == 'pass' for k in shared), 'b_passes': sum(right[k]['outcome'] == 'pass' for k in shared), 'a_recorded_list_cost_usd': round(sum(float(left[k]['cost_usd'] or 0) for k in shared), 6), 'b_recorded_list_cost_usd': round(sum(float(right[k]['cost_usd'] or 0) for k in shared), 6)})
    five_way = []
    for model in sorted({m for m, h in groups}):
        labels = sorted(h for m, h in groups if m == model and (h in ('claude-code', 'codex', 'opencode', 'pi') or h.startswith('tofu')))
        if len(labels) != 5:
            continue
        by_arm = {h: {(r['run'], r['task']): r for r in groups[(model, h)]} for h in labels}
        shared = sorted(set.intersection(*(set(index) for index in by_arm.values())))
        if shared:
            five_way.append({'model': model, 'matched_run_tasks': len(shared), 'matched_tasks': len({t for r, t in shared}), 'passes': {h: sum(by_arm[h][k]['outcome'] == 'pass' for k in shared) for h in labels}})
    retained_tasks = {r['task'] for group in groups.values() for r in group}
    return {'comparison': 'RETAINED-A10-ISOLATION-METADATA-20261119', 'universe': 'Historical per_trial.csv; no later attempts merged', 'rows': len(rows), 'isolation_labels': dict(collections.Counter(r['ingress_isolation'] for r in rows)), 'decisions': dict(collections.Counter(d['decision'] for d in decisions)), 'uncovered_tasks': sorted({r['task'] for r in rows} - retained_tasks), 'eligible_tasks': len(retained_tasks), 'eligible_passes': sum(c['passes'] for c in cells), 'recorded_list_cost_usd': round(sum(c['recorded_list_cost_usd'] for c in cells), 6), 'cells': cells, 'matched_pairs': pairs, 'five_way': five_way}, decisions, files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    summary, decisions, files = project(args.matrix)
    args.out.mkdir(parents=True, exist_ok=False)
    for name, data in [('coverage.json', summary), ('decisions.json', decisions), ('sha256.json', dict(sorted(files.items())))]:
        (args.out / name).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    lines = ['# ' + summary['comparison'], '', 'Historical snapshot, descriptive only. Isolation means retained rule metadata, not independently proven sandbox containment.', '', '| Model | Harness | Pass/n | Tasks / 89 | Complete 89-task runs | Recorded list $ |', '|---|---|---:|---:|---:|---:|']
    for c in summary['cells']:
        lines.append(f"| {c['model']} | {c['harness']} | {c['passes']}/{c['n']} | {c['tasks']} | {c['complete_89_task_runs']} | {c['recorded_list_cost_usd']:.4f} |")
    lines += ['', 'Exclusion counts (first applicable reason):', '', '```json', json.dumps(summary['decisions'], indent=2, sort_keys=True), '```', '', 'Matched comparisons are in coverage.json: exact (model, run, task) intersections, no unmatched denominator pooling. No significance or ranking claim.']
    (args.out / 'coverage.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({k: summary[k] for k in ('comparison', 'rows', 'isolation_labels', 'decisions')}, indent=2))
    print('cells:', len(summary['cells']), 'matched_pairs:', len(summary['matched_pairs']), 'hashed_inputs:', len(files))


if __name__ == '__main__':
    main()
