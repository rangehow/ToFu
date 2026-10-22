"""Paired real checked protocol conversion with frozen argument validation.

Raw wall/CPU and separate full-cohort peak RSS workers. No inference, durable
history,token-admission or full-context claim. Source/input digests and exact
ConversionResult traces are required; no production globals are patched.
"""
import argparse
import ast
from dataclasses import asdict
import gc
import hashlib
import inspect
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
from types import FunctionType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from lib.llm import conversion
from lib.llm.anthropic_outbound import _to_anthropic
from lib.llm.responses_outbound import _to_responses

REFERENCE = '041035db5'
CASES = ('small', 'large_text', 'unicode_text', 'wide_numeric', 'wide_floats')


def original_arguments():
    source = subprocess.check_output(['/usr/bin/git', 'show', REFERENCE + ':lib/llm/conversion.py'], cwd=ROOT).decode()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'object_arguments')
    text = ast.get_source_segment(source, node)
    namespace = dict(vars(conversion)); exec(compile(text, '<frozen-object-arguments>', 'exec'), namespace)
    return namespace['object_arguments'], hashlib.sha256(text.encode()).hexdigest()


def clone(function, namespace):
    result = FunctionType(function.__code__, namespace, argdefs=function.__defaults__)
    result.__kwdefaults__ = function.__kwdefaults__
    return result


def projector(protocol, argument_parser):
    validation = clone(conversion.validate_request, dict(conversion.validate_request.__globals__, object_arguments=argument_parser))
    module = _to_anthropic if protocol == 'anthropic' else _to_responses
    namespace = dict(vars(module), object_arguments=argument_parser, validate_request=validation)
    for name, function in vars(module).items():
        if inspect.isfunction(function) and function.__module__ == module.__name__:
            namespace[name] = clone(function, namespace)
    return namespace['openai_body_to_anthropic_checked' if protocol == 'anthropic' else 'openai_body_to_responses_checked']


def fixture(case):
    value = ({'path': 'fixture.py'} if case == 'small' else
             {'code': 'def fixture(): return 1\n' * 12000} if case == 'large_text' else
             {'text': '中文上下文测试' * 40000} if case == 'unicode_text' else
             {'values': [i / 7 for i in range(30000)]} if case == 'wide_floats' else
             {'values': list(range(30000))})
    arguments = json.dumps(value)
    messages = [{'role': 'user', 'content': 'offline conversion fixture'}]
    for i in range(8):
        messages.extend([{'role': 'assistant', 'tool_calls': [{'id': f'call_{i}', 'type': 'function',
            'function': {'name': 'fixture', 'arguments': arguments}}]},
            {'role': 'tool', 'tool_call_id': f'call_{i}', 'content': 'ok'}])
    return {'model': 'fixture-model', 'max_tokens': 1024, 'messages': messages}


def worker(variant, protocol, case, samples, memory):
    parser = original_arguments()[0] if variant == 'reference' else conversion.object_arguments
    project = projector(protocol, parser); body = fixture(case)
    source = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    for _ in range(20): project(body).require_payload()
    if memory: gc.collect()
    wall, cpu = [], []; trace = hashlib.sha256()
    for _ in range(samples):
        start = time.perf_counter_ns(); clock = time.process_time_ns()
        result = project(body); result.require_payload()
        cpu.append((time.process_time_ns() - clock) / 1e6); wall.append((time.perf_counter_ns() - start) / 1e6)
        trace.update(json.dumps(asdict(result), sort_keys=True).encode()); trace.update(b'\0')
        del result
    assert source == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    ordered = sorted(wall)
    return {'pid': os.getpid(), 'trace_sha256': trace.hexdigest(), 'fixture_sha256': source,
            'raw_wall_ms': [] if memory else wall, 'raw_cpu_ms': [] if memory else cpu,
            'p50_ms': statistics.median(wall), 'p95_ms': ordered[int((samples - 1) * .95)],
            'p99_ms': ordered[int((samples - 1) * .99)], 'cpu_ms_per_operation': statistics.mean(cpu),
            'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            'warmup_operations': 20, 'memory_worker': memory}


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--output', type=Path)
    p.add_argument('--samples', type=int, default=100); p.add_argument('--pairs', type=int, default=3)
    p.add_argument('--worker', choices=('reference', 'candidate')); p.add_argument('--protocol', choices=('anthropic', 'responses'))
    p.add_argument('--case', choices=CASES); p.add_argument('--memory', action='store_true')
    p.add_argument('--cases', nargs='+', choices=CASES, default=CASES); args = p.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.protocol, args.case, args.samples, args.memory))); return
    assert args.output and args.samples > 0 and args.pairs > 0
    names = ('lib/llm/conversion.py', 'lib/llm/anthropic_outbound/_to_anthropic.py',
             'lib/llm/responses_outbound/_to_responses.py', 'benchmarks/context_compiler/tool_argument_measure.py')
    sources = {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in names}
    report = {'scope': __doc__, 'sources': sources, 'reference_revision': REFERENCE,
              'reference_function_sha256': original_arguments()[1], 'python': sys.version,
              'platform': platform.platform(), 'samples': args.samples, 'required_pairs': args.pairs,
              'profiler_enabled': False, 'tracemalloc_enabled': False, 'cases': []}
    python = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    for protocol in ('anthropic', 'responses'):
        for case in args.cases:
            row = {'protocol': protocol, 'case': case, 'pairs': []}; report['cases'].append(row)
            for i in range(args.pairs):
                values = {}
                for variant in (('reference', 'candidate') if i % 2 == 0 else ('candidate', 'reference')):
                    assert sources == {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in names}
                    command = [python, __file__, '--worker', variant, '--protocol', protocol, '--case', case, '--samples', str(args.samples)]
                    result = json.loads(subprocess.run(command, capture_output=True, text=True, check=True, timeout=300).stdout)
                    memory = json.loads(subprocess.run(command + ['--memory'], capture_output=True, text=True, check=True, timeout=300).stdout)
                    assert result['trace_sha256'] == memory['trace_sha256']; result['memory'] = memory; values[variant] = result
                assert values['reference']['trace_sha256'] == values['candidate']['trace_sha256']
                row['pairs'].append(values); args.output.write_text(json.dumps(report, indent=2) + '\n')
            print(protocol, case, [round(x['candidate']['p50_ms'] / x['reference']['p50_ms'], 3) for x in row['pairs']], flush=True)


if __name__ == '__main__': main()
