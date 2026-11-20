"""Assess measured conversation evidence; no timing assertions in unit CI.

Policy owner: conversation_concurrency_contract.json. Used by single-run and
paired reports. A correctness pass, a missing sample, or a microbenchmark must
never be promoted to an interactive-latency/capacity pass.
"""
from __future__ import annotations

import json
import signal
from pathlib import Path

from benchmarks.conversation_provenance import (
    FORMAT as PROVENANCE_FORMAT,
    capture_sources,
    qualify_pipeline_sources,
    qualify_provenance,
    sqlite_identity_failure,
)

from benchmarks.conversation_receipts import (
    TERMINAL, authority_path, count, full_sqlite, latency_percentiles, latency_phase_gaps, linux_pid,
    metric_origin_gaps, number, obj, unexpected_terminal_evidence, workload_complete,
)

POLICY = json.loads(Path(__file__).with_name('conversation_concurrency_contract.json').read_text())


REPORT_FORMAT = 'tofu.conversation-concurrency/v5'
WRITER_METRICS_BOUNDARIES = ('admission_storage_metrics', 'productive_start_storage_metrics', 'storage_metrics')
CRASH_CUTPOINTS = ('all_running_before_stream_release', 'after_settlement')
SQLITE_BOUNDARIES = ('launch', 'admission_storage_metrics', 'reopen_running',
                     'productive_start_storage_metrics', 'storage_metrics', 'reopen_final', 'final')


def crash_coverage(report, authority):
    receipts = report.get('crash_recovery_receipts')
    directory = authority_path(authority.get('authority_directory'))
    if not isinstance(receipts, dict) or not isinstance(directory, str) or not directory:
        return False
    chain = []
    for name in CRASH_CUTPOINTS:
        row = receipts.get(name)
        if (not isinstance(row, dict) or type(row.get('exit_code')) is not int
                or row['exit_code'] != -signal.SIGKILL
                or authority_path(row.get('authority_directory')) != directory):
            return False
        pids = [row.get(key) for key in ('killed_pid', 'reopened_pid')]
        if not all(map(linux_pid, pids)) or pids[0] == pids[1]:
            return False
        chain.append(pids)
    return chain[0][1] == chain[1][0]


def assess(report: dict, *, current_sources: dict | None = None) -> dict:
    report = obj(report)
    receipt = report.get('provenance')
    # An unsupported receipt cannot be qualified by any checkout inventory.
    # Supported receipts still require a fresh capture, including local edits.
    if (current_sources is None and isinstance(receipt, dict)
            and receipt.get('format') == PROVENANCE_FORMAT):
        try:
            current_sources = capture_sources()
        except (OSError, RuntimeError, ValueError) as exc:
            current_sources = {'capture_error': str(exc)}
    authority = report.get('authority')
    authority = authority if isinstance(authority, dict) else {}
    provenance = qualify_provenance(
        receipt, backend=report.get('backend'),
        binary_sha256=report.get('binary_sha256'), authority=authority,
        current_sources=current_sources)
    extra = ('report missing current evidence format; replay historical reports with their pinned checker'
             if report.get('format') != REPORT_FORMAT else
             sqlite_identity_failure(report, SQLITE_BOUNDARIES) if report.get('backend') == 'sqlite' else None)
    if extra:
        provenance['failures'].append(extra)
        provenance['frozen_failures'].append(extra)
        provenance['current_source_qualified'] = provenance['frozen_build_comparable'] = False
    failures = list(provenance['failures'])
    sources = report.get('provenance', {})
    sources = sources.get('sources_before', {}) if isinstance(sources, dict) else {}
    files = sources.get('files', {}) if isinstance(sources, dict) else {}
    if 'delivery_pipeline' in report:
        pipeline_provenance = qualify_pipeline_sources(report['delivery_pipeline'], sources=sources)
        failures.extend(pipeline_provenance['failures'])
        for key in ('failures', 'frozen_failures'):
            provenance[key].extend(pipeline_provenance[key])
        for key in ('current_source_qualified', 'frozen_build_comparable'):
            provenance[key] = provenance[key] and pipeline_provenance[key]
    if (not isinstance(files, dict)
            or report.get('benchmark_source_sha256') != files.get('benchmarks/conversation_concurrency.py')
            or not report.get('benchmark_source_sha256')):
        reason = 'benchmark source identity does not match provenance receipt'
        failures.append(reason)
        provenance['failures'].append(reason)
        provenance['frozen_failures'].append(reason)
        provenance['current_source_qualified'] = False
        provenance['frozen_build_comparable'] = False
    # Inspect the observed settings as well as a wrapper's verdict. The plain
    # concurrency entry has no wrapper, and old diagnostic reports may omit it.
    requested_profile = report.get('requested_native_profile_every')
    profiled = requested_profile not in (None, 0, '0')
    profiled |= bool(report.get('requested_client_profile')) or 'client_stages_ms' in report
    def profile_enabled(profile):
        if not isinstance(profile, dict):
            return True  # A malformed observation cannot prove profiling off.
        return (bool(profile.get('enabled'))
                or profile.get('sampleEvery') not in (None, 0, '0'))

    for field in ('admission_storage_metrics', 'productive_start_storage_metrics',
                  'terminal_boundary_storage_metrics', 'storage_metrics'):
        envelope = report.get(field, {})
        profiled |= (not isinstance(envelope, dict)
                     or profile_enabled(envelope.get('requestProfile', {})))
    pipeline = report.get('delivery_pipeline', {})
    if isinstance(pipeline, dict):
        coordinator = pipeline.get('coordinator', {})
        profiled |= pipeline.get('delivery_profile_every', 0) not in (0, '0')
        profiled |= (not isinstance(coordinator, dict)
                     or profile_enabled(coordinator.get('performance_profile', {})))
    # Instrumentation eligibility is a separate dimension, not a source verdict.
    latency_eligible = report.get('latency_comparison_eligible') is not False and not profiled
    if not latency_eligible:
        failures.append('diagnostic profiling enabled or source provenance changed; latency comparison is not qualified')
    coverage = []
    sizes = {}
    for field in ('conversations', 'rounds', 'chunk_bytes'):
        required = POLICY['minimum_' + field]
        value = report.get(field)
        sizes[field] = value if type(value) is int and value >= 0 else 0
        if sizes[field] < required:
            coverage.append(f'{field}: {sizes[field]} < {required}')
    def measured_restart(field):
        return number(report.get(field))

    expected_conversations = sizes['conversations']
    if (report.get('restart_while_running') is not True
            or not measured_restart('running_restart_ms')
            or not count(report.get('peak_simultaneous_running'), expected_conversations)
            or not count(report.get('recovered_running'), expected_conversations)):
        coverage.append('running crash/reopen not measured for all requested conversations')
    if (not measured_restart('restart_ms')
            or not count(report.get('recovered_completed'), expected_conversations)
            or not count(report.get('recovered_exact_content'), expected_conversations)
            or (report.get('carry_stream_events')
                and not count(report.get('recovered_exact_replay'), expected_conversations))):
        coverage.append('post-completion crash/reopen not measured for all requested conversations')
    integrity_code = report.get('integrity_returncode')
    if not isinstance(integrity_code, int) or isinstance(integrity_code, bool) or integrity_code != 0:
        coverage.append('integrity verification not completed successfully')
    if authority.get('crash_scope') not in ('sigkill', 'in-process-abandon'):
        coverage.append('process-crash mechanism not verified')
    if authority.get('crash_scope') == 'sigkill' and not crash_coverage(report, authority):
        coverage.append('verified SIGKILL and same-store reopen receipts missing or contradictory')
    if authority.get('crash_scope') == 'in-process-abandon':
        coverage.append('in-process abandon is not a process-crash durability test')
    if (any(type(report.get(key)) is not bool
            for key in ('terminal_barrier', 'carry_stream_events', 'recover_interrupted'))
            or report.get('stream_event_kind') not in ('phase', 'delta')
            or (report.get('terminal_barrier') is False and unexpected_terminal_evidence(report))
            or TERMINAL in obj(report.get('sqlite_loaded_artifacts'))):
        coverage.append('workload mode flags malformed')
    if report.get('recover_interrupted'):
        coverage.append('streaming skipped for interrupted recovery')
    cadence = report.get('stream_interval_ms')
    if type(cadence) not in (int, float) or not 0 <= cadence <= POLICY['maximum_stream_interval_ms']:
        coverage.append('scheduled offered load not measured at required cadence')
    if (report.get('rpc_deadline_seconds') != POLICY['rpc_deadline_seconds']
            or authority.get('rpc_deadline_seconds') != POLICY['rpc_deadline_seconds']):
        coverage.append('common RPC deadline not verified for this backend')
    if report.get('durability') != 'strict':
        coverage.append('strict durability not verified')
    if report.get('backend') != 'sqlite' and 'sqlite_loaded_artifacts' in report:
        coverage.append('SQLite mapping evidence on a non-SQLite authority')
    coverage.extend(metric_origin_gaps(report, CRASH_CUTPOINTS))
    coverage.extend(latency_phase_gaps(report))
    boundaries = WRITER_METRICS_BOUNDARIES + ((TERMINAL,) if report.get('terminal_barrier') is True else ())
    if report.get('backend') == 'tofudb' and not all(
            isinstance(report.get(key), dict) and report[key].get('walDurability') == 'strict'
            for key in boundaries):
        coverage.append('effective native Strict durability missing or mismatched')
    if report.get('backend') == 'sqlite':
        witnesses = report.get('sqlite_loaded_artifacts')
        if obj(authority.get('sqlite_fastpath')).get('active') is not False:
            coverage.append('SQLite WAL non-relocation not verified')
        if TERMINAL in boundaries and not full_sqlite(obj(report.get(TERMINAL)).get('writer_configuration_at_open')):
            coverage.append('SQLite terminal writer WAL/FULL and active coalescing not verified')
        for boundary in SQLITE_BOUNDARIES:
            row = witnesses.get(boundary) if isinstance(witnesses, dict) else None
            config = row.get('writer_configuration_at_open') if isinstance(row, dict) else None
            if not full_sqlite(config):
                coverage.append(f'SQLite writer WAL/FULL and active coalescing not verified at {boundary}')
            if boundary in WRITER_METRICS_BOUNDARIES:
                metrics = report.get(boundary)
                provenance_rows = report.get('storage_metrics_provenance')
                origin = provenance_rows.get(boundary) if isinstance(provenance_rows, dict) else None
                incarnation = int(boundary != 'admission_storage_metrics')
                if (not isinstance(row, dict) or not isinstance(metrics, dict)
                        or not full_sqlite(metrics.get('writer_configuration_at_open'))
                        or metrics.get('writer_configuration_at_open') != config
                        or not isinstance(origin, dict) or origin.get('process_id') != row.get('pid')
                        or origin.get('authority_incarnation') != incarnation):
                    coverage.append(f'SQLite writer receipt contradicts source metrics or incarnation at {boundary}')
    measurements = obj(report.get('latency_ms'))
    metrics = {}
    for stage, budget in POLICY['p99_budget_ms'].items():
        summary = obj(measurements.get(stage))
        expected = expected_conversations * (sizes['rounds'] if stage == 'stream_scheduled' else 1)
        value = summary.get('p99')
        complete = expected > 0 and count(summary.get('count'), expected)
        valid = number(value)
        if summary and not latency_percentiles(summary, expected):
            coverage.append(f'{stage}: malformed or unordered latency percentiles')
            valid = False
        passed = complete and valid and value <= budget
        metrics[stage] = {'p99_ms': value if valid else None, 'complete': complete and valid, 'budget_ms': budget, 'samples': summary.get('count', 0),
                          'expected_samples': expected, 'passed': passed}
        if not passed:
            failures.append(f'{stage}: missing/invalid samples or P99 above {budget} ms')
    correctness = report.get('correctness_passed') is True and workload_complete(report, expected_conversations, sizes['rounds'])
    return {'policy': POLICY['format'], 'correctness_passed': correctness,
            'current_source_qualified': provenance['current_source_qualified'],
            'frozen_build_comparable': provenance['frozen_build_comparable'],
            'provenance': provenance,
            'latency_comparison_eligible': latency_eligible,
            'latency_passed': not failures, 'coverage_passed': not coverage,
            'passed': correctness and not failures and not coverage,
            'metrics': metrics, 'failures': failures, 'coverage_gaps': coverage,
            'uncovered': POLICY['uncovered']}
