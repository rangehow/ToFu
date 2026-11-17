"""The durable lead-audit stream: outside the logs/ budget, kept 7+ days, fed by lead evidence."""
import json
import os
import time

import pytest

from lib.logging_pkg import lead_audit, log_retention

pytestmark = pytest.mark.unit

LEAD = 'coordinator-4950501b927a581b666a974cb9ce21b5'


def records(logs_dir, kind=None):
    rows = []
    for path in sorted(lead_audit.stream_directory(logs_dir).glob('lead-audit-*.jsonl')):
        rows += [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if kind is None or row['kind'] == kind]


def test_records_survive_global_budget_eviction_of_ordinary_logs(tmp_path, monkeypatch):
    logs = tmp_path / 'logs'
    logs.mkdir()
    assert lead_audit.record('authority_refusal', {'conv': LEAD, 'code': 'x'}, logs_dir=logs)
    rotated = logs / 'app.log.2026-11-10'
    rotated.write_bytes(b'x' * 4096)
    three_days_ago = time.time() - 3 * 86_400
    os.utime(rotated, (three_days_ago, three_days_ago))
    monkeypatch.setattr(log_retention, 'total_log_budget_bytes', lambda: 1)
    result = log_retention.maintain_logs(logs)
    assert [item['reason'] for item in result['removed']] == ['global_budget']
    assert not rotated.exists()
    assert [row['code'] for row in records(logs)] == ['x']
    assert not any('lead-audit' in item['path'] for item in result['unmanaged'])


def test_files_of_today_and_the_previous_seven_days_are_kept(tmp_path):
    logs = tmp_path / 'logs'
    directory = lead_audit.stream_directory(logs)
    directory.mkdir(parents=True)
    now = time.mktime((2026, 11, 18, 12, 0, 0, 0, 0, -1))
    for day in ('2026-11-09', '2026-11-10', '2026-11-11', '2026-11-17'):
        (directory / f'lead-audit-{day}.jsonl').write_text('{}\n')
    (directory / 'unrelated.txt').write_text('kept')
    assert lead_audit.record('restart_outcome', {'outcome': 'ready'}, logs_dir=logs, now=now)
    assert sorted(path.name for path in directory.iterdir()) == [
        'lead-audit-2026-11-11.jsonl', 'lead-audit-2026-11-17.jsonl',
        'lead-audit-2026-11-18.jsonl', 'unrelated.txt']


def test_an_unwritable_stream_never_raises(tmp_path):
    blocker = tmp_path / 'logs'
    blocker.write_text('not a directory')
    assert lead_audit.record('restart_outcome', {}, logs_dir=blocker) is False


def test_lead_tool_refusals_are_recorded_and_worker_errors_are_not(tmp_path, monkeypatch):
    from lib.tasks_pkg.handlers import _adapter
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: tmp_path)
    refusal = json.dumps({'error': 'Synthetic authorization origin denied',
                          'code': 'nonhuman_request', 'recovery': 'ask the owner'})
    _adapter._record_lead_refusal({'id': 't1', 'convId': LEAD, '_turnId': 'turn-1'},
                                  'delegate_project_work', refusal)
    _adapter._record_lead_refusal({'id': 't2', 'convId': 'worker-1'}, 'delegate_project_work', refusal)
    _adapter._record_lead_refusal({'id': 't3', 'convId': LEAD}, 'run_command', 'Error: exit 1')
    (row,) = records(tmp_path, 'authority_refusal')
    assert row['task'] == 't1' and row['turn'] == 'turn-1' and row['tool'] == 'delegate_project_work'
    assert (row['code'], row['error']) == ('nonhuman_request', 'Synthetic authorization origin denied')


def test_slow_lead_round_preparation_is_recorded(tmp_path, monkeypatch):
    from lib.tasks_pkg import round_prep_timing as rpt
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: tmp_path)
    from types import SimpleNamespace
    clock = [100.0]
    monkeypatch.setattr(rpt, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    for conv in (LEAD, 'worker-1'):
        task = {'id': 'task-1', 'convId': conv}
        rpt.begin_round_prep(task, 0)
        with rpt.round_prep_step(task, 'receipts'):
            clock[0] += 2.0
        rpt.finish_round_prep(task, request_tag='R1')
    (row,) = records(tmp_path, 'round_prep_timing')
    assert row['conversation_id'] == LEAD and row['stepsMs']['receipts'] == 2000.0


def test_a_capped_lead_trace_keeps_every_round_aggregate(tmp_path, monkeypatch):
    from lib.tasks_pkg import turn_trace
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: tmp_path)
    origin, rows, events = 1_000_000, [], []
    rounds = 200
    for n in range(rounds):
        base = n * 1_000
        events += [
            ('round_start', base, {'roundNum': n}),
            ('model_request_start', base + 100, {'spanId': f'm{n}', 'requestLevel': 'logical'}),
            ('model_request_complete', base + 400, {'spanId': f'm{n}', 'status': 'succeeded'}),
            ('tool_start', base + 400, {'roundNum': n, 'toolCallId': f'c{n}', 'toolName': 'poll',
                                        'tStart': origin + base + 400}),
            ('tool_result', base + 900, {'roundNum': n, 'toolCallId': f'c{n}', 'status': 'done',
                                         'tEnd': origin + base + 900}),
            ('round_end', base + 950, {'roundNum': n, 'reason': 'tools'}),
        ]
    for index, (kind, offset, payload) in enumerate(events):
        rows.append({'event_id': index, 'type': kind, 'ts_ms': origin + offset,
                     'payload': {'type': kind, 'executionClockId': 'clock',
                                 'executionOriginAtMs': origin,
                                 'executionAtMs': origin + offset, **payload}})
    monkeypatch.setattr(turn_trace, '_read_trace_rows', lambda _: rows)
    end = origin + rounds * 1_000
    projection = turn_trace.finalize_trace_projection(
        {}, {'id': 'lead-task', 'convId': LEAD, '_turnId': 'turn-1'},
        {'type': 'done', 'finishReason': 'stop', 'executionAtMs': end},
        now_ms=end, pending_sequence=len(rows))
    assert projection['timingTrace']['droppedSpans'] > 0
    (summary,) = records(tmp_path, 'turn_trace_summary')
    document_summary = dict(projection['timingTrace']['summary'])
    document_summary.pop('overBudget')
    assert summary['rounds'] == rounds and summary['summary'] == document_summary
    (trace,) = records(tmp_path, 'turn_trace_rounds')
    table = [dict(zip(trace['columns'], row)) for row in trace['rows']]
    assert len(table) == rounds
    assert all((r['roundPrepMs'], r['llmMs'], r['toolMs'], r['requests'], r['tools'])
               == (100, 300, 500, 1, 1) for r in table)
    assert all(r['totalMs'] == 1_000 for r in table[:-1])
    assert sum(r['toolMs'] for r in table) == projection['timingTrace']['summary']['toolMs']
