"""The harness scorecard (scripts/harness_metrics.py, docs/METRICS.md) counts what it claims."""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import harness_metrics as hm

pytestmark = pytest.mark.unit

H = 3_600_000  # one hour in ms
# Local midnight in whatever zone the run uses: the scorecard counts local days.
DAY0 = int(dt.datetime(2026, 11, 8).timestamp() * 1000)


def _turn(conv, turn, created, *, actor='assistant', kind='reply', status='completed', updated=None,
          tools=(), cause='', text=None, initiator=None, forwarded=False, presentation='', cost=None):
    row = {'conv': conv, 'turn': turn, 'ordinal': created // 1000, 'lane': 'main', 'kind': kind,
           'actor': actor, 'status': status, 'cause': cause, 'error': '', 'created': created,
           'updated': updated if updated is not None else created + 60_000, 'parent': None,
           'attempt': None, 'presentation': presentation, 'initiator': initiator,
           'forwarded': forwarded, 'channel': None}
    if text is not None:
        row['text'] = text
    if tools:
        row['tools'] = list(tools)
    if cost is not None:
        row['costUsd'] = cost
    return row


def _tool(name, start=0, **extra):
    return {'name': name, 'attention': extra.pop('attention', 'important'), 'status': extra.pop('status', 'done'),
            'exec': extra.pop('exec', 'ok'), 'start': start, 'end': extra.pop('end', start + 1000), **extra}


def _evidence(**parts):
    evidence = {'version': 1, 'generatedAt': DAY0 + 24 * H, 'window': {'start': DAY0, 'end': DAY0 + 24 * H},
                'conversations': {}, 'turns': [], 'attempts': [], 'tasks': {}, 'assignments': [],
                'approvals': [], 'gaps': []}
    evidence.update(parts)
    return evidence


def _meta(role, created=DAY0 - H):
    return {'role': role, 'created': created, 'updated': DAY0 + H, 'title': '', 'source': None, 'project': None}


@pytest.mark.parametrize(('text', 'context', 'expected'), [
    ('开', {}, 'approve'),
    ('删除', {}, 'approve'),
    ('可以，开始吧', {}, 'approve'),
    ('用方案B', {'after_question': True}, 'approve'),
    ('这个负责人又挂了，你自己修复一下。然后继续推进。', {}, 'recover'),
    ('随便说点什么', {'after_failure': True}, 'recover'),
    ('不对，应该先跑基准再改代码。', {}, 'correct'),
    ('换个思路', {'after_stop': True}, 'correct'),
    ('Why did you skip the tests?', {}, 'correct'),
    ('我说过了，先发报告。', {}, 'repeat'),
    ('把周报发给我看看吧', {'earlier': ['把周报发给我看看']}, 'repeat'),
    ('我想做一篇论文，方向是高效的知识注入。不要用太多算力，也别重复别人的工作。', {}, 'new'),
    ('是不是可以并行跑？', {}, 'new'),
    ('Please download the markdown report', {}, 'new'),
    ('A', {}, 'approve'),
    ('行，那你把待办事项都推进一下吧。', {}, 'approve'),
    ('我先去申请账号。安卓这个你定就好', {}, 'approve'),
    ('你别管署名了。先跑起来', {}, 'correct'),
    ('那你要优化这个机制啊', {}, 'correct'),
    ('这个进度推进得太慢了。', {}, 'repeat'),
    ('你看到的解析结果对不对？我不清楚够不够稳', {}, 'new'),
])
def test_owner_messages_are_classified_by_their_first_sentence_and_context(text, context, expected):
    assert hm.classify_owner_message(text, **context) == expected


def test_only_the_owners_own_words_count_and_browser_actions_join_them():
    gr, router = 'coordinator-g', 'coordinator-p'
    evidence = _evidence(
        conversations={gr: _meta('global_router'), router: _meta('project_router')},
        turns=[
            _turn(gr, 'q', DAY0 + H, kind='coordinator_report.decision'),
            _turn(gr, 'a', DAY0 + 2 * H, actor='human', kind='input', text='开', initiator='human'),
            _turn(router, 'fwd', DAY0 + 2 * H, actor='human', kind='input', text='开', initiator='human',
                  forwarded=True),
            _turn(router, 'notice', DAY0 + 3 * H, actor='human', kind='input', text='继续', initiator='proactive'),
            _turn(router, 'brief', DAY0 + 4 * H, actor='human', kind='input', text='写一份新的周报'),
        ],
        attempts=[{'conv': router, 'attempt': 'x', 'turn': 'r', 'operation': 'regenerate', 'status': 'completed',
                   'command': 'regenerate:c6fe333c-c3b9-47e0-85f4-7ad98b0d4aeb:3', 'created': DAY0 + 5 * H,
                   'started': DAY0 + 5 * H, 'settled': None},
                  {'conv': router, 'attempt': 'y', 'turn': 's', 'operation': 'continue', 'status': 'completed',
                   'command': 'session-control:abc', 'created': DAY0 + 6 * H, 'started': None, 'settled': None}],
        approvals=[{'action': 'restart', 'status': 'consumed', 'requested': (DAY0 + 7 * H) / 1000,
                    'decided': (DAY0 + 7 * H) / 1000, 'decidedBy': 'ui'},
                   {'action': 'restart', 'status': 'consumed', 'requested': (DAY0 + 8 * H) / 1000,
                    'decided': (DAY0 + 8 * H) / 1000, 'decidedBy': 'environment-override'}])
    result = hm.owner_interventions(evidence, hm.window_days(DAY0, DAY0 + 24 * H))
    assert result['messages'] == 2  # the forward and the proactive notice are not the owner's words
    # The browser Regenerate and the browser restart approval are the owner's; a Router's
    # session-control Continue and a gate-passed environment override are not.
    assert result['uiActions'] == 2
    assert result['byClass'] == {'approve': 2, 'new': 1, 'recover': 1}
    assert result['interventions'] == 3 and result['meanPerDay'] == 3


def _router_turn(turn, created, status, *, updated, tools=0):
    return _turn('r', turn, created, kind='scheduled_reply', status=status, updated=updated,
                 tools=[_tool('send_project_message')] * tools)


def test_outage_runs_from_the_last_service_to_the_next_turn_that_works():
    turns = [
        _router_turn('long', 0, 'interrupted', updated=7 * H, tools=50),  # restart after real work
        _router_turn('queued', 1 * H, 'failed', updated=8 * H),            # queued, failed without work
        _router_turn('fail', 9 * H, 'failed', updated=9 * H + 60_000),
        _router_turn('ok', 10 * H, 'completed', updated=11 * H, tools=3),
        _router_turn('quick', 12 * H, 'failed', updated=12 * H + 60_000),
    ]
    attempts = {'queued': {'started': 7 * H + 5 * 60_000}, 'ok': {'started': 10 * H + 30 * 60_000}}
    assert hm.router_outages(turns, attempts, 24 * H) == [(7 * H, 10 * H + 30 * 60_000), (12 * H, 24 * H)]


def test_availability_counts_each_router_once_and_skips_routers_that_never_ran():
    evidence = _evidence(
        conversations={'r': _meta('project_router'), 'idle': _meta('project_router'), 'w': _meta('worker')},
        turns=[_router_turn('a', DAY0 + H, 'failed', updated=DAY0 + H + 1000),
               _router_turn('b', DAY0 + 7 * H, 'completed', updated=DAY0 + 8 * H, tools=1),
               _turn('idle', 'c', DAY0 + H, kind='scheduled_reply', status='running'),
               _turn('w', 'd', DAY0 + H, status='failed')],
        attempts=[{'conv': 'r', 'turn': 'b', 'attempt': 'b1', 'started': DAY0 + 7 * H}])
    result = hm.availability(evidence, [])
    assert set(result['routers']) == {'r'}
    row = result['routers']['r']
    assert (row['turns'], row['completed'], row['successRate']) == (2, 1, 0.5)
    assert row['downMinutes'] == 6 * 60 and row['availability'] == pytest.approx(0.75)
    assert result['worstAvailability'] == pytest.approx(0.75)


def test_worker_start_success_and_latency_follow_the_birth_not_the_call():
    router = 'coordinator-p'
    calls = [
        _tool('delegate_project_work', DAY0 + H, result={'assignment_id': 'a1', 'conversation_id': 'w1',
                                                          'status': 'starting'}),
        _tool('delegate_project_work', DAY0 + H + 5000, result={'assignment_id': 'a1', 'conversation_id': 'w1',
                                                                 'status': 'starting'}),  # replay
        _tool('delegate_project_work', DAY0 + 2 * H, result={'assignment_id': 'a2', 'conversation_id': 'w2',
                                                              'status': 'starting'}),
        _tool('delegate_project_work', DAY0 + 3 * H, exec='error', result={'unparsed': 'refused'}),
        _tool('delegate_project_work', DAY0 + 3 * H, status='rejected', exec='rejected'),
        _tool('delegate_project_work', DAY0 + 4 * H, result={'assignment_id': 'a3', 'conversation_id': 'w3',
                                                              'status': 'starting'}),
        _tool('delegate_project_work', DAY0 + 5 * H, result={'assignment_id': BIRTH_REFUSED,
                                                              'conversation_id': 'w4', 'status': 'starting'}),
        _tool('delegate_project_work', DAY0 + 5 * H, result={'assignment_id': REPLAYED,
                                                              'conversation_id': 'w5', 'status': 'starting'}),
    ]
    # The births of the last two were refused after the calls returned; only the
    # next Worker status read says so. A replay later started w5.
    status_read = _slim_status_read(f'Delegations {BIRTH_REFUSED}, {REPLAYED} could not start a Worker '
                                    '(status failed_to_start): act on each error and its recovery.')
    evidence = _evidence(
        conversations={router: _meta('project_router')},
        turns=[_turn(router, 't', DAY0 + H, tools=calls),
               _turn(router, 'u', DAY0 + 6 * H, tools=[status_read])],
        tasks={'w1': [{'task': 'k', 'created': DAY0 + H + 20_000}],
               'w3': [{'task': 'm', 'created': DAY0 - 24 * H}],  # w3 was born before the call
               'w5': [{'task': 'n', 'created': DAY0 + 7 * H}]})
    result = hm.worker_starts(evidence, hm.window_days(DAY0, DAY0 + 24 * H))
    assert result['outcomes'] == {'started': 2, 'not_started': 1, 'refused': 2}
    assert result['rejectedCalls'] == 1
    assert result['successRate'] == pytest.approx(2 / 5)
    assert result['latencySeconds'] == {'p50': 20.0, 'p95': 7200.0, 'n': 2}


BIRTH_REFUSED = '202079ab-0f44-5fdf-aadf-bea22ba018f4'
REPLAYED = '4a092c7c-aa8e-5d97-bbf0-fcac323c4982'


def _slim_status_read(instruction):
    content = json.dumps({'instruction': instruction, 'wait': 'settled', 'workers': []})
    return hm._slim_tool({'toolName': 'poll', 'attentionKind': 'routine', 'status': 'done',
                          'toolResultEvidence': {'executionStatus': 'ok'}, 'tStart': DAY0 + 6 * H,
                          'tEnd': DAY0 + 6 * H, 'toolArgs': '{}', 'toolContent': content})


def test_mttr_reads_signal_times_from_notices_and_resolve_results_also_when_truncated():
    watchdog = 'coordinator-w'
    opened_ms = DAY0 + 9 * H
    opened = dt.datetime.fromtimestamp(opened_ms / 1000, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    truncated = ('summary (text; 12000 chars):\n{"resolved":[{"createdAt":' + str(DAY0 + 10 * H)
                 + ',"error":{"detail":"x"},"owner":1,"resolution":"r","resolvedAt":' + str(DAY0 + 11 * H)
                 + ',"signalId":"' + 'b' * 32 + '","source":"turn","state"')
    evidence = _evidence(
        conversations={watchdog: _meta('watchdog')},
        turns=[
            _turn(watchdog, 'n', opened_ms, actor='human', kind='input', initiator='peer',
                  text=f'Signal: {"a" * 32} (source=recovery, opened {opened} UTC)\nSubject: x'),
            _turn(watchdog, 'r', DAY0 + 12 * H, tools=[
                _tool('watchdog_resolve_signals', DAY0 + 12 * H, end=DAY0 + 12 * H,
                      args={'signal_ids': ['a' * 32]}, result={'records': hm._resolved_records(truncated)})]),
        ])
    result = hm.mttr(evidence, [])
    assert result['signalsOpened'] == 2 and result['resolved'] == 2
    assert result['mttrMinutes']['p50'] == 60 and result['mttrMinutes']['mean'] == 120
    assert result['bySource'] == {'recovery': {'n': 1, 'p50': 180.0}, 'turn': {'n': 1, 'p50': 60.0}}


def test_throughput_counts_the_result_turn_settled_in_the_window_and_cost_divides_by_it():
    evidence = _evidence(
        conversations={'w1': _meta('worker'), 'w2': _meta('worker'), 'w3': _meta('worker')},
        turns=[_turn('w1', 'r1', DAY0 + H, updated=DAY0 + 2 * H, cost=30.0),
               _turn('w2', 'r2', DAY0 + H, updated=DAY0 + 3 * H, status='failed', cost=10.0),
               _turn('w3', 'r3', DAY0 + H, status='running', cost=20.0)],
        assignments=[
            {'assignment': 'a1', 'worker': 'w1', 'execution': 'completed', 'executionTurn': 'r1',
             'delivery': 'delivered', 'publishedAt': (DAY0 + 30 * H) / 1000},
            {'assignment': 'a0', 'worker': 'w1', 'execution': 'completed', 'executionTurn': 'older',
             'delivery': 'delivered', 'publishedAt': None},  # its result Turn is outside the window
            {'assignment': 'a2', 'worker': 'w2', 'execution': 'failed', 'executionTurn': 'r2', 'delivery': '',
             'publishedAt': None},
            {'assignment': 'a3', 'worker': 'w3', 'execution': 'running', 'executionTurn': 'r3',
             'delivery': 'working', 'publishedAt': None}])
    days = hm.window_days(DAY0, DAY0 + 24 * H)
    flow = hm.throughput(evidence, days)
    assert (flow['completed'], flow['delivered']) == (1, 1)
    assert flow['deliveredPerDay'] == {'2026-11-08': 1}  # published after the window: clipped to its end
    assert flow['endedUnsuccessfully'] == {'failed': 1}
    spend = hm.cost(evidence, days, flow['completed'])
    assert spend['totalUsd'] == 60.0 and spend['perCompletedTaskUsd'] == 60.0


def test_owner_needed_share_counts_questions_decisions_approval_tools_and_lifecycle_approvals():
    evidence = _evidence(
        conversations={'w': _meta('worker'), 'p': _meta('project_router')},
        turns=[_turn('w', 't', DAY0 + H, tools=[_tool('ask_human'), _tool('run_command'),
                                                _tool('send_file', attention='interactive'),
                                                _tool('edit_file', status='rejected')]),
               _turn('p', 'u', DAY0 + H, tools=[_tool('report_to_user', args={'kind': 'decision'}),
                                                _tool('report_to_user', args={'kind': 'progress'})])],
        approvals=[{'action': 'restart', 'status': 'expired', 'requested': (DAY0 + 2 * H) / 1000},
                   {'action': 'restart', 'status': 'expired', 'requested': (DAY0 - 2 * H) / 1000}])
    result = hm.owner_needed_share(evidence, hm.window_days(DAY0, DAY0 + 24 * H))
    assert result['byKind'] == {'ask_human': 1, 'approval_tool': 1, 'decision_report': 1, 'lifecycle_approval': 1}
    assert (result['ownerNeeded'], result['actions']) == (4, 6)


class _FakeReader:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, path, params=None, *, accept=(200,)):
        self.calls.append((path, dict(params or {})))
        return self.pages.pop(0)


def _wire_turn(turn, created):
    return {'turnId': turn, 'ordinal': created, 'kind': 'reply', 'actor': 'assistant', 'status': 'completed',
            'createdAt': created, 'updatedAt': created, 'settlement': {}, 'projection': {}}


def test_history_is_paged_only_back_to_the_window_and_a_stale_page_is_a_reported_gap():
    reader = _FakeReader([
        (200, {'turns': [_wire_turn('t3', 300), _wire_turn('t4', 400)], 'attempts': [], 'syncSeq': 9,
               'turnWindow': {'hasMore': True, 'nextBeforeOrdinal': 300}}),
        (200, {'turns': [_wire_turn('t2', 200)], 'attempts': [], 'hasMore': True, 'nextBeforeOrdinal': 200}),
        (200, {'turns': [_wire_turn('t1', 100)], 'attempts': [], 'hasMore': True, 'nextBeforeOrdinal': 100}),
    ])
    window = hm._conversation_window(reader, 'c', 150, 400)
    assert [t['turn'] for t in window['turns']] == ['t3', 't2']  # t4 is at the exclusive end, t1 before
    assert len(reader.calls) == 3 and reader.calls[1][1]['beforeOrdinal'] == 300
    stale = _FakeReader([
        (200, {'turns': [_wire_turn('t3', 300)], 'attempts': [], 'syncSeq': 9,
               'turnWindow': {'hasMore': True, 'nextBeforeOrdinal': 300}}),
        (409, {'error': 'history_page_stale'})])
    assert 'stale' in hm._conversation_window(stale, 'c', 150, 400)['gap']


def test_reader_refuses_anything_but_a_loopback_http_origin():
    for origin in ('http://10.0.0.1:15000', 'https://127.0.0.1:15000', 'http://127.0.0.1:15000/api'):
        with pytest.raises(hm.APIError):
            hm.LoopbackReader(origin)
    hm.LoopbackReader('http://127.0.0.1:15000')


def test_merged_daily_evidence_keeps_the_latest_row_and_reports_missing_days():
    first = _evidence(generatedAt=1, window={'start': DAY0, 'end': DAY0 + 24 * H},
                      turns=[_turn('c', 't', DAY0 + H, status='running')])
    later = _evidence(generatedAt=2, window={'start': DAY0 + 48 * H, 'end': DAY0 + 72 * H},
                      turns=[_turn('c', 't', DAY0 + H, status='completed')])
    merged = hm.merge_evidence([later, first])
    assert merged['window'] == {'start': DAY0, 'end': DAY0 + 72 * H}
    assert [t['status'] for t in merged['turns']] == ['completed']
    assert merged['gaps'] == [{'gap': 'no evidence for 2026-11-09 .. 2026-11-09'}]


def test_cli_recomputes_from_saved_evidence_without_the_api(tmp_path):
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps(_evidence(conversations={'r': _meta('project_router')},
                                         turns=[_router_turn('a', DAY0 + H, 'completed', updated=DAY0 + 2 * H)])))
    out = tmp_path / 'metrics.json'
    script = Path(hm.__file__)
    run = subprocess.run([sys.executable, str(script), '--from-evidence', str(path), '--json', str(out)],
                         capture_output=True, text=True, check=False, cwd=script.parents[1])
    assert run.returncode == 0, run.stderr
    assert '| M7 | Coordinator turn success | 100.0% |' in run.stdout
    assert json.loads(out.read_text())['availability']['routers']['r']['availability'] == 1
    assert out.stat().st_mode & 0o077 == 0


def test_outputs_are_private_to_the_owner_even_when_they_already_existed(tmp_path):
    existing = tmp_path / 'evidence.json'
    existing.write_text('old')
    existing.chmod(0o644)
    hm.write_private(existing, '{"owner": "words"}')
    fresh = tmp_path / 'nested' / 'day.json'
    hm.write_private(fresh, '{}')
    assert existing.read_text() == '{"owner": "words"}'
    assert existing.stat().st_mode & 0o777 == 0o600 and fresh.stat().st_mode & 0o777 == 0o600
