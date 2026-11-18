"""benchmarks/rust_rewrite_time_breakdown.py stays runnable and its split is exact.

Smoke test for the "Where time goes" evidence (docs/RUST_REWRITE_EVIDENCE.md):
the per-Turn split of a trace summary is disjoint and sums to the Turn's wall
time, tool time is apportioned to families by the retained tool spans, swarm
rounds split at the post-tool snapshot, and the CLI analyzes an evidence file
and samples its own process tree without a running application.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'benchmarks' / 'rust_rewrite_time_breakdown.py'
spec = importlib.util.spec_from_file_location('rust_rewrite_time_breakdown', SCRIPT)
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)


def _root(kind='worker', created=1_000, **summary):
    base = dict(totalMs=10_000, llmMs=6_000, ttftMs=4_000, failedAttemptMs=500, toolMs=2_000,
                waitMs=300, retryWaitMs=100, admissionWaitMs=50, spawnWaitMs=150,
                orchestrationMs=1_400, startupMs=400, roundPrepMs=600, roundSettleMs=300,
                finalizeMs=100, compactionMs=200, approvalWaitMs=0, flowStepMs=0, unattributedMs=100)
    base.update(summary)
    return {'taskId': f'conversation-attempt:{kind}-{created}', 'conv': 'c', 'kind': kind,
            'status': 'completed', 'createdAt': created, 'rounds': 4,
            'trace': {'summary': base, 'droppedSpans': 0,
                      'toolSpans': {'run_command': [2, 1_500], 'read_files': [1, 300],
                                    'mcp__hope__get_status': [1, 200]},
                      'startupPhases': {'dispatch': 100, 'context': 200}}}


def test_root_split_is_disjoint_and_sums_to_wall_time():
    parts = measure.root_components(_root())
    assert sum(parts[key] for key in measure.COMPONENTS) == 10_000
    assert parts['model_ttft'] == 4_000 and parts['model_failed_attempt'] == 500
    assert parts['model_stream'] == 1_500
    assert parts['tool_shell'] == 1_500 and parts['tool_file'] == 300 and parts['tool_mcp'] == 200
    assert parts['harness_round'] == 1_000 and parts['startup_queue'] == 600
    assert parts['_startupQueuePhases'] == 100


def test_tool_families():
    assert measure.tool_family('run_command') == 'shell'
    assert measure.tool_family('browser_click') == 'browser'
    assert measure.tool_family('mcp__overleaf__list_files') == 'mcp'
    assert measure.tool_family('fetch_url') == 'web'
    assert measure.tool_family('poll') == 'agent_wait'
    assert measure.tool_family('delegate_project_work') == 'internal'


def test_swarm_rounds_split_at_the_post_tool_snapshot():
    child = {'createdAt': 0, 'toolCallsOmitted': 0,
             'requests': [{'round': 1, 'ts': 0}, {'round': 2, 'ts': 10_000}, {'round': 3, 'ts': 16_000}],
             'toolCalls': [{'round': 1, 'tool': 'run_command', 'elapsedS': 2.0},
                           {'round': 2, 'tool': 'read_files', 'elapsedS': 1.0}],
             'stateTs': {'1': 9_000, '2': 15_500}}
    parts = measure.swarm_components(child)
    assert parts['_total'] == 16_000
    assert parts['tool_shell'] == 2_000 and parts['tool_file'] == 1_000
    assert parts['model_stream'] == 7_000 + 4_500
    assert parts['harness_round'] == 1_000 + 500


def test_analysis_reports_medians_p90_and_stalls():
    evidence = {'startMs': 0, 'nowMs': 10, 'days': 7,
                'roots': [_root(created=i) for i in range(1, 6)]
                + [_root(kind='lead', created=9, totalMs=20_000_000, roundPrepMs=4_000_000,
                         llmMs=10_000_000, ttftMs=1_000, toolMs=5_000_000)],
                'swarm': []}
    result = measure.analyze(evidence)
    worker = result['kinds']['worker']
    assert worker['turns'] == 5
    assert worker['components']['harness_round']['shareMedian'] == 0.1
    assert worker['components']['harness_round']['shareP90'] == 0.1
    lead = result['kinds']['lead']
    assert len(lead['stallTurns']) == 1 and lead['withoutStallTurns'] is None
    evidence['swarm'] = [{'createdAt': 1, 'requests': [{'round': 1, 'ts': 0}, {'round': 2, 'ts': 5}],
                          'toolCalls': []}]
    assert measure.analyze(evidence)['kinds']['swarm']['turns'] == 1
    late = measure.analyze(evidence, since_ms=9)
    assert set(late['kinds']) == {'lead'}
    unit = {'modelMsPerRequest': 20.0, 'toolMsPerCall': 50.0}
    bounds = measure.rewrite_upper_bounds(worker, unit)
    assert bounds['harness_round']['upperBound'] == 0.1
    assert bounds['model_wait']['upperBound'] == 5 * 4 * 20 / 50_000
    assert bounds['external_tools']['upperBound'] == 5 * 4 * 50 / 50_000
    assert bounds['compaction']['upperBound'] == 0.0
    assert abs(bounds['total']['componentShare'] - 1.0) < 1e-9
    report = {'tofu': {'turn_trace_ms': {'chat': {'rounds': 1, 'llmMs': 20, 'toolMs': 0},
                                         'shell': {'rounds': 21, 'llmMs': 420, 'toolMs': 1000}}}}
    assert measure.harness_unit_costs(report)['toolMsPerCall'] == 50.0


def test_log_timings_itemize_round_prep(tmp_path):
    log = tmp_path / 'app.log'
    log.write_text(
        '2026-11-18 10:00:00 [INFO] x: [TurnTraceSummary] task=t1 conv=c status=done totalMs=9000 '
        'roundPrepMs=4000 roundSettleMs=500 finalizeMs=10\n'
        '2026-11-18 10:00:01 [INFO] x: [RoundPrepTiming] ' + json.dumps(
            {'task_id': 't1', 'totalMs': 2000, 'stepsMs': {'compaction': 1500, 'build_body': 500},
             'build_bodyMs': {'mcp_authority': 400}}) + '\n'
        '2026-11-18 10:00:02 [INFO] x: [Task t1][R2] [Timing] TTFT=3.5s (context-ready), request=3.0s (x)\n')
    result = measure.log_timings([log])
    assert result['roundPrepMsTotal'] == 4000 and result['roundPrepMsInRoundsOverOneSecond'] == 2000
    assert result['roundPrepStepShareOfAllPrep']['compaction'] == 0.375
    assert result['roundPrepSubstepShareOfAllPrep']['build_body.mcp_authority'] == 0.1
    assert result['harnessMsInsideTtft']['median'] == 500.0
    assert result['covered']['first'] == '2026-11-18 10:00:00'


def test_cli_analyzes_evidence_and_samples_its_own_process(tmp_path):
    evidence = tmp_path / 'evidence.json'
    evidence.write_text(json.dumps({'startMs': 0, 'nowMs': 86_400_000 * 3, 'days': 7,
                                    'roots': [_root()], 'swarm': []}))
    out = tmp_path / 'analysis.json'
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    done = subprocess.run([sys.executable, str(SCRIPT), 'analyze', str(evidence), '--out', str(out)],
                          capture_output=True, text=True, timeout=60, env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads(out.read_text())['all']['kinds']['worker']['turns'] == 1
    window = measure.resource_window(os.getpid(), 0.05)
    assert window['roles']['other']['processes'] >= 1
