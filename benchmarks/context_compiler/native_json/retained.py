"""Separate-process unique-input retention probe; no latency/admission claim.

Both arms preload identical native code. The owned lazy service is used in the
candidate arm. Complete input/output traces and runtime/source/binary witnesses
are compared. Traced deltas include observer records; they are not isolated
application live bytes. Cold activation and full-context ownership are separate.
"""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tracemalloc

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import psutil
from lib.llm import conversion
from check import load
from decoder import make_decoder
from lazy import LazyArgumentDecoder
from controlled import runtime_witness
from measure import SOURCES

WITNESSED_SOURCES = (*SOURCES, 'benchmarks/context_compiler/native_json/lazy.py',
                     'benchmarks/context_compiler/native_json/controlled.py',
                     'benchmarks/context_compiler/native_json/retained.py')


def worker(args):
    os.sched_setaffinity(0, {args.cpu})
    native = make_decoder(load(args.library))
    service = LazyArgumentDecoder(lambda: native)
    parser = conversion.object_arguments if args.worker == 'reference' else service
    runtime = runtime_witness()
    witnesses = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in WITNESSED_SOURCES}
    binary = hashlib.sha256(args.library.read_bytes()).hexdigest()
    for _ in range(20):
        assert parser(json.dumps({'warmup': 'x' * 2000})) == {'warmup': 'x' * 2000}
    gc.collect()
    process = psutil.Process()
    source_trace, output_trace = hashlib.sha256(), hashlib.sha256()
    observations = []
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    for cohort in range(args.cohorts):
        for offset in range(args.operations):
            index = cohort * args.operations + offset
            key = 'unique_' + str(index) + '_' + 'k' * 60
            raw = json.dumps({key: 'x' * 2000, 'float': index / 7})
            value = parser(raw)
            assert value == {key: 'x' * 2000, 'float': index / 7}
            source_trace.update(raw.encode()); source_trace.update(b'\0')
            output_trace.update(json.dumps(value, sort_keys=True).encode()); output_trace.update(b'\0')
            del value, raw, key
        gc.collect()
        observations.append({'cohort': cohort,
                             'traced_delta_bytes': tracemalloc.get_traced_memory()[0] - baseline,
                             'rss_bytes': process.memory_info().rss})
    return {'pid': os.getpid(), 'arm': args.worker, 'operations': args.cohorts * args.operations,
            'observations': observations, 'input_trace_sha256': source_trace.hexdigest(),
            'output_trace_sha256': output_trace.hexdigest(), 'runtime': runtime,
            'sources': witnesses, 'binary_sha256': binary,
            'native_activations': service.activations,
            'affinity_cpu_ids': sorted(os.sched_getaffinity(0)),
            'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--cohorts', type=int, default=10)
    parser.add_argument('--operations', type=int, default=2000)
    parser.add_argument('--worker', choices=('reference', 'candidate'))
    args = parser.parse_args()
    assert min(args.pairs, args.cohorts, args.operations) > 0
    if args.worker:
        print(json.dumps(worker(args)))
        return
    assert args.output
    from runtime_guards import probe_system_resources
    names = WITNESSED_SOURCES
    witnesses = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    binary = hashlib.sha256(args.library.read_bytes()).hexdigest()
    report = {'scope': __doc__, 'sources': witnesses, 'binary_sha256': binary,
              'capacity_probe': probe_system_resources().as_dict(), 'cpu': args.cpu,
              'cohorts': args.cohorts, 'operations_per_cohort': args.operations,
              'pairs': [], 'production_promoted': False, 'whole_system_admission': False}
    executable = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    workers = []
    for pair_index in range(args.pairs):
        pair = {}
        order = ('reference', 'candidate') if pair_index % 2 == 0 else ('candidate', 'reference')
        for arm in order:
            command = [executable, __file__, '--worker', arm, '--library', str(args.library),
                       '--cpu', str(args.cpu), '--cohorts', str(args.cohorts),
                       '--operations', str(args.operations)]
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=120)
            pair[arm] = json.loads(result.stdout)
            assert pair[arm]['sources'] == witnesses and pair[arm]['binary_sha256'] == binary
            workers.append(pair[arm]['pid'])
        reference, candidate = pair['reference'], pair['candidate']
        for key in ('input_trace_sha256', 'output_trace_sha256', 'runtime', 'operations', 'affinity_cpu_ids'):
            assert reference[key] == candidate[key], key
        assert reference['native_activations'] == 0 and candidate['native_activations'] == 1
        report['pairs'].append(pair)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print('completed pair', pair_index, flush=True)
    assert len(workers) == len(set(workers)) == args.pairs * 2
    assert witnesses == {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    assert binary == hashlib.sha256(args.library.read_bytes()).hexdigest()


if __name__ == '__main__':
    main()
