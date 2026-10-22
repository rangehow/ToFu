#!/usr/bin/env python3
"""Independent-process AB/BA repository and request projection comparisons.

CLI: --reference-directory containing baseline outline.py/rank.py, --output.
Uses deterministic local fixtures, identical interpreter and optional-library
availability, exact output verification, separate RSS processes and raw data.
This exercises the real repository tool pipeline, not agent task success or
the other system-performance domains. Temporary fixtures have one CLI lifetime.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.repo_map import outline, rank
from lib.repo_map.render import render_map


def load_reference(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def operations(reference: Path, fixture: Path, pipeline: str = 'repository'):
    if pipeline == 'event_poll':
        from lib.task_replay import task_replay
        from lib.agent_core import task_runtime
        from lib.identity import PrincipalContext
        old_replay = load_reference(reference / 'task_replay.py', 'event_poll_reference')
        parameters = json.loads((fixture / 'poll.json').read_text())
        size, suffix = parameters['events'], parameters['suffix']
        runtime = task_runtime.TaskRuntime('benchmark-system-poll', push_channel='')
        task = runtime.create(task_id='fixture', principal=PrincipalContext.system(
            subject_id='benchmark.system-poll', owner_user_id=900001,
            scopes={'benchmark:system-poll'}))
        task['events'] = [{'type': 'delta', 'seq': 100 + i, 'delta': 'synthetic 中文'} for i in range(size)]
        task['_eventBaseSeq'] = 100
        task['_eventNextSeq'] = 100 + size

        def poll(reader):
            original = task_runtime.task_memory_replay_page
            try:
                task_runtime.task_memory_replay_page = reader
                return runtime.poll('fixture', 100 + size - suffix)
            finally:
                task_runtime.task_memory_replay_page = original

        return {'reference': lambda: poll(old_replay.task_memory_replay_page),
                'candidate': lambda: poll(task_replay.task_memory_replay_page)}
    if pipeline in ('wire_projection', 'body_construction'):
        from lib.llm_sanitize._fields import _strip_non_api_fields
        old_fields = load_reference(reference / 'fields.py', 'wire_projection_reference')
        messages = json.loads((fixture / 'messages.json').read_text())
        if pipeline == 'wire_projection':
            return {'reference': lambda: old_fields._strip_non_api_fields(messages, carry_same_role_seam_hints=True),
                    'candidate': lambda: _strip_non_api_fields(messages, carry_same_role_seam_hints=True)}
        from lib.llm.body import _build

        def build(projector):
            original = _build._strip_non_api_fields
            try:
                _build._strip_non_api_fields = projector
                return _build.build_body('gpt-4o', messages, max_tokens=1024,
                                        context_window=128000, precomputed_input_tokens=10000,
                                        defer_reasoning=True, provider_id='fixture')
            finally:
                _build._strip_non_api_fields = original

        return {'reference': lambda: build(old_fields._strip_non_api_fields),
                'candidate': lambda: build(_strip_non_api_fields)}
    old_outline = load_reference(reference / 'outline.py', 'lib.repo_map.pipeline_reference_outline')
    old_rank = load_reference(reference / 'rank.py', 'lib.repo_map.pipeline_reference_rank')

    def run(extractor, ranker):
        repo = extractor.scan(str(fixture))
        scores = ranker.ensure_scores(repo, ranker.sumbd(repo, {'module_000.py'}))
        return render_map(repo, scores, chat_rel_paths={'module_000.py'})

    return {'reference': lambda: run(old_outline, old_rank),
            'candidate': lambda: run(outline, rank)}


def retained_size(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(retained_size(k, seen) + retained_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(retained_size(v, seen) for v in value)
    return size


def worker(reference: Path, fixture: Path, output: Path, samples: int, memory_variant: str,
           pipeline: str = 'repository', cold_variant: str = ''):
    runner_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    variants = operations(reference, fixture, pipeline)
    identity = uuid.uuid4().hex
    if cold_variant:
        cpu_started = time.process_time_ns()
        started = time.perf_counter_ns()
        result = variants[cold_variant]()
        wall_ms = (time.perf_counter_ns() - started) / 1e6
        cpu_ms = (time.process_time_ns() - cpu_started) / 1e6
        output.write_text(json.dumps({'runner_sha256': runner_sha256, 'process_identity': identity, 'wall_ms': wall_ms,
                                     'cpu_ms': cpu_ms, 'output_sha256': hashlib.sha256(
                                         json.dumps(result, sort_keys=True).encode()).hexdigest()}))
        return
    if memory_variant:
        result = variants[memory_variant]()
        gc.collect()
        # Linux ru_maxrss is KiB; macOS reports bytes.
        scale = 1 if sys.platform == 'darwin' else 1024
        output.write_text(json.dumps({'runner_sha256': runner_sha256, 'process_identity': identity,
                                     'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale,
                                     'retained_bytes': retained_size(result)}))
        return
    reference_output = variants['reference']()
    candidate_output = variants['candidate']()
    if reference_output != candidate_output:
        raise AssertionError('Repository pipeline output mismatch')
    raw = {name: {'wall_samples_ms': [], 'cpu_samples_ms': []} for name in variants}
    # Output verification warms once but does not establish steady state.
    # Cold first-use behavior is a separate process experiment, never a
    # selectively discarded sample from the steady-state distribution.
    from benchmarks.system_performance import CONTRACT
    warmups = CONTRACT['comparison']['steady_state_warmup_operations_per_variant']
    for _ in range(warmups):
        for operation in variants.values():
            operation()
    for iteration in range(samples):
        order = ('reference', 'candidate') if iteration % 2 == 0 else ('candidate', 'reference')
        for name in order:
            cpu_started = time.process_time_ns()
            started = time.perf_counter_ns()
            variants[name]()
            raw[name]['wall_samples_ms'].append((time.perf_counter_ns() - started) / 1e6)
            raw[name]['cpu_samples_ms'].append((time.process_time_ns() - cpu_started) / 1e6)
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != runner_sha256:
        raise RuntimeError('Benchmark runner changed during measurement')
    result = {'runner_sha256': runner_sha256, 'correctness': 'equivalent', 'output_sha256': hashlib.sha256(json.dumps(reference_output, sort_keys=True).encode()).hexdigest(),
              'warmup_operations_per_variant': warmups,
              'measurements': {}}
    for name, values in raw.items():
        wall = values['wall_samples_ms']; ordered = sorted(wall)
        result['measurements'][name] = {
            'process_identity': identity, **values,
            'p50_ms': statistics.median(wall), 'p95_ms': ordered[int((samples - 1) * .95)],
            'p99_ms': ordered[int((samples - 1) * .99)],
            'throughput_per_second': samples * 1000 / sum(wall),
            'cpu_ms_per_operation': statistics.mean(values['cpu_samples_ms']),
            'profiler_enabled': False, 'tracemalloc_enabled': False}
    output.write_text(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--memory-variant', choices=['reference', 'candidate'], default='')
    parser.add_argument('--pipeline', choices=['repository', 'wire_projection', 'body_construction', 'event_poll'], default='repository')
    parser.add_argument('--events', type=int, default=4096)
    parser.add_argument('--suffix', type=int, default=4)
    parser.add_argument('--cold', action='store_true', help='First operation after module imports; fresh process per sample')
    parser.add_argument('--cold-variant', choices=['reference', 'candidate'], default='')
    args = parser.parse_args()
    if args.worker:
        worker(args.reference_directory, args.fixture, args.output, args.samples, args.memory_variant, args.pipeline, args.cold_variant)
        return
    if args.samples < 100 or args.pairs < 3:
        parser.error('at least 100 samples and three independent pairs required')
    result = {'format': 'tofu.paired-pipeline/v1', 'pipeline': args.pipeline,
              'scope': {'repository': 'repository_tool_pipeline',
                        'wire_projection': 'wire_projection_microbenchmark',
                        'body_construction': 'body_construction_pipeline',
                        'event_poll': 'task_runtime_poll_pipeline'}[args.pipeline],
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'phase': 'first_operation_after_imports' if args.cold else 'steady_state',
              'pairs': [], 'runtime': {'python': sys.version, 'platform': platform.platform(),
                                      'cpu_affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None},
              'sources': {}}
    if args.pipeline == 'repository':
        source_paths = [('reference', [args.reference_directory / p for p in ('outline.py', 'rank.py')]),
                        ('candidate', [Path(outline.__file__), Path(rank.__file__)])]
    elif args.pipeline in ('wire_projection', 'body_construction'):
        from lib.llm_sanitize import _fields
        source_paths = [('reference', [args.reference_directory / 'fields.py']),
                        ('candidate', [Path(_fields.__file__)])]
    else:
        from lib.task_replay import task_replay
        source_paths = [('reference', [args.reference_directory / 'task_replay.py']),
                        ('candidate', [Path(task_replay.__file__)])]
    for name, paths in source_paths:
        result['sources'][name] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    with tempfile.TemporaryDirectory(prefix='tofu-repository-pipeline-') as temporary:
        temporary = Path(temporary); fixture = temporary / 'fixture'; fixture.mkdir()
        digest = hashlib.sha256()
        for module in range(32):
            source = '\n'.join(f'def symbol_{module}_{index}(value="中文"):\n'
                               f'    return symbol_{(module + 1) % 32}_{index}(value)\n'
                               for index in range(20))
            name = f'module_{module:03}.py'
            (fixture / name).write_text(source)
            digest.update(name.encode()); digest.update(source.encode())
        if args.pipeline in ('wire_projection', 'body_construction'):
            messages = [{'role': 'assistant' if i % 2 else 'user',
                         'content': [{'type': 'text', 'text': '中文 content ' * 12},
                                     {'type': 'image_url', 'image_url': {'url': 'https://fixture.example/image.png'}}],
                         'tool_calls': [{'id': f'call-{i}', 'type': 'function',
                                         'function': {'name': 'tool', 'arguments': '{}'}}],
                         'reasoning_details': [{'text': 'reasoning', 'signature': 'signature'}],
                         'frontendMetadata': {'payload': 'discard'},
                         '_contextComposer': i % 3 == 0} for i in range(128)]
            if args.pipeline == 'body_construction':
                messages = []
                for i in range(40):
                    messages.extend([
                        {'role': 'user', 'content': [{'type': 'text', 'text': '中文 question ' * 12}]},
                        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'checking evidence ' * 12}],
                         'tool_calls': [{'id': f'call-{i}', 'type': 'function',
                                         'function': {'name': 'tool', 'arguments': '{}'}}],
                         'reasoning_details': [{'text': 'reasoning', 'signature': 'signature'}]},
                        {'role': 'tool', 'tool_call_id': f'call-{i}', 'content': 'synthetic result ' * 24},
                    ])
            encoded = json.dumps(messages)
            (fixture / 'messages.json').write_text(encoded)
            digest = hashlib.sha256(encoded.encode())
        elif args.pipeline == 'event_poll':
            if args.events < 0 or not 0 <= args.suffix <= args.events:
                parser.error('event counts must be nonnegative and suffix within retained window')
            encoded = json.dumps({'events': args.events, 'suffix': args.suffix})
            (fixture / 'poll.json').write_text(encoded)
            digest = hashlib.sha256(encoded.encode())
        result['fixture_sha256'] = digest.hexdigest()
        base = [sys.executable, str(Path(__file__).resolve()), '--reference-directory', str(args.reference_directory.resolve()),
                '--fixture', str(fixture), '--worker', '--samples', str(args.samples),
                '--pipeline', args.pipeline]
        for pair in range(args.pairs):
            pair_output = temporary / f'pair-{pair}.json'
            if args.cold:
                raw = {name: {'wall_samples_ms': [], 'cpu_samples_ms': [], 'sample_process_identities': []}
                       for name in ('reference', 'candidate')}
                expected = None
                for sample in range(args.samples):
                    order = ('reference', 'candidate') if sample % 2 == 0 else ('candidate', 'reference')
                    for name in order:
                        subprocess.run(base + ['--output', str(pair_output), '--cold-variant', name],
                                       check=True, timeout=300)
                        observed = json.loads(pair_output.read_text())
                        if observed['runner_sha256'] != result['runner_sha256']:
                            raise RuntimeError('Benchmark runner changed between cold processes')
                        if expected is not None and observed['output_sha256'] != expected:
                            raise AssertionError('Cold pipeline output mismatch')
                        expected = observed['output_sha256']
                        raw[name]['wall_samples_ms'].append(observed['wall_ms'])
                        raw[name]['cpu_samples_ms'].append(observed['cpu_ms'])
                        raw[name]['sample_process_identities'].append(observed['process_identity'])
                measured = {'correctness': 'equivalent', 'output_sha256': expected, 'measurements': {}}
                for name, values in raw.items():
                    wall = values['wall_samples_ms']; ordered = sorted(wall)
                    measured['measurements'][name] = {
                        **values, 'process_identity': uuid.uuid4().hex,
                        'p50_ms': statistics.median(wall), 'p95_ms': ordered[int((args.samples - 1) * .95)],
                        'p99_ms': ordered[int((args.samples - 1) * .99)],
                        'throughput_per_second': args.samples * 1000 / sum(wall),
                        'cpu_ms_per_operation': statistics.mean(values['cpu_samples_ms']),
                        'profiler_enabled': False, 'tracemalloc_enabled': False}
            else:
                subprocess.run(base + ['--output', str(pair_output)], check=True, timeout=300)
                measured = json.loads(pair_output.read_text())
                if measured['runner_sha256'] != result['runner_sha256']:
                    raise RuntimeError('Benchmark runner changed between paired processes')
            for name in ('reference', 'candidate'):
                memory_output = temporary / f'memory-{pair}-{name}.json'
                subprocess.run(base + ['--output', str(memory_output), '--memory-variant', name], check=True, timeout=300)
                memory = json.loads(memory_output.read_text())
                if memory['runner_sha256'] != result['runner_sha256']:
                    raise RuntimeError('Benchmark runner changed between memory processes')
                measured['measurements'][name].update({
                    'memory_process_identity': memory['process_identity'],
                    'peak_rss_bytes': memory['peak_rss_bytes'], 'retained_bytes': memory['retained_bytes'],
                    'memory_separate_process': True})
            result['pairs'].append(measured)
            ratios = {metric: measured['measurements']['candidate'][metric] / measured['measurements']['reference'][metric]
                      for metric in ('p50_ms', 'p95_ms', 'p99_ms', 'cpu_ms_per_operation', 'peak_rss_bytes')}
            print(f'pair={pair} ratios={json.dumps(ratios)} correctness=equivalent', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
