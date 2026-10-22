#!/usr/bin/env python3
"""Cold, fresh-process search import comparison with native PDF dependencies.

Use an explicit interpreter/compatibility launcher and reference package root.
Each sample owns a process; no application storage or network is started.
Optional native capability activation, wall/CPU time and RSS are observable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json,resource,sys,time
started=time.perf_counter_ns();cpu=time.process_time_ns()
import tofu_search
wall_ms=(time.perf_counter_ns()-started)/1e6;cpu_ms=(time.process_time_ns()-cpu)/1e6
print('TOFU_MEASUREMENT='+json.dumps({'wall_ms':wall_ms,'cpu_ms':cpu_ms,
    'rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
    'pymupdf_loaded':'pymupdf' in sys.modules,'onnx_loaded':'onnxruntime' in sys.modules,
    'python':sys.version,'package_source':tofu_search.__file__}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True)
    parser.add_argument('--reference-package', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    args = parser.parse_args()
    if args.samples < 100 or args.pairs < 3:
        parser.error('100 fresh-process samples and three pairs required')
    roots = {'reference': args.reference_package.resolve(), 'candidate': ROOT / 'packages/tofu-search'}
    report = {'format': 'tofu.search-startup-experiment/v1', 'whole_system_verdict': 'incomplete',
              'scope': 'cold web-search package import with optional native PDF stack installed',
              'python_launcher': args.python, 'affinity': sorted(os.sched_getaffinity(0)),
              'sources': {}, 'pairs': []}
    for name, root in roots.items():
        report['sources'][name] = {path: hashlib.sha256((root / 'tofu_search/fetch' / path).read_bytes()).hexdigest()
                                  for path in ('pdf_extract.py','utils.py','html_extract.py')}
    with tempfile.TemporaryDirectory(prefix='tofu-search-startup-') as temporary:
        temporary = Path(temporary)
        for pair in range(args.pairs):
            raw = {name: [] for name in roots}
            for sample in range(args.samples):
                order = ('reference','candidate') if sample % 2 == 0 else ('candidate','reference')
                for name in order:
                    environment = dict(os.environ)
                    environment['PYTHONPATH'] = str(roots[name]) + os.pathsep + str(ROOT)
                    completed = subprocess.run([args.python,'-c',CHILD], env=environment,
                                               capture_output=True, text=True, timeout=60)
                    if completed.returncode:
                        raise RuntimeError(completed.stderr[-2000:])
                    payload = next(line for line in completed.stdout.splitlines() if line.startswith('TOFU_MEASUREMENT='))
                    measured = json.loads(payload.partition('=')[2])
                    measured['diagnostic_stderr_sha256'] = hashlib.sha256(completed.stderr.encode()).hexdigest()
                    raw[name].append(measured)
                if sample % 20 == 19:
                    print(f'pair={pair} completed={sample+1}/{args.samples}',flush=True)
            values = {}
            for name, measurements in raw.items():
                wall = sorted(row['wall_ms'] for row in measurements)
                values[name] = {'p50_ms':statistics.median(wall),'p95_ms':wall[int((args.samples-1)*.95)],
                                'p99_ms':wall[int((args.samples-1)*.99)],
                                'cpu_ms_per_operation':statistics.mean(row['cpu_ms'] for row in measurements),
                                'median_rss_bytes':statistics.median(row['rss_bytes'] for row in measurements),
                                'peak_rss_bytes':max(row['rss_bytes'] for row in measurements),'raw':measurements}
            report['pairs'].append(values)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report,indent=2)+'\n')
            print('pair',pair,{key:values['candidate'][key]/values['reference'][key] for key in
                              ('p50_ms','p99_ms','cpu_ms_per_operation','peak_rss_bytes')},flush=True)


if __name__=='__main__':
    main()
