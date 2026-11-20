"""Rust prototype arm of the harness round benchmark (``packages/tofu-core-rs``).

``python3 benchmarks/harness_perf/rust_prototype.py rust --binary BIN --root DIR --out DIR``
boots what ``round_profile.py`` boots below the agent loop — the pinned native
TofuDB engine and the zero-latency stub — and runs the prototype's Turns
against them: warm-up, ``noop`` Turns of ``--rounds`` read rounds, ``true``
Turns of one ``run_command true`` round, then (unless ``--no-concurrency``)
N concurrent sessions of ``shell`` Turns per ``--concurrency`` value, and
``--cold-starts`` fresh processes of one ``chat`` Turn. Round overheads come
from the stub's records through ``run.session_metrics``/``run.summarize``, so
both arms are measured by the same clock and code; ``result.json`` has the same
keys as ``round_profile.py --no-spy`` plus the prototype's per-round stage
profile, process RSS/CPU, concurrency and cold start.

``python3 benchmarks/harness_perf/rust_prototype.py python-concurrency --root DIR --out DIR``
measures Python Tofu at HEAD on the same concurrent shape (``run.py``'s
``concurrent_phase``) for each ``--concurrency`` value, with RSS/CPU of the
API host, execution host (and shards) and engine.

Each Rust session is a real Turn: ``turn.create_pair``, worker dispatch and
claim, ``turn.attempt.start``, per-round projection events and the raw
archive, a terminal settlement and ``worker_job.complete``. The system prompt
is the replay corpus's longest, and the tools are the corpus schemas of the
surface Tofu offered on its round-0 request (``TOFU_OFFERED_TOOLS``), so the
requests are the same order of size as Tofu's own (about 55 KB against 70 KB).
Stage values in ``*_stages_ms`` are milliseconds; their keys keep the binary's
``*_us`` field names.
Evidence: docs/RUST_REWRITE_EVIDENCE.md, "Core loop prototype".
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]


def _harness():
    sys.path[:0] = [str(PROJECT_ROOT), str(HERE), str(PROJECT_ROOT / 'scripts')]
    import run as harness
    return harness


# The tools Tofu at HEAD offered on its round-0 request in this harness on
# 2026-11-18 (the stub's ``offered_tools`` record; Tool Search defers the rest,
# todo_write included, which is why its ``noop`` round reads a file).
TOFU_OFFERED_TOOLS = (
    'web_search', 'fetch_url', 'read_files', 'inspect_image', 'search_files', 'find_files', 'write_file',
    'edit_file', 'run_command', 'list_conversations', 'get_conversation', 'inspect_video', 'search_skills',
    'load_skill', 'read_skill_resource', 'timer_create', 'inspect_tool_effect', 'read_tool_artifact',
    'search_tool_artifact', 'read_working_facts', 'spawn_agents', 'poll', 'kill', 'search_tools',
    'execute_tools', 'execute_program')


def _system_text(request: dict) -> str:
    parts = [request.get('system')] if request.get('system') else []
    for message in request.get('messages') or []:
        if message.get('role') == 'system':
            content = message.get('content')
            parts.append(content if isinstance(content, str) else ''.join(
                block.get('text', '') for block in content or [] if isinstance(block, dict)))
    return '\n\n'.join(part for part in parts if part)


def corpus_prompt() -> tuple[str, list]:
    """The corpus's longest system text and its schemas of Tofu's offered tools."""
    import byte_identity
    system, schemas = '', {}
    for _scenario, steps in byte_identity.recorded_requests():
        for request in steps:
            text = _system_text(request)
            if len(text) > len(system):
                system = text
            for tool in request.get('tools') or []:
                schemas.setdefault((tool.get('function') or {}).get('name'), tool)
    tools = [schemas[name] for name in TOFU_OFFERED_TOOLS if name in schemas]
    if not {'read_files', 'run_command'} <= {(t.get('function') or {}).get('name') for t in tools}:
        raise SystemExit('the replay corpus lacks read_files or run_command schemas')
    return system, tools


class Sampler:
    """RSS/CPU of named processes every 0.25 s (``run.proc_sample``)."""

    def __init__(self, harness, pids: dict[str, int]):
        self.harness, self.pids = harness, pids
        self.samples: dict[str, list[dict]] = {name: [] for name in pids}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(0.25):
            for name, pid in self.pids.items():
                sample = self.harness.proc_sample(pid)
                if sample:
                    self.samples[name].append(sample)

    def __enter__(self):
        self.started = time.monotonic()
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        elapsed = time.monotonic() - self.started
        out = {}
        for name, samples in self.samples.items():
            if not samples:
                continue
            rss = [s['rss_bytes'] for s in samples]
            out[name] = {'rss_mb_mean': round(sum(rss) / len(rss) / 2**20, 1),
                         'rss_mb_max': round(max(rss) / 2**20, 1),
                         'cpu_cores': round((samples[-1]['cpu_s'] - samples[0]['cpu_s']) / elapsed, 3)
                         if len(samples) > 1 else None}
        self.result = out


class RustArm:
    def __init__(self, args, harness, stub, connection_file: Path, workspace: Path, engine_pid: int):
        from scripts.generated_storage_operations import STORAGE_SCHEMA_VERSION
        self.args, self.harness, self.stub = args, harness, stub
        self.engine_pid = engine_pid
        system, tools = corpus_prompt()
        self.base = {
            'connection_file': str(connection_file), 'schema_id': STORAGE_SCHEMA_VERSION,
            'base_url': stub.base_url, 'protocol': 'anthropic', 'api_key': 'sk-harness-perf',
            'model': 'agent-loop-load-model', 'max_tokens': 16384, 'temperature': 1.0,
            'system': system, 'tools': tools, 'workspace': str(workspace), 'events': args.events,
        }
        self.out = Path(args.out)
        self.counter = 0

    def session(self, scenario: str, rounds: int, tag: str) -> dict:
        session = f'{scenario}-{uuid.uuid4().hex[:12]}'
        return {'session': session, 'conversation_id': f'rs{tag}{uuid.uuid4().hex[:10]}',
                'prompt': self.harness._prompt(scenario, rounds, session), 'max_rounds': rounds}

    def launch(self, workers: list[list[dict]], sample: bool = False) -> dict:
        self.counter += 1
        config_path = self.out / f'sessions-{self.counter}.json'
        config_path.write_text(json.dumps({**self.base, 'workers': workers}))
        launched_ns = time.monotonic_ns()
        process = subprocess.Popen([self.args.binary, 'sessions', str(config_path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sampler = Sampler(self.harness, {'rust_process': process.pid, 'storage_engine': self.engine_pid})
        with sampler:
            stdout, stderr = process.communicate(timeout=3600)
        if process.returncode != 0:
            raise SystemExit(f'tofu-core-rs failed ({process.returncode}): {stderr.decode()[-2000:]}')
        report = json.loads(stdout)
        report['driver_launched_ns'] = launched_ns
        report['resources'] = sampler.result
        failed = [s for s in report['sessions'] if s.get('status') != 'completed']
        if failed:
            raise SystemExit(f'tofu-core-rs sessions failed: {json.dumps(failed)[:2000]}')
        return report

    def metrics(self, report: dict, scenario: str) -> dict:
        by_session = {}
        for record in self.stub.records():
            by_session.setdefault(record['session'], []).append(record)
        sessions = []
        for item in report['sessions']:
            metrics = self.harness.session_metrics(by_session.get(item['session'], []), item['started_ns'],
                                                   item['settled_ns'])
            metrics.update(status=item['status'], settle_source='rust')
            sessions.append(metrics)
        return self.harness.summarize(scenario, sessions)


def durable_check(report: dict, rounds: int) -> dict:
    """Read every prototype Turn back through the Python storage client."""
    from lib.storage import get_storage_client
    client = get_storage_client()
    checked = []
    for item in report['sessions']:
        turn = client.query('turn.get', {'conversation_id': item['conversation_id'], 'turn_id': item['turn_id'],
                                         'user_id': 1})
        attempt = client.query('turn.attempt.get', {'attempt_id': item['attempt_id'], 'user_id': 1})
        projection = turn.get('projection') or {}
        tool_rounds = projection.get('toolRounds') or []
        checked.append({
            'session': item['session'], 'turn_status': turn.get('status'),
            'attempt_status': (attempt or {}).get('status'),
            'projection_revision': turn.get('projectionRevision'),
            'segments': len(projection.get('segments') or []), 'tool_rounds': len(tool_rounds),
            'receipts_done': sum(1 for r in tool_rounds if r.get('status') == 'done'),
            'requests': item['requests'], 'events_committed': item['events_committed']})
    return {
        'turns': len(checked),
        'all_completed': all(c['turn_status'] == 'completed' and c['attempt_status'] == 'completed'
                             for c in checked),
        'tool_rounds_match_requests': all(c['tool_rounds'] == c['requests'] - 1 for c in checked),
        'receipts_match_tool_rounds': all(c['receipts_done'] == c['tool_rounds'] for c in checked),
        'sample': checked[-3:],
    }


STAGES = ('decode_us', 'intent_commit_us', 'tool_us', 'receipt_commit_us', 'boundary_reads_us',
          'build_body_us', 'translate_seal_us', 'attempt_start_us', 'send_us')


def stage_profile(harness, report: dict) -> dict:
    rounds = [r for s in report['sessions'] for r in s['rounds']]
    return {stage: harness.quantiles([r[stage] / 1000 for r in rounds]) for stage in STAGES} | {
        'request_bytes': harness.quantiles([r['request_bytes'] for r in rounds])}


def _strip_samples(value):
    if isinstance(value, dict):
        return {k: _strip_samples(v) for k, v in value.items() if k != 'samples'}
    return value


def rust_child(args) -> int:
    harness = _harness()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    root = Path(args.root)
    stub = harness.Stub(out)
    import agent_loop_load_test as agent_loop
    from lib.storage.connection_file import write_connection_document
    driver = agent_loop.Driver(argparse.Namespace(backend='tofudb', startup_timeout=600.0, random_seed=20261118), root)
    result: dict = {'events': args.events, 'binary': args.binary}
    try:
        started = time.monotonic()
        status = driver.start_storage()
        result['storage_cold_start_s'] = round(time.monotonic() - started, 3)
        run_dir = root / 'run'
        run_dir.mkdir(mode=0o700, exist_ok=True)
        connection_file = run_dir / 'storage-connection.json'
        write_connection_document(connection_file, driver.storage.runtime._supervisor.connection_document(
            service_id=uuid.uuid4().hex))
        workspace = root / 'workspace'
        workspace.mkdir(mode=0o700, exist_ok=True)
        harness.write_noop_files(workspace)
        arm = RustArm(args, harness, stub, connection_file, workspace, status.get('pid'))
        warm = [arm.session('chat', 0, 'w'), arm.session('noop', 3, 'w'), arm.session('true', 1, 'w'),
                arm.session('true', 1, 'w')]
        noop = [arm.session('noop', args.rounds, 'noop') for _ in range(args.noop_turns)]
        true = [arm.session('true', 1, 'true') for _ in range(args.true_turns)]
        report = arm.launch([warm + noop + true])
        result['durable_check'] = durable_check(report, args.rounds)
        warm_ids = {s['session'] for s in warm}
        for scenario, chosen in (('noop', noop), ('true', true)):
            ids = {s['session'] for s in chosen}
            part = {**report, 'sessions': [s for s in report['sessions'] if s['session'] in ids]}
            phase = arm.metrics(part, scenario)
            result[scenario] = {key: phase[key] for key in (
                'round_overhead_ms', 'steady_round_overhead_ms', 'ttfr_ms', 'settle_ms', 'requests_per_turn',
                'body_bytes_round0', 'prefix_ratio', 'prefix_breaks', 'tools_seen', 'all_reached_final_round')}
            result[f'{scenario}_stages_ms'] = stage_profile(harness, part)
            print(scenario, json.dumps({k: (v or {}).get('median') for k, v in result[scenario].items()
                                        if isinstance(v, dict) and 'median' in (v or {})}), flush=True)
        result['sequential_process'] = report['process']
        result['sequential_resources'] = report['resources']
        result['warm_sessions'] = len(warm_ids)
        if not args.no_concurrency:
            result['concurrency'] = {}
            for count in args.concurrency:
                workers = [[arm.session('shell', args.rounds, f'c{count}') for _ in range(2)]
                           for _ in range(count)]
                report = arm.launch(workers)
                phase = arm.metrics(report, 'shell')
                result['concurrency'][str(count)] = _strip_samples({
                    'sessions': count, 'turns_per_session': 2, 'rounds': args.rounds,
                    'round_overhead_ms': phase['round_overhead_ms'], 'ttfr_ms': phase['ttfr_ms'],
                    'settle_ms': phase['settle_ms'], 'all_reached_final_round': phase['all_reached_final_round'],
                    'process': report['process'], 'resources': report['resources'],
                    'wall_s': round((report['finished_ns'] - report['started_ns']) / 1e9, 3),
                    'stages_ms': stage_profile(harness, report)})
                print('concurrency', count, json.dumps(result['concurrency'][str(count)]['round_overhead_ms']),
                      flush=True)
        if args.cold_starts:
            starts = []
            for _ in range(args.cold_starts):
                session = arm.session('chat', 0, 'cold')
                report = arm.launch([[session]])
                records = [r for r in stub.records() if r['session'] == session['session']]
                first = min(r['arrived_ns'] for r in records)
                starts.append({'launch_to_first_request_ms': (first - report['driver_launched_ns']) / 1e6,
                               'launch_to_main_ms': (report['launched_ns'] - report['driver_launched_ns']) / 1e6,
                               'process_peak_rss_mb': report['process']['peak_rss_kb'] / 1024})
            result['cold_start'] = {key: harness.quantiles([s[key] for s in starts]) for key in starts[0]}
    finally:
        driver.close()
        stub.close()
        (out / 'result.json').write_text(json.dumps(result, indent=1, default=str))
    return 0


def python_concurrency_child(args) -> int:
    harness = _harness()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stub = harness.Stub(out)
    deployment = harness.TofuDeployment(Path(args.root), stub, argparse.Namespace(
        startup_timeout=600.0, turn_timeout=1800.0, engine=''))
    result: dict = {}
    try:
        deployment.start()
        result['cold_start'] = deployment.cold_start
        harness.sequential_phase(deployment, stub, 'chat', 1, 0, 0.01)
        harness.sequential_phase(deployment, stub, 'shell', 1, 3, 0.01)
        for count in args.concurrency:
            phase = harness.concurrent_phase(deployment, stub, count, 2, args.rounds, 0.05, out, '')
            result[str(count)] = _strip_samples({key: phase.get(key) for key in (
                'round_overhead_ms', 'ttfr_ms', 'settle_ms', 'all_reached_final_round', 'errors', 'resources',
                'statuses', 'by_turn', 'turns')})
            print('python concurrency', count, json.dumps(result[str(count)]['round_overhead_ms']), flush=True)
    finally:
        deployment.close()
        stub.close()
        (out / 'result.json').write_text(json.dumps(result, indent=1, default=str))
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('rust', 'python-concurrency'))
    parser.add_argument('--binary', default='')
    parser.add_argument('--root', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--noop-turns', type=int, default=4)
    parser.add_argument('--true-turns', type=int, default=20)
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--events', choices=('parity', 'lean'), default='parity')
    parser.add_argument('--concurrency', type=lambda text: [int(v) for v in text.split(',') if v], default=[20, 100])
    parser.add_argument('--no-concurrency', action='store_true')
    parser.add_argument('--cold-starts', type=int, default=0)
    parser.add_argument('--engine', default=os.environ.get('TOFUDB_BIN', ''))
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return rust_child(args) if args.mode == 'rust' else python_concurrency_child(args)
    if args.mode == 'rust' and not Path(args.binary).is_file():
        raise SystemExit('--binary must name a built tofu-core-rs')
    harness = _harness()
    from serverctl_pkg.local_placement import temporary_root
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    harness.storage_harness._refuse_live_root(root)
    (root / '.tofudb-load-test').touch()
    if not args.engine or not Path(args.engine).is_file():
        raise SystemExit('--engine (or $TOFUDB_BIN) must name a tofu-db binary')
    engine, digest = harness.storage_harness._pin_engine(Path(args.engine), root)
    scratch = Path(tempfile.mkdtemp(prefix='rust-prototype-tmp-', dir=temporary_root()))
    try:
        environment = harness.storage_harness._child_environment(root.resolve(), scratch, engine, digest,
                                                                 'auto', 'tofudb')
        environment['TOFU_PROJECT_PATH'] = str(PROJECT_ROOT)
        environment['TOFU_SUPERVISOR_PORT'] = str(harness._free_port())
        command = [sys.executable, str(Path(__file__).resolve()), *argv, '--child']
        return subprocess.run(command, env=environment, cwd=str(root.resolve())).returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
