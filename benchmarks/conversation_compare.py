"""Paired active-conversation acceptance on one explicit disposable volume.

Each leg runs in a fresh process through conversation_concurrency.py; repeats
alternate backend order. JSON preserves failed legs and separates correctness,
absolute latency, coverage, and SQLite comparison. No live models/user data.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.conversation_acceptance import POLICY, assess
from benchmarks.conversation_provenance import capture_sources
from benchmarks.conversation_receipts import authority_path, number, obj, publish_json
from scripts.tofudb_bench import _mount_type


def backend_order(repeat):
    return POLICY['required_backends'][::1 if repeat % 2 == 0 else -1]


def collect_child_report(leg, output):
    try:
        report = json.loads(output.read_text())
        if not isinstance(report, dict):
            raise ValueError('child report is not an object')
        leg['report'] = report
    except (OSError, ValueError) as exc:
        leg['report_read_error'] = f'{type(exc).__name__}: {exc}'
        leg['unpublished_report_paths'] = [str(path) for path in sorted(output.parent.glob(output.name + '.*.partial'))]


def compare(legs: list[dict], repeats: int, filesystem: str, *, current_sources: dict | None = None) -> dict:
    if current_sources is None:
        try:
            current_sources = capture_sources()
        except (OSError, RuntimeError, ValueError) as exc:
            current_sources = {'capture_error': str(exc)}
    gaps = []
    comparisons = []
    builds = {}
    changed_builds = set()
    executions, directories, stores = set(), set(), set()
    campaign_gaps = []
    baseline = None
    expected_order = [(repeat, backend) for repeat in range(repeats) for backend in backend_order(repeat)]
    if repeats < POLICY['minimum_repeats']:
        campaign_gaps.append('fewer than required paired repeats')
    if not isinstance(legs, list):
        legs = []
    valid_legs = [leg for leg in legs if isinstance(leg, dict)
                  and type(leg.get('repeat')) is int and leg.get('backend') in POLICY['required_backends']]
    if (len(valid_legs) != len(legs)
            or [(leg['repeat'], leg['backend']) for leg in valid_legs] != expected_order):
        campaign_gaps.append('missing, duplicated or out-of-order legs; expected alternating backend order')
    for repeat in range(repeats):
        selected = [row for row in valid_legs if row['repeat'] == repeat]
        pair = {row['backend']: row['report'] if isinstance(row.get('report'), dict) else {}
                for row in selected}
        authorities = {backend: report['authority'] if isinstance(report.get('authority'), dict) else {}
                       for backend, report in pair.items()}
        if len(selected) != len(POLICY['required_backends']):
            gaps.append(f'repeat {repeat}: missing or duplicated leg')
        completed = True
        for row in selected:
            report = row.get('report')
            exit_code = row.get('exit_code')
            verdict = report.get('passed') if isinstance(report, dict) else None
            if type(row.get('timed_out', False)) is not bool:
                outcome = 'malformed timeout flag'
            elif row.get('timed_out') is True:
                outcome = 'leg timed out'
            elif row.get('error') or type(exit_code) is not int or exit_code not in (0, 1):
                outcome = f'abnormal leg exit ({exit_code!r})'
            elif row.get('report_read_error') or obj(report).get('measurement_completed') is not True:
                outcome = 'measurement did not complete or report is unreadable'
            elif type(verdict) is not bool or exit_code != int(not verdict):
                outcome = 'missing or contradictory completed leg verdict'
            else:
                execution = report.get('execution_id')
                authority = obj(report.get('authority'))
                directory = authority_path(authority.get('authority_directory'))
                device, inode = authority.get('volume_device'), authority.get('volume_inode')
                if (isinstance(execution, str) and re.fullmatch(r'[0-9a-f]{32}', execution)
                        and directory not in (None, '/')
                        and type(device) is int and device >= 0 and type(inode) is int and inode > 0
                        and execution not in executions and directory not in directories
                        and (device, inode) not in stores):
                    executions.add(execution)
                    directories.add(directory)
                    stores.add((device, inode))
                    continue
                outcome = 'duplicate or missing execution/authority identity'
            completed = False
            campaign_gaps.append(f"repeat {repeat}: {row['backend']} {outcome}")
        if set(pair) != set(POLICY['required_backends']):
            gaps.append(f'repeat {repeat}: missing backend')
            continue
        evaluations = {backend: assess(report, current_sources=current_sources)
                       for backend, report in pair.items()}
        for backend, evaluation in evaluations.items():
            report = pair[backend]
            if report.get('backend') != backend or not evaluation['current_source_qualified']:
                continue
            if backend == 'sqlite':
                launch = report['sqlite_loaded_artifacts']['launch']
                identity = tuple(launch[kind]['sha256'] for kind in ('sqlite', 'vfs'))
            else:
                identity = report['provenance']['artifact_before']
            if builds.setdefault(backend, identity) != identity:
                changed_builds.add(backend)
        for backend, verdict in evaluations.items():
            if pair[backend].get('passed') is not verdict['passed']:
                completed = False
                campaign_gaps.append(f'repeat {repeat}: {backend} recorded verdict/exit contradicts assessment')
        labels_match = all(backend == report.get('backend') for backend, report in pair.items())
        if not labels_match:
            gaps.append(f'repeat {repeat}: leg backend differs from measured authority')
        for backend, report in pair.items():
            verdict = evaluations[backend]
            if not verdict['coverage_passed']:
                gaps.append(f'repeat {repeat}: {backend} coverage refused')
            if not verdict['correctness_passed']:
                gaps.append(f'repeat {repeat}: {backend} correctness failed')
            if not verdict['current_source_qualified'] or not verdict['latency_comparison_eligible']:
                gaps.append(f'repeat {repeat}: {backend} provenance or timing evidence refused')
            if any(not metric['passed'] for metric in verdict['metrics'].values()):
                gaps.append(f'repeat {repeat}: {backend} absolute latency budget or sample check failed')
            if authorities[backend].get('filesystem') != filesystem:
                gaps.append(f'repeat {repeat}: {backend} authority volume not verified')
        shape = ('conversations', 'rounds', 'chunk_bytes', 'stream_interval_ms', 'stream_event_kind', 'carry_stream_events', 'terminal_barrier',
                 'restart_while_running', 'durability', 'benchmark_source_sha256', 'rpc_deadline_seconds')
        same_sources = all(item['current_source_qualified'] for item in evaluations.values())
        def pipeline_shape(report):
            if 'delivery_pipeline' not in report:
                return None
            pipeline = report['delivery_pipeline']
            if not isinstance(pipeline, dict):
                return ('invalid-pipeline',)
            return tuple(pipeline.get(key) for key in (
                'contract', 'submission_mode', 'source_manifest_sha256'))

        same_shape = (labels_match and same_sources
                      and pipeline_shape(pair['tofudb']) == pipeline_shape(pair['sqlite'])
                      and all(pair['tofudb'].get(key) == pair['sqlite'].get(key) for key in shape))
        source_known = all(isinstance(item.get('benchmark_source_sha256'), str)
                           and re.fullmatch(r'[0-9a-f]{64}', item['benchmark_source_sha256'])
                           for item in pair.values())
        if not source_known:
            gaps.append(f'repeat {repeat}: benchmark source identity missing or invalid')
        devices = [item.get('volume_device') for item in authorities.values()]
        same_volume = (filesystem not in ('', 'unknown') and all(item.get('filesystem') == filesystem for item in authorities.values())
                       and all(isinstance(device, int) and not isinstance(device, bool) and device >= 0
                               for device in devices) and len(set(devices)) == 1)
        if not same_volume:
            gaps.append(f'repeat {repeat}: common authority device not verified')
        if not same_shape:
            campaign_gaps.append(f'repeat {repeat}: workload, durability or harness identity differs')
        if not same_volume or any(not (v['coverage_passed'] and v['correctness_passed']
                                      and v['latency_comparison_eligible']) for v in evaluations.values()):
            campaign_gaps.append(f'repeat {repeat}: structural comparison evidence ineligible')
        if same_volume and same_shape:
            report = pair['tofudb']
            current = (tuple(report.get(key) for key in shape), pipeline_shape(report),
                       authorities['tofudb'].get('client_mode'), devices[0])
            if baseline is None:
                baseline = current
            elif baseline != current:
                campaign_gaps.append(f'repeat {repeat}: workload or authority device changed between repeats')
        for metric in POLICY['p99_budget_ms']:
            native = evaluations['tofudb']['metrics'][metric]['p99_ms']
            sqlite = evaluations['sqlite']['metrics'][metric]['p99_ms']
            complete = all(item['correctness_passed'] and item['coverage_passed']
                           and item['current_source_qualified']
                           and item['latency_comparison_eligible']
                           and item['metrics'][metric]['complete']
                           for item in evaluations.values())
            valid = (completed and complete and same_shape and source_known and same_volume
                     and native is not None and sqlite is not None and sqlite > 0)
            ratio = native / sqlite if valid else None
            valid = valid and number(ratio)
            wins = valid and native <= sqlite
            comparisons.append({'repeat': repeat, 'metric': metric, 'tofudb_p99_ms': native,
                                'sqlite_p99_ms': sqlite, 'tofudb_over_sqlite': ratio if valid else None,
                                'comparable': valid, 'passed': wins})
    campaign_gaps.extend(f'{backend} build identity changed between repeats' for backend in sorted(changed_builds))
    gaps.extend(campaign_gaps)
    qualified = {metric: sum(row['comparable'] for row in comparisons if row['metric'] == metric)
                 for metric in POLICY['p99_budget_ms']}
    for row in comparisons:
        label = f"repeat {row['repeat']}: {row['metric']}"
        if campaign_gaps or qualified[row['metric']] < POLICY['minimum_repeats']:
            row.update(comparable=False, passed=False, tofudb_over_sqlite=None)
        if not row['comparable']:
            gaps.append(f'{label} ineligible for comparison')
        elif not row['passed']:
            gaps.append(f'{label} native P99 slower than SQLite')
    return {'passed': not gaps, 'failures': gaps, 'comparisons': comparisons,
            'scope': 'bounded-scheduled-stream-burst-and-recovery', 'uncovered': POLICY['uncovered']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-client', choices=('application', 'embedded'), default='application')
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--conversations', type=int, default=1000)
    parser.add_argument('--rounds', type=int, default=4)
    parser.add_argument('--chunk-bytes', type=int, default=8192)
    parser.add_argument('--stream-event-kind', choices=('phase', 'delta'), default='phase')
    parser.add_argument('--terminal-barrier', action='store_true')
    parser.add_argument('--carry-stream-events', action='store_true')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--stream-interval-ms', type=float, default=1000)
    parser.add_argument('--leg-timeout', type=float, default=900)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 30 <= args.leg_timeout <= 3600:
        parser.error('repeats=1..10, leg-timeout=30..3600')
    if not args.directory.is_dir() or not args.binary.is_file():
        parser.error('directory and binary must already exist')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    artifacts = args.output.parent / (args.output.stem + '-legs')
    artifacts.mkdir(exist_ok=False)
    report = {'format': 'tofu.conversation-comparison/v2', 'policy': POLICY,
              'filesystem': _mount_type(args.directory), 'legs': []}
    for repeat in range(args.repeats):
        order = backend_order(repeat)
        for backend in order:
            name = f'{repeat}-{backend}'
            output = artifacts / (name + '.json')
            log = artifacts / (name + '.log')
            command = [sys.executable, str(Path(__file__).with_name('conversation_concurrency.py')),
                       '--backend', backend, '--native-client', args.native_client, '--binary', str(args.binary.resolve()),
                       '--directory', str(args.directory.resolve()), '--output', str(output.resolve()),
                       '--conversations', str(args.conversations), '--rounds', str(args.rounds),
                       '--chunk-bytes', str(args.chunk_bytes), '--restart-while-running',
                       '--stream-interval-ms', str(args.stream_interval_ms),
                       '--stream-event-kind', args.stream_event_kind]
            if args.terminal_barrier:
                command.append('--terminal-barrier')
            if args.carry_stream_events:
                command.append('--carry-stream-events')
            print(f'Running repeat={repeat} backend={backend}', flush=True)
            leg = {'repeat': repeat, 'backend': backend, 'log': str(log), 'output': str(output)}
            with log.open('w') as stream:
                process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                try:
                    leg['exit_code'] = process.wait(timeout=args.leg_timeout)
                except subprocess.TimeoutExpired:
                    # Kill the private process group, including its scratch daemon.
                    import os
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                    leg['exit_code'] = process.wait(timeout=30)
                    leg['timed_out'] = True
                    leg['error'] = 'leg deadline exceeded; incomplete measurements retained'
            collect_child_report(leg, output)
            report['legs'].append(leg)
            report.pop('acceptance', None)
            publish_json(args.output, report)
            report['acceptance'] = compare(report['legs'], args.repeats, report['filesystem'])
            publish_json(args.output, report)
    print(json.dumps(report['acceptance'], indent=2))
    return 0 if report['acceptance']['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
