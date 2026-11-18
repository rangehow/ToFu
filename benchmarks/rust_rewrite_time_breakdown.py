#!/usr/bin/env python3
"""Where real Tasks spend wall time, from recorded evidence (read-only).

Responsibility: the "Where time goes" evidence of docs/RUST_REWRITE_EVIDENCE.md.
It splits the wall time of real settled Turns (chat, Worker, lead, swarm child)
into model wait, tool execution by family, harness Python per round,
startup/queueing and compaction, and samples live process resources.

Subcommands (all read-only against the running application):

- ``collect``: GET-only loopback reads. Conversations, task rows
  (``/api/v4/tasks/by-conv``), the durable attempt timing trace of every
  settled root task in the window (``/api/v4/tasks/<id>/trace``, contract
  docs/contracts/TURN_TRACE_CONTRACT.md), and for swarm children the Request
  Inspector rows (``/requests``), the swarm status tool calls
  (``/api/v4/swarm/status/<parent>``) and, for a deterministic subsample, the
  post-tool state snapshot timestamps (``/requests/<n>?kind=state``).
- ``analyze``: pure fold of an evidence file into per-kind component shares
  (median / p90 of per-Turn shares, plus the pooled share) and the upper bound
  on the end-to-end saving per component.
- ``resources``: procfs CPU/RSS of the API host, execution host, its shards,
  MCP children and the storage engine over a window, with the number of
  running tasks and the execution host's storage RPC time (``/metrics``).

Entry point: ``main``. Dependencies: standard library only. Specification:
tests/meta/test_rust_rewrite_time_breakdown.py.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

DEFAULT_ORIGIN = ('127.0.0.1', 15000)
REQUEST_DEADLINE_SECONDS = 180
DAY_MS = 86_400_000
SETTLED = ('completed', 'interrupted', 'failed')
# Measurement design, not product settings: how many swarm children get
# their per-round state timestamps read (each read returns a full payload).
STATE_SUBSAMPLE = 48

TOOL_FAMILIES = (
    ('shell', ('run_command', 'kill')),
    ('file', ('read_files', 'write_file', 'edit_file', 'search_files', 'find_files',
              'list_dir', 'glob_search', 'apply_patch', 'repo_map', 'read_tool_artifact',
              'search_tool_artifact', 'inspect_image')),
    ('web', ('web_search', 'fetch_url')),
    ('browser', ('browser_',)),
    ('mcp', ('mcp__', 'mcp_')),
    ('agent_wait', ('poll', 'await_', 'get_agent_result', 'ask_human')),
)
# Every other tool is implemented inside Tofu's Python processes (delegation,
# project messages, conversation/memory/skill reads, Router and Watchdog
# tools, programmatic tool calls): family ``internal``.
COMPONENTS = (
    'model_ttft', 'model_stream', 'model_failed_attempt', 'provider_retry_wait',
    'tool_shell', 'tool_file', 'tool_web', 'tool_browser', 'tool_mcp',
    'tool_agent_wait', 'tool_internal', 'tool_unattributed',
    'harness_round', 'startup_queue', 'compaction', 'approval_wait', 'flow_step',
    'unattributed',
)
STARTUP_QUEUE_PHASES = ('dispatch', 'executor_queue')


def tool_family(name: str, mcp_servers: frozenset[str] = frozenset()) -> str:
    name = name or ''
    for family, prefixes in TOOL_FAMILIES:
        for prefix in prefixes:
            if name == prefix or (prefix.endswith('_') and name.startswith(prefix)):
                return family
    if any(name.startswith(server + '_') or name.startswith(server + '__')
           for server in mcp_servers):
        return 'mcp'
    return 'internal'


def percentile(values, fraction):
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    rank = max(1, -(-int(fraction * len(ordered) * 1000) // 1000))
    return ordered[min(len(ordered), rank) - 1]


# ── Collection (GET only) ────────────────────────────────────────────────────

class Reader:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.requests = 0
        self.bytes = 0
        self.failures: list[str] = []

    def get(self, path: str, params: dict | None = None):
        target = path + ('?' + urlencode(params) if params else '')
        connection = http.client.HTTPConnection(self.host, self.port, timeout=REQUEST_DEADLINE_SECONDS)
        try:
            connection.request('GET', target, headers={'Accept': 'application/json'})
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        self.requests += 1
        self.bytes += len(body)
        if response.status != 200:
            self.failures.append(f'{response.status} {path}')
            return None
        payload = json.loads(body)
        return payload.get('data', payload) if isinstance(payload, dict) else payload


def conversation_kind(conversation: dict) -> str:
    settings = conversation.get('settings') or {}
    if settings.get('_coordinatorSource'):
        return 'worker'
    if str(conversation.get('id') or '').startswith('coordinator-'):
        return 'lead'
    return 'chat'


def compact_trace(trace: dict) -> dict:
    tools: dict[str, list] = {}
    startup: dict[str, int] = {}
    for span in trace.get('spans') or []:
        start, end = span.get('tStart'), span.get('tEnd')
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        elapsed = max(0, int(end - start))
        if span.get('kind') == 'tool':
            name = (span.get('attrs') or {}).get('toolName') or span.get('name') or ''
            entry = tools.setdefault(name, [0, 0])
            entry[0] += 1
            entry[1] += elapsed
        elif span.get('kind') == 'startup' and span.get('name') != 'startup':
            startup[span.get('name')] = startup.get(span.get('name'), 0) + elapsed
    return {
        'source': trace.get('source'), 'status': trace.get('status'),
        'coverage': trace.get('coverage'), 'coverageReason': trace.get('coverageReason'),
        'clockSource': trace.get('clockSource'), 'droppedSpans': trace.get('droppedSpans') or 0,
        'summary': trace.get('summary'), 'toolSpans': tools, 'startupPhases': startup,
    }


def _turn_tool_rounds(reader: Reader, entry: dict) -> dict | None:
    """Per-tool count and elapsed ms from the Turn's complete tool rounds.

    A long attempt's trace keeps at most 256 spans, so its tool spans are a
    sample; the Turn projection keeps every tool round of its current
    attempt with its own clock. Rounds of other attempts are ignored.
    """
    path = ('/api/v4/conversations/' + quote(entry['conv'], safe='') + '/turns/'
            + quote(entry['turnId'], safe=''))
    payload = reader.get(path) or {}
    projection = (payload.get('turn') or {}).get('projection') or {}
    tools: dict[str, list] = {}
    matched = False
    for item in projection.get('toolRounds') or []:
        if item.get('taskId') != entry['taskId']:
            continue
        matched = True
        start, end = item.get('tStart'), item.get('tEnd')
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        bucket = tools.setdefault(item.get('toolName') or '', [0, 0])
        bucket[0] += 1
        bucket[1] += max(0, int(end - start))
    return tools if matched else None


def _swarm_child(reader: Reader, row: dict, agent: dict | None, with_state: bool) -> dict:
    task = quote(row['taskId'], safe='')
    listing = reader.get(f'/api/v4/tasks/{task}/requests') or {}
    requests = [{'round': r.get('roundNum'), 'ts': r.get('ts')}
                for r in listing.get('requests') or [] if not r.get('sourceTaskId')]
    calls = []
    for call in (agent or {}).get('toolCalls') or []:
        calls.append({'round': (call.get('inspectorRef') or {}).get('roundNum'),
                      'tool': call.get('toolName') or '', 'elapsedS': call.get('elapsed'),
                      'status': call.get('status') or ''})
    child = {'taskId': row['taskId'], 'conv': row['conv'], 'parentKind': row['kind'],
             'createdAt': row.get('createdAt'), 'requests': requests, 'toolCalls': calls,
             'toolCallsOmitted': (agent or {}).get('toolCallsOmitted'),
             'agentStatus': (agent or {}).get('status'), 'role': (agent or {}).get('role')}
    if with_state:
        states = {}
        for request in requests:
            number = request['round']
            payload = reader.get(f'/api/v4/tasks/{task}/requests/{number}', {'kind': 'state'})
            if payload and payload.get('ts'):
                states[str(number)] = payload['ts']
        child['stateTs'] = states
    return child


def collect(reader: Reader, *, now_ms: int, days: int, log=print) -> dict:
    start_ms = now_ms - days * DAY_MS
    conversations, params = [], {'limit': 200}
    while True:
        page = reader.get('/api/v4/conversations', params)
        conversations += page['items']
        cursor = page.get('page') or {}
        if not cursor.get('hasMore'):
            break
        params = {'limit': 200, 'before': cursor['nextBefore'], 'before_id': cursor['nextBeforeId']}
    rows = []
    for conversation in conversations:
        kind = conversation_kind(conversation)
        path = '/api/v4/tasks/by-conv/' + quote(conversation['id'], safe='')
        params = {'limit': 100}
        while True:
            page = reader.get(path, params) or {}
            batch = page.get('tasks') or []
            for row in batch:
                row['conv'], row['kind'] = conversation['id'], kind
            rows += batch
            oldest = min((r.get('createdAt') or 0) for r in batch) if batch else 0
            if not page.get('hasMore') or not batch or oldest < start_ms:
                break
            params = {'limit': 100, 'before': oldest}
    rows = [r for r in rows if start_ms <= (r.get('createdAt') or 0) < now_ms]
    log(f'{len(conversations)} conversations, {len(rows)} task rows in window')
    roots = []
    for row in rows:
        if row.get('isSwarmAgent') or row.get('status') not in SETTLED:
            continue
        trace = reader.get('/api/v4/tasks/' + quote(row['taskId'], safe='') + '/trace') or {}
        entry = {'taskId': row['taskId'], 'conv': row['conv'], 'kind': row['kind'],
                 'turnId': row.get('turnId'), 'status': row['status'],
                 'createdAt': row.get('createdAt'), 'rounds': row.get('requestCount'),
                 'trace': compact_trace(trace)}
        if entry['trace']['droppedSpans'] and entry['turnId']:
            entry['toolRounds'] = _turn_tool_rounds(reader, entry)
        roots.append(entry)
    log(f'{len(roots)} settled root traces read, '
        f'{sum(1 for r in roots if "toolRounds" in r)} bounded traces completed from Turn tool rounds')
    children = [r for r in rows if r.get('isSwarmAgent')]
    by_parent: dict[str, list] = {}
    for row in children:
        by_parent.setdefault(row.get('parentTaskId') or '', []).append(row)
    ranked = sorted(children, key=lambda r: hashlib.sha256(r['taskId'].encode()).hexdigest())
    state_ids = {r['taskId'] for r in ranked[:STATE_SUBSAMPLE]}
    swarm = []
    for parent, group in by_parent.items():
        status = reader.get('/api/v4/swarm/status/' + quote(parent, safe='')) or {}
        agents = {}
        for agent in status.get('agents') or []:
            for call in agent.get('toolCalls') or []:
                agents[str(call.get('callId') or '').split(':')[0]] = agent
                break
        for row in group:
            swarm.append(_swarm_child(reader, row, agents.get(row.get('agentId')),
                                      row['taskId'] in state_ids))
    log(f'{len(swarm)} swarm children read ({len(state_ids)} with state timestamps)')
    return {'version': 1, 'nowMs': now_ms, 'startMs': start_ms, 'days': days,
            'conversations': len(conversations),
            'taskRowCounts': _counts((r['kind'], 'swarm' if r.get('isSwarmAgent') else r.get('status'))
                                     for r in rows),
            'roots': roots, 'swarm': swarm,
            'reads': {'requests': reader.requests, 'bytes': reader.bytes,
                      'failures': reader.failures[:200]}}


def _counts(items) -> dict:
    out: dict[str, int] = {}
    for item in items:
        key = '/'.join(map(str, item))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


# ── Analysis (pure) ──────────────────────────────────────────────────────────

def root_components(entry: dict) -> dict | None:
    """Disjoint millisecond split of one settled root Turn, or None if unusable."""
    trace = entry.get('trace') or {}
    summary = trace.get('summary')
    if not isinstance(summary, dict) or not summary.get('totalMs'):
        return None
    get = lambda key: max(0, int(summary.get(key) or 0))  # noqa: E731
    llm, ttft, failed = get('llmMs'), get('ttftMs'), get('failedAttemptMs')
    ttft = min(ttft, llm)
    failed = min(failed, llm - ttft)
    out = dict.fromkeys(COMPONENTS, 0)
    out['model_ttft'] = ttft
    out['model_failed_attempt'] = failed
    out['model_stream'] = llm - ttft - failed
    out['provider_retry_wait'] = get('retryWaitMs')
    tool_ms = get('toolMs')
    spans = entry.get('toolRounds') or trace.get('toolSpans') or {}
    span_total = sum(ms for _n, ms in spans.values())
    if tool_ms and span_total:
        for name, (_count, ms) in spans.items():
            out['tool_' + tool_family(name)] += tool_ms * ms / span_total
    else:
        out['tool_unattributed'] += tool_ms
    out['harness_round'] = get('roundPrepMs') + get('roundSettleMs') + get('finalizeMs')
    out['startup_queue'] = get('startupMs') + get('admissionWaitMs') + get('spawnWaitMs')
    out['compaction'] = get('compactionMs')
    out['approval_wait'] = get('approvalWaitMs')
    out['flow_step'] = get('flowStepMs')
    out['unattributed'] = get('unattributedMs')
    out['_total'] = get('totalMs')
    out['_rounds'] = int(entry.get('rounds') or 0)
    out['_toolCalls'] = sum(n for n, _ms in spans.values())
    out['_droppedSpans'] = int(trace.get('droppedSpans') or 0)
    phases = trace.get('startupPhases') or {}
    out['_startupQueuePhases'] = sum(phases.get(p, 0) for p in STARTUP_QUEUE_PHASES)
    out['_startupPhases'] = phases
    return out


def swarm_components(child: dict) -> dict | None:
    """Split a swarm child's rounds covered by its retained tool calls.

    Window: from the request of the first covered round to the request of the
    last round (the final answer's own model time is not observable). Tool
    time per round is the retained calls' elapsed sum, capped at the round
    interval. With state timestamps (post-tool snapshot), each interval splits
    into in-round time (request -> state: model, tools, in-round settle) and
    between-round harness time (state -> next request). Without retained tool
    calls the in-round time stays ``model_plus_tool``; without state
    timestamps the remainder stays ``model_plus_harness``.
    """
    requests = sorted((r for r in child.get('requests') or [] if isinstance(r.get('ts'), int)
                       and isinstance(r.get('round'), int)), key=lambda r: r['round'])
    if len(requests) < 2:
        return None
    calls = child.get('toolCalls') or []
    covered = [c['round'] for c in calls if isinstance(c.get('round'), int)]
    first = min(covered) if covered and child.get('toolCallsOmitted') else requests[0]['round']
    states = {int(k): v for k, v in (child.get('stateTs') or {}).items()}
    out = dict.fromkeys(COMPONENTS, 0)
    out.update(model_plus_harness=0, model_plus_tool=0)
    total = 0
    for current, following in zip(requests, requests[1:]):
        if current['round'] < first:
            continue
        interval = max(0, following['ts'] - current['ts'])
        total += interval
        by_family: dict[str, float] = {}
        for call in calls:
            if call.get('round') == current['round']:
                family = tool_family(call.get('tool') or '')
                by_family[family] = by_family.get(family, 0) + 1000 * float(call.get('elapsedS') or 0)
        tool = sum(by_family.values())
        scale = min(1.0, interval / tool) if tool else 0
        for family, ms in by_family.items():
            out['tool_' + family] += ms * scale
        rest = interval - tool * scale
        state = states.get(current['round'])
        if states and state is not None and current['ts'] <= state <= following['ts']:
            in_round = min(rest, max(0, state - current['ts'] - tool * scale))
            out['model_stream' if calls else 'model_plus_tool'] += in_round
            out['harness_round'] += max(0, rest - in_round)
        else:
            out['model_plus_harness'] += rest
    if not total:
        return None
    out['_total'] = total
    out['_rounds'] = sum(1 for r in requests if r['round'] >= first)
    # Calls are retained only while the swarm session lives: not a count.
    out['_toolCalls'] = None
    out['_withState'] = bool(states)
    return out


def _summarize(rows: list[dict], keys) -> dict:
    total = sum(r['_total'] for r in rows)
    calls = [r.get('_toolCalls') for r in rows]
    result = {'turns': len(rows), 'wallMsTotal': total,
              'roundsTotal': sum(r['_rounds'] for r in rows),
              'toolCallsTotal': sum(calls) if all(isinstance(c, int) for c in calls) else None,
              'wallMsMedian': percentile([r['_total'] for r in rows], 0.5),
              'wallMsP90': percentile([r['_total'] for r in rows], 0.9),
              'roundsMedian': percentile([r['_rounds'] for r in rows], 0.5),
              'components': {}}
    for key in keys:
        shares = [r.get(key, 0) / r['_total'] for r in rows if r['_total']]
        pooled = sum(r.get(key, 0) for r in rows) / total if total else 0
        result['components'][key] = {
            'shareMedian': percentile(shares, 0.5), 'shareP90': percentile(shares, 0.9),
            'sharePooled': pooled,
            'msPerTurnMedian': percentile([r.get(key, 0) for r in rows], 0.5),
            'msPerTurnP90': percentile([r.get(key, 0) for r in rows], 0.9),
        }
    rounds = [r for r in rows if r['_rounds']]
    if rounds:
        result['harnessMsPerRoundMedian'] = percentile(
            [r['harness_round'] / r['_rounds'] for r in rounds], 0.5)
        result['harnessMsPerRoundP90'] = percentile(
            [r['harness_round'] / r['_rounds'] for r in rounds], 0.9)
    return result


# A Turn whose harness bucket alone exceeds an hour is a stall, not per-round
# Python work: the slowest round preparation itemized by [RoundPrepTiming] on
# 2026-11-18 took 40 s. Reported separately, never dropped.
STALL_HARNESS_MS = 3_600_000


def analyze(evidence: dict, since_ms: int | None = None) -> dict:
    by_kind: dict[str, list] = {}
    skipped: dict[str, int] = {}
    startup_phases: dict[str, dict] = {}
    stalls: dict[str, list] = {}
    for entry in evidence.get('roots') or []:
        if since_ms is not None and (entry.get('createdAt') or 0) < since_ms:
            continue
        parts = root_components(entry)
        if parts is None:
            skipped[entry['kind']] = skipped.get(entry['kind'], 0) + 1
            continue
        by_kind.setdefault(entry['kind'], []).append(parts)
        if parts['harness_round'] >= STALL_HARNESS_MS:
            stalls.setdefault(entry['kind'], []).append(
                {'taskId': entry['taskId'], 'status': entry['status'],
                 'createdAt': entry.get('createdAt'), 'endAt': (entry.get('createdAt') or 0) + parts['_total'],
                 'harnessMs': parts['harness_round'], 'totalMs': parts['_total'], 'rounds': parts['_rounds'],
                 'coverageReason': (entry.get('trace') or {}).get('coverageReason')})
        bucket = startup_phases.setdefault(entry['kind'], {})
        for phase, ms in parts['_startupPhases'].items():
            bucket[phase] = bucket.get(phase, 0) + ms
    kinds = {kind: _summarize(rows, COMPONENTS) for kind, rows in sorted(by_kind.items())}
    for kind, rows in by_kind.items():
        kinds[kind]['droppedSpanTurns'] = sum(1 for r in rows if r['_droppedSpans'])
        kinds[kind]['startupPhaseMsTotal'] = dict(sorted(startup_phases[kind].items()))
        calm = [r for r in rows if r['harness_round'] < STALL_HARNESS_MS]
        kinds[kind]['withoutStallTurns'] = _summarize(calm, COMPONENTS) if calm else None
        kinds[kind]['stallTurns'] = stalls.get(kind, [])
    swarm_rows = [s for s in (swarm_components(c) for c in evidence.get('swarm') or []
                              if since_ms is None or (c.get('createdAt') or 0) >= since_ms) if s]
    swarm_keys = tuple(k for k in COMPONENTS if k.startswith('tool_')) + (
        'model_plus_harness', 'model_plus_tool', 'model_stream', 'harness_round')
    if swarm_rows:
        kinds['swarm'] = _summarize(swarm_rows, swarm_keys)
        state_rows = [r for r in swarm_rows if r['_withState']]
        if state_rows:
            kinds['swarm']['stateSubsample'] = _summarize(state_rows, swarm_keys)
    all_roots = [r for rows in by_kind.values() for r in rows]
    pooled_all = _summarize(all_roots, COMPONENTS) if all_roots else None
    return {'window': {'startMs': since_ms or evidence.get('startMs'), 'nowMs': evidence.get('nowMs'),
                       'days': evidence.get('days')},
            'taskRowCounts': evidence.get('taskRowCounts'),
            'skippedNoSummary': skipped, 'kinds': kinds, 'allRoots': pooled_all}


def harness_unit_costs(harness_perf_report: dict) -> dict:
    """Python ms per model request and per tool call, from a zero-latency run.

    With ``benchmarks/harness_perf`` the stub provider answers in under a
    millisecond and the scripted tools do no work, so the Turn trace's
    ``llmMs`` per round and ``toolMs`` per call are harness time. The largest
    phase (normally 20 concurrent sessions) is taken, so the bound errs high.
    """
    phases = harness_perf_report['tofu']['turn_trace_ms']
    model = max(p['llmMs'] / p['rounds'] for p in phases.values() if p.get('rounds'))
    tool = max(p['toolMs'] / (p['rounds'] - 1) for p in phases.values() if (p.get('rounds') or 0) > 1)
    return {'modelMsPerRequest': model, 'toolMsPerCall': tool,
            'phases': {name: {k: p.get(k) for k in ('rounds', 'llmMs', 'toolMs')}
                       for name, p in phases.items()}}


EXTERNAL_TOOLS = ('tool_shell', 'tool_file', 'tool_web', 'tool_browser', 'tool_mcp',
                  'tool_agent_wait', 'tool_unattributed')
WHOLLY_PYTHON = ('tool_internal', 'harness_round', 'startup_queue', 'unattributed', 'flow_step')
NOT_PYTHON = ('provider_retry_wait', 'approval_wait', 'compaction', 'model_failed_attempt')


def rewrite_upper_bounds(summary: dict, unit: dict) -> dict:
    """Upper bound on the end-to-end saving per component if its Python cost were zero.

    Shares are of the stratum's pooled wall time. Components that are wholly
    Tofu Python work (internal tools, round preparation/settlement, startup
    and queueing, unattributed gaps) may vanish entirely. Model waits keep
    at most the measured per-request harness cost, external tools at most the
    measured per-call harness cost; provider retries, human approval and the
    model-based compaction are not Python. Never a prediction: nothing
    becomes faster than zero and no other component shrinks.
    """
    wall = summary['wallMsTotal'] or 1
    shares = {key: value['sharePooled'] for key, value in summary['components'].items()}
    rounds = summary.get('roundsTotal') or 0
    calls = summary.get('toolCallsTotal')
    if calls is None:
        calls = rounds
    out = {}
    model_share = sum(shares.get(k, 0) for k in ('model_ttft', 'model_stream', 'model_plus_tool',
                                                  'model_plus_harness'))
    out['model_wait'] = {'componentShare': model_share,
                         'upperBound': min(model_share, rounds * unit['modelMsPerRequest'] / wall)}
    external = sum(shares.get(k, 0) for k in EXTERNAL_TOOLS)
    out['external_tools'] = {'componentShare': external,
                             'upperBound': min(external + (model_share - out['model_wait']['upperBound'])
                                               * ('model_plus_tool' in shares),
                                               calls * unit['toolMsPerCall'] / wall)}
    for key in WHOLLY_PYTHON:
        if key in shares:
            out[key] = {'componentShare': shares[key], 'upperBound': shares[key]}
    for key in NOT_PYTHON:
        if key in shares:
            out[key] = {'componentShare': shares[key], 'upperBound': 0.0}
    out['total'] = {'componentShare': sum(v['componentShare'] for v in out.values()),
                    'upperBound': sum(v['upperBound'] for v in out.values())}
    return out


RENDER_ROWS = (
    ('Model: time to first token', ('model_ttft',)),
    ('Model: streaming', ('model_stream',)),
    ('Model: failed attempts + provider retry waits', ('model_failed_attempt', 'provider_retry_wait')),
    ('Swarm: in-round model + tools (inseparable)', ('model_plus_tool',)),
    ('Swarm: model + harness (no state timestamp)', ('model_plus_harness',)),
    ('Tool: shell (`run_command`)', ('tool_shell',)),
    ('Tool: file', ('tool_file',)),
    ('Tool: web', ('tool_web',)),
    ('Tool: browser', ('tool_browser',)),
    ('Tool: MCP', ('tool_mcp',)),
    ('Tool: waiting on agents/commands/human (`poll`, ...)', ('tool_agent_wait',)),
    ('Tool: Tofu-internal (Python)', ('tool_internal', 'tool_unattributed')),
    ('Harness Python per round (prep + settle + finalize)', ('harness_round',)),
    ('Startup + queueing', ('startup_queue',)),
    ('Compaction (marked spans)', ('compaction',)),
    ('Approval wait / Flow / unattributed', ('approval_wait', 'flow_step', 'unattributed')),
)


def _pct(value) -> str:
    return '-' if value is None else f'{100 * value:.1f}'


def render_shares(cohort: dict) -> str:
    """Markdown: per kind, median / p90 of per-Turn shares and the pooled share."""
    kinds = list(cohort['kinds'])
    lines = ['| component | ' + ' | '.join(f'{k} med / p90 / pooled %' for k in kinds) + ' |',
             '|---|' + '---|' * len(kinds)]
    for label, keys in RENDER_ROWS:
        cells, seen = [], False
        for kind in kinds:
            components = cohort['kinds'][kind]['components']
            present = [components[k] for k in keys if k in components]
            if not present:
                cells.append('n/a')
                continue
            # Multi-key rows report the pooled sum; medians/p90 only for one key.
            pooled = sum(c['sharePooled'] for c in present)
            seen = seen or pooled > 0 or any((c['shareP90'] or 0) > 0 for c in present)
            if len(present) == 1:
                cells.append(f"{_pct(present[0]['shareMedian'])} / {_pct(present[0]['shareP90'])} / {_pct(pooled)}")
            else:
                cells.append(f'- / - / {_pct(pooled)}')
        if seen:
            lines.append(f'| {label} | ' + ' | '.join(cells) + ' |')
    lines.append('| Turns (wall median / p90 s; total h; rounds) | ' + ' | '.join(
        f"{s['turns']} ({s['wallMsMedian'] / 1000:.0f} / {s['wallMsP90'] / 1000:.0f}; "
        f"{s['wallMsTotal'] / 3.6e6:.0f} h; {s['roundsTotal']})" for s in cohort['kinds'].values()) + ' |')
    return '\n'.join(lines)


def render_bounds(bounds: dict) -> str:
    keys = ('model_wait', 'external_tools', 'tool_internal', 'harness_round', 'startup_queue',
            'compaction', 'provider_retry_wait', 'total')
    kinds = list(bounds)
    lines = ['| component | ' + ' | '.join(f'{k} share / bound %' for k in kinds) + ' |',
             '|---|' + '---|' * len(kinds)]
    for key in keys:
        cells = []
        for kind in kinds:
            value = bounds[kind].get(key)
            cells.append('n/a' if value is None else f"{_pct(value['componentShare'])} / **{_pct(value['upperBound'])}**")
        lines.append(f'| {key} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


# ── Retained application logs (read-only) ────────────────────────────────────

def log_timings(paths) -> dict:
    """Itemize harness time from retained ``app.log*`` timing lines.

    ``[RoundPrepTiming]`` (rounds whose preparation reached one second, with
    named steps), ``[TurnTraceSummary]`` (the same Turns' trace buckets, the
    denominator), ``[Timing] TTFT=`` (context-ready vs stream-entry to first
    token), ``[TaskStartupTiming]`` stage durations and ``[CompactionTiming]``.
    """
    import re
    summaries, preps, ttft, startup, compaction = {}, {}, [], {}, {}
    first = last = None
    for path in paths:
        with open(path, errors='replace') as handle:
            for line in handle:
                stamp = line[:19]
                if stamp[:2] == '20':
                    first = min(first or stamp, stamp)
                    last = max(last or stamp, stamp)
                if '[TurnTraceSummary]' in line:
                    fields = dict(re.findall(r'(\w+)=(\S+)', line.split('[TurnTraceSummary]', 1)[1]))
                    summaries[fields.get('task')] = fields
                elif '[RoundPrepTiming] ' in line:
                    try:
                        record = json.loads(line.split('[RoundPrepTiming] ', 1)[1])
                    except ValueError:
                        continue
                    preps.setdefault(record.get('task_id'), []).append(record)
                elif '] [Timing] TTFT=' in line:
                    match = re.search(r'TTFT=([\d.]+)s .*request=([\d.]+)s', line)
                    if match:
                        ttft.append((float(match.group(1)), float(match.group(2))))
                elif '[TaskStartupTiming] ' in line or '[TaskCompletionTiming] ' in line:
                    marker = '[TaskStartupTiming] ' if '[TaskStartupTiming] ' in line else '[TaskCompletionTiming] '
                    try:
                        record = json.loads(line.split(marker, 1)[1])
                    except ValueError:
                        continue
                    if record.get('boundary') == 'complete' and isinstance(record.get('durationMs'), (int, float)):
                        prefix = '' if marker.startswith('[TaskStartup') else 'completion.'
                        startup.setdefault(prefix + str(record.get('stage')), []).append(record['durationMs'])
                elif '[CompactionTiming]' in line:
                    match = re.search(r'stage=(\S+) status=\S+ elapsed_ms=([\d.]+)', line)
                    if match:
                        compaction.setdefault(match.group(1), []).append(float(match.group(2)))
    number = lambda fields, key: int(fields.get(key, '0')) if fields.get(key, '0').isdigit() else 0  # noqa: E731
    prep_total = sum(number(f, 'roundPrepMs') for f in summaries.values())
    settle_total = sum(number(f, 'roundSettleMs') for f in summaries.values())
    finalize_total = sum(number(f, 'finalizeMs') for f in summaries.values())
    steps: dict[str, float] = {}
    subs: dict[str, float] = {}
    logged = 0.0
    for task in summaries:
        for record in preps.get(task, []):
            logged += record.get('totalMs') or 0
            for name, ms in (record.get('stepsMs') or {}).items():
                steps[name] = steps.get(name, 0) + ms
            for group in ('inputsMs', 'build_bodyMs'):
                for name, ms in (record.get(group) or {}).items():
                    key = group[:-2] + '.' + name
                    subs[key] = subs.get(key, 0) + ms
    stats = lambda values: {'n': len(values), 'median': percentile(values, 0.5),  # noqa: E731
                            'p90': percentile(values, 0.9), 'totalMs': sum(values)}
    gaps = [round(1000 * (t - r), 1) for t, r in ttft]
    return {
        'covered': {'first': first, 'last': last, 'files': [str(p) for p in paths]},
        'turnTraceSummaries': len(summaries),
        'roundPrepMsTotal': prep_total, 'roundSettleMsTotal': settle_total,
        'finalizeMsTotal': finalize_total,
        'roundPrepMsInRoundsOverOneSecond': logged,
        'roundPrepStepShareOfAllPrep': {k: v / prep_total for k, v in sorted(steps.items(), key=lambda x: -x[1])}
        if prep_total else {},
        'roundPrepSubstepShareOfAllPrep': {k: v / prep_total for k, v in sorted(subs.items(), key=lambda x: -x[1])}
        if prep_total else {},
        'roundPrepRecordsOverOneSecond': sum(len(preps.get(t, [])) for t in summaries),
        'ttftSeconds': stats([t for t, _r in ttft]),
        'harnessMsInsideTtft': stats(gaps),
        'startupStageMs': {stage: stats(values) for stage, values in sorted(startup.items())},
        'compactionMs': {stage: stats(values) for stage, values in sorted(compaction.items())},
    }


# ── Live resources (procfs, read-only) ───────────────────────────────────────

def _proc_children() -> dict[int, list[int]]:
    tree: dict[int, list[int]] = {}
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open(f'/proc/{name}/stat') as handle:
                fields = handle.read().rsplit(')', 1)[1].split()
        except OSError:
            continue
        tree.setdefault(int(fields[1]), []).append(int(name))
    return tree


def _proc_sample(pid: int) -> dict | None:
    try:
        with open(f'/proc/{pid}/stat') as handle:
            fields = handle.read().rsplit(')', 1)[1].split()
        with open(f'/proc/{pid}/cmdline', 'rb') as handle:
            command = handle.read().replace(b'\0', b' ').decode(errors='replace').strip()
        with open(f'/proc/{pid}/status') as handle:
            status = dict(line.split(':', 1) for line in handle if ':' in line)
    except OSError:
        return None
    pss_kb = 0
    try:
        with open(f'/proc/{pid}/smaps_rollup') as handle:
            for line in handle:
                if line.startswith('Pss:'):
                    pss_kb = int(line.split()[1])
    except OSError:
        pass
    return {'pid': pid, 'cpuTicks': int(fields[11]) + int(fields[12]), 'pssKb': pss_kb,
            'rssKb': int(status.get('VmRSS', '0 kB').split()[0]),
            'threads': int(status.get('Threads', '0').strip()), 'command': command[:300]}


def classify_process(command: str) -> str:
    if 'execution_shard_boot.py' in command:
        return 'execution_shard'
    if 'serverctl_pkg.execution_host' in command:
        return 'execution_host'
    if command.endswith('server.py') or ' server.py' in command:
        return 'api_host'
    if 'tofu-db' in command and ' serve' in command:
        return 'storage_engine'
    if 'native_storage_host' in command:
        return 'storage_host'
    if 'mcp' in command.lower() or 'bridge.mjs' in command:
        return 'mcp_child'
    if 'supervisor.py' in command:
        return 'supervisor'
    return 'other'


def resource_window(manager_pid: int, seconds: float) -> dict:
    """CPU and RSS of the manager's Tofu processes over ``seconds``.

    Execution shards' tool subprocesses (user commands) are excluded: they
    are the work, not the harness. MCP children are direct children of the
    execution host or a shard whose command names an MCP server.
    """
    def snapshot():
        tree = _proc_children()
        found = {}
        stack = [(manager_pid, 'supervisor')]
        while stack:
            pid, parent_role = stack.pop()
            sample = _proc_sample(pid)
            if sample is None:
                continue
            role = classify_process(sample['command'])
            if parent_role in ('execution_shard', 'execution_host') and role == 'other':
                continue
            if parent_role == 'mcp_child':
                role = 'mcp_child'
            sample['role'] = role
            found[pid] = sample
            if role in ('storage_engine',):
                continue
            stack += [(child, role) for child in tree.get(pid, [])]
        return found, time.monotonic()
    first, t0 = snapshot()
    time.sleep(seconds)
    second, t1 = snapshot()
    ticks = os.sysconf('SC_CLK_TCK')
    roles: dict[str, dict] = {}
    for pid, sample in second.items():
        before = first.get(pid)
        cpu = (sample['cpuTicks'] - before['cpuTicks']) / ticks / (t1 - t0) if before else None
        bucket = roles.setdefault(sample['role'], {'processes': 0, 'rssMb': 0.0, 'pssMb': 0.0,
                                                   'cpuCores': 0.0, 'threads': 0, 'members': []})
        bucket['processes'] += 1
        bucket['rssMb'] += sample['rssKb'] / 1024
        bucket['pssMb'] += sample['pssKb'] / 1024
        bucket['threads'] += sample['threads']
        bucket['cpuCores'] += cpu or 0
        bucket['members'].append({'pid': pid, 'rssMb': round(sample['rssKb'] / 1024, 1),
                                  'cpuCores': None if cpu is None else round(cpu, 3),
                                  'threads': sample['threads'], 'command': sample['command'][:160]})
    return {'seconds': round(t1 - t0, 2), 'roles': roles}


def active_sessions(port: int) -> dict:
    """Running execution sessions and in-flight agent calls from ``/metrics``."""
    connection = http.client.HTTPConnection(DEFAULT_ORIGIN[0], port, timeout=REQUEST_DEADLINE_SECONDS)
    try:
        connection.request('GET', '/metrics')
        text = connection.getresponse().read().decode()
    finally:
        connection.close()
    sessions, inflight = 0.0, None
    for line in text.splitlines():
        if line.startswith('tofu_execution_sessions_active{'):
            sessions += float(line.rsplit(' ', 1)[1])
        elif line.startswith('tofu_agent_inflight '):
            inflight = float(line.rsplit(' ', 1)[1])
    return {'executionSessionsActive': sessions, 'agentInflight': inflight}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('collect')
    c.add_argument('--out', required=True)
    c.add_argument('--days', type=int, default=7)
    c.add_argument('--port', type=int, default=DEFAULT_ORIGIN[1])
    a = sub.add_parser('analyze')
    a.add_argument('evidence')
    a.add_argument('--out', required=True)
    a.add_argument('--harness-perf', help='benchmarks/harness_perf report.json for per-request/per-call costs')
    m = sub.add_parser('render')
    m.add_argument('analysis')
    g = sub.add_parser('logs')
    g.add_argument('paths', nargs='+')
    g.add_argument('--out', required=True)
    r = sub.add_parser('resources')
    r.add_argument('--manager-pid', type=int, required=True)
    r.add_argument('--seconds', type=float, default=60.0)
    r.add_argument('--out', required=True)
    r.add_argument('--port', type=int, default=DEFAULT_ORIGIN[1])
    args = parser.parse_args(argv)
    if args.command == 'collect':
        reader = Reader(DEFAULT_ORIGIN[0], args.port)
        evidence = collect(reader, now_ms=int(time.time() * 1000), days=args.days,
                           log=lambda m: print(m, file=sys.stderr))
        Path(args.out).write_text(json.dumps(evidence))
        print(f'{reader.requests} GETs, {reader.bytes / 1e6:.1f} MB, '
              f'{len(reader.failures)} non-200', file=sys.stderr)
    elif args.command == 'analyze':
        evidence = json.loads(Path(args.evidence).read_text())
        now = time.localtime(evidence['nowMs'] / 1000)
        midnight = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)
        result = {'all': analyze(evidence), 'lastDay': analyze(evidence, since_ms=midnight)}
        if args.harness_perf:
            unit = harness_unit_costs(json.loads(Path(args.harness_perf).read_text()))
            result['unitCosts'] = unit
            result['upperBounds'] = {}
            for cohort in ('all', 'lastDay'):
                for kind, summary in result[cohort]['kinds'].items():
                    bounds = result['upperBounds'].setdefault(cohort, {})
                    measured = summary.get('stateSubsample') if kind == 'swarm' else summary
                    if measured:
                        bounds[kind] = rewrite_upper_bounds(measured, unit)
                    if summary.get('stallTurns') and summary.get('withoutStallTurns'):
                        bounds[kind + '_withoutStallTurns'] = rewrite_upper_bounds(
                            summary['withoutStallTurns'], unit)
        Path(args.out).write_text(json.dumps(result, indent=1))
    elif args.command == 'render':
        result = json.loads(Path(args.analysis).read_text())
        for cohort in ('all', 'lastDay'):
            print(f'### Shares: {cohort}\n\n' + render_shares(result[cohort]) + '\n')
        for cohort, bounds in (result.get('upperBounds') or {}).items():
            print(f'### Upper bounds: {cohort}\n\n' + render_bounds(bounds) + '\n')
    elif args.command == 'logs':
        Path(args.out).write_text(json.dumps(log_timings(sorted(args.paths)), indent=1))
    else:
        before = active_sessions(args.port)
        window = resource_window(args.manager_pid, args.seconds)
        window['sessionsBefore'], window['sessionsAfter'] = before, active_sessions(args.port)
        Path(args.out).write_text(json.dumps(window, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
